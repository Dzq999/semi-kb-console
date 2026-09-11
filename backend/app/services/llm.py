from __future__ import annotations

import asyncio
import base64
import html
import hashlib
import json
import random
import re
import ssl
from datetime import datetime, timezone
from html.parser import HTMLParser
from ipaddress import ip_address
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataclasses import dataclass

from ..config import settings
from ..models import EncryptedCredential, UserPreference
from ..security import decrypt_secret


class ExternalServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmEndpoint:
    """已解析的模型端点：文本补全基址、模型目录地址与接口风格。

    每个字段都先取用户 UserPreference 里的自定义值，缺省再回落到进程级 settings，
    因此未设置端点的用户与历史行为完全一致。"""

    base_url: str
    catalog_url: str
    api_style: str


def _default_endpoint() -> LlmEndpoint:
    return LlmEndpoint(base_url=settings.llm_base_url, catalog_url=settings.model_catalog_url, api_style=settings.llm_api_style)


def user_llm_endpoint(db: Session, user_id: int) -> LlmEndpoint:
    """解析某用户的模型端点：自定义值优先，未填字段各自回落到 settings 默认。"""
    pref = db.get(UserPreference, user_id)
    base = (pref.llm_base_url if pref and pref.llm_base_url else settings.llm_base_url).rstrip("/")
    catalog = pref.model_catalog_url if pref and pref.model_catalog_url else settings.model_catalog_url
    style = (pref.llm_api_style if pref and pref.llm_api_style else settings.llm_api_style).casefold()
    return LlmEndpoint(base_url=base, catalog_url=catalog, api_style=style)


# 瞬时网络错误：连接被上游代理中途掐断、连接/读超时、连接失败等，均为可重试的抖动
_TRANSIENT_HTTP_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_HTTP_RETRIES = 3  # 初次调用之外的额外重试次数（共 4 次尝试）——中转首包前丢连接较频，多兜一次
_HTTP_BACKOFF_BASE = 1.0
_HTTP_BACKOFF_CAP = 8.0


def user_api_key(db: Session, user_id: int) -> str | None:
    row = db.scalar(select(EncryptedCredential).where(EncryptedCredential.user_id == user_id, EncryptedCredential.kind == "llm_api_key"))
    return decrypt_secret(row.ciphertext) if row else settings.llm_api_key


def user_stream_mode(db: Session, user_id: int) -> bool:
    """该用户是否对研究 agent 启用流式聚合调用；缺省 False（非流式，历史行为）。"""
    pref = db.get(UserPreference, user_id)
    return bool(pref.llm_stream_mode) if pref else False


