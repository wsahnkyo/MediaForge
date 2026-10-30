import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import select

from mediaforge.config import Settings
from mediaforge.database import MetadataMapping, OperationFailure, OperationStore, operation_actor
from mediaforge.files import FileService, signature
from mediaforge.scrapers import (
    ScraperClient,
    ScraperDefinition,
    ScraperService,
    ScraperSettingsUpdate,
    ScraperUpdate,
    fill_missing,
)

logger = logging.getLogger(__name__)
MediaType = Literal["tv", "movie"]
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".wmv", ".m2ts"}
EPISODE = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,3})e(\d{1,4})(?!\d)")
MULTI_EPISODE = re.compile(
    r"(?i)s\d+e\d+(?:[ ._-]*e\d+|[ ._-]+\d{1,4}(?![a-z0-9])|[ ._-]*s\d+e\d+)"
)
EXTRA_SUFFIX = re.compile(
    r"(?i)-(?:behindthescenes|deleted|featurette|interview|other|scene|short|trailer)$"
)


class MetadataPreview(BaseModel):
    root: int = Field(ge=0)
    path: str = Field(default="", max_length=2000)
    media_type: MediaType = "tv"
    api_key: SecretStr | None = Field(default=None, exclude=True)
    language: Literal["zh-CN", "zh-TW", "en-US"] = "zh-CN"
    overwrite: bool = False
    scrapers: list[str] | None = Field(default=None, max_length=32)
    roots_version: str | None = None


class MetadataApply(BaseModel):
    preview_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]+$")
    api_key: SecretStr | None = Field(default=None, exclude=True)


class MetadataSearch(BaseModel):
    query: str = Field(min_length=1, max_length=255)
    media_type: MediaType = "tv"
    language: Literal["zh-CN", "zh-TW", "en-US"] = "zh-CN"
    page: int = Field(default=1, ge=1, le=500)
    parse_folder_name: bool = False
    api_key: SecretStr | None = Field(default=None, exclude=True)
    scrapers: list[str] | None = Field(default=None, max_length=32)

    @field_validator("query")
    @classmethod
    def valid_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入要搜索的剧名或电影名")
        return value.strip()


class MetadataMappingInput(BaseModel):
    media_type: MediaType
    root: int = Field(ge=0)
    path: str = Field(default="", max_length=2000)
    tmdb_id: int = Field(strict=True, gt=0, le=2147483647)
    roots_version: str | None = None


class MetadataCleanup(BaseModel):
    dry_run: bool = True
    api_key: SecretStr | None = Field(default=None, exclude=True)


class MetadataMappingsDelete(BaseModel):
    ids: list[Annotated[str, Field(min_length=1, max_length=36)]] = Field(
        min_length=1, max_length=1000
    )


class TMDBHTTPError(ValueError):
    def __init__(self, status_code: int, image: bool = False):
        self.status_code = status_code
        super().__init__(
            f"TMDB {'图片' if image else 'API'} 请求失败（HTTP {status_code}），"
            "请检查凭据、网络或请求额度"
        )


def validate_tmdb_key(key: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9._-]{16,2048}", key):
        raise ValueError("请填写有效的 TMDB API Key（v3）或 API Read Access Token")


