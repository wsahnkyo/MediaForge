import ipaddress
import json
import logging
import os
import shutil
import socket
import ssl
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

import dns.exception
import dns.resolver
import httpx

from mediaforge.config import Settings, system_hosts_path
from mediaforge.database import OperationFailure, OperationStore
from mediaforge.files import is_link

logger = logging.getLogger(__name__)
BEGIN = b"# MediaForge Hosts BEGIN"
END = b"# MediaForge Hosts END"
DOH_URLS = {
    "google": "https://dns.google/resolve",
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "alidns": "https://dns.alidns.com/resolve",
}
GITHUB_DOMAINS = (
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "avatars.githubusercontent.com",
    "github.githubassets.com",
)


def public_address(value: str, version: int):
    address = ipaddress.ip_address(value)
    if (
        address.version != version
        or not address.is_global
        or address.is_multicast
        or address.is_reserved
        or "%" in value
    ):
        raise ValueError(f"拒绝非公网、特殊用途或地址族不符的 IP: {value}")
    return address


def add_log(logs: list[dict[str, str]], level: str, message: str, domain: str = "") -> None:
    logs.append(
        {
            "time": datetime.now(UTC).isoformat(),
            "level": level,
            "domain": domain,
            "message": message,
        }
    )
    severity = {
        "info": logging.INFO,
        "success": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }[level]
    logger.log(severity, "%s%s", f"[{domain}] " if domain else "", message)


def managed_bounds(content: bytes) -> tuple[int, int] | None:
    lines = content.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == BEGIN]
    ends = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == END]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("hosts 托管区标记不完整或重复，请先人工检查")
    return starts[0], ends[0] + 1


def merge_hosts(content: bytes, entries: list[dict[str, Any]], domains: list[str]) -> bytes:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\0" in content:
        raise ValueError("hosts 不支持 UTF-16 编码，请先另存为 UTF-8 或 ANSI")
    bounds = managed_bounds(content)
    lines = content.splitlines(keepends=True)
    outside = lines if bounds is None else lines[: bounds[0]] + lines[bounds[1] :]
    managed_domains = {domain.encode("ascii") for domain in domains}
    for line in outside:
        fields = line.split(b"#", 1)[0].split()
        if len(fields) >= 2 and managed_domains.intersection(
            field.lower().rstrip(b".") for field in fields[1:]
        ):
            raise ValueError("托管区外存在同名域名，请人工处理旧映射后再更新")
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    block = newline.join(
        [BEGIN]
        + [f"{entry['ip']}\t{entry['domain']}".encode("ascii") for entry in entries]
        + [END, b""]
    )
    if bounds is None:
        separator = newline if content and not content.endswith(b"\n") else b""
        return content + separator + block
    return b"".join(lines[: bounds[0]]) + block + b"".join(lines[bounds[1] :])


