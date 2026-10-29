from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from mediaforge.config import Settings
from mediaforge.database import OperationStore, ScraperSetting


class ScraperClient(Protocol):
    def search(
        self, media_type: str, query: str, page: int = 1, year: str | None = None
    ) -> dict[str, Any]: ...

    def details(self, media_type: str, media_id: int) -> dict[str, Any]: ...

    def season(self, media_id: int, season: int) -> dict[str, Any]: ...

    def image(self, path: str) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ScraperDefinition:
    id: str
    name: str
    factory: Callable[[str, str], ScraperClient]
    validate_key: Callable[[str], None]


class ScraperUpdate(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=1000)
    api_key: SecretStr | None = Field(default=None, exclude=True)
    clear_api_key: bool = False


class ScraperSettingsUpdate(BaseModel):
    scrapers: list[ScraperUpdate] = Field(min_length=1, max_length=32)


def fill_missing(preferred: dict[str, Any], supplement: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(preferred)
    for key, value in supplement.items():
        existing = result.get(key)
        if (
            existing is None
            or isinstance(existing, str)
            and not existing.strip()
            or existing == []
            or existing == {}
        ):
            result[key] = deepcopy(value)
        elif isinstance(existing, dict) and isinstance(value, dict):
            result[key] = fill_missing(existing, value)
    return result


class ScraperService:
    def __init__(
        self, settings: Settings, store: OperationStore, registry: dict[str, ScraperDefinition]
    ):
        self.settings = settings
        self.store = store
        self.registry = registry

    def definition(self, scraper_id: str) -> ScraperDefinition:
        if scraper_id not in self.registry:
            raise ValueError("所选刮削器尚未接入，请刷新刮削器列表")
        return self.registry[scraper_id]

    def _environment_key(self, scraper_id: str) -> SecretStr | None:
        return self.settings.tmdb_api_key if scraper_id == "tmdb" else None

    def credential(self, scraper_id: str, override: SecretStr | None = None) -> SecretStr | None:
        self.definition(scraper_id)
        if override:
            return override
        with self.store.sessions() as session:
            setting = session.get(ScraperSetting, scraper_id)
            if setting is not None:
                return SecretStr(setting.api_key) if setting.api_key else None
        return self._environment_key(scraper_id)

    def configurations(self) -> list[dict[str, Any]]:
        with self.store.sessions() as session:
            saved = {setting.id: setting for setting in session.scalars(select(ScraperSetting))}
        result = []
        for index, definition in enumerate(self.registry.values()):
            setting = saved.get(definition.id)
            key = setting.api_key if setting else self._environment_key(definition.id)
            result.append(
                {
                    "id": definition.id,
                    "name": definition.name,
                    "enabled": setting.enabled if setting else True,
                    "priority": setting.priority if setting else index,
                    "configured": bool(key),
                    "key_source": ("database" if setting else "environment") if key else "none",
                }
            )
        return sorted(result, key=lambda item: (item["priority"], item["id"]))

    def selection(self, requested: list[str] | None = None) -> list[str]:
        selected = (
            requested
            if requested is not None
            else [item["id"] for item in self.configurations() if item["enabled"]]
        )
        if not selected:
            raise ValueError("请至少选择一个刮削器")
        if len(selected) != len(set(selected)):
            raise ValueError("刮削器不能重复选择")
        for scraper_id in selected:
            self.definition(scraper_id)
        return selected

    def open(
        self, scraper_id: str, language: str, override: SecretStr | None = None
    ) -> ScraperClient:
        definition = self.definition(scraper_id)
        key = self.credential(scraper_id, override)
        if not key:
            raise ValueError(f"尚未配置 {definition.name} Key，请在页面“刮削器配置”中填写并保存")
        return definition.factory(key.get_secret_value(), language)

    def save(self, request: ScraperSettingsUpdate) -> dict[str, Any]:
        ids = [item.id for item in request.scrapers]
        if len(ids) != len(set(ids)):
            raise ValueError("刮削器配置不能重复")
        for item in request.scrapers:
            definition = self.definition(item.id)
            if item.api_key is not None:
                if item.clear_api_key:
                    raise ValueError("不能同时更新和清除 Key")
                definition.validate_key(item.api_key.get_secret_value())

        def apply() -> dict[str, Any]:
            with self.store.sessions() as session:
                for item in request.scrapers:
                    setting = session.get(ScraperSetting, item.id)
                    if setting is None:
                        legacy = self._environment_key(item.id)
                        setting = ScraperSetting(
                            id=item.id,
                            api_key=legacy.get_secret_value() if legacy else None,
                        )
                        session.add(setting)
                    setting.enabled = item.enabled
                    setting.priority = item.priority
                    if item.clear_api_key:
                        setting.api_key = None
                    elif item.api_key is not None:
                        setting.api_key = item.api_key.get_secret_value()
                session.commit()
            return {"scrapers": self.configurations()}

        return self.store.execute(
            "metadata.scrapers.save",
            {
                "scrapers": [
                    {**item.model_dump(), "key_updated": item.api_key is not None}
                    for item in request.scrapers
                ]
            },
            apply,
        )
