from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


AUTH_SESSION_COOKIE = "__Host-learning_agent_session"
AUTH_CSRF_COOKIE = "__Host-learning_agent_csrf"
AUTH_CSRF_HEADER = "x-csrf-token"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SESSION_VERSION = 1
_BEARER_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/\-]+=*$")


class DeploymentConfigError(ValueError):
    """A local/server deployment boundary is incomplete or ambiguous."""


def _contains_forbidden_control(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or character in {"\u2028", "\u2029"}
        for character in value
    )


def _normalized_hostname(value: str) -> str:
    normalized = value.rstrip(".").casefold()
    if not normalized:
        raise DeploymentConfigError("host name cannot be empty")
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DeploymentConfigError("host name is not valid IDNA") from exc


@dataclass(frozen=True)
class ParsedOrigin:
    value: str
    scheme: str
    hostname: str
    port: int


def parse_origin(value: str, *, require_https: bool) -> ParsedOrigin:
    raw = value.strip()
    if not raw or _contains_forbidden_control(raw):
        raise DeploymentConfigError("origin is empty or contains control characters")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise DeploymentConfigError("origin has an invalid port") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or (require_https and scheme != "https"):
        raise DeploymentConfigError("origin must use the required HTTP(S) scheme")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.hostname.endswith(".")
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DeploymentConfigError("origin must contain only scheme and authority")
    hostname = _normalized_hostname(parsed.hostname)
    effective_port = port or (443 if scheme == "https" else 80)
    host_text = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    authority = host_text if effective_port == default_port else f"{host_text}:{effective_port}"
    return ParsedOrigin(
        value=f"{scheme}://{authority}",
        scheme=scheme,
        hostname=hostname,
        port=effective_port,
    )


def parse_cors_origins(
    raw_origins: str,
    *,
    mode: str,
    public_origin: ParsedOrigin | None,
) -> tuple[str, ...]:
    values = [value.strip() for value in raw_origins.split(",") if value.strip()]
    if not values:
        raise DeploymentConfigError("at least one explicit CORS origin is required")
    if len(values) != len(set(values)):
        raise DeploymentConfigError("CORS origins must be unique")
    parsed = tuple(
        parse_origin(value, require_https=mode == "server") for value in values
    )
    if len(parsed) != len({origin.value for origin in parsed}):
        raise DeploymentConfigError("CORS origins must be unique after normalization")
    if mode == "local":
        if any(origin.hostname not in _LOCAL_HOSTS for origin in parsed):
            raise DeploymentConfigError("local mode CORS origins must be loopback hosts")
    elif public_origin is None or any(origin.value != public_origin.value for origin in parsed):
        raise DeploymentConfigError("server mode CORS origin must equal public origin")
    return tuple(origin.value for origin in parsed)


def _token_text(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value or "")


