import errno
import os
import subprocess
from pathlib import Path

import pytest

from mediaforge.files import (
    FileAction,
    FileDeleteBatch,
    FileMigration,
    FileService,
    MediaRootsUpdate,
)


@pytest.fixture
def files(tmp_path, store):
    root = tmp_path / "media"
    root.mkdir()
    (root / "movie.mkv").write_bytes(b"movie")
    (root / "archive").mkdir()
    return FileService([root], store)


def confirmed(files, request):
    preview = files.execute(request.model_copy(update={"dry_run": True}))
    return files.execute(
        request.model_copy(
            update={
                "dry_run": False,
                "preview_id": preview["preview_id"],
            }
        )
    )


@pytest.fixture
def downloads(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "new.mkv").write_bytes(b"new video")
    (folder / "movie.mkv").write_bytes(b"downloaded duplicate")
    (folder / "keep.srt").write_bytes(b"unselected subtitle")
    (folder / "child").mkdir()
    return folder


def migration_request(files, downloads, **changes):
    return FileMigration(
        **{
            "source_folder": str(downloads),
            "paths": ["new.mkv", "movie.mkv"],
            "target_root": 0,
            "roots_version": files.roots_version,
            **changes,
        }
    )


def test_migration_cancellation_keeps_completed_moves_and_remaining_sources(
    files, downloads, settings, monkeypatch
):
    request = migration_request(files, downloads)
    preview = files.migrate(request, settings)
    monkeypatch.setattr(
        "mediaforge.files.task_progress", lambda completed, total, current: completed < 1
    )
    result = files.migrate(
        request.model_copy(update={"dry_run": False, "preview_id": preview["preview_id"]}),
        settings,
    )
    assert result["outcome"] == "cancelled"
    assert result["moved_count"] == 1 and result["cancelled_count"] == 1
    assert result["failed_count"] == 0
    assert not (downloads / "new.mkv").exists()
    assert (downloads / "movie.mkv").exists()
    assert (files.roots[0] / "new.mkv").read_bytes() == b"new video"
    assert (files.roots[0] / "movie.mkv").read_bytes() == b"movie"


def test_migration_from_external_download_folder_skips_conflicts_and_audits(
    files, downloads, settings
):
    listing = files.migration_source(str(downloads), settings)
    assert listing["source_folder"] == str(downloads)
    assert len(listing["entries"]) == 4
    before_roots = files.roots.copy()
    request = migration_request(files, downloads)
    preview = files.migrate(request, settings)
    assert preview["ready_count"] == 1 and preview["skipped_count"] == 1
    assert (downloads / "new.mkv").exists()
    assert not files.store.recent()
    result = files.migrate(
        request.model_copy(
            update={
                "dry_run": False,
                "preview_id": preview["preview_id"],
            }
        ),
        settings,
    )
    assert (result["moved_count"], result["skipped_count"], result["failed_count"]) == (1, 1, 0)
    assert result["outcome"] == "partial"
    assert not (downloads / "new.mkv").exists()
    assert (files.roots[0] / "new.mkv").read_bytes() == b"new video"
    assert (files.roots[0] / "movie.mkv").read_bytes() == b"movie"
    assert (downloads / "movie.mkv").read_bytes() == b"downloaded duplicate"
    assert (downloads / "keep.srt").exists() and (downloads / "child").is_dir()
    assert files.roots == before_roots
    assert files.store.get(result["operation_id"])["kind"] == "file.migrate"
    assert preview["preview_id"] not in files.previews


@pytest.mark.parametrize("kind", ["relative", "protected", "same", "child", "duplicate", "escape"])
def test_migration_rejects_unsafe_sources_and_selections(files, downloads, settings, kind):
    changes = {
        "relative": {"source_folder": "downloads"},
        "protected": {"source_folder": str(settings.hosts_path.parent)},
        "same": {"source_folder": str(files.roots[0]), "paths": ["movie.mkv"]},
        "child": {"paths": ["child"]},
        "duplicate": {"paths": ["new.mkv", "new.mkv"]},
        "escape": {"paths": ["../media/movie.mkv"]},
    }[kind]
    with pytest.raises((ValueError, OSError)):
        files.migrate(migration_request(files, downloads, **changes), settings)
    assert (downloads / "new.mkv").exists()
    assert not (files.roots[0] / "new.mkv").exists()