class HostsService:
    def __init__(self, settings: Settings, store: OperationStore):
        self.settings = settings
        self.store = store
        self.lock = RLock()
        self.tls_context = ssl.create_default_context()

    @property
    def domains(self) -> list[str]:
        return list(
            dict.fromkeys(
                self.settings.hosts_domains
                + (list(GITHUB_DOMAINS) if self.settings.hosts_include_github else [])
            )
        )

    @property
    def families(self) -> list[str]:
        return (
            ["A", "AAAA"]
            if self.settings.ip_family == "both"
            else ["A" if self.settings.ip_family == "ipv4" else "AAAA"]
        )

    def _json(
        self, client: httpx.Client, url: str, params: dict[str, str], headers: dict[str, str]
    ) -> dict[str, Any]:
        for attempt in range(self.settings.dns_attempts):
            try:
                with client.stream("GET", url, params=params, headers=headers) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 1024 * 1024:
                            raise ValueError("DNS 响应超过 1 MiB")
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError("DNS 响应不是 JSON 对象")
                return data
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retryable = (
                    not isinstance(exc, httpx.HTTPStatusError)
                    or exc.response.status_code >= 500
                    or exc.response.status_code == 429
                )
                if not retryable or attempt + 1 == self.settings.dns_attempts:
                    raise
                logger.warning("HTTPS DNS 请求失败，重试 %s: %s", attempt + 1, type(exc).__name__)
                time.sleep(0.2 * (attempt + 1))
        raise RuntimeError("DNS 重试次数配置无效")

    def _resolve(
        self, provider: str, domain: str, record_type: str, client: httpx.Client
    ) -> list[str]:
        if provider == "dns":
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = self.settings.dns_servers
            resolver.timeout = self.settings.network_timeout
            resolver.lifetime = self.settings.network_timeout * len(self.settings.dns_servers)
            return [
                answer.to_text() for answer in resolver.resolve(domain, record_type, search=False)
            ]
        if provider not in DOH_URLS:
            raise ValueError(f"不支持的 DNS 查询源: {provider}")
        data = self._json(
            client,
            DOH_URLS[provider],
            {"name": domain, "type": record_type},
            {"accept": "application/dns-json"},
        )
        if data.get("Status") != 0:
            raise ValueError(f"DoH 查询失败，DNS 状态: {data.get('Status')}")
        answers = data.get("Answer", [])
        if not isinstance(answers, list):
            raise ValueError("DoH Answer 字段不是列表")
        # A CNAME's data is a hostname, not a candidate IP.
        return [
            answer["data"]
            for answer in answers
            if isinstance(answer, dict)
            and answer.get("type") == (1 if record_type == "A" else 28)
            and isinstance(answer.get("data"), str)
        ]

    def _probe(self, domain: str, address: str) -> float:
        started = time.perf_counter()
        with socket.create_connection(
            (address, 443), timeout=self.settings.network_timeout
        ) as connection:
            with self.tls_context.wrap_socket(connection, server_hostname=domain):
                return round((time.perf_counter() - started) * 1000, 2)

    def _domain(self, domain: str, client: httpx.Client) -> dict[str, Any]:
        entries = []
        errors = []
        logs = []
        unresolved = []

        def warn(message: str) -> None:
            errors.append(message)
            add_log(logs, "warning", message, domain)

        for record_type in self.families:
            checked = set()
            for provider in self.settings.dns_providers:
                label = f"{record_type} / {provider}"
                add_log(logs, "info", f"开始查询 {label}", domain)
                try:
                    addresses = self._resolve(provider, domain, record_type, client)
                except (dns.exception.DNSException, httpx.HTTPError, ValueError) as exc:
                    warn(f"{label} 查询失败: {exc}")
                    continue
                if not addresses:
                    warn(f"{label} 未返回地址")
                candidates = []
                for value in list(dict.fromkeys(addresses))[:8]:
                    try:
                        address = str(public_address(value, 4 if record_type == "A" else 6))
                    except ValueError as exc:
                        warn(f"{label} {exc}")
                        continue
                    if address in checked:
                        continue
                    checked.add(address)
                    try:
                        latency = self._probe(domain, address)
                    except OSError as exc:
                        warn(f"{label} {address} TLS 校验/连接失败: {exc}")
                        continue
                    candidates.append(
                        {
                            "domain": domain,
                            "ip": address,
                            "latency_ms": latency,
                            "source": provider,
                            "retained": False,
                        }
                    )
                if candidates:
                    best = min(candidates, key=lambda item: item["latency_ms"])
                    entries.append(best)
                    add_log(
                        logs,
                        "success",
                        f"{label} 成功，选用 {best['ip']}，TLS 延迟 {best['latency_ms']} ms",
                        domain,
                    )
                    break
                warn(f"{label} 无通过 TLS 校验的地址，尝试下一查询源")
            else:
                unresolved.append({"domain": domain, "record_type": record_type})
                add_log(logs, "error", f"{record_type} 所有查询源均无可用映射", domain)
        return {
            "domain": domain,
            "entries": entries,
            "warnings": errors,
            "unresolved": unresolved,
            "logs": logs,
        }

    def scan(self) -> dict[str, Any]:
        logs: list[dict[str, str]] = []
        add_log(
            logs, "info", f"开始检测 {len(self.domains)} 个域名，地址族 {self.settings.ip_family}"
        )
        with (
            httpx.Client(
                timeout=self.settings.network_timeout,
                verify=self.tls_context,
                follow_redirects=False,
            ) as client,
            ThreadPoolExecutor(max_workers=self.settings.probe_workers) as pool,
        ):
            results = list(pool.map(lambda domain: self._domain(domain, client), self.domains))
        entries = [entry for result in results for entry in result["entries"]]
        unresolved = [item for result in results for item in result["unresolved"]]
        logs.extend(
            sorted(
                (item for result in results for item in result["logs"]),
                key=lambda item: item["time"],
            )
        )
        timestamp = datetime.now(UTC).isoformat()
        exports = {}
        for family in ("ipv4", "ipv6"):
            selected = [entry for entry in entries if (":" in entry["ip"]) == (family == "ipv6")]
            exports[family] = (
                (
                    f"# MediaForge {family} - TLS verified at {timestamp}\n"
                    "# Only addresses verified in this scan; retained old mappings are excluded.\n"
                    + "".join(f"{entry['ip']}\t{entry['domain']}\n" for entry in selected)
                )
                if selected
                else ""
            )
        return {
            "entries": entries,
            "failures": [result["domain"] for result in results if not result["entries"]],
            "unresolved": unresolved,
            "partial": bool(unresolved),
            "diagnostics": results,
            "target": str(self.settings.hosts_path),
            "exports": exports,
            "logs": logs,
        }

    def _read_target(self) -> bytes:
        target = self.settings.hosts_path
        for component in [target, *target.parents]:
            if (component.exists() or component.is_symlink()) and is_link(component):
                raise ValueError("hosts 路径不能包含符号链接、目录联接或重解析点")
        if target.resolve() == system_hosts_path().resolve():
            if not self.settings.hosts_allow_system:
                raise ValueError("尚未允许修改系统 hosts")
            if not target.is_file():
                raise ValueError("系统 hosts 文件不存在，拒绝自动创建")
        return target.read_bytes() if target.exists() else b""

    def _prepare(self, scan: dict[str, Any], original: bytes) -> dict[str, Any]:
        unresolved = {
            (item["domain"], 4 if item["record_type"] == "A" else 6) for item in scan["unresolved"]
        }
        retained = []
        bounds = managed_bounds(original)
        if bounds:
            for line in original.splitlines()[bounds[0] + 1 : bounds[1] - 1]:
                fields = line.split(b"#", 1)[0].split()
                if not fields:
                    continue
                if len(fields) < 2:
                    raise ValueError("hosts 托管区包含无效映射，请先人工检查")
                address = ipaddress.ip_address(fields[0].decode("ascii"))
                for value in fields[1:]:
                    domain = value.decode("ascii").lower().rstrip(".")
                    if (domain, address.version) in unresolved:
                        public_address(str(address), address.version)
                        entry = {
                            "domain": domain,
                            "ip": str(address),
                            "latency_ms": None,
                            "source": "previous",
                            "retained": True,
                        }
                        if entry not in retained:
                            retained.append(entry)
        return {
            **scan,
            "entries": [*scan["entries"], *retained],
            "retained": retained,
            "can_apply": bool(scan["entries"]),
            "blocked_reason": ""
            if scan["entries"]
            else "本轮没有通过 TLS 校验的新映射，保留原文件",
            "outcome": (
                "failed" if not scan["entries"] else "partial" if scan["partial"] else "success"
            ),
        }

    def _write(self, scan: dict[str, Any]) -> dict[str, Any]:
        original = self._read_target()
        report = self._prepare(scan, original)
        for entry in report["retained"]:
            add_log(
                report["logs"],
                "warning",
                f"保留旧映射 {entry['ip']}，本轮未验证可用性",
                entry["domain"],
            )
        if not report["can_apply"]:
            raise ValueError(report["blocked_reason"])
        target = self.settings.hosts_path
        merged = merge_hosts(
            original, report["entries"], [entry["domain"] for entry in report["entries"]]
        )
        if merged == original:
            add_log(report["logs"], "info", "映射内容未变化，无需写入")
            return {**report, "changed": False, "backup": None}
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if target.exists():
            backup = target.with_name(target.name + ".bak." + uuid4().hex)
            shutil.copy2(target, backup)
            add_log(report["logs"], "info", f"已备份原文件: {backup}")
        descriptor, temporary = tempfile.mkstemp(prefix=".mediaforge-", dir=target.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(merged)
                stream.flush()
                os.fsync(stream.fileno())
            if target.exists():
                os.chmod(temporary_path, target.stat().st_mode)
            if (target.read_bytes() if target.exists() else b"") != original:
                raise ValueError("hosts 被其他程序修改，本次更新已取消")
            os.replace(temporary_path, target)
            add_log(report["logs"], "success", f"已写入 {target}")
        finally:
            temporary_path.unlink(missing_ok=True)
        return {**report, "changed": True, "backup": str(backup) if backup else None}

    def update(self, dry_run: bool = True) -> dict[str, Any]:
        with self.lock:

            def apply() -> dict[str, Any]:
                scan: dict[str, Any] = {
                    "entries": [],
                    "failures": [],
                    "unresolved": [],
                    "diagnostics": [],
                    "exports": {"ipv4": "", "ipv6": ""},
                    "logs": [],
                    "target": str(self.settings.hosts_path),
                }
                try:
                    original = self._read_target()
                    scan = self.scan()
                    if dry_run:
                        report = self._prepare(scan, original)
                        merge_hosts(
                            original,
                            report["entries"],
                            [item["domain"] for item in report["entries"]],
                        )
                        for entry in report["retained"]:
                            add_log(
                                report["logs"],
                                "warning",
                                f"将保留旧映射 {entry['ip']}，本轮未验证可用性",
                                entry["domain"],
                            )
                        add_log(report["logs"], "info", "预览结束，未写入 hosts 文件")
                    else:
                        report = self._write(scan)
                except (OSError, ValueError) as exc:
                    add_log(scan["logs"], "error", f"执行失败: {exc}")
                    raise OperationFailure(
                        str(exc),
                        {
                            **scan,
                            "dry_run": dry_run,
                            "outcome": "failed",
                            "can_apply": False,
                            "blocked_reason": str(exc),
                        },
                    ) from exc
                level = {"success": "success", "partial": "warning", "failed": "error"}[
                    report["outcome"]
                ]
                summary = {
                    "success": "本次执行成功",
                    "partial": "本次部分成功；未解析成功的地址族保留旧映射（如有）",
                    "failed": report["blocked_reason"],
                }[report["outcome"]]
                add_log(report["logs"], level, summary)
                return {"dry_run": dry_run, **report}

            return self.store.execute(
                "hosts.preview" if dry_run else "hosts.update",
                {"target": str(self.settings.hosts_path), "dry_run": dry_run},
                apply,
            )

    def scheduled_update(self) -> None:
        try:
            result = self.update(dry_run=False)
            if result["partial"]:
                logger.warning("hosts 部分更新，失败地址族保留旧映射: %s", result["unresolved"])
        except (OSError, ValueError, RuntimeError):
            logger.exception("定时 hosts 更新失败，详情见操作记录")
