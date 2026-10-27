import socket
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import dns.exception
import httpx
import pytest

from mediaforge.database import OperationFailure
from mediaforge.hosts import BEGIN, END, HostsService, merge_hosts


def report_for(entries, unresolved=()):
    return {
        "entries": entries,
        "failures": list(dict.fromkeys(item["domain"] for item in unresolved)),
        "unresolved": list(unresolved),
        "partial": bool(unresolved),
        "diagnostics": [],
        "logs": [],
        "exports": {"ipv4": "", "ipv6": ""},
    }


def mock_http(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("mediaforge.hosts.httpx.Client", lambda **kwargs: client)


def test_merge_is_idempotent_and_preserves_unmanaged_bytes():
    original = b"# ANSI: \xd6\xd0\xce\xc4\r\n127.0.0.1 localhost\r\n"
    entries = [{"domain": "api.themoviedb.org", "ip": "1.1.1.1", "latency_ms": 1}]
    merged = merge_hosts(original, entries, ["api.themoviedb.org"])
    assert merged.startswith(original)
    assert merged.count(BEGIN) == 1
    assert b"1.1.1.1\tapi.themoviedb.org\r\n" in merged
    assert merge_hosts(merged, entries, ["api.themoviedb.org"]) == merged
    newer = merge_hosts(
        merged + b"# tail\r\n",
        [{"domain": "api.themoviedb.org", "ip": "8.8.8.8"}],
        ["api.themoviedb.org"],
    )
    assert newer.startswith(original) and newer.endswith(b"# tail\r\n")
    assert b"1.1.1.1\t" not in newer


@pytest.mark.parametrize(
    "original",
    [
        BEGIN + b"\n",
        END + b"\n" + BEGIN,
        BEGIN + b"\n" + END + b"\n" + BEGIN,
        b"127.0.0.1 api.themoviedb.org\n",
        b"\xff\xfe1\0",
    ],
)
def test_refuse_malformed_blocks_conflicts_and_utf16(original):
    with pytest.raises(ValueError):
        merge_hosts(original, [], ["api.themoviedb.org"])


def test_update_backup_noop_and_failure_keep_original(settings, store, monkeypatch):
    service = HostsService(settings, store)
    original = b"127.0.0.1 localhost\n"
    settings.hosts_path.write_bytes(original)
    report = report_for([{"domain": "api.themoviedb.org", "ip": "1.1.1.1", "latency_ms": 1}])
    monkeypatch.setattr(service, "scan", lambda: deepcopy(report))
    assert service.update()["dry_run"] is True
    assert settings.hosts_path.read_bytes() == original
    result = service.update(False)
    assert Path(result["backup"]).read_bytes() == original
    changed = settings.hosts_path.read_bytes()
    assert service.update(False)["changed"] is False
    report = report_for([], [{"domain": "api.themoviedb.org", "record_type": "A"}])
    with pytest.raises(ValueError, match="保留原文件"):
        service.update(False)
    assert settings.hosts_path.read_bytes() == changed
    assert store.recent()[0]["status"] == "failed"
    assert store.recent()[0]["result"]["logs"][-1]["level"] == "error"
    assert any(record["kind"] == "hosts.preview" for record in store.recent())


def test_dns_failure_is_reported(settings, store, monkeypatch):
    def timeout(*args, **kwargs):
        raise dns.exception.Timeout

    monkeypatch.setattr("dns.resolver.Resolver.resolve", timeout)
    report = HostsService(settings, store).scan()
    assert report["failures"] == ["api.themoviedb.org"]
    assert report["diagnostics"][0]["warnings"]


def test_private_ips_and_tls_failures_are_rejected(settings, store, monkeypatch):
    class Answer:
        def __init__(self, value):
            self.value = value

        def to_text(self):
            return self.value

    monkeypatch.setattr(
        "dns.resolver.Resolver.resolve",
        lambda *args, **kwargs: [Answer("127.0.0.1"), Answer("1.1.1.1")],
    )
    probed = []

    def unavailable(address, **kwargs):
        probed.append(address)
        raise TimeoutError("TLS unavailable")

    monkeypatch.setattr(socket, "create_connection", unavailable)
    report = HostsService(settings, store).scan()
    assert probed == [("1.1.1.1", 443)]
    assert report["entries"] == []
    assert report["failures"]


def test_select_fastest_candidate_with_correct_tls_hostname(settings, store, monkeypatch):
    service = HostsService(settings, store)
    answers = [SimpleNamespace(to_text=lambda ip=ip: ip) for ip in ["1.1.1.1", "8.8.8.8"]]
    monkeypatch.setattr("dns.resolver.Resolver.resolve", lambda *args, **kwargs: answers)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: nullcontext(object()))
    hostnames = []

    def wrap(connection, server_hostname):
        hostnames.append(server_hostname)
        return nullcontext()

    service.tls_context = SimpleNamespace(wrap_socket=wrap)
    clock = iter([0.0, 0.02, 1.0, 1.01])
    monkeypatch.setattr("mediaforge.hosts.time.perf_counter", lambda: next(clock))
    report = service.scan()
    assert hostnames == ["api.themoviedb.org", "api.themoviedb.org"]
    assert report["entries"] == [
        {
            "domain": "api.themoviedb.org",
            "ip": "8.8.8.8",
            "latency_ms": 10.0,
            "source": "dns",
            "retained": False,
        }
    ]


