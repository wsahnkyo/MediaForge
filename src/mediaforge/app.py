import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from mediaforge.auth import (
    COOKIE,
    AuthService,
    LoginRequest,
    PasswordChange,
    UserCreate,
    UserUpdate,
)
from mediaforge.config import HostsSource, Settings
from mediaforge.database import (
    OperationFailure,
    OperationStore,
    initialize_database,
    make_engine,
    operation_actor,
)
from mediaforge.files import FileAction, FileDeleteBatch, FileService, MediaRootsUpdate
from mediaforge.hosts import CHECKTMDB_URLS, HostsService
from mediaforge.logs import LogService
from mediaforge.metadata import (
    MetadataApply,
    MetadataCleanup,
    MetadataMappingInput,
    MetadataMappingsDelete,
    MetadataPreview,
    MetadataSearch,
    MetadataService,
)
from mediaforge.renaming import (
    RenameApplyRequest,
    RenamePreviewRequest,
    RenameReviewRequest,
    RenameService,
    RuleInput,
    RulesDelete,
    RulesUpdate,
)
from mediaforge.scrapers import ScraperSettingsUpdate
from mediaforge.video_info import SCAN_INTERVAL_MINUTES, VideoInfoRequest, VideoInfoService

logger = logging.getLogger(__name__)
WEB = Path(__file__).with_name("web")