@dataclass(frozen=True)
class DeploymentPolicy:
    mode: str
    public_origin: ParsedOrigin | None
    cors_origins: tuple[str, ...]
    session_ttl_seconds: int
    _auth_token: bytes = field(default=b"", repr=False)

    @classmethod
    def from_settings(cls, settings: Any) -> "DeploymentPolicy":
        mode = str(settings.DEPLOYMENT_MODE).strip().casefold()
        if mode not in {"local", "server"}:
            raise DeploymentConfigError("DEPLOYMENT_MODE must be local or server")
        token = _token_text(settings.SERVER_AUTH_TOKEN)
        if _contains_forbidden_control(token):
            raise DeploymentConfigError("server authentication token contains controls")
        public_raw = str(settings.SERVER_PUBLIC_ORIGIN or "").strip()
        public_origin = (
            parse_origin(public_raw, require_https=True) if public_raw else None
        )
        if mode == "server":
            if len(token.encode("utf-8")) < 32:
                raise DeploymentConfigError(
                    "server mode requires an authentication token of at least 32 bytes"
                )
            if not _BEARER_TOKEN_PATTERN.fullmatch(token):
                raise DeploymentConfigError(
                    "server authentication token must use bearer-token characters"
                )
            if public_origin is None:
                raise DeploymentConfigError("server mode requires SERVER_PUBLIC_ORIGIN")
        elif public_origin is not None:
            raise DeploymentConfigError("SERVER_PUBLIC_ORIGIN is valid only in server mode")
        cors_origins = parse_cors_origins(
            str(settings.CORS_ORIGINS),
            mode=mode,
            public_origin=public_origin,
        )
        ttl = int(settings.SERVER_SESSION_TTL_SECONDS)
        return cls(
            mode=mode,
            public_origin=public_origin,
            cors_origins=cors_origins,
            session_ttl_seconds=ttl,
            _auth_token=token.encode("utf-8"),
        )

    @property
    def authentication_enabled(self) -> bool:
        return self.mode == "server"

    def authenticate_bearer(self, authorization: str | None) -> bool:
        if not self.authentication_enabled or not authorization:
            return False
        scheme, separator, supplied = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not supplied:
            return False
        return hmac.compare_digest(supplied.encode("utf-8"), self._auth_token)

    def host_is_allowed(self, authority: str | None) -> bool:
        parsed = _parse_host_authority(authority)
        if parsed is None:
            return False
        hostname, port = parsed
        if self.mode == "local":
            return hostname in _LOCAL_HOSTS
        assert self.public_origin is not None
        effective_port = port or (443 if self.public_origin.scheme == "https" else 80)
        return (
            hostname == self.public_origin.hostname
            and effective_port == self.public_origin.port
        )

    def origin_is_allowed(self, origin: str | None) -> bool:
        if not origin:
            return True
        try:
            normalized = parse_origin(origin, require_https=self.mode == "server").value
        except DeploymentConfigError:
            return False
        return normalized in self.cors_origins

    def issue_session(self, *, now: int | None = None) -> tuple[str, str, int]:
        if not self.authentication_enabled:
            raise DeploymentConfigError("authentication sessions exist only in server mode")
        issued_at = int(time.time() if now is None else now)
        expires_at = issued_at + self.session_ttl_seconds
        csrf_token = secrets.token_urlsafe(32)
        payload = json.dumps(
            {
                "csrf": hashlib.sha256(csrf_token.encode("utf-8")).hexdigest(),
                "exp": expires_at,
                "iat": issued_at,
                "nonce": secrets.token_urlsafe(18),
                "v": _SESSION_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = _base64url_encode(payload)
        signature = _base64url_encode(self._sign(encoded.encode("ascii")))
        return f"{encoded}.{signature}", csrf_token, expires_at

    def verify_session(self, value: str | None, *, now: int | None = None) -> dict[str, Any] | None:
        if not self.authentication_enabled or not value or len(value) > 2048:
            return None
        encoded, separator, supplied_signature = value.partition(".")
        if not separator:
            return None
        expected_signature = _base64url_encode(self._sign(encoded.encode("ascii", "ignore")))
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        try:
            payload = json.loads(_base64url_decode(encoded))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        current = int(time.time() if now is None else now)
        if (
            not isinstance(payload, dict)
            or payload.get("v") != _SESSION_VERSION
            or type(payload.get("iat")) is not int
            or type(payload.get("exp")) is not int
            or payload["iat"] > current + 30
            or payload["exp"] <= current
            or payload["exp"] - payload["iat"] != self.session_ttl_seconds
            or not isinstance(payload.get("nonce"), str)
            or not isinstance(payload.get("csrf"), str)
            or len(payload["csrf"]) != 64
        ):
            return None
        return payload

    def csrf_is_valid(
        self,
        payload: dict[str, Any],
        *,
        header_token: str | None,
        cookie_token: str | None,
    ) -> bool:
        if not header_token or not cookie_token:
            return False
        if not hmac.compare_digest(header_token, cookie_token):
            return False
        observed = hashlib.sha256(header_token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(observed, str(payload.get("csrf", "")))

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(
            self._auth_token,
            b"learning-agent-server-session-v1\0" + payload,
            hashlib.sha256,
        ).digest()


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    ).decode("utf-8")


def _parse_host_authority(authority: str | None) -> tuple[str, int | None] | None:
    if not authority or _contains_forbidden_control(authority) or any(
        character.isspace() for character in authority
    ):
        return None
    try:
        parsed = urlsplit(f"//{authority}")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.hostname is None
        or parsed.hostname.endswith(".")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        return _normalized_hostname(parsed.hostname), port
    except DeploymentConfigError:
        return None


def validate_bind_host(policy: DeploymentPolicy, host: str) -> None:
    """Reject any non-loopback bind in local mode before Uvicorn starts."""

    if policy.mode == "server":
        return
    normalized = host.strip()
    if not normalized or _contains_forbidden_control(normalized):
        raise DeploymentConfigError("local bind host is invalid")
    try:
        addresses = {ipaddress.ip_address(normalized)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise DeploymentConfigError("local bind host cannot be resolved safely") from exc
    if not addresses or any(not address.is_loopback for address in addresses):
        raise DeploymentConfigError("local mode accepts only loopback bind addresses")


def _scope_uses_loopback(scope: Scope) -> bool:
    server = scope.get("server")
    if server is None:
        return False
    host = str(server[0]).strip().casefold()
    if host in {"localhost", "testserver"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_testclient_authority(scope: Scope, authority: str | None) -> bool:
    """Permit Starlette's synthetic host only inside its synthetic ASGI scope."""

    server = scope.get("server")
    return bool(
        server
        and str(server[0]).casefold() == "testserver"
        and authority is not None
        and _parse_host_authority(authority) == ("testserver", None)
    )


async def _close_websocket(send: Send, code: int = 1008) -> None:
    await send({"type": "websocket.close", "code": code})


class DeploymentBoundaryMiddleware:
    """Enforce Host, local socket and server authentication before routing."""

    def __init__(self, app: ASGIApp, *, policy: DeploymentPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not self.policy.host_is_allowed(headers.get("host")) and not (
            self.policy.mode == "local"
            and _is_testclient_authority(scope, headers.get("host"))
        ):
            if scope["type"] == "websocket":
                await _close_websocket(send)
            else:
                await PlainTextResponse("Invalid host header", status_code=400)(
                    scope, receive, send
                )
            return
        if self.policy.mode == "server":
            expected_scheme = "https" if scope["type"] == "http" else "wss"
            if str(scope.get("scheme", "")).casefold() != expected_scheme:
                if scope["type"] == "websocket":
                    await _close_websocket(send)
                else:
                    await JSONResponse(
                        {"detail": "server mode requires an HTTPS request scope"},
                        status_code=400,
                    )(scope, receive, send)
                return
        if self.policy.mode == "local" and not _scope_uses_loopback(scope):
            if scope["type"] == "websocket":
                await _close_websocket(send)
            else:
                await JSONResponse(
                    {"detail": "local mode requires a loopback connection"},
                    status_code=403,
                )(scope, receive, send)
            return
        if self.policy.mode != "server" or not str(scope.get("path", "")).startswith(
            "/api/v1"
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await _close_websocket(send)
            return
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method == "OPTIONS" or (method == "POST" and path == "/api/v1/auth/session"):
            await self.app(scope, receive, send)
            return
        if self.policy.authenticate_bearer(headers.get("authorization")):
            scope.setdefault("state", {})["authentication"] = "bearer"
            await self.app(scope, receive, send)
            return
        cookies = _parse_cookie_header(headers.get("cookie"))
        session = self.policy.verify_session(cookies.get(AUTH_SESSION_COOKIE))
        if session is None:
            await JSONResponse(
                {"detail": "authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        request_origin = headers.get("origin")
        if method in _UNSAFE_METHODS and (
            request_origin is None
            or not self.policy.origin_is_allowed(request_origin)
            or not self.policy.csrf_is_valid(
                session,
                header_token=headers.get(AUTH_CSRF_HEADER),
                cookie_token=cookies.get(AUTH_CSRF_COOKIE),
            )
        ):
            await JSONResponse(
                {"detail": "CSRF validation failed"}, status_code=403
            )(scope, receive, send)
            return
        scope.setdefault("state", {})["authentication"] = "cookie"
        await self.app(scope, receive, send)


def _parse_cookie_header(raw: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for fragment in (raw or "").split(";"):
        name, separator, value = fragment.strip().partition("=")
        if separator and name and name not in cookies:
            cookies[name] = value
    return cookies


__all__ = [
    "AUTH_CSRF_COOKIE",
    "AUTH_CSRF_HEADER",
    "AUTH_SESSION_COOKIE",
    "DeploymentBoundaryMiddleware",
    "DeploymentConfigError",
    "DeploymentPolicy",
    "parse_cors_origins",
    "parse_origin",
    "validate_bind_host",
]
