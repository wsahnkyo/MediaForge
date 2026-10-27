import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mediaforge.database import LoginSession, User

USERNAME = re.compile(r"[a-zA-Z0-9_.-]{3,64}\Z")
COOKIE = "mediaforge_session"
ITERATIONS = 600_000


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=4, max_length=128)
    role: Literal["admin", "viewer"] = "viewer"


class UserUpdate(BaseModel):
    role: Literal["admin", "viewer"]
    active: bool
    password: str | None = Field(default=None, min_length=4, max_length=128)


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=4, max_length=128)


def hash_password(password: str) -> str:
    if not 4 <= len(password) <= 128:
        raise ValueError("密码长度必须为 4–128 个字符")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    algorithm, iterations, salt, expected = stored.split("$")
    if algorithm != "pbkdf2_sha256":
        raise ValueError("不支持的密码摘要格式")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(actual, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_info(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "active": user.active,
    }


class AuthService:
    def __init__(self, sessions: sessionmaker[Session], session_hours: int):
        self.sessions = sessions
        self.session_hours = session_hours
        self.lock = Lock()
        self.failures: dict[str, list[float]] = {}
        self.dummy_hash = hash_password(secrets.token_urlsafe(32))

    def login(self, username: str, password: str, client: str) -> str:
        with self.lock:
            now = monotonic()
            self.failures = {
                key: [value for value in times if now - value < 300]
                for key, times in self.failures.items()
                if times and now - times[-1] < 300
            }
            attempts = self.failures.setdefault(client, [])
            if len(attempts) >= 10:
                raise HTTPException(429, "登录尝试过多，请 5 分钟后重试")
            # Reserve the attempt before password verification to bound parallel requests.
            attempts.append(now)
        with self.sessions() as session:
            user = session.scalar(select(User).where(User.username == username).with_for_update())
            valid = verify_password(password, user.password_hash if user else self.dummy_hash)
            if not valid or user is None or not user.active:
                raise HTTPException(401, "用户名或密码错误，或账号已停用")
            with self.lock:
                self.failures.pop(client, None)
            session.execute(
                delete(LoginSession).where(LoginSession.expires_at <= datetime.now(UTC))
            )
            token = secrets.token_urlsafe(32)
            session.add(
                LoginSession(
                    token_hash=token_hash(token),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=self.session_hours),
                )
            )
            session.commit()
            return token

    def current(self, token: str | None) -> dict:
        if not token or len(token) > 128:
            raise HTTPException(401, "请先登录")
        with self.sessions() as session:
            user = session.scalar(
                select(User)
                .join(LoginSession, LoginSession.user_id == User.id)
                .where(
                    LoginSession.token_hash == token_hash(token),
                    LoginSession.expires_at > datetime.now(UTC),
                    User.active.is_(True),
                )
            )
            if user is None:
                raise HTTPException(401, "登录已失效，请重新登录")
            return user_info(user)

    def logout(self, token: str | None) -> None:
        if token:
            with self.sessions() as session:
                session.execute(
                    delete(LoginSession).where(LoginSession.token_hash == token_hash(token))
                )
                session.commit()

    def users(self) -> list[dict]:
        with self.sessions() as session:
            return [
                user_info(user) for user in session.scalars(select(User).order_by(User.username))
            ]

    def create_user(self, request: UserCreate) -> dict:
        if not USERNAME.fullmatch(request.username):
            raise ValueError("用户名需为 3–64 位英文字母、数字、下划线、点或短横线")
        with self.sessions() as session:
            if session.scalar(select(User).where(User.username == request.username)):
                raise ValueError("用户名已存在")
            user = User(
                username=request.username,
                password_hash=hash_password(request.password),
                role=request.role,
            )
            session.add(user)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("用户名已存在") from exc
            return user_info(user)

    def update_user(self, user_id: str, request: UserUpdate, current_id: str) -> dict:
        if user_id == current_id and (not request.active or request.role != "admin"):
            raise ValueError("不能停用自己或移除自己的管理员权限")
        with self.sessions() as session:
            # Serializes role changes so concurrent requests cannot remove the last admin.
            admins = list(
                session.scalars(
                    select(User)
                    .where(User.role == "admin", User.active.is_(True))
                    .with_for_update()
                )
            )
            user = session.get(User, user_id, with_for_update=True)
            if user is None:
                raise ValueError("用户不存在")
            if (
                user.role == "admin"
                and user.active
                and (not request.active or request.role != "admin")
            ):
                if len(admins) <= 1:
                    raise ValueError("必须保留至少一个启用的管理员")
            user.role = request.role
            user.active = request.active
            if request.password is not None:
                user.password_hash = hash_password(request.password)
            session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            session.commit()
            return user_info(user)

    def change_password(self, user_id: str, request: PasswordChange) -> None:
        with self.sessions() as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None or not verify_password(request.old_password, user.password_hash):
                raise HTTPException(400, "原密码错误")
            user.password_hash = hash_password(request.new_password)
            session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            session.commit()