class LlmService:
    async def list_models(self, api_key: str, search: str = "", endpoint: LlmEndpoint | None = None) -> list[dict]:
        endpoint = endpoint or _default_endpoint()
        headers = {"Authorization": f"Bearer {api_key}"}
        # 目录取自与 complete 同源的第三方代理，同样会遇到瞬时抖动（ConnectTimeout / 连接被中途掐断
        # / 瞬时 5xx）。原先单次即抛，任务编排里就会频繁弹“模型目录网络失败”；这里与 complete 一致
        # 就地重试再上抛，消化网络抖动而非把每次抖动都暴露给编排层。
        response = None
        for attempt in range(_HTTP_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    response = await client.get(endpoint.catalog_url, headers=headers)
            except (*_TRANSIENT_HTTP_ERRORS, OSError) as exc:
                if attempt >= _HTTP_RETRIES:
                    raise ExternalServiceError(f"模型目录网络失败：{type(exc).__name__}") from exc
                await self._backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                raise ExternalServiceError(f"模型目录网络失败：{type(exc).__name__}") from exc
            if response.status_code in _RETRYABLE_STATUS and attempt < _HTTP_RETRIES:
                await self._backoff(attempt)
                continue
            break
        if response is None:
            raise ExternalServiceError("模型目录网络失败：无响应")
        if response.status_code >= 400:
            raise ExternalServiceError(f"模型目录请求失败：HTTP {response.status_code}")
        payload = response.json()
        raw = payload.get("data", payload if isinstance(payload, list) else [])
        models = [{"id": str(item.get("id", "")), "owned_by": item.get("owned_by"), "available": True} for item in raw if isinstance(item, dict) and item.get("id")]
        if search:
            needle = search.casefold()
            models = [item for item in models if needle in item["id"].casefold()]
        return sorted(models, key=lambda item: item["id"])

    async def complete(self, api_key: str, model: str, system: str, user: str, temperature: float = 0.2, timeout_seconds: int = 120, max_tokens: int | None = None, endpoint: LlmEndpoint | None = None) -> str:
        endpoint = endpoint or _default_endpoint()
        # Claude 模型经中转的 OpenAI 兼容层（/chat/completions）做协议转译，长响应/并发下更易被中途
        # 掐断（RemoteProtocolError）；原生 /v1/messages 少一层转译、更稳，是本系统默认走的路。system 在
        # 原生契约里是顶层字段而非消息，且 max_tokens 必填。两套响应结构都在 _extract_text 里兼容解析。
        max_tokens = max_tokens or settings.llm_max_tokens
        if endpoint.api_style == "anthropic":
            url = f"{endpoint.base_url}/messages"
            payload = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}], "temperature": temperature}
        else:
            url = f"{endpoint.base_url}/chat/completions"
            payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temperature}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # 瞬时 5xx/429 与连接抖动是网络问题而非“模型输出不合法”，在 HTTP 层就地重试即可消化，
        # 不应上抛去挤占 agent 仅有的产物校验重试次数、更不该污染下一轮 prompt。
        response = None
        for attempt in range(_HTTP_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
                    response = await client.post(url, json=payload, headers=headers)
            except (*_TRANSIENT_HTTP_ERRORS, OSError) as exc:
                if attempt >= _HTTP_RETRIES:
                    raise ExternalServiceError(f"模型网络调用失败：{type(exc).__name__}") from exc
                await self._backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                raise ExternalServiceError(f"模型网络调用失败：{type(exc).__name__}") from exc
            if response.status_code in _RETRYABLE_STATUS and attempt < _HTTP_RETRIES:
                await self._backoff(attempt)
                continue
            break
        if response is None:
            raise ExternalServiceError("模型网络调用失败：无响应")
        if response.status_code >= 400:
            raise ExternalServiceError(f"模型调用失败：HTTP {response.status_code}")
        try:
            return self._extract_text(response.json())
        except json.JSONDecodeError as exc:
            raise ExternalServiceError("模型响应结构不兼容") from exc

    async def complete_via_stream(self, api_key: str, model: str, system: str, user: str, temperature: float = 0.2, timeout_seconds: int = 300, max_tokens: int | None = None, endpoint: LlmEndpoint | None = None) -> str:
        """流式聚合的一次性补全：与 complete() 契约相同（返回完整文本），但走 SSE。

        为什么单列一条而不复用 stream_complete()：stream_complete 面向问答“逐字浮现”，在 aiter_lines
        自然结束时就正常返回——若中转在生成中途【静默断流】而不发终止事件，它会把残缺文本当成功。
        编排 agent 的产物要进候选/入库，绝不能把截断响应误当完整。故这里显式跟踪终止信号：
          - anthropic：message_stop 事件，或 message_delta 里带 stop_reason
          - openai   ：choices[].finish_reason 非空，或 [DONE] 哨兵
        整段流结束后若从未见到任一终止信号，按“被中途掐断”上抛 RemoteProtocolError（可重试网络错误），
        与非流式被掐时的显式失败语义对齐，交由 orchestrator 网络类重试处理，而非污染下游校验。

        重试语义与 complete() 一致：仅在【首个增量到达前】重试连接/瞬时 5xx；一旦已收过增量，
        中断无法从头重放，直接作为网络错误上抛（不吞、不半吐）。流式保持连接活性，规避中转对长响应
        的空闲掐断（RemoteProtocolError），这正是本方法存在的理由。
        """
        endpoint = endpoint or _default_endpoint()
        max_tokens = max_tokens or settings.llm_max_tokens
        if endpoint.api_style == "anthropic":
            url = f"{endpoint.base_url}/messages"
            payload = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}], "temperature": temperature, "stream": True}
        else:
            url = f"{endpoint.base_url}/chat/completions"
            payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temperature, "stream": True}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        for attempt in range(_HTTP_RETRIES + 1):
            chunks: list[str] = []
            terminated = False
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
                    async with client.stream("POST", url, json=payload, headers=headers) as response:
                        if response.status_code in _RETRYABLE_STATUS and attempt < _HTTP_RETRIES and not chunks:
                            await self._backoff(attempt)
                            continue
                        if response.status_code >= 400:
                            raise ExternalServiceError(f"模型流式调用失败：HTTP {response.status_code}")
                        async for line in response.aiter_lines():
                            piece, kind = self._parse_stream_line_terminal(line, endpoint.api_style)
                            if piece:
                                chunks.append(piece)
                            if kind == "cut":
                                # 预算耗尽/中转截断：累积文本必是残缺 JSON。按瞬时网络错误上抛，
                                # 交 orchestrator 的网络类重试重跑（不把残缺响应塞进候选，也不污染 prompt）。
                                raise httpx.RemoteProtocolError("响应被截断（stop_reason=max_tokens / finish_reason=length，疑似中转截断）")
                            if kind == "ok":
                                terminated = True
                if not terminated:
                    # 见到 EOF 却从未见终止事件：被中途掐断。构造成瞬时错误走本方法的重试。
                    raise httpx.RemoteProtocolError("流在终止事件到达前结束（疑似中转掐断）")
                text = "".join(chunks)
                if not text.strip():
                    raise ExternalServiceError("模型响应结构不兼容")
                return text
            except (*_TRANSIENT_HTTP_ERRORS, OSError) as exc:
                # 已收过增量则无法从头重放；未收过且还有重试额度则退避重连。
                if chunks or attempt >= _HTTP_RETRIES:
                    raise ExternalServiceError(f"模型网络调用失败：{type(exc).__name__}") from exc
                await self._backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                raise ExternalServiceError(f"模型网络调用失败：{type(exc).__name__}") from exc
        raise ExternalServiceError("模型网络调用失败：无响应")

    async def stream_complete(self, api_key: str, model: str, system: str, user: str, temperature: float = 0.2, timeout_seconds: int = 180, max_tokens: int | None = None, endpoint: LlmEndpoint | None = None):
        """流式补全：逐段 yield 增量文本。用于问答汇总阶段的答案「逐字浮现」。

        重试语义与 complete() 不同：只能在「首个增量到达前」重试连接/瞬时 5xx；一旦已经 yield 过
        文本，中途断连无法从头重放，直接作为流错误上抛（保留已产出的部分）。两套协议(anthropic 原生
        content_block_delta / OpenAI choices[].delta)在 _parse_stream_line 里统一解析。"""
        endpoint = endpoint or _default_endpoint()
        max_tokens = max_tokens or settings.llm_max_tokens
        if endpoint.api_style == "anthropic":
            url = f"{endpoint.base_url}/messages"
            payload = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}], "temperature": temperature, "stream": True}
        else:
            url = f"{endpoint.base_url}/chat/completions"
            payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temperature, "stream": True}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        started = False
        for attempt in range(_HTTP_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
                    async with client.stream("POST", url, json=payload, headers=headers) as response:
                        if response.status_code in _RETRYABLE_STATUS and attempt < _HTTP_RETRIES:
                            await self._backoff(attempt)
                            continue
                        if response.status_code >= 400:
                            raise ExternalServiceError(f"模型流式调用失败：HTTP {response.status_code}")
                        async for line in response.aiter_lines():
                            piece = self._parse_stream_line(line, endpoint.api_style)
                            if piece:
                                started = True
                                yield piece
                return
            except (*_TRANSIENT_HTTP_ERRORS, OSError) as exc:
                if started or attempt >= _HTTP_RETRIES:
                    raise ExternalServiceError(f"模型流式网络失败：{type(exc).__name__}") from exc
                await self._backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                raise ExternalServiceError(f"模型流式网络失败：{type(exc).__name__}") from exc

    @staticmethod
    def _parse_stream_line(line: str, api_style: str) -> str:
        """从一行 SSE 里取出增量文本；非数据行/心跳/[DONE]/无文本增量都返回空串。"""
        if not line or not line.startswith("data:"):
            return ""
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return ""
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return ""
        if not isinstance(obj, dict):
            return ""
        delta = obj.get("delta")  # anthropic: {"type":"text_delta","text":"..."}
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            return delta["text"]
        choices = obj.get("choices")  # openai: choices[0].delta.content
        if isinstance(choices, list) and choices:
            piece = ((choices[0] or {}).get("delta") or {}).get("content")
            if isinstance(piece, str):
                return piece
        return ""

    @staticmethod
    def _parse_stream_line_terminal(line: str, api_style: str) -> tuple[str, str]:
        """同 _parse_stream_line 取增量文本，另返回终止类别："" | "ok" | "cut"。

        "ok"  = 正常收尾（anthropic end_turn/stop_sequence、message_stop 事件；openai finish_reason=stop 等）
        "cut" = 被截断（anthropic stop_reason=max_tokens；openai finish_reason=length）——预算耗尽或中转
                中途截断，此时累积文本必然是残缺 JSON，绝不能当成功返回。
        供 complete_via_stream 判定：见 "cut" 直接按网络类失败上抛重试，避免残缺响应污染候选。
        不改动 _parse_stream_line（问答流式仍用它）。
        """
        if not line:
            return "", ""
        stripped = line.strip()
        # anthropic SSE 先发一行 `event: message_stop`（无 data:）作为正常终止事件
        if stripped.startswith("event:"):
            return "", ("ok" if stripped[6:].strip() == "message_stop" else "")
        if not line.startswith("data:"):
            return "", ""
        data = line[5:].strip()
        if not data:
            return "", ""
        if data == "[DONE]":  # openai 正常终止哨兵
            return "", "ok"
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return "", ""
        if not isinstance(obj, dict):
            return "", ""
        if obj.get("type") == "message_stop":  # anthropic：终止事件也可能出现在 data 里
            return "", "ok"
        delta = obj.get("delta")
        if isinstance(delta, dict):
            if isinstance(delta.get("text"), str):
                return delta["text"], ""
            stop = delta.get("stop_reason")  # anthropic message_delta 携带 stop_reason 即已收尾
            if stop:
                return "", ("cut" if stop == "max_tokens" else "ok")
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] or {}
            finish = first.get("finish_reason")
            kind = ("cut" if finish == "length" else "ok") if finish else ""
            piece = (first.get("delta") or {}).get("content")
            if isinstance(piece, str) and piece:
                return piece, kind
            if finish:  # openai：finish_reason 非空即收尾
                return "", kind
        return "", ""

    @staticmethod
    def _extract_text(data: dict) -> str:
        """兼容两套响应结构：原生 Anthropic 的 content[].text 与 OpenAI/中转归一化后的 choices[].message.content。

        apifox 文档里原生端点的响应示例是 OpenAI 结构（疑为复制粘贴），无法据以确定中转究竟回哪套，
        故两套都试：优先原生 text 块，缺失再退回 choices。任一取到非空文本即返回，都取不到才判不兼容。
        """
        try:
            content = data.get("content")
            if isinstance(content, list):
                text = "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
                if text.strip():
                    return text
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                text = ((choices[0] or {}).get("message") or {}).get("content")
                if isinstance(text, str):
                    return text
        except (AttributeError, KeyError, IndexError, TypeError):
            pass
        raise ExternalServiceError("模型响应结构不兼容")

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(min(_HTTP_BACKOFF_CAP, _HTTP_BACKOFF_BASE * (2 ** attempt)) + random.uniform(0, 0.4))

    async def generate_image(self, api_key: str, model: str, prompt: str, size: str = "1536x1024", endpoint: LlmEndpoint | None = None) -> tuple[bytes, str]:
        """Generate one image and return bytes plus a safe extension.

        The provider follows the OpenAI-compatible /images/generations contract.
        URL responses are downloaded server-side; data URLs never reach logs.
        """
        endpoint = endpoint or _default_endpoint()
        payload = {"model": model, "prompt": prompt, "size": size, "response_format": "b64_json"}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
                response = await client.post(f"{endpoint.base_url}/images/generations", json=payload, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            raise ExternalServiceError(f"图片模型网络调用失败：{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise ExternalServiceError(f"图片模型调用失败：HTTP {response.status_code}")
        try:
            item = response.json()["data"][0]
            if item.get("b64_json"):
                return base64.b64decode(item["b64_json"]), ".png"
            image_url = item.get("url")
            if image_url:
                async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
                    image = await client.get(image_url)
                image.raise_for_status()
                return image.content, ".png"
        except (KeyError, IndexError, TypeError, ValueError, httpx.HTTPError) as exc:
            raise ExternalServiceError("图片模型响应结构不兼容") from exc
        raise ExternalServiceError("图片模型未返回图片")


class WebResearchService:
    class _TextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self.ignored = 0

        def handle_starttag(self, tag: str, attrs) -> None:
            if tag in {"script", "style", "svg", "noscript"}:
                self.ignored += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in {"script", "style", "svg", "noscript"} and self.ignored:
                self.ignored -= 1

        def handle_data(self, data: str) -> None:
            if not self.ignored:
                value = " ".join(data.split())
                if len(value) >= 20:
                    self.parts.append(value)

    @staticmethod
    def _safe_url(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.hostname.casefold() in {"localhost", "localhost.localdomain"}:
            return False
        try:
            address = ip_address(parsed.hostname)
        except ValueError:
            return True
        return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)

    async def _fetch_page(self, client: httpx.AsyncClient, item: dict) -> dict:
        if not self._safe_url(item["url"]):
            return {**item, "fetch_status": "blocked_url", "excerpt": ""}
        try:
            response = await client.get(item["url"])
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type and "text/plain" not in content_type:
                return {**item, "fetch_status": "unsupported_content", "excerpt": "", "content_type": content_type}
            extractor = self._TextExtractor()
            extractor.feed(response.text)
            text = "\n".join(extractor.parts)
            excerpt = text[:settings.evidence_chars_per_page]
            return {
                **item,
                "fetch_status": "ok" if excerpt else "empty",
                "excerpt": excerpt,
                "content_type": content_type,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "content_sha256": hashlib.sha256(response.content).hexdigest(),
            }
        except (httpx.HTTPError, OSError, ssl.SSLError, UnicodeError, ValueError) as exc:
            return {**item, "fetch_status": "failed", "excerpt": "", "error": type(exc).__name__}

    async def search(self, query: str, limit: int | None = None) -> list[dict]:
        limit = limit or settings.research_result_limit
        headers = {"User-Agent": "Mozilla/5.0 SEMI-KB-Research/1.0"}
        providers = [
            (f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'),
            (f"https://www.bing.com/search?q={quote_plus(query)}", r'<li[^>]+class="[^"]*b_algo[^"]*".*?<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>'),
        ]
        anchors = []
        failures = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
            for url, pattern in providers:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    anchors = re.findall(pattern, response.text, re.I | re.S)
                except (httpx.HTTPError, OSError, ssl.SSLError) as exc:
                    failures.append(f"{urlparse(url).hostname}:{type(exc).__name__}")
                    continue
                if anchors:
                    break
        if not anchors:
            raise ExternalServiceError("搜索服务均不可用或页面结构无法解析：" + "、".join(failures or ["no_results"]))
        results: list[dict] = []
        for href, title_html in anchors:
            href = html.unescape(href)
            parsed = urlparse(href)
            if "uddg" in parse_qs(parsed.query):
                href = parse_qs(parsed.query)["uddg"][0]
            title = re.sub(r"<[^>]+>", "", html.unescape(title_html)).strip()
            if href.startswith("http") and not any(item["url"] == href for item in results):
                results.append({"title": title, "url": href, "source_type": "web"})
            if len(results) >= limit:
                break
        if not results:
            raise ExternalServiceError("搜索没有返回可解析结果")
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers=headers, max_redirects=5) as client:
            enriched = await asyncio.gather(*(self._fetch_page(client, item) for item in results))
        if not any(item.get("fetch_status") == "ok" for item in enriched):
            raise ExternalServiceError("搜索结果页面均无法提取正文证据")
        return enriched


llm_service = LlmService()
web_research = WebResearchService()
