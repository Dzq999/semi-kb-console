from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from ..models import ArticleAsset


class WechatPublisherError(RuntimeError):
    """A user-safe error raised by the WeChat official-account API."""


class WechatPublisher:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.wechat_api_base_url).rstrip("/")

    @staticmethod
    def _api_error(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            message = payload.get("errmsg") or payload.get("message")
            if message:
                return str(message)[:240]
        return fallback

    async def _access_token(self, app_id: str, app_secret: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(
                    f"{self.base_url}/cgi-bin/token",
                    params={"grant_type": "client_credential", "appid": app_id, "secret": app_secret},
                )
        except (httpx.HTTPError, OSError) as exc:
            raise WechatPublisherError(f"微信 access_token 请求失败：{type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise WechatPublisherError("微信 access_token 响应无法解析") from exc
        if response.status_code >= 400 or not payload.get("access_token"):
            raise WechatPublisherError(self._api_error(payload, f"微信 access_token 请求失败：HTTP {response.status_code}"))
        return str(payload["access_token"])

    @staticmethod
    def _asset_path(asset: ArticleAsset) -> Path:
        path = Path(asset.file_path).resolve()
        root = (settings.semi_kb_root / "knowledge" / "articles").resolve()
        if root not in path.parents or not path.is_file():
            raise WechatPublisherError("文章图片文件不存在或不在允许目录")
        if asset.mime_type not in {"image/png", "image/jpeg", "image/gif"}:
            raise WechatPublisherError("微信草稿仅支持 PNG、JPEG 或 GIF 图片")
        return path

    async def _upload_content_image(self, token: str, asset: ArticleAsset) -> str:
        path = self._asset_path(asset)
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                with path.open("rb") as handle:
                    response = await client.post(
                        f"{self.base_url}/cgi-bin/media/uploadimg",
                        params={"access_token": token},
                        files={"media": (path.name, handle, asset.mime_type)},
                    )
        except (httpx.HTTPError, OSError) as exc:
            raise WechatPublisherError(f"微信正文图片上传失败：{type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise WechatPublisherError("微信正文图片上传响应无法解析") from exc
        if response.status_code >= 400 or not payload.get("url"):
            raise WechatPublisherError(self._api_error(payload, f"微信正文图片上传失败：HTTP {response.status_code}"))
        return str(payload["url"])

    async def _upload_thumb(self, token: str, asset: ArticleAsset) -> str | None:
        try:
            path = self._asset_path(asset)
        except WechatPublisherError:
            return None
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                with path.open("rb") as handle:
                    response = await client.post(
                        f"{self.base_url}/cgi-bin/material/add_material",
                        params={"access_token": token, "type": "image"},
                        files={"media": (path.name, handle, asset.mime_type)},
                    )
        except (httpx.HTTPError, OSError) as exc:
            raise WechatPublisherError(f"微信封面上传失败：{type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise WechatPublisherError("微信封面上传响应无法解析") from exc
        if response.status_code >= 400 or not payload.get("media_id"):
            raise WechatPublisherError(self._api_error(payload, f"微信封面上传失败：HTTP {response.status_code}"))
        return str(payload["media_id"])

    async def _prepare_content(self, token: str, content_html: str, assets: list[ArticleAsset]) -> str:
        by_url: dict[str, ArticleAsset] = {
            f"/api/articles/{asset.article_id}/assets/{asset.id}": asset for asset in assets
        }
        uploads: dict[int, str] = {}
        for asset in assets:
            if asset.mime_type not in {"image/png", "image/jpeg", "image/gif"}:
                continue
            uploads[asset.id] = await self._upload_content_image(token, asset)

        def replace(match: re.Match[str]) -> str:
            source = html.unescape(match.group(1))
            asset = by_url.get(source)
            if not asset:
                return match.group(0)
            url = uploads.get(asset.id)
            if not url:
                return ""
            return f'src="{html.escape(url, quote=True)}"'

        return re.sub(r'src="([^\"]+)"', replace, content_html)

    async def create_draft(
        self,
        app_id: str,
        app_secret: str,
        title: str,
        digest: str,
        content_html: str,
        assets: list[ArticleAsset],
        author: str | None = None,
    ) -> dict[str, Any]:
        token = await self._access_token(app_id, app_secret)
        content = await self._prepare_content(token, content_html, assets)
        thumb_asset = next((asset for asset in assets if asset.mime_type in {"image/png", "image/jpeg", "image/gif"}), None)
        thumb_media_id = await self._upload_thumb(token, thumb_asset) if thumb_asset is not None else None
        if not thumb_media_id:
            raise WechatPublisherError("请至少准备一张 PNG、JPEG 或 GIF 配图作为公众号封面")
        article: dict[str, Any] = {
            "title": title[:64],
            "author": (author or settings.wechat_account_name)[:16],
            "digest": digest[:120],
            "content": content,
            "content_source_url": "",
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }
        if thumb_media_id:
            article["thumb_media_id"] = thumb_media_id
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.post(
                    f"{self.base_url}/cgi-bin/draft/add",
                    params={"access_token": token},
                    json={"articles": [article]},
                )
        except (httpx.HTTPError, OSError) as exc:
            raise WechatPublisherError(f"微信草稿创建失败：{type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise WechatPublisherError("微信草稿创建响应无法解析") from exc
        if response.status_code >= 400 or payload.get("errcode", 0) != 0 or not payload.get("media_id"):
            raise WechatPublisherError(self._api_error(payload, f"微信草稿创建失败：HTTP {response.status_code}"))
        return {"media_id": str(payload["media_id"]), "errcode": payload.get("errcode", 0), "errmsg": payload.get("errmsg", "ok")}


wechat_publisher = WechatPublisher()