@pytest.mark.parametrize("change", ["file", "selection", "actor", "expired", "roots"])
def test_migration_requires_unchanged_actor_bound_preview(files, downloads, settings, change):
    request = migration_request(files, downloads)
    preview = files.migrate(request, settings)
    request = request.model_copy(update={"dry_run": False, "preview_id": preview["preview_id"]})
    if change == "file":
        (downloads / "new.mkv").write_bytes(b"still downloading")
    elif change == "selection":
        request = request.model_copy(update={"paths": ["new.mkv"]})
    elif change == "actor":
        files.previews[preview["preview_id"]].actor = "another-admin"
    elif change == "expired":
        files.previews[preview["preview_id"]].created -= 1801
    else:
        files.save_roots(MediaRootsUpdate(roots=[]), settings)
    with pytest.raises(ValueError):
        files.migrate(request, settings)
    assert (downloads / "new.mkv").exists()


def test_migration_new_conflict_is_skipped_and_preview_skips_stay_skipped(
    files, downloads, settings
):
    request = migration_request(files, downloads, paths=["new.mkv", "movie.mkv", "keep.srt"])
    preview = files.migrate(request, settings)
    (files.roots[0] / "new.mkv").write_bytes(b"created after preview")
    (files.roots[0] / "movie.mkv").unlink()
    result = files.migrate(
        request.model_copy(
            update={
                "dry_run": False,
                "preview_id": preview["preview_id"],
            }
        ),
        settings,
    )
    assert result["moved_count"] == 1 and result["skipped_count"] == 2
    assert (files.roots[0] / "new.mkv").read_bytes() == b"created after preview"
    assert (downloads / "new.mkv").exists() and (downloads / "movie.mkv").exists()
    assert (files.roots[0] / "keep.srt").read_bytes() == b"unselected subtitle"


def test_migration_reports_partial_failures_and_never_replays(
    files, downloads, settings, monkeypatch
):
    request = migration_request(files, downloads, paths=["new.mkv", "keep.srt"])
    preview = files.migrate(request, settings)
    move = files._move

    def fail_one(source, target):
        if source.name == "new.mkv":
            raise PermissionError("test file locked")
        move(source, target)

    monkeypatch.setattr(files, "_move", fail_one)
    request = request.model_copy(update={"dry_run": False, "preview_id": preview["preview_id"]})
    result = files.migrate(request, settings)
    assert result["moved_count"] == 1 and result["failed_count"] == 1
    assert result["files"][0]["error"] == "test file locked"
    assert files.store.get(result["operation_id"])["status"] == "partial"
    assert (downloads / "new.mkv").exists()
    (files.roots[0] / "keep.srt").rename(downloads / "keep.srt")
    with pytest.raises(ValueError, match="预览"):
        files.migrate(request, settings)


@pytest.mark.parametrize("changed", [False, True])
def test_cross_volume_migration_preserves_source_if_download_changes(
    files, downloads, settings, monkeypatch, changed
):
    request = migration_request(files, downloads, paths=["new.mkv"])
    preview = files.migrate(request, settings)

    def cross_device(*args):
        raise OSError(errno.EXDEV, "cross-device")

    if os.name == "nt":
        monkeypatch.setattr(Path, "rename", cross_device)
    else:
        monkeypatch.setattr(os, "link", cross_device)
    from mediaforge import files as files_module

    copy = files_module.shutil.copyfileobj

    def copy_and_change(reader, writer, **kwargs):
        copy(reader, writer, **kwargs)
        if changed:
            with (downloads / "new.mkv").open("ab") as source:
                source.write(b"more downloaded bytes")

    monkeypatch.setattr(files_module.shutil, "copyfileobj", copy_and_change)
    result = files.migrate(
        request.model_copy(
            update={
                "dry_run": False,
                "preview_id": preview["preview_id"],
            }
        ),
        settings,
    )
    if changed:
        assert result["outcome"] == "failed"
        assert "复制期间发生变化" in result["files"][0]["error"]
        assert (downloads / "new.mkv").read_bytes() == b"new videomore downloaded bytes"
        assert not (files.roots[0] / "new.mkv").exists()
    else:
        assert result["outcome"] == "success"
        assert not (downloads / "new.mkv").exists()
        assert (files.roots[0] / "new.mkv").read_bytes() == b"new video"


