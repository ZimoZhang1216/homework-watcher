from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User


AUTH_COOKIE_NAME = "hw_v2_session"
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60
PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 210_000
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,63}$")


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class CurrentUser:
    username: str
    display_name: str


def parse_urlencoded_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def normalize_username(username: str) -> str:
    return re.sub(r"\s+", "", username.strip().lower())


def validate_username(username: str) -> str:
    normalized = normalize_username(username)
    if not USERNAME_RE.fullmatch(normalized):
        raise AuthError("用户名只能使用 3-64 位小写字母、数字、点、下划线或短横线。")
    return normalized


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise AuthError("密码至少需要 8 位。")


def create_user(session: Session, *, username: str, password: str, display_name: str = "") -> User:
    normalized = validate_username(username)
    validate_password(password)
    existing = session.scalar(select(User).where(User.username == normalized))
    if existing is not None:
        raise AuthError("这个用户名已经存在。")
    now = datetime.now()
    user = User(
        username=normalized,
        display_name=display_name.strip() or normalized,
        password_hash=hash_password(password),
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.commit()
    return user


def authenticate_user(session: Session, *, username: str, password: str) -> User | None:
    normalized = normalize_username(username)
    if not normalized:
        return None
    user = session.scalar(select(User).where(User.username == normalized))
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def get_user(session: Session, username: str) -> User | None:
    normalized = normalize_username(username)
    if not normalized:
        return None
    return session.scalar(select(User).where(User.username == normalized))


def user_to_current(user: User) -> CurrentUser:
    return CurrentUser(username=user.username, display_name=user.display_name or user.username)


def hash_password(password: str) -> str:
    salt = secrets.token_urlsafe(18)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_HASH_ITERATIONS,
    )
    return "$".join(
        [
            PASSWORD_HASH_ALGORITHM,
            str(PASSWORD_HASH_ITERATIONS),
            salt,
            _b64encode(digest),
        ]
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, raw_iterations, salt, expected_digest = password_hash.split("$", 3)
        if algorithm != PASSWORD_HASH_ALGORITHM:
            return False
        iterations = int(raw_iterations)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return hmac.compare_digest(_b64encode(digest), expected_digest)


def create_session_token(username: str, secret_key: str, *, now: int | None = None) -> str:
    issued_at = now or int(time.time())
    payload = {
        "u": normalize_username(username),
        "iat": issued_at,
        "exp": issued_at + SESSION_TTL_SECONDS,
        "n": secrets.token_urlsafe(8),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(body, secret_key)
    return f"{body}.{signature}"


def read_session_username(token: str | None, secret_key: str, *, now: int | None = None) -> str | None:
    if not token or "." not in token:
        return None
    body, signature = token.split(".", 1)
    if not hmac.compare_digest(_sign(body, secret_key), signature):
        return None
    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    expires_at = int(payload.get("exp") or 0)
    if expires_at < (now or int(time.time())):
        return None
    username = normalize_username(str(payload.get("u") or ""))
    return username or None


def _sign(body: str, secret_key: str) -> str:
    digest = hmac.new(secret_key.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return _b64encode(digest)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