class HostsUpdate(BaseModel):
    dry_run: bool = True
    source: HostsSource | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url.get_secret_value())
    sessions = sessionmaker(engine, expire_on_commit=False)
    store = OperationStore(sessions)
    auth = AuthService(sessions, settings.session_hours)
    files = FileService(settings.media_roots, store)
    metadata = MetadataService(settings, files, store)
    renaming = RenameService(files, store)
    video_info = VideoInfoService(files, store)
    hosts = HostsService(settings, store)
    logs = LogService(settings)
    scheduler = BackgroundScheduler()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            logs.start()
            initialize_database(engine)
            files.load_roots(settings)
            logger.info("MediaForge 启动，数据库初始化完成")
            if settings.hosts_interval_seconds:
                scheduler.add_job(
                    hosts.scheduled_update,
                    "interval",
                    seconds=settings.hosts_interval_seconds,
                    max_instances=1,
                    coalesce=True,
                )
            scheduler.add_job(logs.cleanup, "interval", minutes=10, max_instances=1)
            scheduler.add_job(
                video_info.request_scan,
                "interval",
                minutes=SCAN_INTERVAL_MINUTES,
                next_run_time=datetime.now(UTC),
                max_instances=1,
                coalesce=True,
                id="video-info",
            )
            scheduler.start()
            yield
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=True)
            video_info.close()
            engine.dispose()
            logger.info("MediaForge 已停止")
            logs.close()

    app = FastAPI(title="MediaForge", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.files = files
    app.state.metadata = metadata
    app.state.renaming = renaming
    app.state.video_info = video_info
    app.state.hosts = hosts
    app.state.auth = auth
    app.state.logs = logs

    @app.middleware("http")
    async def request_security(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "DELETE"}:
            if request.headers.get("x-mediaforge-request") != "1":
                return JSONResponse(status_code=403, content={"detail": "缺少同源请求校验头"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self'; img-src 'self' data:; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    async def current_user(request: Request):
        user = await run_in_threadpool(auth.current, request.cookies.get(COOKIE))
        token = operation_actor.set(user["username"])
        try:
            yield user
        finally:
            operation_actor.reset(token)

    def require_admin(user: Annotated[dict, Depends(current_user)]) -> dict:
        if user["role"] != "admin":
            raise HTTPException(403, "此操作需要管理员权限")
        return user

    secured = [Depends(current_user)]
    admin = [Depends(require_admin)]

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request, response: Response):
        client = request.client.host if request.client else "local"
        token = auth.login(body.username, body.password, client)
        response.set_cookie(
            COOKIE,
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        return auth.current(token)

    @app.get("/api/auth/me", dependencies=secured)
    def me(user: Annotated[dict, Depends(current_user)]):
        return user

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/", samesite="strict")
        return {"message": "已退出登录"}

    @app.post("/api/auth/password", dependencies=secured)
    def password_change(
        body: PasswordChange, user: Annotated[dict, Depends(current_user)], response: Response
    ):
        auth.change_password(user["id"], body)
        response.delete_cookie(COOKIE, path="/", samesite="strict")
        return {"message": "密码已修改，请重新登录"}

    @app.get("/api/users", dependencies=admin)
    def users():
        return auth.users()

    @app.post("/api/users", dependencies=admin)
    def create_user(body: UserCreate):
        return store.execute(
            "user.create",
            {"username": body.username, "role": body.role},
            lambda: auth.create_user(body),
        )

    @app.put("/api/users/{user_id}", dependencies=admin)
    def update_user(user_id: str, body: UserUpdate, user: Annotated[dict, Depends(current_user)]):
        return store.execute(
            "user.update",
            {
                "user_id": user_id,
                "role": body.role,
                "active": body.active,
                "password_reset": body.password is not None,
            },
            lambda: auth.update_user(user_id, body, user["id"]),
        )

    @app.exception_handler(ValueError)
    async def invalid_input(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(OperationFailure)
    async def operation_failed(request: Request, exc: OperationFailure):
        return JSONResponse(status_code=400, content={"detail": str(exc), "report": exc.result})

    @app.exception_handler(OSError)
    async def filesystem_error(request: Request, exc: OSError):
        logger.warning("文件/网络操作失败: %s", exc)
        if isinstance(exc, FileNotFoundError):
            code = 404
        elif isinstance(exc, FileExistsError):
            code = 409
        elif isinstance(exc, PermissionError):
            code = 403
        else:
            code = 400
        return JSONResponse(status_code=code, content={"detail": str(exc)})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        # Do not expose SQL, parameters or connection credentials to browser clients.
        logger.error("数据库操作失败: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "数据库不可用。若已提交文件操作，请先检查磁盘和操作记录，勿直接重试。"
            },
        )

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/api/health")
    def health():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/api/modules", dependencies=secured)
    def modules():
        return [
            {"id": "downloads", "name": "影视下载", "status": "planned"},
            {"id": "metadata", "name": "元数据刮削", "status": "available"},
            {"id": "files", "name": "文件管理", "status": "available"},
            {"id": "hosts", "name": "Hosts 更新", "status": "available"},
            {"id": "video-info", "name": "视频参数识别", "status": "available"},
        ]

    @app.get("/api/config", dependencies=secured)
    def config():
        with files.lock:
            roots = [{"id": i, "path": str(root)} for i, root in enumerate(files.roots)]
            roots_version = files.roots_version
        return {
            "roots": roots,
            "roots_version": roots_version,
            "tmdb_key_configured": bool(metadata.scrapers.credential("tmdb")),
            "hosts_path": str(settings.hosts_path),
            "hosts_interval_seconds": settings.hosts_interval_seconds,
            "hosts_source": settings.hosts_source,
            "hosts_subscription_urls": CHECKTMDB_URLS,
            "dns_servers": settings.dns_servers,
            "dns_providers": settings.dns_providers,
            "hosts_domains": hosts.domains,
            "hosts_include_github": settings.hosts_include_github,
            "log_path": str(settings.log_path),
            "log_retention_days": settings.log_retention_days,
            "log_max_total_mb": settings.log_max_total_mb,
            "log_max_file_mb": settings.log_max_file_mb,
            "ip_family": settings.ip_family,
        }

    @app.get("/api/config/media-folders", dependencies=admin)
    def browse_media_folders(path: str | None = Query(default=None, max_length=2000)):
        return files.browse_media_folders(path, settings)

    @app.put("/api/config/media-roots", dependencies=admin)
    def update_media_roots(body: MediaRootsUpdate):
        with files.lock:
            result = files.save_roots(body, settings)
            metadata.previews.clear()
            renaming.previews.clear()
            video_info.request_scan()
            return result

    @app.get("/api/files", dependencies=secured)
    def list_files(root: int = 0, path: str = "", roots_version: str | None = None):
        return files.list(root, path, roots_version)

    @app.post("/api/files/action", dependencies=admin)
    def file_action(action: FileAction):
        return files.execute(action)

    @app.post("/api/files/delete-batch", dependencies=admin)
    def delete_files(body: FileDeleteBatch):
        return files.delete_batch(body)

    @app.get("/api/renaming/rules", dependencies=secured)
    def rename_rules():
        return [rule.model_dump() for rule in renaming.rules()]

    @app.put("/api/renaming/rules", dependencies=admin)
    def save_rename_rules(body: RulesUpdate):
        return renaming.save_rules(body)

    @app.post("/api/renaming/rules", dependencies=admin)
    def add_rename_rule(body: RuleInput):
        return renaming.add_rule(body)

    @app.post("/api/renaming/rules/delete", dependencies=admin)
    def delete_rename_rules(body: RulesDelete):
        return renaming.delete_rules(body)

    @app.post("/api/renaming/preview", dependencies=admin)
    def preview_renaming(body: RenamePreviewRequest):
        return renaming.preview(body)

    @app.post("/api/renaming/review", dependencies=admin)
    def review_renaming(body: RenameReviewRequest):
        return renaming.review(body)

    @app.post("/api/renaming/apply", dependencies=admin)
    def apply_renaming(body: RenameApplyRequest):
        with files.lock:
            result = renaming.apply(body)
            metadata.previews.clear()
            return result

    @app.post("/api/metadata/preview", dependencies=admin)
    def metadata_preview(body: MetadataPreview):
        return metadata.preview(body)

    @app.get("/api/video-info/status", dependencies=secured)
    def video_info_status():
        return video_info.status()

    @app.get("/api/video-info", dependencies=secured)
    def video_information(root: int = 0, path: str = "", roots_version: str | None = None):
        return video_info.listing(
            VideoInfoRequest(
                root=root,
                path=path,
                roots_version=roots_version,
            )
        )

    @app.post("/api/video-info/scan", dependencies=admin)
    def inspect_videos(body: VideoInfoRequest):
        return video_info.scan(body)

    @app.post("/api/video-info/scan-all", dependencies=admin)
    def inspect_all_videos():
        return video_info.request_scan()

    @app.get("/api/metadata/scrapers", dependencies=secured)
    def metadata_scrapers():
        return metadata.scrapers.configurations()

    @app.put("/api/metadata/scrapers", dependencies=admin)
    def update_metadata_scrapers(body: ScraperSettingsUpdate):
        return metadata.save_scrapers(body)

    @app.delete("/api/metadata/scrapers/{scraper_id}/key", dependencies=admin)
    def clear_metadata_key(scraper_id: str):
        return metadata.clear_scraper_key(scraper_id)

    @app.post("/api/metadata/search", dependencies=admin)
    def metadata_search(body: MetadataSearch):
        return metadata.search(body)

    @app.get("/api/metadata/mappings", dependencies=secured)
    def metadata_mappings():
        return metadata.mappings()

    @app.post("/api/metadata/mappings/cleanup", dependencies=admin)
    def cleanup_metadata_mappings(body: MetadataCleanup):
        return metadata.cleanup_mappings(body)

    @app.post("/api/metadata/mappings", dependencies=admin)
    def save_metadata_mapping(body: MetadataMappingInput):
        return metadata.save_mapping(body)

    @app.delete("/api/metadata/mappings/{mapping_id}", dependencies=admin)
    def delete_metadata_mapping(mapping_id: str):
        return metadata.delete_mapping(mapping_id)

    @app.post("/api/metadata/mappings/delete", dependencies=admin)
    def delete_metadata_mappings(body: MetadataMappingsDelete):
        return metadata.delete_mappings(body)

    @app.post("/api/metadata/apply", dependencies=admin)
    def metadata_apply(body: MetadataApply):
        return metadata.apply(body)

    @app.post("/api/hosts/update", dependencies=secured)
    def update_hosts(body: HostsUpdate, user: Annotated[dict, Depends(current_user)]):
        if not body.dry_run and user["role"] != "admin":
            raise HTTPException(403, "更新 hosts 需要管理员权限")
        return hosts.update(body.dry_run, body.source)

    @app.get("/api/operations", dependencies=secured)
    def operations(summary: bool = False):
        return store.recent(summary=summary)

    @app.get("/api/operations/{operation_id}", dependencies=secured)
    def operation_detail(operation_id: str):
        record = store.get(operation_id)
        if record is None:
            raise HTTPException(404, "操作记录不存在")
        return record

    @app.get("/api/logs", dependencies=secured)
    def runtime_logs(limit: int = Query(default=200, ge=1, le=1000)):
        return logs.tail(limit)

    return app
