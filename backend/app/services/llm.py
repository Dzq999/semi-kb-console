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

from ..config import settings
from ..models import EncryptedCredential
from ..security import decrypt_secret


class ExternalServiceError(RuntimeError):
    pass


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
_HTTP_RETRIES = 2  # 初次调用之外的额外重试次数（共 3 次尝试）
_HTTP_BACKOFF_BASE = 1.0
_HTTP_BACKOFF_CAP = 8.0


def user_api_key(db: Session, user_id: int) -> str | None:
    row = db.scalar(select(EncryptedCredential).where(EncryptedCredential.user_id == user_id, EncryptedCredential.kind == "llm_api_key"))
    return decrypt_secret(row.ciphertext) if row else settings.llm_api_key


class LlmService:
    async def list_models(self, api_key: str, search: str = "") -> list[dict]:
        headers = {"Authorization": f"Bearer {api_key}"}
        # 目录取自与 complete 同源的第三方代理，同样会遇到瞬时抖动（ConnectTimeout / 连接被中途掐断
        # / 瞬时 5xx）。原先单次即抛，任务编排里就会频繁弹“模型目录网络失败”；这里与 complete 一致
        # 就地重试再上抛，消化网络抖动而非把每次抖动都暴露给编排层。
        response = None
        for attempt in range(_HTTP_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    response = await client.get(settings.model_catalog_url, headers=headers)
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

    async def complete(self, api_key: str, model: str, system: str, user: str, temperature: float = 0.2, timeout_seconds: int = 120, max_tokens: int | None = None) -> str:
        # Claude 模型经中转的 OpenAI 兼容层（/chat/completions）做协议转译，长响应/并发下更易被中途
        # 掐断（RemoteProtocolError）；原生 /v1/messages 少一层转译、更稳，是本系统默认走的路。system 在
        # 原生契约里是顶层字段而非消息，且 max_tokens 必填。两套响应结构都在 _extract_text 里兼容解析。
        max_tokens = max_tokens or settings.llm_max_tokens
        if settings.llm_api_style == "anthropic":
            url = f"{settings.llm_base_url}/messages"
            payload = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}], "temperature": temperature}
        else:
            url = f"{settings.llm_base_url}/chat/completions"
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

    async def generate_image(self, api_key: str, model: str, prompt: str, size: str = "1536x1024") -> tuple[bytes, str]:
        """Generate one image and return bytes plus a safe extension.

        The provider follows the OpenAI-compatible /images/generations contract.
        URL responses are downloaded server-side; data URLs never reach logs.
        """
        payload = {"model": model, "prompt": prompt, "size": size, "response_format": "b64_json"}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
                response = await client.post(f"{settings.llm_base_url}/images/generations", json=payload, headers=headers)
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
