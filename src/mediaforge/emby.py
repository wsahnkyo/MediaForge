import json
import logging
import re
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.exc import SQLAlchemyError

from mediaforge.database import EmbySetting, OperationStore

logger = logging.getLogger(__name__)


class EmbyUpdate(BaseModel):
    server_url: str
    api_token: SecretStr | None = Field(default=None, exclude=True)
    auto_refresh: bool = False


class EmbyRefresh(BaseModel):
    library_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,128}$")


class EmbyService:
    def __init__(self, store: OperationStore):
        self.store = store
        self.lock = RLock()
        self.transport: httpx.BaseTransport | None = None

    def _setting(self) -> EmbySetting:
        with self.store.sessions() as session:
            return session.get(EmbySetting, 1) or EmbySetting(
                id=1,
                server_url="",
                api_token=None,
                auto_refresh=False,
            )

    def configuration(self) -> dict[str, Any]:
        setting = self._setting()
        return {
            "server_url": setting.server_url,
            "configured": bool(setting.api_token),
            "auto_refresh": setting.auto_refresh,
        }

    def save(self, request: EmbyUpdate) -> dict[str, Any]:
        url = request.server_url.strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            )
            _ = parsed.port
            httpx.URL(url)
        except (ValueError, httpx.InvalidURL):
            valid = False
        if not valid or len(url) > 2000 or any(ord(char) < 33 for char in url):
            raise ValueError(
                "Emby 地址必须是 HTTP/HTTPS 服务地址，不能包含账号密码、查询参数或片段"
            )
        key = request.api_token.get_secret_value().strip() if request.api_token else ""
        if key and not re.fullmatch(r"[a-zA-Z0-9._-]{1,2048}", key):
            raise ValueError("Emby Token 格式不正确")

        def apply() -> dict[str, Any]:
            with self.store.sessions() as session:
                setting = session.get(EmbySetting, 1) or EmbySetting(id=1)
                previous_url = setting.server_url
                setting.server_url = url
                if key:
                    setting.api_token = key
                elif setting.api_token and url != previous_url:
                    raise ValueError(
                        "更换 Emby 地址时必须重新填写 Token，避免将原 Token 发送给其他服务器"
                    )
                if request.auto_refresh and not setting.api_token:
                    raise ValueError("启用自动刷新前请先配置 Emby Token")
                setting.auto_refresh = request.auto_refresh
                session.add(setting)
                session.commit()
            return self.configuration()

        with self.lock:
            return self.store.execute(
                "emby.settings.save",
                {
                    "server_url": url,
                    "auto_refresh": request.auto_refresh,
                    "token_updated": bool(key),
                },
                apply,
            )

    def clear_token(self) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            with self.store.sessions() as session:
                setting = session.get(EmbySetting, 1)
                if setting:
                    setting.api_token = None
                    setting.auto_refresh = False
                    session.commit()
            return self.configuration()

        with self.lock:
            return self.store.execute("emby.token.clear", {}, apply)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        setting: EmbySetting | None = None,
    ) -> Any:
        setting = setting or self._setting()
        if not setting.server_url or not setting.api_token:
            raise ValueError("请先配置 Emby 地址和 Token")
        try:
            with httpx.Client(
                base_url=setting.server_url + "/",
                headers={"X-Emby-Token": setting.api_token},
                timeout=15,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    method, path, params=params, **({"json": {}} if method == "POST" else {})
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise ValueError(
                            f"Emby 请求失败（HTTP {response.status_code}），"
                            "请检查地址、Token 和权限"
                        )
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        content.extend(chunk)
                        if len(content) > 2 * 1024 * 1024:
                            raise ValueError("Emby 返回数据过大")
                    if method == "POST":
                        return None
                    try:
                        return json.loads(content)
                    except (ValueError, UnicodeError):
                        raise ValueError("Emby 返回了无效 JSON") from None
        except httpx.HTTPError:
            raise ValueError("无法连接 Emby，请检查服务地址、网络或 HTTPS 证书") from None

    def test_connection(self) -> dict[str, Any]:
        data = self._request("GET", "System/Info")
        if not isinstance(data, dict) or not isinstance(data.get("ServerName"), str):
            raise ValueError("Emby 系统信息响应不正确")
        return {"server_name": data["ServerName"], "version": data.get("Version", "")}

    def libraries(self, setting: EmbySetting | None = None) -> list[dict[str, str]]:
        setting = setting or self._setting()
        libraries = []
        for start in range(0, 1000, 200):
            data = self._request(
                "GET",
                "Library/VirtualFolders/Query",
                params={"StartIndex": start, "Limit": 200},
                setting=setting,
            )
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("Items"), list)
                or not isinstance(data.get("TotalRecordCount"), int)
            ):
                raise ValueError("Emby 媒体库列表响应不正确")
            for item in data["Items"]:
                if not isinstance(item, dict):
                    raise ValueError("Emby 媒体库记录不正确")
                library_id = item.get("ItemId") or item.get("Id")
                if (
                    not isinstance(library_id, str)
                    or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", library_id)
                    or not isinstance(item.get("Name"), str)
                ):
                    raise ValueError("Emby 媒体库缺少有效的编号或名称")
                libraries.append({"id": library_id, "name": item["Name"]})
            if len(libraries) >= data["TotalRecordCount"]:
                return libraries
            if len(data["Items"]) != 200:
                raise ValueError("Emby 媒体库分页不完整，请重试")
        raise ValueError("Emby 媒体库超过 1000 个，无法完整列出")

    def refresh(self, request: EmbyRefresh) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            setting = self._setting()
            if request.library_id:
                if request.library_id not in {item["id"] for item in self.libraries(setting)}:
                    raise ValueError("所选 Emby 媒体库已不存在，请刷新列表")
                self._request(
                    "POST",
                    f"Items/{request.library_id}/Refresh",
                    params={
                        "Recursive": "true",
                        "MetadataRefreshMode": "Default",
                        "ImageRefreshMode": "Default",
                        "ReplaceAllMetadata": "false",
                        "ReplaceAllImages": "false",
                    },
                    setting=setting,
                )
            else:
                self._request("POST", "Library/Refresh", setting=setting)
            return {
                "accepted": True,
                "library_id": request.library_id,
                "message": "Emby 已接受刷新请求；实际扫描进度请在 Emby 查看。",
            }

        return self.store.execute("emby.library.refresh", request.model_dump(), apply)

    def after_change(self, result: dict[str, Any], changed: bool) -> dict[str, Any]:
        if not changed:
            return result
        try:
            if self._setting().auto_refresh:
                result["emby_refresh"] = {"status": "success", **self.refresh(EmbyRefresh())}
        except (OSError, ValueError, SQLAlchemyError) as exc:
            message = (
                "Emby 配置或刷新记录无法保存，请核对数据库"
                if isinstance(exc, SQLAlchemyError)
                else str(exc)
            )
            logger.warning("媒体操作已完成，但 Emby 自动刷新失败: %s", message)
            result["emby_refresh"] = {"status": "failed", "error": message}
            if result.get("outcome") == "success":
                result["outcome"] = "partial"
        return result