class TMDBClient:
    """Use the transport directly so HTTPX cannot log URLs containing an API key."""

    def __init__(self, key: str, language: str, transport: httpx.BaseTransport | None = None):
        validate_tmdb_key(key)
        self.key = key
        self.language = language
        self.transport = transport or httpx.HTTPTransport(retries=1)

    def close(self) -> None:
        self.transport.close()

    def _request(self, url: str, *, image: bool = False, **params: Any) -> bytes:
        headers = {}
        if not image:
            params["language"] = self.language
            if "." in self.key:
                headers["Authorization"] = "Bearer " + self.key
            else:
                params["api_key"] = self.key
        request = httpx.Request(
            "GET",
            url,
            params=params,
            headers=headers,
            extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), 15.0)},
        )
        try:
            with closing(self.transport.handle_request(request)) as response:
                if response.status_code != 200:
                    raise TMDBHTTPError(response.status_code, image)
                if image and response.headers.get("content-type", "").split(";")[0] != "image/jpeg":
                    raise ValueError("TMDB 图片不是 JPEG，未写入")
                limit = 20 * 1024 * 1024 if image else 10 * 1024 * 1024
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ValueError("TMDB 响应超过大小限制")
                if image and not content.startswith(b"\xff\xd8\xff"):
                    raise ValueError("TMDB 图片内容无效，未写入")
                return bytes(content)
        except httpx.HTTPError:
            # HTTPX exception strings can contain credentials from the request URL.
            raise ValueError("TMDB 网络请求失败，请检查网络、DNS 和 HTTPS 证书") from None

    def get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        content = self._request("https://api.themoviedb.org/3/" + endpoint, **params)
        try:
            result = json.loads(content)
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("TMDB 返回了无效 JSON") from None
        if not isinstance(result, dict):
            raise ValueError("TMDB 返回了无效数据")
        if endpoint.startswith("search/") and (
            not isinstance(result.get("results"), list)
            or any(not isinstance(item, dict) for item in result["results"])
            or not isinstance(result.get("total_pages", 1), int)
        ):
            raise ValueError("TMDB 返回了无效搜索结果")
        return result

    def image(self, path: str) -> bytes:
        if not re.fullmatch(r"/[A-Za-z0-9_-]+\.jpg", path):
            raise ValueError("TMDB 图片路径无效")
        return self._request("https://image.tmdb.org/t/p/original" + path, image=True)

    def search(
        self, media_type: str, query: str, page: int = 1, year: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query, "page": page, "include_adult": "false"}
        if year:
            params["first_air_date_year" if media_type == "tv" else "year"] = year
        return self.get("search/" + media_type, **params)

    def details(self, media_type: str, media_id: int) -> dict[str, Any]:
        data = self.get(f"{media_type}/{media_id}", append_to_response="credits,external_ids")
        data["provider_ids"] = {"tmdb": str(data.get("id", ""))}
        for key in ("poster_path", "backdrop_path"):
            if data.get(key) and not re.fullmatch(r"/[A-Za-z0-9_-]+\.jpg", data[key]):
                raise ValueError("TMDB 图片路径无效")
        return data

    def season(self, media_id: int, season: int) -> dict[str, Any]:
        data = self.get(f"tv/{media_id}/season/{season}")
        if isinstance(data.get("episodes"), list):
            for episode in data["episodes"]:
                if isinstance(episode, dict) and isinstance(episode.get("id"), int):
                    episode["provider_ids"] = {"tmdb": str(episode["id"])}
        return data


