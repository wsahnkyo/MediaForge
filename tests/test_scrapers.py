import xml.etree.ElementTree as ET

import pytest
from pydantic import SecretStr

from mediaforge.database import OperationFailure
from mediaforge.files import FileService
from mediaforge.metadata import (
    MetadataApply,
    MetadataMappingInput,
    MetadataPreview,
    MetadataSearch,
    MetadataService,
    validate_tmdb_key,
)
from mediaforge.scrapers import ScraperDefinition, ScraperSettingsUpdate, fill_missing

KEY = "a" * 32
REPLACEMENT = "b" * 32


@pytest.fixture
def service(settings, store):
    settings.tmdb_api_key = None
    settings.media_roots[0].mkdir()
    return MetadataService(settings, FileService(settings.media_roots, store), store)


def save(service, **fields):
    return service.save_scrapers(ScraperSettingsUpdate(scrapers=[{"id": "tmdb", **fields}]))


def test_credential_lifecycle_persistence_and_audit_redaction(service, caplog):
    save(service, api_key=KEY, enabled=False, priority=4)
    recreated = MetadataService(service.settings, service.files, service.store)
    assert recreated.scrapers.credential("tmdb").get_secret_value() == KEY
    assert recreated.scrapers.configurations()[0]["enabled"] is False
    assert recreated.scrapers.configurations()[0]["priority"] == 4
    assert recreated.scrapers.selection(["tmdb"]) == ["tmdb"]
    with pytest.raises(ValueError, match="至少选择"):
        recreated.scrapers.selection()
    save(recreated)
    assert recreated.scrapers.credential("tmdb").get_secret_value() == KEY
    save(recreated, api_key=REPLACEMENT)
    assert recreated.scrapers.credential("tmdb").get_secret_value() == REPLACEMENT
    recreated.settings.tmdb_api_key = SecretStr(KEY)
    response = save(recreated, clear_api_key=True)
    assert recreated.scrapers.credential("tmdb") is None
    assert response["scrapers"][0]["configured"] is False
    exposed = str(response) + str(service.store.recent()) + caplog.text
    assert KEY not in exposed and REPLACEMENT not in exposed


def test_environment_fallback_is_imported_only_before_database_override(service):
    service.settings.tmdb_api_key = SecretStr(KEY)
    assert service.scrapers.configurations()[0]["key_source"] == "environment"
    assert service.scrapers.credential("tmdb").get_secret_value() == KEY
    save(service)
    service.settings.tmdb_api_key = SecretStr(REPLACEMENT)
    assert service.scrapers.configurations()[0]["key_source"] == "database"
    assert service.scrapers.credential("tmdb").get_secret_value() == KEY
    save(service, clear_api_key=True)
    assert service.scrapers.credential("tmdb") is None


@pytest.mark.parametrize(
    "updates",
    [
        [{"id": "tmdb", "api_key": "invalid private key"}],
        [{"id": "tmdb", "api_key": KEY, "clear_api_key": True}],
        [{"id": "tmdb", "api_key": REPLACEMENT}, {"id": "unknown"}],
        [{"id": "tmdb"}, {"id": "tmdb"}],
    ],
)
def test_invalid_configurations_are_atomic_and_do_not_echo_keys(service, updates):
    save(service, api_key=KEY)
    with pytest.raises(ValueError) as error:
        service.save_scrapers(ScraperSettingsUpdate(scrapers=updates))
    assert service.scrapers.credential("tmdb").get_secret_value() == KEY
    assert "invalid private key" not in str(error.value)
    assert KEY not in str(error.value) and REPLACEMENT not in str(error.value)


@pytest.mark.parametrize("selection", [[], ["tmdb", "tmdb"], ["unknown"]])
def test_invalid_source_selection_is_rejected(service, selection):
    with pytest.raises(ValueError):
        service.search(MetadataSearch(query="Example", scrapers=selection))


def test_missing_credentials_block_search_and_preview(service):
    (service.files.roots[0] / "Example (2020)").mkdir()
    for action in (
        lambda: service.search(MetadataSearch(query="Example")),
        lambda: service.preview(MetadataPreview(root=0, path="Example (2020)")),
    ):
        with pytest.raises(ValueError, match="尚未配置 TMDB Key"):
            action()
    assert not list(service.files.roots[0].rglob("*.nfo"))


def test_fill_missing_preserves_values_and_does_not_mutate_sources():
    preferred = {"zero": 0, "flag": False, "nested": {"keep": "first"}, "list": [1]}
    supplement = {
        "zero": 8,
        "flag": True,
        "nested": {"keep": "second", "new": ["added"]},
        "list": [2],
    }
    merged = fill_missing(preferred, supplement)
    assert merged == {
        "zero": 0,
        "flag": False,
        "nested": {"keep": "first", "new": ["added"]},
        "list": [1],
    }
    merged["nested"]["new"].append("changed")
    assert supplement["nested"]["new"] == ["added"]
    assert preferred["nested"] == {"keep": "first"}


