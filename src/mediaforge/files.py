import errno
import json
import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from threading import RLock
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from mediaforge.config import Settings, validate_media_targets
from mediaforge.database import MediaRootSetting, OperationStore, operation_actor

logger = logging.getLogger(__name__)


class MediaRootsUpdate(BaseModel):
    roots: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(max_length=32)
    roots_version: str | None = None


class FileDeleteBatch(BaseModel):
    root: int = Field(ge=0)
    paths: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        min_length=1, max_length=1000
    )
    roots_version: str = Field(min_length=1, max_length=64)
    dry_run: bool = True
    preview_id: str | None = None


class FileAction(BaseModel):
    action: Literal["rename", "move", "delete"]
    root: int = Field(ge=0)
    path: str = Field(min_length=1, max_length=2000)
    destination_root: int | None = Field(default=None, ge=0)
    destination: str | None = Field(default=None, max_length=2000)
    dry_run: bool = True
    confirm_delete: str | None = None
    roots_version: str | None = None
    preview_id: str | None = None


@dataclass
class FilePreview:
    request: dict[str, Any]
    plan: dict[str, Any]
    roots_version: str
    actor: str | None
    created: float


def signature(path: Path) -> tuple[int, int, int] | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"目标不是普通文件：{path.name}")
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_ino


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def validate_root(root: Path) -> None:
    if not root.is_dir() or is_link(root):
        raise ValueError("媒体目录不可用，或已变为链接")
    if any(is_link(parent) for parent in root.parents):
        raise ValueError("媒体目录的父路径包含链接")


def media_drives() -> list[Path]:
    if os.name == "nt":
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        if not mask:
            raise OSError("无法读取本机磁盘列表")
        return [Path(f"{chr(65 + index)}:\\") for index in range(26) if mask & (1 << index)]
    return [Path(Path.cwd().anchor)]