def test_batch_delete_preview_execution_and_audit(files, store):
    root = files.roots[0]
    (root / "second.mkv").write_bytes(b"second")
    request = FileDeleteBatch(
        root=0, paths=["movie.mkv", "second.mkv"], roots_version=files.roots_version
    )
    preview = files.delete_batch(request)
    assert len(preview["files"]) == 2 and preview["permanent"] is True
    assert store.recent() == []
    assert (root / "movie.mkv").exists() and (root / "second.mkv").exists()
    result = files.delete_batch(
        request.model_copy(
            update={
                "dry_run": False,
                "preview_id": preview["preview_id"],
            }
        )
    )
    assert result["deleted_count"] == 2 and result["outcome"] == "success"
    assert not (root / "movie.mkv").exists() and not (root / "second.mkv").exists()
    assert (root / "archive").is_dir()
    record = store.get(result["operation_id"])
    assert record["kind"] == "file.delete.batch" and record["status"] == "success"
    assert preview["preview_id"] not in files.previews


@pytest.mark.parametrize(
    "paths",
    [
        ["movie.mkv", "movie.mkv"],
        ["movie.mkv", "archive"],
        ["movie.mkv", "../outside.mkv"],
        ["movie.mkv", "missing.mkv"],
    ],
)
def test_batch_delete_rejects_invalid_selection_without_touching_files(files, paths):
    request = FileDeleteBatch(root=0, paths=paths, roots_version=files.roots_version)
    with pytest.raises((ValueError, OSError)):
        files.delete_batch(request)
    assert (files.roots[0] / "movie.mkv").read_bytes() == b"movie"
    assert not files.previews


@pytest.mark.parametrize("change", ["file", "selection", "actor", "expired", "missing-token"])
def test_batch_delete_revalidates_entire_confirmation_before_any_deletion(files, change):
    root = files.roots[0]
    (root / "second.mkv").write_bytes(b"second")
    request = FileDeleteBatch(
        root=0, paths=["movie.mkv", "second.mkv"], roots_version=files.roots_version
    )
    preview = files.delete_batch(request)
    request = request.model_copy(update={"dry_run": False, "preview_id": preview["preview_id"]})
    if change == "file":
        (root / "second.mkv").write_bytes(b"changed")
    elif change == "selection":
        request = request.model_copy(update={"paths": ["movie.mkv"]})
    elif change == "actor":
        files.previews[preview["preview_id"]].actor = "other-admin"
    elif change == "expired":
        files.previews[preview["preview_id"]].created -= 1801
    else:
        request = request.model_copy(update={"preview_id": None})
    with pytest.raises(ValueError, match="预览"):
        files.delete_batch(request)
    assert (root / "movie.mkv").exists() and (root / "second.mkv").exists()


@pytest.mark.parametrize("fail_all", [False, True])
def test_batch_delete_reports_os_errors_and_consumes_token(files, store, monkeypatch, fail_all):
    root = files.roots[0]
    (root / "second.mkv").write_bytes(b"second")
    request = FileDeleteBatch(
        root=0, paths=["movie.mkv", "second.mkv"], roots_version=files.roots_version
    )
    preview = files.delete_batch(request)
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if fail_all or path.name == "movie.mkv":
            raise PermissionError("test file locked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    result = files.delete_batch(
        request.model_copy(
            update={
                "dry_run": False,
                "preview_id": preview["preview_id"],
            }
        )
    )
    assert result["outcome"] == ("failed" if fail_all else "partial")
    assert result["deleted_count"] == (0 if fail_all else 1)
    assert result["files"][0]["error"] == "test file locked"
    assert store.get(result["operation_id"])["status"] == result["outcome"]
    assert preview["preview_id"] not in files.previews
    assert (root / "movie.mkv").exists()