def test_doh_retries_then_falls_back_and_filters_records(settings, store, monkeypatch):
    settings.dns_providers = ["google", "cloudflare"]
    service = HostsService(settings, store)
    queries = []

    def handler(request):
        queries.append(request.url.host)
        assert request.url.params["name"] == "api.themoviedb.org"
        assert request.url.params["type"] == "A"
        if request.url.host == "dns.google":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "Status": 0,
                "Answer": [
                    {"type": 5, "data": "alias.example.com"},
                    {"type": 28, "data": "2606:4700:4700::1111"},
                    {"type": 1, "data": "127.0.0.1"},
                    {"type": 1, "data": "224.0.0.1"},
                    {"type": 1, "data": "not-an-address"},
                    {"type": 1, "data": "1.1.1.1"},
                ],
            },
        )

    mock_http(monkeypatch, handler)
    probed = []
    monkeypatch.setattr(service, "_probe", lambda domain, address: probed.append(address) or 5.0)
    report = service.update()
    assert queries == ["dns.google", "dns.google", "cloudflare-dns.com"]
    assert probed == ["1.1.1.1"]
    assert report["entries"][0]["source"] == "cloudflare"
    assert report["outcome"] == "success"
    assert any("503" in item["message"] for item in report["logs"])
    assert report["logs"][-1]["level"] == "success"
    assert "1.1.1.1\tapi.themoviedb.org" in report["exports"]["ipv4"]
    assert report["exports"]["ipv6"] == ""


def test_provider_switches_when_resolved_ips_fail_tls(settings, store, monkeypatch):
    settings.dns_providers = ["google", "alidns"]
    service = HostsService(settings, store)

    def handler(request):
        ip = "1.1.1.1" if request.url.host == "dns.google" else "8.8.8.8"
        return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 1, "data": ip}]})

    def probe(domain, address):
        if address == "1.1.1.1":
            raise OSError("certificate verification failed")
        return 2.0

    mock_http(monkeypatch, handler)
    monkeypatch.setattr(service, "_probe", probe)
    report = service.scan()
    assert report["entries"][0]["ip"] == "8.8.8.8"
    assert report["entries"][0]["source"] == "alidns"


@pytest.mark.parametrize(
    "data",
    [
        {"Status": 3},
        {"Status": 0, "Answer": "bad"},
        {"Status": 0},
        ["not an object"],
    ],
)
def test_bad_doh_response_is_visible_not_silently_successful(settings, store, monkeypatch, data):
    settings.dns_providers = ["google"]
    mock_http(monkeypatch, lambda request: httpx.Response(200, json=data))
    report = HostsService(settings, store).update()
    assert report["outcome"] == "failed"
    assert not report["can_apply"]
    assert report["logs"][-1]["level"] == "error"
    assert store.recent()[0]["status"] == "failed"
    assert not settings.hosts_path.exists()


