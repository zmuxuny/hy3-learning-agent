from __future__ import annotations

import gzip
import socket
import time
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.search import providers
from app.search import security
from app.search.providers import BingSearchProvider, DuckDuckGoSearchProvider, SearchResult
from app.tools import web as web_tools


PUBLIC_IP = "93.184.216.34"


class _PeerStream:
    def __init__(self, address: str = PUBLIC_IP):
        self.address = address

    def get_extra_info(self, name: str):
        if name in {"server_addr", "peername"}:
            return (self.address, 443)
        return None


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _dns(address_by_host: dict[str, list[str]] | None = None):
    address_by_host = address_by_host or {}

    def resolve(host: str, port: int):
        addresses = address_by_host.get(host, [PUBLIC_IP])
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (
                (address, port, 0, 0) if ":" in address else (address, port)
            ))
            for address in addresses
        ]

    return resolve


@pytest.mark.asyncio
async def test_resolver_rejects_every_non_public_answer_and_mixed_dns(monkeypatch):
    rejected = [
        "100.64.0.1",
        "198.18.4.11",
        "224.0.0.251",
        "0.0.0.0",
        "fd00::1",
        "2001::c085:4dbd",
    ]
    for address in rejected:
        monkeypatch.setattr(security.socket, "getaddrinfo", _dns({"fixture.invalid": [address]}))
        with pytest.raises(ValueError, match="Non-public"):
            await security.validate_public_url("https://fixture.invalid/path")

    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        _dns({"fixture.invalid": [PUBLIC_IP, "10.0.0.8"]}),
    )
    with pytest.raises(ValueError, match="Non-public"):
        await security.validate_public_url("https://fixture.invalid/path")


