import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from functools import cache

from starlette.responses import Response

from app.identity import principals

log = logging.getLogger(__name__)

COOKIE = "claims_qa_session"
MAX_AGE_S = 12 * 3600
CLOCK_SKEW_S = 60


@dataclass(frozen=True)
class Session:
    user_id: str
    session_id: str
    issued: int


@cache
def _secret() -> bytes:
    value = os.environ.get("SESSION_SECRET")
    if value:
        return value.encode()
    log.warning("SESSION_SECRET is not set, so sessions end when this process restarts")
    return secrets.token_bytes(32)


def _sign(payload: str) -> bytes:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest().encode()


def start(user_id: str) -> Session:
    """A fresh session. Choosing a user, even the same one again, always starts a new session id."""
    return Session(user_id, secrets.token_hex(16), int(time.time()))


def encode(session: Session) -> str:
    payload = f"{session.user_id}.{session.session_id}.{session.issued}"
    return f"{payload}.{_sign(payload).decode()}"


def decode(value: str | None, *, now: float | None = None) -> Session | None:
    """The session a cookie carries, or None when it is missing, altered, expired or names an unknown user."""
    payload, _, signature = (value or "").rpartition(".")
    if not payload or not hmac.compare_digest(signature.encode(), _sign(payload)):
        return None
    parts = payload.split(".")
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    user_id, session_id, issued = parts[0], parts[1], int(parts[2])
    age = (time.time() if now is None else now) - issued
    if user_id not in principals() or not -CLOCK_SKEW_S <= age <= MAX_AGE_S:
        return None
    return Session(user_id, session_id, issued)


def set_cookie(response: Response, session: Session, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE, encode(session), max_age=MAX_AGE_S, path="/", httponly=True, samesite="lax", secure=secure
    )