class StubScraper:
    def __init__(self, source, calls, failures):
        self.source = source
        self.calls = calls
        self.failures = failures
        self.media_id = 42 if source == "tmdb" else 84

    def close(self):
        self.calls.append((self.source, "close"))

    def search(self, media_type, query, page=1, year=None):
        self.calls.append((self.source, "search", query, year))
        if self.source in self.failures:
            raise ValueError("source unavailable")
        return {
            "results": [{"id": self.media_id, "name": "Example", "first_air_date": "2020-01-01"}],
            "total_pages": 1,
        }

    def details(self, media_type, media_id):
        assert media_id == self.media_id
        primary = self.source == "tmdb"
        return {
            "id": media_id,
            "name": "Example",
            "first_air_date": "2020-01-01",
            "overview": "" if primary else "Supplemented plot",
            "vote_average": 0 if primary else 9,
            "poster_path": None if primary else "/poster.jpg",
            "backdrop_path": "/backdrop.jpg",
            "genres": [] if primary else [{"name": "Drama"}],
            "credits": {"cast": [] if primary else [{"name": "Actor"}]},
            "provider_ids": {self.source: str(media_id)},
        }

    def season(self, media_id, season):
        assert media_id == self.media_id
        return {
            "episodes": [
                {
                    "id": media_id + 100,
                    "season_number": season,
                    "episode_number": 1,
                    "name": self.source,
                    "overview": "" if self.source == "tmdb" else "Episode plot",
                    "provider_ids": {self.source: str(media_id + 100)},
                }
            ]
        }

    def image(self, path):
        self.calls.append((self.source, "image", path))
        return b"\xff\xd8\xff" + self.source.encode()


@pytest.fixture
def composed(service):
    calls, failures = [], set()
    for source in ("tmdb", "backup"):
        service.scrapers.registry[source] = ScraperDefinition(
            source,
            source,
            lambda key, language, source=source: StubScraper(source, calls, failures),
            validate_tmdb_key,
        )
    service.save_scrapers(
        ScraperSettingsUpdate(
            scrapers=[
                {"id": "tmdb", "api_key": KEY, "priority": 0},
                {"id": "backup", "api_key": REPLACEMENT, "priority": 1},
            ]
        )
    )
    season = service.files.roots[0] / "Example (2020)"
    season.mkdir(parents=True)
    (season / "Show.S01E01.mkv").write_bytes(b"video")
    return service, calls, failures


@pytest.mark.parametrize("order,rating", [(None, "0"), (["backup", "tmdb"], "9")])
def test_composition_priority_episodes_ids_and_artwork_provenance(composed, order, rating):
    service, calls, _ = composed
    plan = service.preview(MetadataPreview(root=0, path="Example (2020)", scrapers=order))
    assert plan["outcome"] == "success"
    assert plan["file_count"] == 4
    assert not any(call[1] == "image" for call in calls)
    assert service.apply(MetadataApply(preview_id=plan["preview_id"]))["outcome"] == "success"
    folder = service.files.roots[0] / "Example (2020)"
    show = ET.parse(folder / "tvshow.nfo").getroot()
    assert show.findtext("rating") == rating
    assert show.findtext("plot") == "Supplemented plot"
    assert show.findtext("genre") == "Drama"
    assert show.findtext("actor/name") == "Actor"
    assert show.findtext("uniqueid[@type='tmdb']") == "42"
    assert show.findtext("uniqueid[@type='backup']") == "84"
    episode = ET.parse(folder / "Show.S01E01.nfo").getroot()
    assert episode.findtext("title") == (order or ["tmdb"])[0]
    assert episode.findtext("plot") == "Episode plot"
    assert episode.findtext("uniqueid[@type='tmdb']") == "142"
    assert (folder / "poster.jpg").read_bytes() == b"\xff\xd8\xffbackup"
    assert (folder / "fanart.jpg").read_bytes() == b"\xff\xd8\xff" + (order or ["tmdb"])[0].encode()


def test_manual_tmdb_identity_anchors_higher_priority_supplement(composed):
    service, calls, _ = composed
    folder = service.files.roots[0] / "Example (2020)"
    alias = folder.with_name("Alias (1999)")
    folder.rename(alias)
    service.save_mapping(
        MetadataMappingInput(
            root=0,
            path=alias.name,
            media_type="tv",
            tmdb_id=42,
        )
    )
    plan = service.preview(MetadataPreview(root=0, path=alias.name, scrapers=["backup", "tmdb"]))
    assert plan["rows"][0]["match_source"] == "manual"
    assert ("backup", "search", "Example", "2020") in calls
    assert not any(call[:2] == ("tmdb", "search") for call in calls)


def test_partial_provider_failure_is_visible_and_all_failures_are_explicit(composed):
    service, _, failures = composed
    failures.add("backup")
    result = service.search(MetadataSearch(query="Example"))
    assert result["outcome"] == "partial"
    assert "source unavailable" in result["warnings"][0]
    plan = service.preview(MetadataPreview(root=0, path="Example (2020)"))
    assert plan["outcome"] == "partial"
    assert any("source unavailable" in warning for warning in plan["rows"][0]["warnings"])
    failures.add("tmdb")
    with pytest.raises(OperationFailure, match="source unavailable"):
        service.search(MetadataSearch(query="Example"))
    assert service.preview(MetadataPreview(root=0, path="Example (2020)"))["outcome"] == "failed"


def test_saving_configuration_invalidates_existing_previews(composed):
    service, _, _ = composed
    plan = service.preview(MetadataPreview(root=0, path="Example (2020)"))
    save(service, api_key=REPLACEMENT)
    with pytest.raises(ValueError, match="预览"):
        service.apply(MetadataApply(preview_id=plan["preview_id"]))
