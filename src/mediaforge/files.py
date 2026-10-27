import errno
import os
import shutil
import stat
from pathlib import Path, PureWindowsPath
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, Field

from mediaforge.database import OperationStore


class FileAction(BaseModel):
    action: Literal["rename", "move", "delete"]
    root: int = Field(ge=0)
    path: str = Field(min_length=1, max_length=2000)
    destination_root: int | None = Field(default=None, ge=0)
    destination: str | None = Field(default=None, max_length=2000)
    dry_run: bool = True
    confirm_delete: str | None = None


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


class FileService:
    def __init__(self, roots: list[Path], store: OperationStore):
        self.roots = roots
        self.store = store
        self.lock = RLock()

    def root(self, index: int) -> Path:
        if not 0 <= index < len(self.roots):
            raise ValueError("媒体目录不存在")
        root = self.roots[index]
        if not root.is_dir() or is_link(root):
            raise ValueError("媒体目录不可用，或已变为链接")
        # Reject reparse points in ancestors as well, including Windows junctions.
        if any(is_link(parent) for parent in root.parents):
            raise ValueError("媒体目录的父路径包含链接")
        return root

    def resolve(self, index: int, relative: str, *, must_exist: bool = True) -> Path:
        root = self.root(index)
        normalized = relative.replace("\\", "/")
        win_path = PureWindowsPath(relative)
        if win_path.drive or win_path.root or normalized.startswith("/"):
            raise ValueError("只允许媒体目录内的相对路径")
        parts = normalized.split("/")
        if relative and any(
            part in ("", ".", "..")
            or any(char in part for char in '<>:"|?*')
            or any(ord(char) < 32 for char in part)
            or part.endswith((" ", "."))
            or PureWindowsPath(part).is_reserved()
            for part in parts
        ):
            raise ValueError("路径包含不安全的片段或 Windows 保留名称")
        path = root
        for part in parts if relative else []:
            path = path / part
            if path.exists() or path.is_symlink():
                if is_link(path):
                    raise ValueError("不允许操作符号链接、目录联接或重解析点")
        if not path.resolve().is_relative_to(root):
            raise ValueError("路径不能越过媒体目录")
        if must_exist and not path.exists():
            raise FileNotFoundError("文件或目录不存在")
        return path

    def list(self, root: int, relative: str) -> dict[str, Any]:
        with self.lock:
            directory = self.resolve(root, relative)
            if not directory.is_dir():
                raise ValueError("请选择目录")
            entries = []
            # Bounded response; users must narrow the directory when it exceeds this limit.
            with os.scandir(directory) as children:
                for child in children:
                    if len(entries) >= 2000:
                        raise ValueError("目录超过 2000 项，请缩小浏览范围")
                    path = Path(child.path)
                    blocked = is_link(path)
                    info = child.stat(follow_symlinks=False)
                    entries.append(
                        {
                            "name": child.name,
                            "path": str(path.relative_to(self.roots[root])),
                            "directory": stat.S_ISDIR(info.st_mode) and not blocked,
                            "blocked": blocked,
                            "size": info.st_size,
                            "modified": info.st_mtime,
                        }
                    )
            return {
                "entries": sorted(entries, key=lambda item: (not item["directory"], item["name"])),
                "path": relative,
            }

    def _plan(self, request: FileAction) -> tuple[Path, Path | None, dict[str, Any]]:
        source = self.resolve(request.root, request.path)
        if not source.is_file():
            raise ValueError("第一版只操作普通文件，不移动或递归删除目录")
        target = None
        if request.action != "delete":
            if not request.destination:
                raise ValueError("请输入目标文件名或相对路径")
            if request.action == "rename":
                if (
                    request.destination in (".", "..")
                    or "/" in request.destination
                    or "\\" in request.destination
                ):
                    raise ValueError("重命名只能填写文件名")
                relative = str(
                    source.parent.relative_to(self.roots[request.root]) / request.destination
                )
                target = self.resolve(request.root, relative, must_exist=False)
            else:
                target_root = request.destination_root
                if target_root is None:
                    raise ValueError("请选择目标媒体目录")
                target = self.resolve(target_root, request.destination, must_exist=False)
            if target.exists():
                raise FileExistsError("目标已存在，不允许覆盖（含仅修改大小写的重命名）")
            if not target.parent.is_dir():
                raise ValueError("目标父目录不存在，请先创建目录")
        if request.action == "delete" and not request.dry_run:
            if request.confirm_delete != source.name:
                raise ValueError("永久删除需要输入完整文件名确认")
        info = source.stat()
        return (
            source,
            target,
            {
                "action": request.action,
                "source": str(source),
                "destination": str(target) if target else None,
                "size": info.st_size,
                "modified_ns": info.st_mtime_ns,
                "permanent": request.action == "delete",
            },
        )

    @staticmethod
    def _move(source: Path, target: Path) -> None:
        try:
            if os.name == "nt":
                # Windows rename is no-replace and preserves the file without copying.
                source.rename(target)
                return
            os.link(source, target)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.ENOTSUP, errno.EPERM):
                raise
            # Exclusive creation prevents accidental replacement, even across volumes.
            with source.open("rb") as reader, target.open("xb") as writer:
                try:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                except OSError:
                    writer.close()
                    target.unlink()
                    raise
            try:
                shutil.copystat(source, target)
            except OSError:
                target.unlink()
                raise
        try:
            source.unlink()
        except OSError:
            target.unlink()
            raise

    def execute(self, request: FileAction) -> dict[str, Any]:
        with self.lock:
            _, _, plan = self._plan(request)
            if request.dry_run:
                return {"dry_run": True, **plan}

            def apply() -> dict[str, Any]:
                source, target, current = self._plan(request)
                if current != plan:
                    raise ValueError("文件在预检后发生变化，请重新操作")
                if target is None:
                    source.unlink()
                else:
                    self._move(source, target)
                return {"dry_run": False, **current}

            return self.store.execute("file." + request.action, request.model_dump(), apply)
