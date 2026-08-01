import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import settings


async def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public HTTP(S) URLs are supported")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".local"):
        raise ValueError("Local network URLs are not allowed")
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise ValueError(f"Non-public IP literals are not allowed: {hostname}")
    addresses = await asyncio.to_thread(
        socket.getaddrinfo,
        hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )
    resolved = {ipaddress.ip_address(address[4][0]) for address in addresses}
    synthetic_proxy_ranges = (
        ipaddress.ip_network("198.18.0.0/15"),
        ipaddress.ip_network("2001::/32"),
    )
    for ip in resolved:
        synthetic_proxy_ip = (
            settings.WEB_ALLOW_SYNTHETIC_DNS
            and literal_ip is None
            and any(ip in network for network in synthetic_proxy_ranges)
        )
        if synthetic_proxy_ip:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"Non-public DNS target is not allowed for {hostname}: {ip}")


async def fetch_with_safe_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
) -> tuple[httpx.Response, int]:
    """Validate every network hop instead of trusting automatic redirects."""
    current_url = url
    current_params = params
    for redirect_count in range(settings.WEB_MAX_REDIRECTS + 1):
        await validate_public_url(current_url)
        response = await client.get(current_url, params=current_params, follow_redirects=False)
        current_params = None
        if response.status_code not in {301, 302, 303, 307, 308}:
            response.raise_for_status()
            return response, redirect_count
        location = response.headers.get("location")
        if not location:
            raise ValueError("Redirect response has no location")
        if redirect_count >= settings.WEB_MAX_REDIRECTS:
            raise ValueError("Too many redirects")
        current_url = urljoin(str(response.url), location)
    raise ValueError("Too many redirects")
