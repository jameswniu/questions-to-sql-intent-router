import hmac
import ipaddress
import logging
import os
from typing import Literal, cast

log = logging.getLogger(__name__)

Mode = Literal["demo", "header"]
# oauth2-proxy's name for the signed-in user. Read only from a request that also carries the proxy's secret.
USER_HEADER = "X-Auth-Request-User"
# The proxy sends AUTH_PROXY_SECRET in this header, so a request that reaches the app without passing through the
# proxy can't name a user, whatever else it sends.
SECRET_HEADER = "X-Auth-Proxy-Secret"


class UnsafeDemo(RuntimeError):
    pass


class NoProxySecret(RuntimeError):
    pass


def identity_mode() -> Mode:
    """How a request says who is asking, from IDENTITY_MODE: header unless set, or demo, where anyone picks a user.

    Demo mode refuses to start unless BIND_HOST, the address uvicorn listens on, is a loopback address, or
    DEMO_ALLOW_NON_LOOPBACK=1 says something else keeps the port private. Header mode refuses to start without
    AUTH_PROXY_SECRET, the value the sign-in proxy sends in X-Auth-Proxy-Secret.
    """
    mode = os.environ.get("IDENTITY_MODE") or "header"
    if mode not in ("demo", "header"):
        raise ValueError(f"IDENTITY_MODE must be demo or header, not {mode!r}")
    if mode == "header" and not proxy_secret():
        raise NoProxySecret(
            f"IDENTITY_MODE=header takes the user from {USER_HEADER}, which anyone who reaches the port can send, so "
            f"it needs AUTH_PROXY_SECRET, a long random value the sign-in proxy sends in {SECRET_HEADER}. Set it "
            "here and in the proxy, or use IDENTITY_MODE=demo on loopback."
        )
    host = os.environ.get("BIND_HOST", "")
    if mode == "demo" and not _loopback(host):
        if os.environ.get("DEMO_ALLOW_NON_LOOPBACK") != "1":
            raise UnsafeDemo(
                f"IDENTITY_MODE=demo lets any caller act as any user, so it serves on loopback only, and BIND_HOST is "
                f"{host or 'not set'}. Bind to 127.0.0.1, or set DEMO_ALLOW_NON_LOOPBACK=1 if the port is private."
            )
        log.warning("Demo identity on %s: anyone who reaches the port can act as any user", host or "an unset host")
    return cast(Mode, mode)


def proxy_secret() -> str:
    """AUTH_PROXY_SECRET, or an empty string when it is unset, which no request can match."""
    return os.environ.get("AUTH_PROXY_SECRET", "")


def from_proxy(sent: str | None, secret: str) -> bool:
    """Whether a request's X-Auth-Proxy-Secret is the proxy's secret, compared in constant time."""
    # Production would verify an identity-aware proxy's signed JWT against its public keys instead of a shared secret.
    return bool(secret) and sent is not None and hmac.compare_digest(sent.encode(), secret.encode())


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
