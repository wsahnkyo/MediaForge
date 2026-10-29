import pytest

from mediaforge.database import RenameRule, operation_actor
from mediaforge.files import FileService
from mediaforge.renaming import (
    RenameApplyRequest,
    RenamePreviewRequest,
    RenameReviewRequest,
    RenameService,
    RulesUpdate,
)


@pytest.fixture
def renaming(settings, store):
    root = settings.media_roots[0]
    root.mkdir()
    return RenameService(FileService([root], store), store)


def add(renaming, name, content=b"video"):
    path = renaming.files.roots[0] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def preview(renaming, **kwargs):
    return renaming.preview(RenamePreviewRequest(root=0, **kwargs))


def apply(renaming, report):
    return renaming.apply(RenameApplyRequest(preview_id=report["preview_id"]))


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Example.2020.S02E03.1080p.x264.mkv", "03.mkv"),
        ("Example - 第04集 - 2160p.mp4", "04.mp4"),
        ("Example.EP05.720p.mp4", "05.mp4"),
        ("Example.06.1080p.x265.mkv", "06.mkv"),
        ("7.mkv", "07.mkv"),
        ("Show.EP08.MP4", "08.MP4"),
        ("Show.09.avi", "09.avi"),
        ("Show.10.mov", "10.mov"),
        ("Show.11.m2ts", "11.m2ts"),
        ("Show.12.ts", "12.ts"),
    ],
)
def test_priority_extraction_preserves_extension_and_requires_preview(renaming, filename, expected):
    source = add(renaming, filename)
    report = preview(renaming)
    assert report["rows"][0]["new_name"] == expected
    assert source.exists() and not source.with_name(expected).exists()
    result = apply(renaming, report)
    assert result["renamed_count"] == 1
    assert source.with_name(expected).read_bytes() == b"video"
    with pytest.raises(ValueError, match="预览"):
        apply(renaming, report)


def test_only_current_directory_and_persistent_unchanged_checkpoint(renaming):
    add(renaming, "Example.01.mkv")
    child = add(renaming, "child/02.mkv")
    result = apply(renaming, preview(renaming))
    assert result["renamed_count"] == 1 and child.exists()
    restarted = RenameService(renaming.files, renaming.store)
    assert preview(restarted)["unchanged"] is True
    child.write_bytes(b"new child content")
    assert preview(restarted)["unchanged"] is True
    added = add(restarted, "Example.03.mkv")
    report = preview(restarted)
    assert report["ready_count"] == 1
    assert added.exists()
    apply(restarted, report)
    assert preview(restarted)["unchanged"] is True
    (restarted.files.roots[0] / "03.mkv").write_bytes(b"modified")
    assert preview(restarted)["unchanged"] is False
    assert preview(restarted, force=True)["unchanged"] is False


def test_editing_preview_revalidates_conflicts_and_old_token_is_invalid(renaming):
    add(renaming, "Example.01.mkv")
    add(renaming, "02.mkv")
    original = preview(renaming)
    revised = renaming.review(
        RenameReviewRequest(
            preview_id=original["preview_id"],
            edits=[{"source": "Example.01.mkv", "new_name": "02.mkv"}],
        )
    )
    assert revised["ready_count"] == 0
    assert (
        next(row for row in revised["rows"] if row["name"] == "Example.01.mkv")["status"]
        == "conflict"
    )
    with pytest.raises(ValueError, match="预览"):
        apply(renaming, original)
    with pytest.raises(ValueError, match="没有通过"):
        apply(renaming, revised)
    validated = renaming.review(
        RenameReviewRequest(
            preview_id=revised["preview_id"],
            edits=[{"source": "Example.01.mkv", "new_name": "03.mkv"}],
        )
    )
    apply(renaming, validated)
    assert (renaming.files.roots[0] / "03.mkv").exists()


def test_duplicate_extraction_year_resolution_multi_episode_and_existing_targets(renaming):
    names = [
        "A.01.mkv",
        "B.01.mkv",
        "2020.1080p.x264.mkv",
        "A.02.03.mkv",
        "A.S01E04-E05.mkv",
        "A.EP07-08.mkv",
        "A.06.mkv",
        "06.mkv",
    ]
    for name in names:
        add(renaming, name)
    report = preview(renaming)
    assert report["ready_count"] == 0
    assert sum(row["status"] == "conflict" for row in report["rows"]) == 3
    assert all((renaming.files.roots[0] / name).exists() for name in names)


def test_rules_database_persistence_invalidation_and_numeric_template(renaming):
    add(renaming, "Example.01.mkv")
    old = preview(renaming)
    rules = renaming.rules()
    for rule in rules:
        rule.template = "{episode:02d}"
    renaming.save_rules(RulesUpdate(rules=rules))
    with pytest.raises(ValueError, match="预览"):
        apply(renaming, old)
    restarted = RenameService(renaming.files, renaming.store)
    report = preview(restarted)
    assert report["rows"][0]["new_name"] == "01.mkv"
    apply(restarted, report)
    assert preview(restarted)["unchanged"] is True
    rules[0].name = "Updated rule"
    restarted.save_rules(RulesUpdate(rules=rules))
    assert preview(restarted)["unchanged"] is False