def folder_query(name: str) -> tuple[str, str | None]:
    name = re.sub(r"(?i)\{(?:tmdb|tvdb|imdb)[^}]*}", "", name)
    years = list(re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", name))
    year = years[-1].group(1) if years else None
    if years:
        name = name[: years[-1].start()].rstrip(" ([._-")
    name = re.split(r"(?i)(?:\bS\d{1,3}\b|\b(?:2160p|1080p|720p|bluray|web-dl|webrip)\b)", name)[0]
    name = re.sub(r"[._]+", " ", name).strip(" []()-")
    if not name:
        raise ValueError("无法从文件夹名称提取片名")
    return name, year


def normalized(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum()
    )


def match_candidate(
    results: list[dict[str, Any]], query: str, year: str | None, media_type: MediaType
) -> dict[str, Any] | None:
    if not normalized(query):
        return None
    title_key = "name" if media_type == "tv" else "title"
    date_key = "first_air_date" if media_type == "tv" else "release_date"
    matches = {
        item["id"]: item
        for item in results
        if isinstance(item.get("id"), int)
        and normalized(query)
        in {
            normalized(item.get(title_key) or ""),
            normalized(item.get("original_" + title_key) or ""),
        }
        and (not year or (item.get(date_key) or "").startswith(year))
    }
    return next(iter(matches.values())) if len(matches) == 1 else None


def nfo(
    data: dict[str, Any],
    media_type: MediaType,
    tmdb_id: int | str | None,
    *,
    episode: bool = False,
) -> bytes:
    root = ET.Element("episodedetails" if episode else "tvshow" if media_type == "tv" else "movie")

    def add(tag: str, value: Any, parent: ET.Element = root, **attrs: str) -> None:
        if value is not None and value != "":
            ET.SubElement(parent, tag, attrs).text = re.sub(
                r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", str(value)
            )

    add("title", data.get("name" if media_type == "tv" else "title"))
    add("originaltitle", data.get("original_name" if media_type == "tv" else "original_title"))
    add("plot", data.get("overview"))
    date = data.get(
        "air_date" if episode else "first_air_date" if media_type == "tv" else "release_date"
    )
    if date:
        add("year", date[:4])
        add("aired" if episode else "premiered", date)
    add("rating", data.get("vote_average"))
    add("votes", data.get("vote_count"))
    runtime = data.get("runtime")
    if not runtime and data.get("episode_run_time"):
        runtime = data["episode_run_time"][0]
    add("runtime", runtime)
    if episode:
        add("season", data["season_number"])
        add("episode", data["episode_number"])
    if tmdb_id is not None:
        add("tmdbid", tmdb_id)
        add("uniqueid", tmdb_id, type="tmdb", default="true")
    for provider, value in data.get("provider_ids", {}).items():
        if provider not in {"tmdb", "imdb", "tvdb"}:
            add("uniqueid", value, type=provider)
    external = data.get("external_ids") or {}
    for provider, value in (
        ("imdb", external.get("imdb_id") or data.get("imdb_id")),
        ("tvdb", external.get("tvdb_id")),
    ):
        if value:
            add(provider + "id", value)
            add("uniqueid", value, type=provider)
    for genre in data.get("genres", []):
        add("genre", genre.get("name"))
    for company in data.get("production_companies", []):
        add("studio", company.get("name"))
    for country in data.get("production_countries", []):
        add("country", country.get("name"))
    add("tagline", data.get("tagline"))
    if media_type == "tv" and not episode:
        add("status", data.get("status"))
    credits = data.get("credits") or {}
    for person in (credits.get("cast") or [])[:30]:
        actor = ET.SubElement(root, "actor")
        add("name", person.get("name"), actor)
        add("role", person.get("character"), actor)
        add("order", person.get("order"), actor)
    for person in credits.get("crew") or []:
        if person.get("job") in {"Director", "Writer", "Screenplay"}:
            add("director" if person["job"] == "Director" else "credits", person.get("name"))
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


@dataclass
class Artifact:
    path: str
    content: bytes | None
    image: str | None
    before: tuple[int, int, int] | None
    source: str | None = None
    source_before: tuple[int, int, int] | None = None
    image_scraper: str = "tmdb"


@dataclass
class ScrapeMatch:
    data: dict[str, Any] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)
    artwork: dict[str, tuple[str, str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class NoMetadataMatch(ValueError):
    pass


@dataclass
class Preview:
    request: MetadataPreview
    actor: str | None
    created: float
    artifacts: list[Artifact] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    ready: bool = False


class MetadataService:
    def __init__(self, settings: Settings, files: FileService, store: OperationStore):
        self.settings = settings
        self.files = files
        self.store = store
        self.lock = Lock()
        self.previews: dict[str, Preview] = {}
        self.scrapers = ScraperService(
            settings,
            store,
            {"tmdb": ScraperDefinition("tmdb", "TMDB", TMDBClient, validate_tmdb_key)},
        )

    def save_scrapers(self, request: ScraperSettingsUpdate) -> dict[str, Any]:
        with self.files.lock:
            result = self.scrapers.save(request)
            self.previews.clear()
            return result

    def clear_scraper_key(self, scraper_id: str) -> dict[str, Any]:
        with self.files.lock:
            self.scrapers.definition(scraper_id)
            config = next(
                item for item in self.scrapers.configurations() if item["id"] == scraper_id
            )
            return self.save_scrapers(
                ScraperSettingsUpdate(
                    scrapers=[
                        ScraperUpdate(
                            id=scraper_id,
                            enabled=config["enabled"],
                            priority=config["priority"],
                            clear_api_key=True,
                        )
                    ]
                )
            )

    @contextmanager
    def _clients(self, selected: list[str], key: SecretStr | None, language: str):
        with ExitStack() as stack:
            clients = {
                scraper_id: stack.enter_context(
                    closing(
                        self.client(
                            key if scraper_id == "tmdb" else None,
                            language,
                            scraper_id,
                        )
                    )
                )
                for scraper_id in selected
            }
            yield clients

    def search(self, request: MetadataSearch) -> dict[str, Any]:
        def fetch() -> dict[str, Any]:
            selected = self.scrapers.selection(request.scrapers)
            results: list[dict[str, Any]] = []
            sources = []
            warnings = []
            with self._clients(selected, request.api_key, request.language) as clients:
                query = (
                    folder_query(request.query)[0] if request.parse_folder_name else request.query
                )
                for scraper_id, client in clients.items():
                    try:
                        data = client.search(request.media_type, query, request.page)
                        results.extend(
                            {**item, "scraper_id": scraper_id} for item in data["results"]
                        )
                        sources.append(
                            {
                                "scraper_id": scraper_id,
                                "total_pages": data.get("total_pages", 1),
                                "total_results": data.get("total_results", len(data["results"])),
                            }
                        )
                    except ValueError as exc:
                        warnings.append(f"{self.scrapers.definition(scraper_id).name}：{exc}")
                report = {
                    "query": query,
                    "media_type": request.media_type,
                    "page": request.page,
                    "total_pages": max((source["total_pages"] for source in sources), default=0),
                    "total_results": sum(source["total_results"] for source in sources),
                    "results": results,
                    "sources": sources,
                    "warnings": warnings,
                    "outcome": "partial" if warnings else "success",
                }
                if not sources:
                    report["outcome"] = "failed"
                    raise OperationFailure("；".join(warnings), report)
                return report

        return self.store.execute("metadata.search", request.model_dump(), fetch)

    def _mapping_data(self, mapping: MetadataMapping) -> dict[str, Any]:
        return {
            "id": mapping.id,
            "media_type": mapping.media_type,
            "root": next(
                (
                    index
                    for index, root in enumerate(self.files.roots)
                    if os.path.normcase(str(root)) == os.path.normcase(mapping.root_path)
                ),
                None,
            ),
            "root_path": mapping.root_path,
            "folder_path": mapping.folder_path,
            "folder_name": Path(mapping.folder_path).name or Path(mapping.root_path).name,
            "tmdb_id": mapping.tmdb_id,
        }

    @staticmethod
    def _location_key(path: Path) -> str:
        return sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()

    def mappings(self) -> list[dict[str, Any]]:
        with self.store.sessions() as session:
            return [
                self._mapping_data(mapping)
                for mapping in session.scalars(
                    select(MetadataMapping).order_by(
                        MetadataMapping.media_type,
                        MetadataMapping.root_path,
                        MetadataMapping.folder_path,
                    )
                )
            ]

    def _invalidate_mapping(self, media_type: str, location_key: str) -> None:
        self.previews = {
            key: preview
            for key, preview in self.previews.items()
            if preview.request.media_type != media_type
            or not any(row.get("location_key") == location_key for row in preview.rows)
        }

    def save_mapping(self, request: MetadataMappingInput) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            folder = self.files.resolve(request.root, request.path)
            if not folder.is_dir():
                raise ValueError("只能关联实际存在的影视文件夹")
            root = self.files.root(request.root)
            relative = str(folder.relative_to(root)) if folder != root else ""
            location_key = self._location_key(folder)
            with self.store.sessions() as session:
                mapping = session.scalar(
                    select(MetadataMapping).where(
                        MetadataMapping.media_type == request.media_type,
                        MetadataMapping.location_key == location_key,
                    )
                )
                if mapping is None:
                    mapping = MetadataMapping(
                        media_type=request.media_type,
                        root_path=str(root),
                        folder_path=relative,
                        location_key=location_key,
                        tmdb_id=request.tmdb_id,
                    )
                    session.add(mapping)
                else:
                    mapping.tmdb_id = request.tmdb_id
                    mapping.root_path = str(root)
                    mapping.folder_path = relative
                session.commit()
                result = self._mapping_data(mapping)
            self._invalidate_mapping(request.media_type, location_key)
            return result

        with self.files.lock:
            self.files.check_roots_version(request.roots_version)
            return self.store.execute("metadata.mapping.save", request.model_dump(), apply)

    def _delete_mappings(self, ids: list[str]) -> list[dict[str, Any]]:
        if len(set(ids)) != len(ids):
            raise ValueError("不能重复选择同一条关联")
        with self.store.sessions() as session:
            mappings = list(
                session.scalars(select(MetadataMapping).where(MetadataMapping.id.in_(ids)))
            )
            if len(mappings) != len(ids):
                raise ValueError("手动映射不存在，请刷新列表")
            results = [self._mapping_data(mapping) for mapping in mappings]
            for mapping in mappings:
                session.delete(mapping)
            session.commit()
            for mapping in mappings:
                self._invalidate_mapping(mapping.media_type, mapping.location_key)
            return results

    def delete_mapping(self, mapping_id: str) -> dict[str, Any]:
        with self.files.lock:
            return self.store.execute(
                "metadata.mapping.delete",
                {"id": mapping_id},
                lambda: self._delete_mappings([mapping_id])[0],
            )

    def delete_mappings(self, request: MetadataMappingsDelete) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            results = self._delete_mappings(request.ids)
            return {"mappings": results, "deleted_count": len(results)}

        with self.files.lock:
            return self.store.execute("metadata.mapping.delete_batch", request.model_dump(), apply)

    def cleanup_mappings(self, request: MetadataCleanup) -> dict[str, Any]:
        with self.lock:
            return self.store.execute(
                "metadata.mapping.cleanup",
                request.model_dump(),
                lambda: self._cleanup_mappings(request),
            )

    def _cleanup_mappings(self, request: MetadataCleanup) -> dict[str, Any]:
        with self.files.lock:
            roots_version = self.files.roots_version
            mappings = self.mappings()
        client = None
        blocked_reason = ""
        try:
            client = self.client(request.api_key, "zh-CN")
        except ValueError as exc:
            blocked_reason = str(exc)
        rows: list[dict[str, Any]] = []
        checks: dict[tuple[str, int], tuple[str, str]] = {}
        try:
            for mapping in mappings:
                row = {**mapping, "status": "unverified", "reason": "", "removed": False}
                rows.append(row)
                root = mapping["root"]
                if root is None:
                    row["reason"] = "媒体根目录不在当前白名单中，保留关联；可手工解除"
                    continue
                try:
                    with self.files.lock:
                        self.files.check_roots_version(roots_version)
                        folder = self.files.resolve(root, mapping["folder_path"], must_exist=False)
                        if not stat.S_ISDIR(folder.stat().st_mode):
                            row.update(status="missing_folder", reason="影视文件夹已不存在")
                            continue
                except FileNotFoundError:
                    row.update(status="missing_folder", reason="影视文件夹已不存在")
                    continue
                except (ValueError, OSError) as exc:
                    row["reason"] = f"目录不可验证，保留关联：{exc}"
                    continue
                key = mapping["media_type"], mapping["tmdb_id"]
                if key not in checks:
                    if blocked_reason or client is None:
                        checks[key] = "unverified", blocked_reason
                    else:
                        try:
                            data = client.details(key[0], key[1])
                            if data.get("id") != key[1]:
                                raise ValueError("TMDB 返回的详情编号无效")
                            checks[key] = "valid", "文件夹及 TMDB ID 均存在"
                        except TMDBHTTPError as exc:
                            checks[key] = (
                                ("missing_id", "TMDB 明确返回 HTTP 404，ID 已不存在")
                                if exc.status_code == 404
                                else ("unverified", str(exc))
                            )
                            if exc.status_code in {401, 403, 429}:
                                blocked_reason = str(exc)
                        except ValueError as exc:
                            checks[key] = "unverified", str(exc)
                row["status"], row["reason"] = checks[key]
            if not request.dry_run:
                with self.files.lock, self.store.sessions() as session:
                    self.files.check_roots_version(roots_version)
                    for row in rows:
                        if row["status"] not in {"missing_folder", "missing_id"}:
                            continue
                        mapping = session.get(MetadataMapping, row["id"])
                        if mapping is None or mapping.tmdb_id != row["tmdb_id"]:
                            row.update(status="unverified", reason="关联已变化，保留并请重新检查")
                            continue
                        if row["status"] == "missing_folder":
                            try:
                                path = self.files.resolve(
                                    row["root"],
                                    row["folder_path"],
                                    must_exist=False,
                                )
                                if stat.S_ISDIR(path.stat().st_mode):
                                    row.update(
                                        status="unverified", reason="文件夹重新出现，保留关联"
                                    )
                                    continue
                            except FileNotFoundError:
                                pass
                            except (ValueError, OSError) as exc:
                                row.update(
                                    status="unverified", reason=f"目录不可验证，保留关联：{exc}"
                                )
                                continue
                        session.delete(mapping)
                        self._invalidate_mapping(mapping.media_type, mapping.location_key)
                        row["removed"] = True
                    session.commit()
        finally:
            if client is not None:
                client.close()
        for row in rows:
            logger.info(
                "关联检查 folder=%s status=%s removed=%s: %s",
                row["folder_path"],
                row["status"],
                row["removed"],
                row["reason"],
            )
        return {
            "dry_run": request.dry_run,
            "rows": rows,
            "removed_count": sum(row["removed"] for row in rows),
            "invalid_count": sum(row["status"] in {"missing_folder", "missing_id"} for row in rows),
            "outcome": "partial"
            if any(row["status"] == "unverified" for row in rows)
            else "success",
        }

    def client(
        self, key: SecretStr | None, language: str, scraper_id: str = "tmdb"
    ) -> ScraperClient:
        return self.scrapers.open(scraper_id, language, key)

    def _resolve_metadata(
        self,
        request: MetadataPreview,
        folder_name: str,
        known_tmdb: int | None,
        clients: dict[str, ScraperClient],
    ) -> ScrapeMatch:
        result = ScrapeMatch()
        errors = []
        anchor = None
        if known_tmdb is not None and "tmdb" in clients:
            anchor = clients["tmdb"].details(request.media_type, known_tmdb)
            if anchor.get("id") != known_tmdb:
                raise ValueError("TMDB 手动关联详情编号无效")
            if not anchor.get("name" if request.media_type == "tv" else "title"):
                raise ValueError("TMDB 手动关联详情缺少片名")
        for scraper_id, client in clients.items():
            name = self.scrapers.definition(scraper_id).name
            try:
                if scraper_id == "tmdb" and anchor is not None and known_tmdb is not None:
                    media_id, data = known_tmdb, anchor
                else:
                    reference = anchor or result.data
                    if reference:
                        query = reference.get("name" if request.media_type == "tv" else "title")
                        date = reference.get(
                            "first_air_date" if request.media_type == "tv" else "release_date"
                        )
                        year = date[:4] if date else None
                    else:
                        query, year = folder_query(folder_name)
                    search = client.search(request.media_type, query, year=year)
                    candidate = match_candidate(
                        search.get("results", []),
                        query,
                        year,
                        request.media_type,
                    )
                    if candidate is None or search.get("total_pages", 1) > 1:
                        result.warnings.append(f"{name} 无唯一同名同年份匹配")
                        continue
                    media_id = candidate["id"]
                    data = client.details(request.media_type, media_id)
                if data.get("id") != media_id or not data.get(
                    "name" if request.media_type == "tv" else "title"
                ):
                    raise ValueError("详情缺少有效的编号或片名")
                result.ids[scraper_id] = media_id
                result.data = fill_missing(result.data, data)
                for key in ("poster_path", "backdrop_path"):
                    if data.get(key) and key not in result.artwork:
                        result.artwork[key] = scraper_id, data[key]
            except ValueError as exc:
                message = f"{name}：{exc}"
                errors.append(message)
                result.warnings.append(message)
        if not result.ids:
            message = "；".join(result.warnings)
            if errors:
                raise ValueError(message)
            raise NoMetadataMatch(message + "，可选择候选 ID 后保存关联")
        return result

    def preview(self, request: MetadataPreview) -> dict[str, Any]:
        payload = request.model_dump()
        with self.lock:
            with self.files.lock:
                self.files.check_roots_version(request.roots_version)
            return self.store.execute("metadata.preview", payload, lambda: self._preview(request))

    def _check_preview(self, preview_id: str, preview: Preview) -> None:
        self.files.check_roots_version(preview.request.roots_version)
        if self.previews.get(preview_id) is not preview:
            raise ValueError("配置或影视关联已变化，请重新预览")

    def _preview(self, request: MetadataPreview) -> dict[str, Any]:
        with self.files.lock:
            self.files.check_roots_version(request.roots_version)
            request = request.model_copy(update={"roots_version": self.files.roots_version})
            selected = self.scrapers.selection(request.scrapers)
            folder = self.files.resolve(request.root, request.path)
            videos = self._videos(request.root, request.path)
            location_key = self._location_key(folder)
            with self.store.sessions() as session:
                known_tmdb = session.scalar(
                    select(MetadataMapping.tmdb_id).where(
                        MetadataMapping.media_type == request.media_type,
                        MetadataMapping.location_key == location_key,
                    )
                )
            preview = Preview(
                request=request.model_copy(update={"api_key": None, "scrapers": selected}),
                actor=operation_actor.get(),
                created=time.monotonic(),
            )
            row: dict[str, Any] = {
                "folder": request.path,
                "folder_name": folder.name,
                "location_key": location_key,
                "status": "skipped",
            }
            rows = preview.rows = [row]
            self.previews = {
                key: value
                for key, value in self.previews.items()
                if time.monotonic() - value.created < 1800
            }
            if len(self.previews) >= 10:
                raise ValueError("未执行的预览过多，请等待旧预览过期后重试")
            preview_id = uuid4().hex
            self.previews[preview_id] = preview
        try:
            with self._clients(selected, request.api_key, request.language) as clients:
                try:
                    known_tmdb = known_tmdb if "tmdb" in selected else None
                    row["match_source"] = "manual" if known_tmdb is not None else "automatic"
                    row["tmdb_id"] = known_tmdb
                    match = self._resolve_metadata(request, folder.name, known_tmdb, clients)
                    data = match.data
                    artifacts, warnings = self._artifacts(
                        request, request.path, match, clients, videos
                    )
                    preview.artifacts.extend(artifacts)
                    row.update(
                        status="matched",
                        title=data.get("name" if request.media_type == "tv" else "title"),
                        year=(
                            data.get(
                                "first_air_date" if request.media_type == "tv" else "release_date"
                            )
                            or ""
                        )[:4],
                        tmdb_id=match.ids.get("tmdb"),
                        scrapers=list(match.ids),
                        files=[item.path for item in artifacts],
                        warnings=match.warnings + warnings,
                    )
                except NoMetadataMatch as exc:
                    row["reason"] = str(exc)
                except (ValueError, OSError) as exc:
                    row.update(status="failed", reason=str(exc))
                    logger.warning("刮削预览失败 folder=%s: %s", folder, exc)
            with self.files.lock:
                self._check_preview(preview_id, preview)
                preview.ready = bool(preview.artifacts)
                preview.created = time.monotonic()
        finally:
            with self.files.lock:
                if not preview.ready:
                    self.previews.pop(preview_id, None)
        has_issues = any(row["status"] != "matched" or row.get("warnings") for row in rows)
        has_matches = any(row["status"] == "matched" for row in rows)
        return {
            "preview_id": preview_id,
            "rows": rows,
            "file_count": len(preview.artifacts),
            "overwrite": request.overwrite,
            "scrapers": selected,
            "outcome": ("partial" if has_issues else "success") if has_matches else "failed",
        }

    def _artifacts(
        self,
        request: MetadataPreview,
        relative: str,
        match: ScrapeMatch,
        clients: dict[str, ScraperClient],
        videos: list[Path],
    ) -> tuple[list[Artifact], list[str]]:
        data = match.data
        artifacts: list[Artifact] = []
        warnings: list[str] = []
        targets: dict[str, Artifact] = {}

        def add(
            path: str,
            content: bytes | None = None,
            image: str | None = None,
            source: str | None = None,
            image_scraper: str = "tmdb",
        ) -> None:
            previous = targets.get(path.casefold())
            if previous:
                if previous.content != content or previous.image != image:
                    raise ValueError(f"多个媒体文件产生冲突的 NFO：{path}")
                return
            with self.files.lock:
                self.files.check_roots_version(request.roots_version)
                target = self.files.resolve(request.root, path, must_exist=False)
                before = signature(target)
                source_before = (
                    signature(self.files.resolve(request.root, source)) if source else None
                )
            if before and not request.overwrite:
                warnings.append(f"保留已有文件：{path}")
                return
            artifact = Artifact(path, content, image, before, source, source_before, image_scraper)
            targets[path.casefold()] = artifact
            artifacts.append(artifact)

        filename = "tvshow.nfo" if request.media_type == "tv" else "movie.nfo"
        add(
            str(Path(relative) / filename),
            nfo(data, request.media_type, data.get("provider_ids", {}).get("tmdb")),
        )
        for key, filename in (("poster_path", "poster.jpg"), ("backdrop_path", "fanart.jpg")):
            if key in match.artwork:
                scraper_id, image = match.artwork[key]
                add(str(Path(relative) / filename), image=image, image_scraper=scraper_id)
            else:
                warnings.append(f"所选刮削器未提供 {filename}")
        if request.media_type == "movie":
            return artifacts, warnings

        if not videos:
            warnings.append("当前文件夹没有直属视频，仅生成剧集详情和图片；不会扫描子目录")
        seasons: dict[int, dict[int, dict[str, Any]]] = {}
        for video in videos:
            episode_match = EPISODE.search(video.name)
            if MULTI_EPISODE.search(video.stem) or len(EPISODE.findall(video.stem)) > 1:
                warnings.append(f"多集文件暂不支持，跳过：{video.name}")
                continue
            if episode_match:
                season, number = map(int, episode_match.groups())
            else:
                if not re.fullmatch(r"\d{1,3}", video.stem):
                    warnings.append(f"无法识别季集编号，跳过：{video.name}")
                    continue
                season, number = 1, int(video.stem)
            if not 0 <= season <= 999 or not 1 <= number <= 999:
                warnings.append(f"季集编号超出范围，跳过：{video.name}")
                continue
            if season not in seasons:
                seasons[season] = {}
                for scraper_id, media_id in match.ids.items():
                    try:
                        season_data = clients[scraper_id].season(media_id, season)
                        episodes = season_data.get("episodes")
                        if not isinstance(episodes, list) or any(
                            not isinstance(item, dict)
                            or not isinstance(item.get("id"), int)
                            or not isinstance(item.get("episode_number"), int)
                            or item.get("season_number") != season
                            for item in episodes
                        ):
                            raise ValueError("返回了无效分集数据")
                        for item in episodes:
                            episode_number = item["episode_number"]
                            seasons[season][episode_number] = fill_missing(
                                seasons[season].get(episode_number, {}),
                                item,
                            )
                    except ValueError as exc:
                        name = self.scrapers.definition(scraper_id).name
                        warnings.append(f"{name} 第 {season} 季分集刮削失败：{exc}")
            episode = seasons[season].get(number)
            if not episode:
                warnings.append(f"所选刮削器缺少 S{season:02}E{number:02}，跳过：{video.name}")
                continue
            target = str(video.with_suffix(".nfo"))
            add(
                target,
                nfo(episode, "tv", episode.get("provider_ids", {}).get("tmdb"), episode=True),
                source=str(video),
            )
        return artifacts, warnings

    def _videos(self, root: int, relative: str) -> list[Path]:
        videos: list[Path] = []
        for entry in self.files.list(root, relative)["entries"]:
            if entry["blocked"]:
                raise ValueError(f"影视目录含链接，禁止操作：{entry['name']}")
            if (
                not entry["directory"]
                and Path(entry["name"]).suffix.lower() in VIDEO_EXTENSIONS
                and not EXTRA_SUFFIX.search(Path(entry["name"]).stem)
            ):
                videos.append(Path(entry["path"]))
        return videos

    def apply(self, request: MetadataApply) -> dict[str, Any]:
        with self.lock:
            with self.files.lock:
                preview = self.previews.get(request.preview_id)
                if (
                    preview is None
                    or not preview.ready
                    or time.monotonic() - preview.created >= 1800
                    or preview.actor != operation_actor.get()
                ):
                    raise ValueError("预览不存在、已过期或不属于当前用户，请重新预览")
                if not preview.artifacts:
                    raise ValueError("预览中没有可写入的文件")
                selected = self.scrapers.selection(preview.request.scrapers)
            with self._clients(selected, request.api_key, preview.request.language) as clients:
                try:
                    return self.store.execute(
                        "metadata.apply",
                        {"preview_id": request.preview_id, **preview.request.model_dump()},
                        lambda: self._apply(request.preview_id, preview, clients),
                    )
                finally:
                    # The metadata lock prevents replay while I/O is in progress.
                    with self.files.lock:
                        self.previews.pop(request.preview_id, None)

    def _validate_artifact(self, root: int, item: Artifact) -> None:
        path = self.files.resolve(root, item.path, must_exist=False)
        if not path.parent.is_dir() or signature(path) != item.before:
            raise ValueError(f"预览后文件或目录发生变化，请重新预览：{item.path}")
        if item.source and signature(self.files.resolve(root, item.source)) != item.source_before:
            raise ValueError(f"预览后视频发生变化，请重新预览：{item.source}")

    def _apply(
        self, preview_id: str, preview: Preview, clients: dict[str, ScraperClient]
    ) -> dict[str, Any]:
        root = preview.request.root
        with self.files.lock:
            self._check_preview(preview_id, preview)
            for item in preview.artifacts:
                self._validate_artifact(root, item)
        results = []
        for item in preview.artifacts:
            result: dict[str, Any] = {"path": item.path, "status": "failed"}
            results.append(result)
            try:
                with self.files.lock:
                    self._check_preview(preview_id, preview)
                content = (
                    clients[item.image_scraper].image(item.image) if item.image else item.content
                )
                if content is None:
                    raise ValueError("缺少待写入内容")
                with self.files.lock:
                    self._check_preview(preview_id, preview)
                    self._validate_artifact(root, item)
                    self._write(root, item, content, result)
                result["status"] = "success"
            except (ValueError, OSError) as exc:
                result["error"] = str(exc)
                logger.warning("刮削写入失败 path=%s: %s", item.path, exc)
        success = sum(item["status"] == "success" for item in results)
        issues = any(row["status"] != "matched" or row.get("warnings") for row in preview.rows)
        return {
            "files": results,
            "rows": preview.rows,
            "outcome": ("success" if success == len(results) and not issues else "partial")
            if success
            else "failed",
        }

    def _write(self, root: int, item: Artifact, content: bytes, result: dict[str, Any]) -> None:
        target = self.files.resolve(root, item.path, must_exist=False)
        if signature(target) != item.before:
            raise ValueError("目标在预览后发生变化，请重新预览")
        descriptor, name = tempfile.mkstemp(prefix=".mediaforge-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if item.before is not None:
                backup = target.with_name(target.name + ".bak." + uuid4().hex)
                with target.open("rb") as source:
                    destination = backup.open("xb")
                    try:
                        with destination:
                            shutil.copyfileobj(source, destination)
                            destination.flush()
                            os.fsync(destination.fileno())
                    except OSError:
                        backup.unlink()
                        raise
                result["backup"] = str(backup.relative_to(self.files.roots[root]))
                target = self.files.resolve(root, item.path)
                if signature(target) != item.before:
                    raise ValueError("备份期间目标发生变化，已取消覆盖")
                os.replace(temporary, target)
            else:
                # Reuse the existing exclusive, cross-platform no-overwrite move.
                self.files.resolve(root, item.path, must_exist=False)
                self.files._move(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
