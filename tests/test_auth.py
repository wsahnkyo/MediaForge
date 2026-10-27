import pytest
from fastapi import HTTPException

from mediaforge.auth import (
    AuthService,
    PasswordChange,
    UserCreate,
    UserUpdate,
    hash_password,
)


@pytest.fixture
def auth(sessions):
    service = AuthService(sessions, 12)
    service.create_user(UserCreate(username="admin", password="test-password-123", role="admin"))
    return service


def test_login_logout_and_password_change_revoke_sessions(auth):
    with pytest.raises(HTTPException) as error:
        auth.login("admin", "incorrect", "local")
    assert error.value.status_code == 401
    first = auth.login("admin", "test-password-123", "local")
    second = auth.login("admin", "test-password-123", "local")
    user = auth.current(first)
    auth.change_password(
        user["id"],
        PasswordChange(old_password="test-password-123", new_password="new-password-123"),
    )
    for token in [first, second]:
        with pytest.raises(HTTPException):
            auth.current(token)
    token = auth.login("admin", "new-password-123", "local")
    auth.logout(token)
    with pytest.raises(HTTPException):
        auth.current(token)


def test_disable_user_and_protect_own_admin(auth):
    token = auth.login("admin", "test-password-123", "local")
    admin = auth.current(token)
    with pytest.raises(ValueError, match="自己"):
        auth.update_user(admin["id"], UserUpdate(role="viewer", active=True), admin["id"])
    viewer = auth.create_user(
        UserCreate(username="viewer", password="viewer-password-123", role="viewer")
    )
    viewer_token = auth.login("viewer", "viewer-password-123", "local")
    auth.update_user(viewer["id"], UserUpdate(role="viewer", active=False), admin["id"])
    with pytest.raises(HTTPException):
        auth.current(viewer_token)
    with pytest.raises(HTTPException):
        auth.login("viewer", "viewer-password-123", "local")


def test_login_throttle(auth):
    for _ in range(10):
        with pytest.raises(HTTPException) as error:
            auth.login("missing", "wrong", "attacker")
        assert error.value.status_code == 401
    with pytest.raises(HTTPException) as error:
        auth.login("admin", "test-password-123", "attacker")
    assert error.value.status_code == 429


@pytest.mark.parametrize("password", ["1234", "p" * 128])
def test_password_boundaries_work_for_create_change_and_reset(auth, password):
    viewer = auth.create_user(UserCreate(username="shortuser", password=password))
    assert auth.current(auth.login("shortuser", password, "local"))["id"] == viewer["id"]
    auth.change_password(viewer["id"], PasswordChange(old_password=password, new_password="5678"))
    assert auth.current(auth.login("shortuser", "5678", "local"))["id"] == viewer["id"]
    admin = next(user for user in auth.users() if user["role"] == "admin")
    auth.update_user(
        viewer["id"], UserUpdate(role="viewer", active=True, password=password), admin["id"]
    )
    assert auth.current(auth.login("shortuser", password, "local"))["id"] == viewer["id"]


@pytest.mark.parametrize("password", ["", "123", "p" * 129])
def test_password_policy_rejects_out_of_range_values(password):
    with pytest.raises(ValueError):
        UserCreate(username="shortuser", password=password)
    with pytest.raises(ValueError):
        UserUpdate(role="viewer", active=True, password=password)
    with pytest.raises(ValueError):
        PasswordChange(old_password="5678", new_password=password)
    with pytest.raises(ValueError, match="4–128"):
        hash_password(password)