def test_batch_delete_rechecks_each_file_after_operation_starts(files, monkeypatch):
    root = files.roots[0]
    second = root / "second.mkv"
    second.write_bytes(b"second")
    request = FileDeleteBatch(
        root=0, paths=["movie.mkv", "second.mkv"], roots_version=files.roots_version
    )
    preview = files.delete_batch(request)
    unlink = Path.unlink

    def change_next(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if path.name == "movie.mkv":
            second.write_bytes(b"changed after confirmation")

    monkeypatch.setattr(Path, "unlink", change_next)
    result = files.delete_batch(
        request.model_copy(
            update={
                "dry_run": False,
                "preview_id": preview["preview_id"],
            }
        )
    )
    assert result["deleted_count"] == 1 and result["outcome"] == "partial"
    assert "发生变化" in result["files"][1]["error"]
    assert second.read_bytes() == b"changed after confirmation"


def test_stale_root_update_and_batch_delete_do_not_change_new_configuration(
    files, settings, tmp_path
):
    previous = files.roots_version
    preview = files.delete_batch(
        FileDeleteBatch(root=0, paths=["movie.mkv"], roots_version=previous)
    )
    new_root = tmp_path / "replacement-library"
    new_root.mkdir()
    (new_root / "movie.mkv").write_bytes(b"replacement")
    files.save_roots(MediaRootsUpdate(roots=[str(new_root)], roots_version=previous), settings)
    with pytest.raises(ValueError, match="配置已变化"):
        files.save_roots(MediaRootsUpdate(roots=[], roots_version=previous), settings)
    with pytest.raises(ValueError, match="配置已变化"):
        files.delete_batch(
            FileDeleteBatch(
                root=0,
                paths=["movie.mkv"],
                roots_version=previous,
                dry_run=False,
                preview_id=preview["preview_id"],
            )
        )
    assert files.roots == [new_root]
    assert (new_root / "movie.mkv").read_bytes() == b"replacement"


def test_media_folder_picker_lists_drives_and_only_direct_subdirectories(
    files, settings, tmp_path, monkeypatch
):
    monkeypatch.setattr("mediaforge.files.media_drives", lambda: [tmp_path])
    drives = files.browse_media_folders(None, settings)
    assert drives["path"] is None
    assert drives["entries"] == [{"name": str(tmp_path), "path": str(tmp_path), "blocked": False}]
    listing = files.browse_media_folders(str(files.roots[0]), settings)
    assert listing["selectable"] is True
    assert [item["name"] for item in listing["entries"]] == ["archive"]
    assert listing["parent"] == str(tmp_path)
    assert listing["path"] == str(files.roots[0].resolve())
    parent = files.browse_media_folders(str(tmp_path), settings)
    assert parent["selectable"] is False
    assert "hosts" in parent["reason"]
    assert any(item["name"] == "media" for item in parent["entries"])
    with pytest.raises(ValueError, match="绝对路径"):
        files.browse_media_folders("relative", settings)
    with pytest.raises(ValueError, match="不可用"):
        files.browse_media_folders(str(tmp_path / "missing"), settings)


def test_media_folder_picker_does_not_hide_directory_access_errors(files, settings, monkeypatch):
    def denied(path):
        raise PermissionError("test directory denied")

    monkeypatch.setattr("mediaforge.files.os.scandir", denied)
    with pytest.raises(PermissionError, match="denied"):
        files.browse_media_folders(str(files.roots[0]), settings)


def test_database_media_roots_override_environment_and_survive_empty_configuration(
    files, settings, tmp_path
):
    new_root = tmp_path / "new-library"
    new_root.mkdir()
    (new_root / "show").mkdir()
    files.save_roots(MediaRootsUpdate(roots=[str(new_root)]), settings)
    restored = FileService(settings.media_roots, files.store)
    restored.load_roots(settings)
    assert restored.list(0, "")["entries"][0]["name"] == "show"
    restored.save_roots(MediaRootsUpdate(roots=[]), settings)
    reloaded = FileService(settings.media_roots, files.store)
    reloaded.load_roots(settings)
    with pytest.raises(ValueError, match="媒体目录不存在"):
        reloaded.list(0, "")
    assert (new_root / "show").is_dir()


@pytest.mark.parametrize("kind", ["relative", "missing", "duplicate", "file", "hosts", "logs"])
def test_invalid_media_root_update_keeps_previous_whitelist(files, settings, tmp_path, kind):
    candidates = {
        "relative": ["relative-library"],
        "missing": [str(tmp_path / "missing")],
        "duplicate": [str(files.roots[0]), str(files.roots[0])],
        "file": [str(files.roots[0] / "movie.mkv")],
        "hosts": [str(settings.hosts_path.parent)],
        "logs": [str(settings.log_path.parent)],
    }
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    before = files.roots.copy()
    with pytest.raises(ValueError):
        files.save_roots(MediaRootsUpdate(roots=candidates[kind]), settings)
    assert files.roots == before
    recreated = FileService(before, files.store)
    recreated.load_roots(settings)
    assert recreated.roots == before


def test_root_reconfiguration_rejects_stale_file_actions_and_preserves_media(
    files, settings, tmp_path
):
    old_root = files.roots[0]
    new_root = tmp_path / "new-library"
    new_root.mkdir()
    (new_root / "movie.mkv").write_bytes(b"other movie")
    request = FileAction(
        action="rename",
        root=0,
        path="movie.mkv",
        destination="film.mkv",
        roots_version=files.roots_version,
    )
    preview = files.execute(request)
    files.save_roots(MediaRootsUpdate(roots=[str(new_root)]), settings)
    with pytest.raises(ValueError, match="配置已变化"):
        files.execute(
            request.model_copy(update={"dry_run": False, "preview_id": preview["preview_id"]})
        )
    with pytest.raises(ValueError, match="配置已变化"):
        files.list(0, "", request.roots_version)
    assert (new_root / "movie.mkv").read_bytes() == b"other movie"
    assert (old_root / "movie.mkv").read_bytes() == b"movie"


def test_preview_rename_move_delete_and_audit(files, store):
    request = FileAction(action="rename", root=0, path="movie.mkv", destination="film.mkv")
    preview = files.execute(request)
    assert preview["dry_run"] is True
    assert (files.roots[0] / "movie.mkv").exists()
    assert store.recent() == []
    files.execute(
        request.model_copy(update={"dry_run": False, "preview_id": preview["preview_id"]})
    )
    assert not (files.roots[0] / "movie.mkv").exists()
    assert (files.roots[0] / "film.mkv").read_bytes() == b"movie"
    confirmed(
        files,
        FileAction(
            action="move",
            root=0,
            path="film.mkv",
            destination_root=0,
            destination="archive/film.mkv",
            dry_run=False,
        ),
    )
    with pytest.raises(ValueError, match="确认"):
        files.execute(FileAction(action="delete", root=0, path="archive/film.mkv", dry_run=False))
    confirmed(
        files,
        FileAction(
            action="delete",
            root=0,
            path="archive/film.mkv",
            dry_run=False,
            confirm_delete="film.mkv",
        ),
    )
    assert not (files.roots[0] / "archive" / "film.mkv").exists()
    assert len(store.recent()) == 3
    assert all(record["status"] == "success" for record in store.recent())


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        r"C:\Windows\hosts",
        r"\\server\share\a",
        "movie.mkv:secret",
        "archive/../../a",
        "CON",
        "archive/NUL.txt",
        "archive/film.mkv.",
        "archive//a",
    ],
)
def test_reject_unsafe_paths(files, path):
    with pytest.raises(ValueError):
        files.resolve(0, path, must_exist=False)


