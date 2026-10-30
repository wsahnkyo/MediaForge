import pytest

from mediaforge.database import RenameRule, operation_actor
from mediaforge.files import FileService
from mediaforge.renaming import (
    RenameApplyRequest,
    RenamePreviewRequest,
    RenameReviewRequest,
    RenameService,
    RuleInput,
    RulesDelete,
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


def test_cancellation_finishes_whole_group_and_does_not_checkpoint_directory(renaming, monkeypatch):
    for extension in ("mkv", "srt", "nfo"):
        add(renaming, f"Show.EP01.{extension}")
    second = add(renaming, "Show.EP02.mkv")

    def progress(completed, total, current):
        if completed:
            assert all(
                (renaming.files.roots[0] / f"01.{ext}").exists() for ext in ("mkv", "srt", "nfo")
            )
            return False
        return True

    monkeypatch.setattr("mediaforge.renaming.task_progress", progress)
    result = apply(renaming, preview(renaming))
    assert result["outcome"] == "cancelled" and result["renamed_count"] == 1
    assert second.exists()
    assert not preview(renaming)["unchanged"]


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("Example.2020.S02E03.1080p.x264.mkv", "03.mkv"),
        ("Example.12.S02E03.mkv", "03.mkv"),
        ("Example - 第04集 - 2160p.mp4", "04.mp4"),
        ("Example.EP05.720p.mp4", "05.mp4"),
        ("Example.EP06.1080p.x265.mkv", "06.mkv"),
        ("Show.E7.mkv", "07.mkv"),
        ("Show.EP08.MP4", "08.MP4"),
        ("Show.EP09.avi", "09.avi"),
        ("Show.EP10.mov", "10.mov"),
        ("Show.EP11.m2ts", "11.m2ts"),
        ("Show.EP12.ts", "12.ts"),
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
    add(renaming, "Example.EP01.mkv")
    child = add(renaming, "child/02.mkv")
    result = apply(renaming, preview(renaming))
    assert result["renamed_count"] == 1 and child.exists()
    restarted = RenameService(renaming.files, renaming.store)
    assert preview(restarted)["unchanged"] is True
    child.write_bytes(b"new child content")
    assert preview(restarted)["unchanged"] is True
    added = add(restarted, "Example.EP03.mkv")
    report = preview(restarted)
    assert report["ready_count"] == 1
    assert added.exists()
    apply(restarted, report)
    assert preview(restarted)["unchanged"] is True
    (restarted.files.roots[0] / "03.mkv").write_bytes(b"modified")
    assert preview(restarted)["unchanged"] is False
    assert preview(restarted, force=True)["unchanged"] is False


def test_editing_preview_revalidates_conflicts_and_old_token_is_invalid(renaming):
    add(renaming, "Example.EP01.mkv")
    add(renaming, "02.mkv")
    original = preview(renaming)
    revised = renaming.review(
        RenameReviewRequest(
            preview_id=original["preview_id"],
            edits=[{"source": "Example.EP01.mkv", "new_name": "02.mkv"}],
        )
    )
    assert revised["ready_count"] == 0
    assert (
        next(row for row in revised["rows"] if row["name"] == "Example.EP01.mkv")["status"]
        == "conflict"
    )
    with pytest.raises(ValueError, match="预览"):
        apply(renaming, original)
    with pytest.raises(ValueError, match="没有通过"):
        apply(renaming, revised)
    validated = renaming.review(
        RenameReviewRequest(
            preview_id=revised["preview_id"],
            edits=[{"source": "Example.EP01.mkv", "new_name": "03.mkv"}],
        )
    )
    apply(renaming, validated)
    assert (renaming.files.roots[0] / "03.mkv").exists()


def test_duplicate_extraction_year_resolution_multi_episode_and_existing_targets(renaming):
    names = [
        "A.EP01.mkv",
        "B.EP01.mkv",
        "2020.1080p.x264.mkv",
        "A.02.03.mkv",
        "A.S01E04-E05.mkv",
        "A.EP07-08.mkv",
        "A.EP06.mkv",
        "06.mkv",
    ]
    for name in names:
        add(renaming, name)
    report = preview(renaming)
    assert report["ready_count"] == 0
    assert sum(row["status"] == "conflict" for row in report["rows"]) == 3
    assert all((renaming.files.roots[0] / name).exists() for name in names)


def test_rules_database_persistence_invalidation_and_numeric_template(renaming):
    add(renaming, "Example.EP01.mkv")
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


def test_fresh_install_general_rule_works_without_database_seed(renaming):
    with renaming.store.sessions() as session:
        assert session.get(RenameRule, "general-episode") is None
    assert [rule.id for rule in renaming.rules()] == ["general-episode"]
    add(renaming, "ExampleEP0007.mkv")
    report = preview(renaming)
    row = report["rows"][0]
    assert row["rule"] == "通用规则" and row["new_name"] == "07.mkv"
    restarted = RenameService(renaming.files, renaming.store)
    assert preview(restarted)["rows"][0]["new_name"] == "07.mkv"
    apply(restarted, preview(restarted))
    assert (renaming.files.roots[0] / "07.mkv").read_bytes() == b"video"


def test_default_does_not_fall_back_to_removed_numeric_rules(renaming):
    for name in ("7.mkv", "Show.06.1080p.mkv"):
        add(renaming, name)
    report = preview(renaming)
    assert report["ready_count"] == 0
    assert all(row["status"] == "skipped" for row in report["rows"])


def test_general_default_does_not_change_existing_saved_rules(renaming):
    custom = RuleInput(id="custom", name="Custom", pattern=r"EP(?P<episode>\d+)")
    renaming.save_rules(RulesUpdate(rules=[custom]))
    restarted = RenameService(renaming.files, renaming.store)
    assert restarted.rules() == [custom]
    add(renaming, "ExampleEP0007.mkv")
    assert preview(restarted)["rows"][0]["rule"] == "Custom"


@pytest.mark.parametrize(
    "old_template", ["{episode:03d}", "Episode-{episode}", "S{season:02d}E{episode:02d}"]
)
def test_existing_custom_templates_use_fixed_two_digit_names_without_migration(
    renaming, old_template
):
    with renaming.store.sessions() as session:
        session.add(
            RenameRule(
                id="legacy",
                name="Legacy",
                pattern=r"EP(?P<episode>\d+)",
                template=old_template,
                enabled=True,
                priority=0,
            )
        )
        session.commit()
    add(renaming, "Show.EP1.mkv")
    add(renaming, "Show.EP12.mp4")
    report = preview(renaming)
    assert {row["new_name"] for row in report["rows"]} == {"01.mkv", "12.mp4"}
    apply(renaming, report)
    assert (renaming.files.roots[0] / "01.mkv").exists()
    assert (renaming.files.roots[0] / "12.mp4").exists()
    with renaming.store.sessions() as session:
        assert session.get(RenameRule, "legacy").template == old_template
    renaming.save_rules(RulesUpdate(rules=renaming.rules()))
    with renaming.store.sessions() as session:
        assert session.get(RenameRule, "legacy").template == "{episode:02d}"


def test_granular_rules_persist_without_replacing_other_saved_edits(renaming):
    renaming.add_rule(RuleInput(id="extra", name="Extra", pattern=r"EP(?P<episode>\d+)"))
    rules = renaming.rules()
    rules[0].name = "Saved custom name"
    for index, rule in enumerate(rules):
        rule.priority = 10 + index
    renaming.save_rules(RulesUpdate(rules=rules))
    add(renaming, "Example.EP01.mkv")
    old_preview = preview(renaming)
    created = RuleInput(id="added", name="Added", pattern=r"(?P<episode>\d+)")
    result = renaming.add_rule(created)
    assert result["rules"][-1]["id"] == "added"
    assert result["rules"][0]["name"] == "Saved custom name"
    assert old_preview["preview_id"] not in renaming.previews
    with pytest.raises(ValueError, match="重复"):
        renaming.add_rule(created)
    removed = [rules[1].id, created.id]
    result = renaming.delete_rules(RulesDelete(ids=removed))
    expected_ids = [rule.id for rule in rules if rule.id not in removed]
    assert [rule["id"] for rule in result["rules"]] == expected_ids
    restarted = RenameService(renaming.files, renaming.store)
    assert [rule.model_dump() for rule in restarted.rules()] == result["rules"]
    with pytest.raises(ValueError, match="不存在"):
        restarted.delete_rules(RulesDelete(ids=removed))
    assert len(restarted.rules()) == len(expected_ids)


@pytest.mark.parametrize("kind", ["duplicate", "missing", "all"])
def test_invalid_rule_bulk_deletion_is_atomic(renaming, kind):
    rules = renaming.rules()
    renaming.save_rules(RulesUpdate(rules=rules))
    ids = {
        "duplicate": [rules[0].id, rules[0].id],
        "missing": [rules[0].id, "missing"],
        "all": [rule.id for rule in rules],
    }[kind]
    with pytest.raises(ValueError):
        renaming.delete_rules(RulesDelete(ids=ids))
    assert renaming.rules() == rules


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
    assert len(renaming.rules()) == len(rules)


def test_nfo_and_language_subtitles_follow_video_in_same_preview(renaming):
    for name in ("Show.EP01.mkv", "Show.EP01.nfo", "Show.EP01.zh-CN.srt"):
        add(renaming, name, name.encode())
    report = preview(renaming)
    assert report["file_count"] == 3
    apply(renaming, report)
    for suffix in (".mkv", ".nfo", ".zh-CN.srt"):
        assert (renaming.files.roots[0] / ("01" + suffix)).exists()


def test_companions_belong_to_longest_video_stem(renaming):
    renaming.save_rules(
        RulesUpdate(rules=[RuleInput(name="last number", pattern=r"(?P<episode>\d{2})$")])
    )
    for name in (
        "Show.01.mkv",
        "Show.01.nfo",
        "Show.01.02.mkv",
        "Show.01.02.nfo",
        "Show.01.02.zh-CN.srt",
    ):
        add(renaming, name, name.encode())
    report = preview(renaming)
    assert report["ready_count"] == 2
    assert report["file_count"] == 5
    apply(renaming, report)
    root = renaming.files.roots[0]
    assert (root / "01.nfo").read_bytes() == b"Show.01.nfo"
    assert (root / "02.nfo").read_bytes() == b"Show.01.02.nfo"
    assert (root / "02.zh-CN.srt").read_bytes() == b"Show.01.02.zh-CN.srt"


def test_preview_indexes_directory_once_instead_of_once_per_video(renaming, monkeypatch):
    for index in range(1, 101):
        add(renaming, f"Show.EP{index:03}.mkv")
    visits = 0

    class CountedEntries(list):
        def __iter__(self):
            nonlocal visits
            for item in super().__iter__():
                visits += 1
                yield item

    original = renaming.files.list

    def listing(*args):
        result = original(*args)
        result["entries"] = CountedEntries(result["entries"])
        return result

    monkeypatch.setattr(renaming.files, "list", listing)
    report = preview(renaming)
    assert report["ready_count"] == 100
    assert visits <= 500


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
    add(renaming, "Show.EP01.mkv")
    add(renaming, "Show.EP01.nfo")
    report = preview(renaming)
    move = renaming.files._move

    def fail_nfo(source, target):
        if source.suffix == ".nfo":
            raise PermissionError("locked subtitle")
        return move(source, target)

    monkeypatch.setattr(renaming.files, "_move", fail_nfo)
    result = apply(renaming, report)
    assert result["outcome"] == "partial"
    assert (renaming.files.roots[0] / "Show.EP01.mkv").exists()
    assert (renaming.files.roots[0] / "Show.EP01.nfo").exists()
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