def test_doh_ipv6_queries_and_exports_are_family_specific(settings, store, monkeypatch):
    settings.dns_providers = ["google"]
    settings.ip_family = "ipv6"
    service = HostsService(settings, store)

    def handler(request):
        assert request.url.params["type"] == "AAAA"
        return httpx.Response(
            200,
            json={
                "Status": 0,
                "Answer": [
                    {"type": 1, "data": "1.1.1.1"},
                    {"type": 28, "data": "2606:4700:4700::1111"},
                    {"type": 28, "data": "2606:4700:4700::1001"},
                ],
            },
        )

    mock_http(monkeypatch, handler)
    monkeypatch.setattr(service, "_probe", lambda domain, address: 3.0)
    report = service.scan()
    assert len(report["entries"]) == 1
    assert report["entries"][0]["source"] == "google"
    assert report["exports"]["ipv6"]
    assert report["exports"]["ipv4"] == ""


def test_partial_update_preserves_failed_family_and_reports_stale_mapping(
    settings, store, monkeypatch
):
    settings.ip_family = "both"
    service = HostsService(settings, store)
    original = merge_hosts(
        b"127.0.0.1 localhost\n",
        [
            {"domain": "api.themoviedb.org", "ip": "8.8.8.8"},
            {"domain": "api.themoviedb.org", "ip": "2606:4700:4700::1111"},
        ],
        settings.hosts_domains,
    )
    settings.hosts_path.write_bytes(original)
    report = report_for(
        [{"domain": "api.themoviedb.org", "ip": "1.1.1.1", "latency_ms": 3}],
        [{"domain": "api.themoviedb.org", "record_type": "AAAA"}],
    )
    monkeypatch.setattr(service, "scan", lambda: deepcopy(report))
    preview = service.update()
    assert settings.hosts_path.read_bytes() == original
    assert preview["outcome"] == "partial"
    assert preview["retained"][0]["latency_ms"] is None
    result = service.update(False)
    content = settings.hosts_path.read_bytes()
    assert b"1.1.1.1\t" in content and b"8.8.8.8\t" not in content
    assert b"2606:4700:4700::1111\t" in content
    assert result["outcome"] == "partial"
    assert store.recent()[0]["status"] == "partial"
    assert any("保留旧映射" in item["message"] for item in result["logs"])


def test_write_failure_includes_page_logs_and_persisted_diagnostics(settings, store, monkeypatch):
    service = HostsService(settings, store)
    monkeypatch.setattr(
        service,
        "scan",
        lambda: report_for([{"domain": "api.themoviedb.org", "ip": "1.1.1.1", "latency_ms": 3}]),
    )

    def denied(*args):
        raise PermissionError("hosts is read-only")

    monkeypatch.setattr(service, "_write", denied)
    with pytest.raises(OperationFailure) as failure:
        service.update(False)
    assert failure.value.result["outcome"] == "failed"
    assert "hosts is read-only" in failure.value.result["logs"][-1]["message"]
    assert store.recent()[0]["result"]["logs"][-1]["level"] == "error"


def test_enabled_github_domains_use_same_validation_pipeline(settings, store, monkeypatch):
    settings.hosts_include_github = True
    settings.hosts_domains = ["github.com"]
    service = HostsService(settings, store)
    monkeypatch.setattr(service, "_resolve", lambda *args: ["1.1.1.1"])
    probed = []
    monkeypatch.setattr(service, "_probe", lambda domain, ip: probed.append(domain) or 3.0)
    result = service.scan()
    assert "raw.githubusercontent.com" in probed
    assert probed.count("github.com") == 1
    assert len(result["entries"]) == len(probed)
