import errno
import os
import subprocess
from pathlib import Path

import pytest

from mediaforge.files import FileAction, FileService, MediaRootsUpdate


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
            files.save_roots(
                MediaRootsUpdate(roots=[str(junction)]),
                settings,
            )
        entry = next(item for item in files.list(0, "")["entries"] if item["name"] == "junction")
        assert entry["blocked"] is True
        assert (outside / "movie.mkv").read_bytes() == b"untouched"
    finally:
        junction.rmdir()
