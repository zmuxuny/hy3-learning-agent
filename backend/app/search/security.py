from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
import zlib

import httpx

from app.core.config import settings


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_FETCH_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/xhtml+xml",
        "application/xml",
        "text/html",
        "text/plain",
        "text/xml",
    }
)


@dataclass(frozen=True)
class ResolvedTarget:
    logical_url: str
    hostname: str
    authority: str
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]


def secure_http_client(*, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    """Return the only client configuration used by production web fetches."""
    timeout_seconds = float(settings.WEB_TOTAL_DEADLINE_SECONDS)
    transport = httpx.AsyncHTTPTransport(
        retries=0,
        limits=httpx.Limits(max_keepalive_connections=0),
    )
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


async def validate_public_url(url: str) -> ResolvedTarget:
    """Resolve a URL once and retain the exact public addresses to be pinned."""
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise ValueError("URL contains control characters")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Invalid public URL") from exc
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public HTTP(S) URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials in web URLs are not allowed")

    raw_hostname = parsed.hostname.rstrip(".")
    try:
        hostname = raw_hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Invalid URL hostname") from exc
    if not hostname or hostname == "localhost" or hostname.endswith(".local"):
        raise ValueError("Local network URLs are not allowed")

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        resolved = {_normalize_ip(literal_ip)}
    else:
        addresses = await asyncio.to_thread(socket.getaddrinfo, hostname, port)
        resolved = {
            _normalize_ip(ipaddress.ip_address(address[4][0]))
            for address in addresses
            if address and len(address) >= 5 and address[4]
        }
    if not resolved:
        raise ValueError(f"DNS returned no addresses for {hostname}")
    for address in resolved:
        if not _is_public_unicast(address):
            raise ValueError(f"Non-public DNS target is not allowed for {hostname}: {address}")

    normalized_host = (
        f"[{hostname}]" if literal_ip is not None and literal_ip.version == 6 else hostname
    )
    default_port = 443 if scheme == "https" else 80
    authority = normalized_host if port == default_port else f"{normalized_host}:{port}"
    logical_url = urlunsplit((scheme, authority, parsed.path or "/", parsed.query, ""))
    ordered = tuple(sorted(resolved, key=lambda item: (item.version, int(item))))
    return ResolvedTarget(
        logical_url=logical_url,
        hostname=hostname,
        authority=authority,
        addresses=ordered,
    )


async def fetch_with_safe_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
) -> tuple[httpx.Response, int]:
    """Fetch bounded content while pinning every hop to its validated address."""
    deadline = float(settings.WEB_TOTAL_DEADLINE_SECONDS)
    if deadline <= 0:
        raise ValueError("Web deadline must be positive")
    try:
        async with asyncio.timeout(deadline):
            return await _fetch_with_safe_redirects(client, url, params=params)
    except TimeoutError as exc:
        raise ValueError("Web fetch exceeded its total deadline") from exc


async def _fetch_with_safe_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None,
) -> tuple[httpx.Response, int]:
    current_url = str(httpx.URL(url, params=params)) if params else url
    for redirect_count in range(settings.WEB_MAX_REDIRECTS + 1):
        target = await validate_public_url(current_url)
        if not isinstance(target, ResolvedTarget):
            raise ValueError("URL resolver did not return a pinned target")
        pinned_address = target.addresses[0]
        response = await _send_pinned_request(client, target, pinned_address)
        try:
            _validate_connected_peer(response, pinned_address)
        except BaseException:
            await response.aclose()
            raise

        if response.status_code in _REDIRECT_STATUSES:
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise ValueError("Redirect response has no location")
            if len(location) > 2000:
                raise ValueError("Redirect location is too long")
            if redirect_count >= settings.WEB_MAX_REDIRECTS:
                raise ValueError("Too many redirects")
            current_url = urljoin(target.logical_url, location)
            continue

        try:
            response.raise_for_status()
            bounded_body = await _read_bounded_body(response)
            bounded_response = _bounded_response(response, target.logical_url, bounded_body)
            await response.aclose()
            return bounded_response, redirect_count
        except BaseException:
            await response.aclose()
            raise
    raise ValueError("Too many redirects")


