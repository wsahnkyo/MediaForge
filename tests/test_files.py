import errno
import os
import subprocess
from pathlib import Path

import pytest

from mediaforge.files import FileAction, FileService


@pytest.fixture
def files(tmp_path, store):
    root = tmp_path / "media"
    root.mkdir()
    (root / "movie.mkv").write_bytes(b"movie")
    (root / "archive").mkdir()
    return FileService([root], store)


def test_preview_rename_move_delete_and_audit(files, store):
    request = FileAction(action="rename", root=0, path="movie.mkv", destination="film.mkv")
    preview = files.execute(request)
    assert preview["dry_run"] is True
    assert (files.roots[0] / "movie.mkv").exists()
    assert store.recent() == []
    files.execute(request.model_copy(update={"dry_run": False}))
    assert not (files.roots[0] / "movie.mkv").exists()
    assert (files.roots[0] / "film.mkv").read_bytes() == b"movie"
    files.execute(
        FileAction(
            action="move",
            root=0,
            path="film.mkv",
            destination_root=0,
            destination="archive/film.mkv",
            dry_run=False,
        )
    )
    with pytest.raises(ValueError, match="确认"):
        files.execute(FileAction(action="delete", root=0, path="archive/film.mkv", dry_run=False))
    files.execute(
        FileAction(
            action="delete",
            root=0,
            path="archive/film.mkv",
            dry_run=False,
            confirm_delete="film.mkv",
        )
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
        files.execute(
            FileAction(
                action="rename", root=0, path="movie.mkv", destination="exists.mkv", dry_run=False
            )
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
    files.execute(
        FileAction(
            action="move",
            root=0,
            path="movie.mkv",
            destination_root=0,
            destination="archive/copied.mkv",
            dry_run=False,
        )
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
        files.execute(
            FileAction(
                action="delete", root=0, path="movie.mkv", dry_run=False, confirm_delete="movie.mkv"
            )
        )
    assert store.recent()[0]["status"] == "failed"
    assert (files.roots[0] / "movie.mkv").exists()


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
def test_reject_windows_junction_without_following_target(files, tmp_path):
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
        entry = next(item for item in files.list(0, "")["entries"] if item["name"] == "junction")
        assert entry["blocked"] is True
        assert (outside / "movie.mkv").read_bytes() == b"untouched"
    finally:
        junction.rmdir()