@pytest.mark.parametrize("first_pattern", [r"part(?P<episode>\d+)", r"(?P<episode>\d+)"])
def test_first_valid_rule_is_used_after_invalid_or_ambiguous_match(renaming, first_pattern):
    source = add(renaming, "Example.part0.EP07.mkv")
    renaming.save_rules(
        RulesUpdate(
            rules=[
                {"id": "invalid", "name": "First invalid", "priority": 0, "pattern": first_pattern},
                {
                    "id": "valid",
                    "name": "First valid",
                    "priority": 1,
                    "pattern": r"EP(?P<episode>\d+)",
                },
                {
                    "id": "later",
                    "name": "Later valid",
                    "priority": 2,
                    "pattern": r"EP(?P<episode>\d+)",
                    "template": "{episode:03d}",
                },
            ]
        )
    )
    report = preview(renaming)
    row = report["rows"][0]
    assert row["rule"] == "First valid"
    assert row["new_name"] == "07.mkv"
    assert row["notes"]
    assert source.exists()
    apply(renaming, report)
    assert source.with_name("07.mkv").exists()
    assert not source.with_name("007.mkv").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("pattern", "("),
        ("pattern", r"(\d+)"),
        ("template", "../{episode}"),
        ("template", "{episode.__class__}"),
        ("template", "{episode:999999999d}"),
        ("template", "S{season:02d}E{episode:02d}"),
        ("template", "CON"),
    ],
)
def test_invalid_rules_are_rejected_without_replacing_saved_rules(renaming, field, value):
    rules = renaming.rules()
    renaming.save_rules(RulesUpdate(rules=rules))
    invalid = rules[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        renaming.save_rules(RulesUpdate(rules=[invalid]))
    assert len(renaming.rules()) == 3


def test_nfo_and_language_subtitles_follow_video_in_same_preview(renaming):
    for name in ("Show.01.mkv", "Show.01.nfo", "Show.01.zh-CN.srt"):
        add(renaming, name, name.encode())
    report = preview(renaming)
    assert report["file_count"] == 3
    apply(renaming, report)
    for suffix in (".mkv", ".nfo", ".zh-CN.srt"):
        assert (renaming.files.roots[0] / ("01" + suffix)).exists()


def test_legacy_default_template_and_season_capture_only_produce_episode(renaming):
    with renaming.store.sessions() as session:
        session.add(
            RenameRule(
                id="legacy",
                name="Legacy",
                enabled=True,
                priority=0,
                pattern=r"S(?P<season>\d+)E(?P<episode>\d+)",
                template="S{season:02d}E{episode:02d}",
                default_season=9,
            )
        )
        session.commit()
    source = add(renaming, "Season 12/Show.S12345E07.mkv")
    report = preview(renaming, path="Season 12")
    assert report["rows"][0]["new_name"] == "07.mkv"
    assert "season" not in report["rows"][0]
    apply(renaming, report)
    assert source.with_name("07.mkv").exists()


def test_source_change_and_other_actor_cannot_use_preview(renaming):
    path = add(renaming, "01.mkv")
    report = preview(renaming)
    actor = operation_actor.set("other-user")
    try:
        with pytest.raises(ValueError, match="当前用户"):
            apply(renaming, report)
    finally:
        operation_actor.reset(actor)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="文件已变化"):
        apply(renaming, report)
    assert path.exists()


@pytest.mark.parametrize("name", ["../02.mkv", "CON.mkv", "02.mp4"])
def test_manual_preview_edits_cannot_escape_or_change_extension(renaming, name):
    add(renaming, "01.mkv")
    report = preview(renaming)
    revised = renaming.review(
        RenameReviewRequest(
            preview_id=report["preview_id"],
            edits=[{"source": "01.mkv", "new_name": name}],
        )
    )
    assert revised["ready_count"] == 0
    assert revised["rows"][0]["status"] == "conflict"


def test_failed_companion_move_rolls_back_video_and_keeps_retry_possible(renaming, monkeypatch):
    add(renaming, "Show.01.mkv")
    add(renaming, "Show.01.nfo")
    report = preview(renaming)
    move = renaming.files._move

    def fail_nfo(source, target):
        if source.suffix == ".nfo":
            raise PermissionError("locked subtitle")
        return move(source, target)

    monkeypatch.setattr(renaming.files, "_move", fail_nfo)
    result = apply(renaming, report)
    assert result["outcome"] == "partial"
    assert (renaming.files.roots[0] / "Show.01.mkv").exists()
    assert (renaming.files.roots[0] / "Show.01.nfo").exists()
    assert not preview(renaming)["unchanged"]


def test_regex_timeout_is_reported_without_hanging_batch(renaming):
    add(renaming, "1" + "a" * 45 + "b.mkv")
    renaming.save_rules(
        RulesUpdate(
            rules=[
                {
                    "id": "timeout",
                    "name": "Timeout",
                    "pattern": r"(?P<episode>1)(?:a|aa)+$",
                }
            ]
        )
    )
    report = preview(renaming)
    assert report["ready_count"] == 0
    assert "超时" in report["rows"][0]["reason"]