async def _send_pinned_request(
    client: httpx.AsyncClient,
    target: ResolvedTarget,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> httpx.Response:
    logical = httpx.URL(target.logical_url)
    pinned_url = logical.copy_with(host=str(address))
    headers = {"Host": target.authority, "Accept-Encoding": "gzip"}
    extensions = {"sni_hostname": target.hostname}
    request = client.build_request("GET", pinned_url, headers=headers, extensions=extensions)
    return await client.send(request, stream=True, follow_redirects=False)


def _validate_connected_peer(
    response: httpx.Response,
    pinned_address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> None:
    network_stream = response.extensions.get("network_stream")
    if network_stream is None or not hasattr(network_stream, "get_extra_info"):
        raise ValueError("Connected peer information is unavailable")
    peer: Any = network_stream.get_extra_info("server_addr")
    if peer is None:
        peer = network_stream.get_extra_info("peername")
    if isinstance(peer, (tuple, list)) and peer:
        peer = peer[0]
    if not isinstance(peer, str):
        raise ValueError("Connected peer information is unavailable")
    try:
        peer_address = _normalize_ip(ipaddress.ip_address(peer))
    except ValueError as exc:
        raise ValueError("Connected peer address is invalid") from exc
    if not _is_public_unicast(peer_address) or peer_address != _normalize_ip(pinned_address):
        raise ValueError(
            f"Connected peer {peer_address} does not match pinned target {pinned_address}"
        )


async def _read_bounded_body(response: httpx.Response) -> bytes:
    media_type = normalized_media_type(response)
    if media_type not in _ALLOWED_FETCH_MEDIA_TYPES:
        raise ValueError(f"Unsupported content type: {media_type}")
    max_wire = _positive_limit("WEB_MAX_WIRE_BYTES", settings.WEB_MAX_WIRE_BYTES)
    max_decoded = _positive_limit("WEB_MAX_DECODED_BYTES", settings.WEB_MAX_DECODED_BYTES)
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            parsed_length = int(content_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header") from exc
        if parsed_length < 0:
            raise ValueError("Invalid Content-Length header")
        if parsed_length > max_wire:
            raise ValueError("Web response exceeds wire byte limit")

    encoding = response.headers.get("content-encoding", "identity").strip().lower() or "identity"
    if encoding not in {"identity", "gzip"}:
        raise ValueError(f"Unsupported Content-Encoding: {encoding}")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    decoded = bytearray()
    wire_count = 0

    def consume(chunk: bytes) -> None:
        nonlocal wire_count
        wire_count += len(chunk)
        if wire_count > max_wire:
            raise ValueError("Web response exceeds wire byte limit")
        if decoder is None:
            _append_with_limit(decoded, chunk, max_decoded)
        else:
            pending = chunk
            while pending:
                try:
                    produced = decoder.decompress(pending, max_decoded - len(decoded) + 1)
                except zlib.error as exc:
                    raise ValueError("Invalid gzip response") from exc
                _append_with_limit(decoded, produced, max_decoded)
                pending = decoder.unconsumed_tail

    if response.is_stream_consumed:
        consume(response.content)
    else:
        raw_iterator = response.aiter_raw()
        try:
            async for chunk in raw_iterator:
                consume(chunk)
        finally:
            await raw_iterator.aclose()

    if decoder is not None:
        try:
            produced = decoder.flush(max_decoded - len(decoded) + 1)
        except zlib.error as exc:
            raise ValueError("Invalid gzip response") from exc
        _append_with_limit(decoded, produced, max_decoded)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Invalid or trailing gzip response")
    return bytes(decoded)


def _append_with_limit(target: bytearray, chunk: bytes, maximum: int) -> None:
    if len(target) + len(chunk) > maximum:
        raise ValueError("Web response exceeds decoded byte limit")
    target.extend(chunk)


def _bounded_response(source: httpx.Response, logical_url: str, body: bytes) -> httpx.Response:
    headers = [
        (name, value)
        for name, value in source.headers.multi_items()
        if name.lower() not in {"content-encoding", "content-length"}
    ]
    return httpx.Response(
        source.status_code,
        headers=headers,
        content=body,
        request=httpx.Request("GET", logical_url),
        extensions={"safe_web_fetch": True},
    )


def normalized_media_type(response: httpx.Response) -> str:
    value = response.headers.get("content-type", "")
    if not value:
        return ""
    token = value.split(";", 1)[0].strip().lower()
    if not token or "," in token:
        return ""
    return token


def _positive_limit(name: str, value: int) -> int:
    value = int(value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _normalize_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_public_unicast(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return address.is_global and not address.is_multicast and not address.is_unspecified
