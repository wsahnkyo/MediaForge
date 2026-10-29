import json
import logging
import os
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from string import Formatter
from typing import Any
from uuid import uuid4

import regex
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from mediaforge.database import OperationStore, RenameDirectoryState, RenameRule, operation_actor
from mediaforge.files import FileService, signature
from mediaforge.metadata import EXTRA_SUFFIX, MULTI_EPISODE, VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)
SIDECARS = {".nfo", ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
EPISODE_RANGE = regex.compile(
    r"(?i)(?:\bEP?|第)\s*\d{1,3}\s*(?:[-~&+]|至|到)\s*(?:EP?|第)?\d{1,3}(?!\d)"
)


class RuleInput(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-zA-Z0-9_-]{1,36}$")
    name: str = Field(min_length=1, max_length=80)
    pattern: str = Field(min_length=1, max_length=2000)
    template: str = Field(default="{episode:02d}", min_length=1, max_length=180)
    enabled: bool = True
    priority: int = Field(default=0, ge=0, le=1000)


class RulesUpdate(BaseModel):
    rules: list[RuleInput] = Field(min_length=1, max_length=32)


class RenamePreviewRequest(BaseModel):
    root: int = Field(ge=0)
    path: str = Field(default="", max_length=2000)
    roots_version: str | None = None
    force: bool = False


class RenameApplyRequest(BaseModel):
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class RenameEdit(BaseModel):
    source: str = Field(min_length=1, max_length=2000)
    new_name: str = Field(min_length=1, max_length=255)


class RenameReviewRequest(RenameApplyRequest):
    edits: list[RenameEdit] = Field(max_length=1000)


def defaults() -> list[RuleInput]:
    return [
        RuleInput(
            id="season-episode",
            name="S01E01 中的集号",
            priority=0,
            pattern=r"(?i)(?<![a-z0-9])S\d+[ ._-]*E(?P<episode>\d{1,3})(?!\d)",
        ),
        RuleInput(
            id="episode-prefix",
            name="第01集 / EP01",
            priority=1,
            pattern=(
                r"(?i)(?:第\s*(?P<episode>\d{1,3})\s*[集话話]|"
                r"(?<![a-z0-9])EP?[ ._-]*(?P<episode>\d{1,3})(?![a-z0-9]))"
            ),
        ),
        RuleInput(
            id="number-token",
            name="独立数字 01 / 1",
            priority=2,
            pattern=r"(?:^|[\s._\-\[\]【】()（）])(?P<episode>\d{1,3})(?=$|[\s._\-\[\]【】()（）])",
        ),
    ]


def digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def compile_rule(rule: RuleInput) -> regex.Pattern:
    try:
        compiled = regex.compile(rule.pattern)
    except regex.error:
        raise ValueError(f"规则“{rule.name}”的正则表达式无效") from None
    if "episode" not in compiled.groupindex:
        raise ValueError(f"规则“{rule.name}”必须包含命名组 (?P<episode>...)")
    try:
        fields = []
        for _, name, spec, conversion in Formatter().parse(rule.template):
            if name is not None:
                if name != "episode" or spec not in {"", "d", "02d", "03d"} or conversion:
                    raise ValueError("模板只支持 {episode:02d} 及三位或不补零集号格式")
                fields.append(name)
        if "episode" not in fields:
            raise ValueError("文件名模板必须包含 episode")
        generated = rule.template.format(episode=1)
        validate_name(generated + ".mkv")
    except (ValueError, KeyError, IndexError):
        raise ValueError(
            f"规则“{rule.name}”的文件名模板无效，请只使用 episode 集号占位符"
        ) from None
    return compiled


def validate_name(name: str) -> None:
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or any(char in name for char in '<>:"/\\|?*')
        or any(ord(char) < 32 for char in name)
        or name.endswith((" ", "."))
        or PureWindowsPath(name).is_reserved()
    ):
        raise ValueError("目标文件名包含非法字符、路径片段或 Windows 保留名称")


@dataclass
class RenamePlan:
    request: RenamePreviewRequest
    actor: str | None
    fingerprint: str
    rules_version: str
    created: float = field(default_factory=time.monotonic)
    rows: list[dict[str, Any]] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)