class FileService:
    def __init__(self, roots: list[Path], store: OperationStore):
        self.roots = roots
        self.store = store
        self.lock = RLock()
        self.previews: dict[str, FilePreview] = {}

    @property
    def roots_version(self) -> str:
        return sha256(json.dumps([str(root) for root in self.roots]).encode()).hexdigest()

    def check_roots_version(self, version: str | None) -> None:
        if version is not None and version != self.roots_version:
            raise ValueError("媒体目录配置已变化，请刷新页面并重新预览")

    def load_roots(self, settings: Settings) -> None:
        with self.lock, self.store.sessions() as session:
            saved = session.get(MediaRootSetting, 1)
            if saved is not None:
                roots = [Path(path) for path in saved.roots]
                validate_media_targets(roots, settings.hosts_path, settings.log_path)
                self.roots = roots

    def browse_media_folders(self, path: str | None, settings: Settings) -> dict[str, Any]:
        if not path:
            return {
                "path": None,
                "parent": None,
                "selectable": False,
                "reason": "",
                "entries": [
                    {"name": str(drive), "path": str(drive), "blocked": False}
                    for drive in media_drives()
                ],
            }
        folder = Path(path)
        if not folder.is_absolute():
            raise ValueError("请选择本机绝对路径的文件夹")
        validate_root(folder)
        folder = folder.resolve()
        entries = []
        with os.scandir(folder) as children:
            for index, child in enumerate(children):
                if index >= 2000:
                    raise ValueError("目录超过 2000 项，无法完整列出子文件夹")
                info = child.stat(follow_symlinks=False)
                blocked = stat.S_ISLNK(info.st_mode) or bool(
                    getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                )
                if blocked or stat.S_ISDIR(info.st_mode):
                    entries.append({"name": child.name, "path": child.path, "blocked": blocked})
        reason = ""
        try:
            validate_media_targets([folder], settings.hosts_path, settings.log_path)
        except ValueError as error:
            reason = str(error)
        return {
            "path": str(folder),
            "parent": str(folder.parent) if folder.parent != folder else None,
            "entries": sorted(entries, key=lambda item: item["name"].casefold()),
            "selectable": not reason,
            "reason": reason,
        }

    def save_roots(self, request: MediaRootsUpdate, settings: Settings) -> dict[str, Any]:
        with self.lock:
            self.check_roots_version(request.roots_version)
            roots = []
            for value in request.roots:
                path = Path(value.strip()).expanduser()
                if not path.is_absolute():
                    raise ValueError("媒体目录必须填写本机绝对路径")
                validate_root(path)
                roots.append(path.resolve())
            if len(set(roots)) != len(roots):
                raise ValueError("媒体目录不能重复")
            validate_media_targets(roots, settings.hosts_path, settings.log_path)

            def apply() -> dict[str, Any]:
                with self.store.sessions() as session:
                    session.merge(MediaRootSetting(id=1, roots=[str(root) for root in roots]))
                    session.commit()
                self.roots = roots
                self.previews.clear()
                return {
                    "roots": [{"id": index, "path": str(root)} for index, root in enumerate(roots)],
                    "roots_version": self.roots_version,
                }

            return self.store.execute("media.roots.save", request.model_dump(), apply)

    def root(self, index: int) -> Path:
        if not 0 <= index < len(self.roots):
            raise ValueError("媒体目录不存在")
        root = self.roots[index]
        validate_root(root)
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

    def list(self, root: int, relative: str, roots_version: str | None = None) -> dict[str, Any]:
        with self.lock:
            self.check_roots_version(roots_version)
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
                "inode": info.st_ino,
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

    def _remember_preview(self, identity: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        self.previews = {
            key: value
            for key, value in self.previews.items()
            if time.monotonic() - value.created < 1800
        }
        if len(self.previews) >= 50:
            raise ValueError("未执行的预览过多，请等待旧预览过期")
        preview_id = uuid4().hex
        self.previews[preview_id] = FilePreview(
            identity, plan, self.roots_version, operation_actor.get(), time.monotonic()
        )
        return {"dry_run": True, "preview_id": preview_id, **plan}

    def _consume_preview(
        self, preview_id: str | None, identity: dict[str, Any], plan: dict[str, Any]
    ) -> None:
        key = preview_id or ""
        preview = self.previews.get(key)
        if (
            preview is None
            or time.monotonic() - preview.created >= 1800
            or preview.actor != operation_actor.get()
            or preview.roots_version != self.roots_version
            or preview.request != identity
            or preview.plan != plan
        ):
            raise ValueError("缺少有效预览，或文件 / 操作已变化，请重新预览并确认")
        del self.previews[key]

    def delete_batch(self, request: FileDeleteBatch) -> dict[str, Any]:
        with self.lock:
            self.check_roots_version(request.roots_version)
            actions = [
                FileAction(action="delete", root=request.root, path=path) for path in request.paths
            ]
            plans = [self._plan(action)[2] for action in actions]
            if len({os.path.normcase(plan["source"]) for plan in plans}) != len(plans):
                raise ValueError("不能重复选择同一个文件")
            identity = {"action": "delete-batch", "root": request.root, "paths": request.paths}
            plan = {"files": plans, "permanent": True}
            if request.dry_run:
                return self._remember_preview(identity, plan)
            self._consume_preview(request.preview_id, identity, plan)

            def apply() -> dict[str, Any]:
                results = []
                for action, expected in zip(actions, plans, strict=True):
                    row = {"path": action.path, "status": "failed", "error": None}
                    results.append(row)
                    try:
                        source, _, current = self._plan(action)
                        if current != expected:
                            raise ValueError("文件在确认后发生变化，未删除")
                        source.unlink()
                        row["status"] = "success"
                    except (OSError, ValueError) as exc:
                        row["error"] = str(exc)
                        logger.warning("批量删除失败 path=%s: %s", action.path, exc)
                deleted = sum(row["status"] == "success" for row in results)
                return {
                    "files": results,
                    "deleted_count": deleted,
                    "outcome": "success"
                    if deleted == len(results)
                    else "partial"
                    if deleted
                    else "failed",
                }

            return self.store.execute("file.delete.batch", request.model_dump(), apply)

    def execute(self, request: FileAction) -> dict[str, Any]:
        with self.lock:
            self.check_roots_version(request.roots_version)
            _, _, plan = self._plan(request)
            identity = request.model_dump(
                exclude={"dry_run", "confirm_delete", "preview_id", "roots_version"}
            )
            if request.dry_run:
                return self._remember_preview(identity, plan)
            self._consume_preview(request.preview_id, identity, plan)

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
