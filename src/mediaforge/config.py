import os
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DOMAIN_PATTERN = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
HostsSource = Literal["dns", "checktmdb"]


def system_hosts_path() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/drivers/etc/hosts"
    return Path("/etc/hosts")


def validate_media_targets(roots: list[Path], hosts_path: Path, log_path: Path) -> None:
    for root in roots:
        if hosts_path.resolve().is_relative_to(root):
            raise ValueError("hosts 文件不能放在可操作的媒体目录中")
        if log_path.resolve().is_relative_to(root):
            raise ValueError("日志文件不能放在可操作的媒体目录中")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEDIAFORGE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: SecretStr
    media_roots: list[Path] = Field(default_factory=list)
    tmdb_api_key: SecretStr | None = None
    hosts_path: Path = Path("data/hosts.preview")
    hosts_allow_system: bool = False
    hosts_interval_seconds: int = Field(default=0, ge=0)
    hosts_source: HostsSource = "dns"
    dns_servers: list[str] = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    dns_providers: list[Literal["google", "cloudflare", "alidns", "dns"]] = Field(
        default_factory=lambda: ["google", "cloudflare", "alidns", "dns"],
        min_length=1,
        max_length=4,
    )
    dns_attempts: int = Field(default=2, ge=1, le=3)
    hosts_include_github: bool = True
    hosts_domains: list[str] = Field(
        default_factory=lambda: [
            "tmdb.org",
            "api.tmdb.org",
            "files.tmdb.org",
            "themoviedb.org",
            "api.themoviedb.org",
            "www.themoviedb.org",
            "auth.themoviedb.org",
            "image.tmdb.org",
            "images.tmdb.org",
            "imdb.com",
            "www.imdb.com",
            "secure.imdb.com",
            "s.media-imdb.com",
            "us.dd.imdb.com",
            "www.imdb.to",
            "origin-www.imdb.com",
            "ia.media-imdb.com",
            "thetvdb.com",
            "api.thetvdb.com",
            "f.media-amazon.com",
            "imdb-video.media-imdb.com",
            "webservice.fanart.tv",
            "images.fanart.tv",
            "assets.fanart.tv",
            "fanart.tv",
            "api.trakt.tv",
            "api-staging.trakt.tv",
            "trakt.tv",
        ]
    )
    ip_family: Literal["ipv4", "ipv6", "both"] = "ipv4"
    network_timeout: float = Field(default=3, gt=0, le=30)
    probe_workers: int = Field(default=8, ge=1, le=32)
    session_hours: int = Field(default=12, ge=1, le=168)
    cookie_secure: bool = False
    log_path: Path = Path("logs/mediaforge.log")
    log_retention_days: int = Field(default=7, ge=1, le=3650)
    log_max_total_mb: int = Field(default=1024, ge=1)
    log_max_file_mb: int = Field(default=50, ge=1)

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("数据库必须使用 postgresql+psycopg:// 连接")
        return value

    @field_validator("hosts_domains")
    @classmethod
    def valid_domains(cls, value: list[str]) -> list[str]:
        domains = list(dict.fromkeys(domain.lower().rstrip(".") for domain in value))
        if (
            not domains
            or len(domains) > 100
            or any(not DOMAIN_PATTERN.fullmatch(domain) for domain in domains)
        ):
            raise ValueError("hosts 域名列表必须包含 1–100 个合法域名")
        return domains

    @field_validator("dns_servers")
    @classmethod
    def valid_dns(cls, value: list[str]) -> list[str]:
        from ipaddress import ip_address

        if not value or len(value) > 4:
            raise ValueError("需要配置 1–4 个 DNS 服务器")
        for server in value:
            ip_address(server)
        return value

    @field_validator("media_roots")
    @classmethod
    def distinct_roots(cls, value: list[Path]) -> list[Path]:
        roots = [path.expanduser().resolve() for path in value]
        if len(set(roots)) != len(roots):
            raise ValueError("媒体目录不能重复")
        return roots

    @model_validator(mode="after")
    def validate_targets(self) -> "Settings":
        if 0 < self.hosts_interval_seconds < 60:
            raise ValueError("自动更新间隔至少 60 秒，或设置为 0 关闭")
        self.hosts_path = self.hosts_path.expanduser().absolute()
        self.log_path = self.log_path.expanduser().absolute()
        if self.log_max_total_mb < self.log_max_file_mb:
            raise ValueError("日志总大小上限不能小于单文件轮转大小")
        if self.log_path.resolve() == self.hosts_path.resolve():
            raise ValueError("日志文件不能与 hosts 文件使用相同路径")
        if self.hosts_path.resolve() == system_hosts_path().resolve():
            if not self.hosts_allow_system:
                raise ValueError("修改系统 hosts 前必须设置 HOSTS_ALLOW_SYSTEM=true")
        validate_media_targets(self.media_roots, self.hosts_path, self.log_path)
        return self