class RenameService:
    def __init__(self, files: FileService, store: OperationStore):
        self.files = files
        self.store = store
        self.previews: dict[str, RenamePlan] = {}

    def rules(self) -> list[RuleInput]:
        with self.store.sessions() as session:
            saved = session.scalars(select(RenameRule).order_by(RenameRule.priority, RenameRule.id))
            rules = []
            for rule in saved:
                values = {key: getattr(rule, key) for key in RuleInput.model_fields}
                if values["template"] == "S{season:02d}E{episode:02d}":
                    values["template"] = "{episode:02d}"
                rules.append(RuleInput(**values))
        return rules or defaults()

    def save_rules(self, request: RulesUpdate) -> dict[str, Any]:
        ids = [rule.id for rule in request.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("规则编号不能重复")
        for rule in request.rules:
            compile_rule(rule)

        def apply() -> dict[str, Any]:
            with self.store.sessions() as session:
                session.execute(delete(RenameRule).where(RenameRule.id.not_in(ids)))
                for rule in request.rules:
                    session.merge(RenameRule(**rule.model_dump()))
                session.commit()
            self.previews.clear()
            return {"rules": [rule.model_dump() for rule in self.rules()]}

        with self.files.lock:
            return self.store.execute("rename.rules.save", request.model_dump(), apply)

    def _listing(self, request: RenamePreviewRequest) -> tuple[list[dict[str, Any]], str]:
        listing = self.files.list(request.root, request.path, request.roots_version)["entries"]
        records = []
        for item in listing:
            if item["directory"]:
                continue
            path = self.files.resolve(request.root, item["path"]) if not item["blocked"] else None
            records.append(
                {
                    "name": item["name"],
                    "blocked": item["blocked"],
                    "signature": signature(path) if path else (item["size"], item["modified"]),
                }
            )
        return listing, digest(records)

    def _location(self, request: RenamePreviewRequest) -> tuple[Path, str]:
        folder = self.files.resolve(request.root, request.path)
        return folder, sha256(os.path.normcase(str(folder)).encode()).hexdigest()

    def _checkpoint(self, plan: RenamePlan, fingerprint: str) -> None:
        _, location_key = self._location(plan.request)
        with self.store.sessions() as session:
            session.merge(
                RenameDirectoryState(
                    location_key=location_key,
                    root_path=str(self.files.root(plan.request.root)),
                    folder_path=plan.request.path,
                    fingerprint=fingerprint,
                    rules_version=plan.rules_version,
                    checked_at=datetime.now(UTC),
                )
            )
            session.commit()

    def _cache(self, plan: RenamePlan, replacing: str | None = None) -> dict[str, Any]:
        self.previews = {
            key: value
            for key, value in self.previews.items()
            if time.monotonic() - value.created < 1800
        }
        if plan.rows and len(self.previews) >= 10 and replacing not in self.previews:
            raise ValueError("未执行的重命名预览过多，请等待旧预览过期")
        preview_id = uuid4().hex
        if plan.rows:
            self.previews[preview_id] = plan
        return {
            "preview_id": preview_id if plan.rows else None,
            "rows": deepcopy(plan.rows),
            "unchanged": False,
            "ready_count": sum(row["status"] == "ready" for row in plan.rows),
            "file_count": sum(
                len(row.get("files", [])) for row in plan.rows if row["status"] == "ready"
            ),
        }

    def _get(self, preview_id: str) -> RenamePlan:
        plan = self.previews.get(preview_id)
        if (
            plan is None
            or plan.actor != operation_actor.get()
            or time.monotonic() - plan.created >= 1800
        ):
            raise ValueError("预览不存在、已过期或不属于当前用户，请重新预览")
        self.files.check_roots_version(plan.request.roots_version)
        if digest([rule.model_dump() for rule in self.rules()]) != plan.rules_version:
            raise ValueError("正则规则已变化，请重新预览")
        if self._listing(plan.request)[1] != plan.fingerprint:
            raise ValueError("预览后当前目录中的文件已变化，请重新预览")
        return plan

    def _target_files(
        self,
        request: RenamePreviewRequest,
        source: str,
        new_name: str,
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        video = self.files.resolve(request.root, source)
        validate_name(new_name)
        if Path(new_name).suffix != video.suffix:
            raise ValueError("必须保留视频原扩展名，不允许修改格式")
        target = video.with_name(new_name)
        pairs = [(video, target)]
        stems = {
            Path(item["name"]).stem.casefold()
            for item in entries
            if not item["directory"] and Path(item["name"]).suffix.lower() in VIDEO_EXTENSIONS
        }
        for item in entries:
            companion = Path(item["name"])
            if item["directory"] or companion.suffix.lower() not in SIDECARS:
                continue
            if not companion.name.casefold().startswith(video.stem.casefold() + "."):
                continue
            if (
                companion.suffix.lower() == ".nfo"
                and companion.stem.casefold() != video.stem.casefold()
            ):
                continue
            owners = [
                stem
                for stem in stems
                if companion.stem.casefold() == stem
                or companion.stem.casefold().startswith(stem + ".")
            ]
            if owners and max(owners, key=len) != video.stem.casefold():
                continue
            if item["blocked"]:
                raise ValueError("同名 NFO / 字幕是链接，已跳过整组文件")
            suffix = companion.name[len(video.stem) :]
            pairs.append(
                (
                    self.files.resolve(request.root, item["path"]),
                    target.with_name(target.stem + suffix),
                )
            )
        result = []
        for old, new in pairs:
            relative = str(new.relative_to(self.files.root(request.root)))
            self.files.resolve(request.root, relative, must_exist=False)
            if new.name == old.name:
                continue
            if new.exists() or new.is_symlink():
                raise ValueError(f"目标已存在，禁止覆盖：{new.name}")
            result.append(
                {
                    "source": str(old.relative_to(self.files.root(request.root))),
                    "target": relative,
                    "signature": signature(old),
                }
            )
        return result

    def _set_target(self, plan: RenamePlan, row: dict[str, Any], new_name: str) -> None:
        row["new_name"] = new_name
        row["files"] = []
        try:
            row["files"] = self._target_files(plan.request, row["source"], new_name, plan.entries)
            row["status"] = "ready" if row["files"] else "unchanged"
            row["reason"] = "" if row["files"] else "文件名已经符合规则"
        except (OSError, ValueError) as error:
            row.update(status="conflict", reason=str(error))

    def _collisions(self, plan: RenamePlan) -> None:
        targets: dict[str, list[dict[str, Any]]] = {}
        sources: dict[str, list[dict[str, Any]]] = {}
        for row in plan.rows:
            if row["status"] != "ready":
                continue
            for item in row["files"]:
                targets.setdefault(item["target"].casefold(), []).append(row)
                sources.setdefault(item["source"].casefold(), []).append(row)
        for groups in (targets, sources):
            for rows in groups.values():
                if len(rows) > 1:
                    for row in rows:
                        row.update(status="conflict", reason="多个提取结果指向同一文件，已全部跳过")

    def preview(self, request: RenamePreviewRequest) -> dict[str, Any]:
        with self.files.lock:
            return self.store.execute(
                "rename.preview", request.model_dump(), lambda: self._preview(request)
            )

    def _preview(self, request: RenamePreviewRequest) -> dict[str, Any]:
        rules = self.rules()
        active = [(rule, compile_rule(rule)) for rule in rules if rule.enabled]
        if not active:
            raise ValueError("请至少启用一个正则规则")
        listing, fingerprint = self._listing(request)
        _, location_key = self._location(request)
        version = digest([rule.model_dump() for rule in rules])
        with self.store.sessions() as session:
            state = session.get(RenameDirectoryState, location_key)
            if (
                not request.force
                and state
                and state.fingerprint == fingerprint
                and state.rules_version == version
            ):
                return {
                    "unchanged": True,
                    "rows": [],
                    "ready_count": 0,
                    "file_count": 0,
                    "reason": "当前目录没有文件新增或修改，且规则未变化，跳过重命名",
                    "checked_at": state.checked_at.isoformat(),
                }
        plan = RenamePlan(
            request.model_copy(update={"roots_version": self.files.roots_version}),
            operation_actor.get(),
            fingerprint,
            version,
        )
        plan.entries = listing
        timed_out = set()
        started = time.monotonic()
        for item in listing:
            path = Path(item["name"])
            if item["directory"] or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if time.monotonic() - started > 20 or len(plan.rows) >= 1000:
                raise ValueError("当前目录超过处理时间或 1000 个视频，请缩小处理范围")
            row = {
                "source": item["path"],
                "name": item["name"],
                "new_name": item["name"],
                "status": "skipped",
                "reason": "没有规则能唯一提取有效集数",
                "files": [],
                "editable": not item["blocked"],
            }
            plan.rows.append(row)
            if (
                item["blocked"]
                or MULTI_EPISODE.search(path.stem)
                or EPISODE_RANGE.search(path.stem)
                or EXTRA_SUFFIX.search(path.stem)
            ):
                row.update(reason="链接、多集文件或额外内容，禁止自动重命名", editable=False)
                continue
            notes = []
            for rule, compiled in active:
                if rule.id in timed_out:
                    notes.append(f"{rule.name} 本轮因超时停用")
                    continue
                try:
                    matches = []
                    for match in compiled.finditer(path.stem, timeout=0.05):
                        matches.append(match)
                        if len(matches) >= 2:
                            break
                    if not matches:
                        continue
                    if len(matches) != 1:
                        notes.append(f"{rule.name} 提取到多个数字，无法确定")
                        continue
                    groups = matches[0].groupdict()
                    value = groups.get("episode") or ""
                    if not value.isdecimal() or not 1 <= int(value) <= 999:
                        notes.append(f"{rule.name} 集数必须为 1–999 的整数")
                        continue
                    new_name = rule.template.format(episode=int(value)) + path.suffix
                    validate_name(new_name)
                    row.update(rule=rule.name, episode=int(value), notes=notes)
                    self._set_target(plan, row, new_name)
                    break
                except ValueError as error:
                    notes.append(f"{rule.name}：{error}")
                except TimeoutError:
                    timed_out.add(rule.id)
                    notes.append(f"{rule.name} 正则超时，已停用该规则至本轮结束")
            else:
                if notes:
                    row["reason"] = "；".join(notes)
        self._collisions(plan)
        if not any(row["status"] in {"ready", "conflict"} for row in plan.rows):
            self._checkpoint(plan, fingerprint)
        return self._cache(plan)

    def review(self, request: RenameReviewRequest) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            original = self._get(request.preview_id)
            edits = {item.source: item.new_name for item in request.edits}
            if len(edits) != len(request.edits):
                raise ValueError("修改项不能重复")
            rows = {row["source"]: row for row in original.rows}
            if any(source not in rows or not rows[source]["editable"] for source in edits):
                raise ValueError("只能修改本次预览中允许重命名的文件")
            plan = deepcopy(original)
            plan.created = time.monotonic()
            for row in plan.rows:
                if row["editable"] and (
                    row["source"] in edits or row["status"] in {"ready", "conflict", "unchanged"}
                ):
                    self._set_target(plan, row, edits.get(row["source"], row["new_name"]))
            self._collisions(plan)
            result = self._cache(plan, replacing=request.preview_id)
            del self.previews[request.preview_id]
            return result

        with self.files.lock:
            return self.store.execute("rename.review", request.model_dump(), apply)

    def apply(self, request: RenameApplyRequest) -> dict[str, Any]:
        with self.files.lock:
            plan = self._get(request.preview_id)
            if not any(row["status"] == "ready" for row in plan.rows):
                raise ValueError("没有通过校验且需要修改的文件，不能执行")
            del self.previews[request.preview_id]
            return self.store.execute(
                "rename.apply", request.model_dump(), lambda: self._apply(plan)
            )

    def _apply(self, plan: RenamePlan) -> dict[str, Any]:
        rows = deepcopy(plan.rows)
        failed = False
        for row in rows:
            if row["status"] != "ready":
                continue
            completed = []
            try:
                for item in row["files"]:
                    source = self.files.resolve(plan.request.root, item["source"])
                    target = self.files.resolve(plan.request.root, item["target"], must_exist=False)
                    if signature(source) != tuple(item["signature"]) or target.exists():
                        raise ValueError("文件在预览后发生变化，未覆盖")
                for item in row["files"]:
                    source = self.files.resolve(plan.request.root, item["source"])
                    target = self.files.resolve(plan.request.root, item["target"], must_exist=False)
                    if signature(source) != tuple(item["signature"]):
                        raise ValueError("文件在预览后发生变化，未重命名")
                    self.files._move(source, target)
                    completed.append(item)
                row["status"] = "renamed"
            except (OSError, ValueError) as error:
                failed = True
                row.update(status="failed", reason=str(error), rollback_errors=[])
                for item in reversed(completed):
                    try:
                        target = self.files.resolve(plan.request.root, item["target"])
                        source = self.files.resolve(
                            plan.request.root, item["source"], must_exist=False
                        )
                        if signature(target) != tuple(item["signature"]) or source.exists():
                            raise ValueError("文件变化，不能安全回滚")
                        self.files._move(target, source)
                    except (OSError, ValueError) as rollback_error:
                        row["rollback_errors"].append(f"{item['target']}：{rollback_error}")
                logger.warning("重命名失败 path=%s: %s", row["source"], error)
        if not failed and not any(row["status"] == "conflict" for row in rows):
            self._checkpoint(plan, self._listing(plan.request)[1])
        return {
            "rows": rows,
            "renamed_count": sum(row["status"] == "renamed" for row in rows),
            "outcome": "partial"
            if failed or any(row["status"] == "conflict" for row in rows)
            else "success",
        }