def test_never_overwrite_or_delete_directory(files):
    (files.roots[0] / "exists.mkv").write_bytes(b"original")
    with pytest.raises(FileExistsError):
        confirmed(
            files,
            FileAction(
                action="rename", root=0, path="movie.mkv", destination="exists.mkv", dry_run=False
            ),
        )
    assert (files.roots[0] / "exists.mkv").read_bytes() == b"original"
    with pytest.raises(ValueError, match="普通文件"):
        files.execute(FileAction(action="delete", root=0, path="archive", dry_run=False))


def test_cross_volume_move_uses_exclusive_copy(files, monkeypatch):
    def cross_device(*args):
        raise OSError(errno.EXDEV, "cross-device")

    if os.name == "nt":
        monkeypatch.setattr(Path, "rename", cross_device)
    else:
        monkeypatch.setattr(os, "link", cross_device)
    confirmed(
        files,
        FileAction(
            action="move",
            root=0,
            path="movie.mkv",
            destination_root=0,
            destination="archive/copied.mkv",
            dry_run=False,
        ),
    )
    assert (files.roots[0] / "archive" / "copied.mkv").read_bytes() == b"movie"
    assert not (files.roots[0] / "movie.mkv").exists()


def test_failed_delete_is_audited(files, store, monkeypatch):
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path.name == "movie.mkv":
            raise PermissionError("locked")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError):
        confirmed(
            files,
            FileAction(
                action="delete", root=0, path="movie.mkv", dry_run=False, confirm_delete="movie.mkv"
            ),
        )
    assert store.recent()[0]["status"] == "failed"
    assert (files.roots[0] / "movie.mkv").exists()