@pytest.mark.asyncio
async def test_fetch_pins_numeric_ip_preserves_host_and_sni_and_closes_source(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", _dns())
    captured: list[httpx.Request] = []
    source_stream = _ChunkStream(b"safe ", b"body")

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            stream=source_stream,
            request=request,
            extensions={"network_stream": _PeerStream()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        response, redirect_count = await security.fetch_with_safe_redirects(
            client,
            "https://fixture.invalid/lesson?q=1",
        )

    assert redirect_count == 0
    assert response.text == "safe body"
    assert str(response.url) == "https://fixture.invalid/lesson?q=1"
    assert captured[0].url.host == PUBLIC_IP
    assert captured[0].headers["host"] == "fixture.invalid"
    assert captured[0].extensions["sni_hostname"] == "fixture.invalid"
    assert source_stream.closed is True


@pytest.mark.asyncio
async def test_fetch_rejects_peer_mismatch_and_missing_peer(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", _dns())
    mismatch_stream = _ChunkStream(b"private")

    async def mismatch(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=mismatch_stream,
            request=request,
            extensions={"network_stream": _PeerStream("127.0.0.1")},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(mismatch)) as client:
        with pytest.raises(ValueError, match="does not match"):
            await security.fetch_with_safe_redirects(client, "https://fixture.invalid/")
    assert mismatch_stream.closed is True

    async def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"unknown",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(missing)) as client:
        with pytest.raises(ValueError, match="peer information"):
            await security.fetch_with_safe_redirects(client, "https://fixture.invalid/")


@pytest.mark.asyncio
async def test_redirect_private_rebinding_is_rejected_before_second_send(monkeypatch):
    monkeypatch.setattr(
        security.socket,
        "getaddrinfo",
        _dns({"first.invalid": [PUBLIC_IP], "second.invalid": ["127.0.0.1"]}),
    )
    sent: list[httpx.Request] = []
    redirect_stream = _ChunkStream(b"ignored redirect body")

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://second.invalid/private"},
            stream=redirect_stream,
            request=request,
            extensions={"network_stream": _PeerStream()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="Non-public"):
            await security.fetch_with_safe_redirects(client, "https://first.invalid/start")
    assert len(sent) == 1
    assert sent[0].url.host == PUBLIC_IP
    assert redirect_stream.closed is True


@pytest.mark.parametrize(
    ("body", "wire_limit", "decoded_limit", "accepted"),
    [
        pytest.param(b"12345678", 8, 8, True, id="exact-limit"),
        pytest.param(b"123456789", 8, 32, False, id="wire-n-plus-one"),
    ],
)
@pytest.mark.asyncio
async def test_identity_wire_limit_is_enforced_on_raw_chunks(
    monkeypatch,
    body: bytes,
    wire_limit: int,
    decoded_limit: int,
    accepted: bool,
):
    monkeypatch.setattr(security.socket, "getaddrinfo", _dns())
    monkeypatch.setitem(settings.__dict__, "WEB_MAX_WIRE_BYTES", wire_limit)
    monkeypatch.setitem(settings.__dict__, "WEB_MAX_DECODED_BYTES", decoded_limit)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=_ChunkStream(body[:4], body[4:]),
            request=request,
            extensions={"network_stream": _PeerStream()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if accepted:
            response, _ = await security.fetch_with_safe_redirects(
                client, "https://fixture.invalid/"
            )
            assert response.content == body
        else:
            with pytest.raises(ValueError, match="wire byte limit"):
                await security.fetch_with_safe_redirects(client, "https://fixture.invalid/")


@pytest.mark.asyncio
async def test_gzip_decoded_limit_and_truncated_stream_fail_closed(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", _dns())
    monkeypatch.setitem(settings.__dict__, "WEB_MAX_WIRE_BYTES", 1024)
    monkeypatch.setitem(settings.__dict__, "WEB_MAX_DECODED_BYTES", 8)
    encoded = gzip.compress(b"A" * 9)

    async def bomb(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-encoding": "gzip"},
            stream=_ChunkStream(encoded[:5], encoded[5:]),
            request=request,
            extensions={"network_stream": _PeerStream()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(bomb)) as client:
        with pytest.raises(ValueError, match="decoded byte limit"):
            await security.fetch_with_safe_redirects(client, "https://fixture.invalid/")

    monkeypatch.setitem(settings.__dict__, "WEB_MAX_DECODED_BYTES", 32)

    async def truncated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-encoding": "gzip"},
            stream=_ChunkStream(encoded[:-4]),
            request=request,
            extensions={"network_stream": _PeerStream()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(truncated)) as client:
        with pytest.raises(ValueError, match="gzip"):
            await security.fetch_with_safe_redirects(client, "https://fixture.invalid/")


@pytest.mark.asyncio
async def test_exact_media_type_rejects_near_match(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", _dns())

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/notjson"},
            content=b"{}",
            request=request,
            extensions={"network_stream": _PeerStream()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="Unsupported content type"):
            await security.fetch_with_safe_redirects(client, "https://fixture.invalid/")


@pytest.mark.parametrize("provider", [DuckDuckGoSearchProvider(), BingSearchProvider()])
@pytest.mark.asyncio
async def test_search_provider_rejects_non_html_even_when_generic_fetch_allows_it(
    monkeypatch,
    provider,
):
    async def fake_fetch(_client, url, *, params=None):
        return (
            httpx.Response(
                200,
                headers={"content-type": "text/plain; charset=utf-8"},
                text="<a class='result__a'>not trusted as html</a>",
                request=httpx.Request("GET", url, params=params),
            ),
            0,
        )

    monkeypatch.setattr(providers, "fetch_with_safe_redirects", fake_fetch)
    with pytest.raises(ValueError, match="unsupported content type"):
        await provider.search("fixture query", 1)


@pytest.mark.asyncio
async def test_unknown_content_encoding_fails_closed(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo", _dns())

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-encoding": "br"},
            content=b"opaque",
            request=request,
            extensions={"network_stream": _PeerStream()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="Content-Encoding"):
            await security.fetch_with_safe_redirects(client, "https://fixture.invalid/")


@pytest.mark.asyncio
async def test_total_deadline_includes_dns_resolution(monkeypatch):
    def slow_dns(_host: str, _port: int):
        time.sleep(0.05)
        return _dns()(_host, _port)

    monkeypatch.setattr(security.socket, "getaddrinfo", slow_dns)
    monkeypatch.setitem(settings.__dict__, "WEB_TOTAL_DEADLINE_SECONDS", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as client:
        with pytest.raises(ValueError, match="total deadline"):
            await security.fetch_with_safe_redirects(client, "https://fixture.invalid/")


@pytest.mark.asyncio
async def test_web_outputs_mark_external_content_untrusted(monkeypatch):
    request = httpx.Request("GET", "https://fixture.invalid/")
    response = httpx.Response(
        200,
        headers={"content-type": "text/html"},
        text="<title>Fixture</title><p>body</p>",
        request=request,
    )

    async def fake_fetch(_client, _url):
        return response, 0

    monkeypatch.setattr(web_tools, "fetch_with_safe_redirects", fake_fetch)
    opened = await web_tools.web_open(None, web_tools.WebOpenArgs(url=str(request.url)))
    assert opened["external_untrusted"] is True

    class _Provider:
        name = "fixture"

        async def search(self, _query: str, _limit: int):
            return [SearchResult(title="Result", url="https://result.invalid/")]

    monkeypatch.setattr(web_tools, "get_search_provider", lambda _name: _Provider())
    searched = await web_tools.web_search(
        SimpleNamespace(plan_id=None),
        web_tools.WebSearchArgs(query="fixture query"),
    )
    assert searched["external_untrusted"] is True
    assert searched["results"][0]["external_untrusted"] is True