def test_file_mutations_require_actor_bound_unchanged_one_shot_preview(files):
    from mediaforge.database import operation_actor

    request = FileAction(action="rename", root=0, path="movie.mkv", destination="film.mkv")
    with pytest.raises(ValueError, match="预览"):
        files.execute(request.model_copy(update={"dry_run": False}))
    preview = files.execute(request)
    apply = request.model_copy(update={"dry_run": False, "preview_id": preview["preview_id"]})
    actor = operation_actor.set("other-user")
    try:
        with pytest.raises(ValueError, match="预览"):
            files.execute(apply)
    finally:
        operation_actor.reset(actor)
    with pytest.raises(ValueError, match="预览"):
        files.execute(apply.model_copy(update={"destination": "other.mkv"}))
    (files.roots[0] / "movie.mkv").write_bytes(b"changed")
    with pytest.raises(ValueError, match="预览"):
        files.execute(apply)
    assert not (files.roots[0] / "film.mkv").exists()


def test_reject_symbolic_links(files, tmp_path):
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    link = files.roots[0] / "link.mkv"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows 当前账户未启用符号链接权限")
    with pytest.raises(ValueError, match="链接"):
        files.resolve(0, "link.mkv")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_reject_windows_junction_without_following_target(files, tmp_path, settings):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "movie.mkv").write_bytes(b"untouched")
    junction = files.roots[0] / "junction"
    result = subprocess.run(
        [os.environ["COMSPEC"], "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("当前 Windows 文件系统不支持创建目录联接")
    try:
        with pytest.raises(ValueError, match="链接"):
            files.resolve(0, r"junction\movie.mkv")
        listing = files.browse_media_folders(str(files.roots[0]), settings)
        assert next(item for item in listing["entries"] if item["name"] == "junction")["blocked"]
        with pytest.raises(ValueError, match="链接"):
            files.browse_media_folders(str(junction), settings)
        with pytest.raises(ValueError, match="链接"):
            files.migration_source(str(junction), settings)
        with pytest.raises(ValueError, match="链接"):
            files.save_roots(
                MediaRootsUpdate(roots=[str(junction)]),
                settings,
            )
        entry = next(item for item in files.list(0, "")["entries"] if item["name"] == "junction")
        assert entry["blocked"] is True
        assert (outside / "movie.mkv").read_bytes() == b"untouched"
    finally:
        junction.rmdir()
