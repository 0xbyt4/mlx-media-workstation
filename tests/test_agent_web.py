"""The web tools with a fake resolver and transport (no network): address checks before every request and after
every redirect, caps, text extraction and the search sources. The real transport is tested against a local server.

The live test at the end runs only with MMW_LIVE=1 (one Wikipedia search, one Marginalia search, one fetch of
https://example.com)."""

import gzip
import http.server
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

from app.agent import (
    Context,
    Net,
    Registry,
    Settings,
    build_registry,
    html_text,
    net,
    web_tools,
)
from app.agent.net import Blocked, HttpRequest, HttpResponse

PUBLIC = "93.184.215.14"
PUBLIC2 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"


class FakeWeb:
    """DNS answers and canned responses by "scheme://host/target"; records every lookup and request."""

    def __init__(self) -> None:
        self.dns: dict[str, list[str]] = {"example.org": [PUBLIC]}
        self.pages: dict[str, HttpResponse | Callable[[HttpRequest], HttpResponse]] = {}
        self.lookups: list[str] = []
        self.requests: list[HttpRequest] = []

    def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        if host not in self.dns:
            raise Blocked(f"{host}: the name does not resolve.")
        return self.dns[host]

    def transport(self, req: HttpRequest) -> HttpResponse:
        self.requests.append(req)
        page = self.pages.get(f"{req.scheme}://{req.host}{req.target}")
        if page is None:
            return HttpResponse(404, {"content-type": "text/plain"}, b"not found")
        return page(req) if callable(page) else page

    def net(self) -> Net:
        return Net(self.resolve, self.transport)


def html(body: str, extra: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        200,
        {"content-type": "text/html; charset=utf-8", **(extra or {})},
        body.encode(),
    )


def redirect(to: str, status: int = 302) -> HttpResponse:
    return HttpResponse(status, {"location": to}, b"")


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def reg(web: FakeWeb) -> Registry:
    return build_registry(None, Settings(), net=web.net())


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    return Context("c1", None, threading.Event(), tmp_path)


PAGE = """<html><head><title>Fox facts</title><style>.x{}</style></head><body>
<nav><a href="/home">Home</a> menu</nav>
<main><h1>Red fox</h1><p>The red fox is the largest of the true foxes and one of the most widely distributed
members of the order Carnivora, present across the entire Northern Hemisphere. It is listed as least concern.</p>
<ul><li>Diet: rodents</li><li>Life span: 2-5 years</li></ul><a href="/more#top">More</a>
<script>steal()</script></main><footer>Copyright</footer></body></html>"""


# Addresses


@pytest.mark.parametrize(
    "address",
    [
        "93.184.215.14",
        "1.1.1.1",
        "8.8.8.8",
        PUBLIC2,
        "2a00:1450:4001:80b::200e",
        "64:ff9b::808:808",  # NAT64 of 8.8.8.8 (DNS64 networks)
    ],
)
def test_public_addresses(address: str) -> None:
    assert net.public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.5.6.7",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # shared (CGNAT)
        "224.0.0.1",
        "239.255.255.250",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.2.1",  # documentation
        "198.18.0.1",  # benchmarking
        "::1",
        "::",
        "fe80::1",
        "fe80::1%en0",
        "fc00::1",
        "fd12:3456::1",
        "fec0::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "64:ff9b::c0a8:101",  # NAT64 of 192.168.1.1
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
        "2002:c0a8:101::",  # 6to4 of 192.168.1.1
        "2001:db8::1",  # documentation
        "not-an-address",
    ],
)
def test_non_public_addresses(address: str) -> None:
    assert not net.public_address(address)


def test_every_address_of_a_host_must_be_public() -> None:
    def resolve(host: str, port: int) -> list[str]:
        return [PUBLIC, "10.0.0.5"]

    with pytest.raises(Blocked, match="10.0.0.5"):
        net.checked_addresses("mixed.example", 443, resolve)
    assert net.checked_addresses("1.1.1.1", 443, resolve) == ["1.1.1.1"]


# fetch_url


def test_fetch_reads_a_page_as_text(reg: Registry, ctx: Context, web: FakeWeb) -> None:
    web.pages["https://example.org/fox"] = html(PAGE)
    r = reg.call("fetch_url", {"url": "https://example.org/fox"}, ctx)
    assert r.status == "ok"
    text = r.content
    assert text.startswith(
        "URL: https://example.org/fox\n\nTitle: Fox facts\n\n# Red fox"
    )
    assert "- Diet: rodents" in text
    assert "steal()" not in text and "menu" not in text and "Copyright" not in text
    assert text.endswith("Links:\n- https://example.org/more")
    req = web.requests[0]
    assert (req.scheme, req.host, req.port, req.target, req.ip) == (
        "https",
        "example.org",
        443,
        "/fox",
        PUBLIC,
    )
    assert web_tools.REPO_URL in req.headers["User-Agent"]
    assert "@" not in req.headers["User-Agent"]
    assert {k.lower() for k in req.headers} == {
        "user-agent",
        "accept",
        "accept-encoding",
    }


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/passwd", "Only http:// and https://"),
        ("ftp://example.org/x", "Only http:// and https://"),
        ("javascript:alert(1)", "Only http:// and https://"),
        ("https://user:pw@example.org/", "user name or password"),
        ("https://user@example.org/", "user name or password"),
        ("http://127.0.0.1:7870/api/jobs", "local, private or reserved"),
        ("http://[::1]:7870/", "local, private or reserved"),
        ("http://169.254.169.254/latest/meta-data/", "local, private or reserved"),
        ("http://example.org:99999/", "bad port"),
        ("https://nowhere.invalid/", "does not resolve"),
    ],
)
def test_fetch_refusals(
    reg: Registry, ctx: Context, web: FakeWeb, url: str, message: str
) -> None:
    r = reg.call("fetch_url", {"url": url}, ctx)
    assert r.status == "error" and message in r.content
    assert web.requests == []


def test_a_name_that_resolves_to_this_mac_is_refused(
    reg: Registry, ctx: Context, web: FakeWeb
) -> None:
    web.dns["evil.example"] = ["127.0.0.1"]
    r = reg.call("fetch_url", {"url": "http://evil.example:7870/api/jobs"}, ctx)
    assert r.status == "error" and "127.0.0.1" in r.content
    assert web.requests == []


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:7870/api/jobs",
        "http://localhost.example/",  # resolves to loopback below
        "http://[::ffff:192.168.1.1]/",
        "https://10.1.2.3/admin",
    ],
)
def test_redirects_are_checked_again(
    reg: Registry, ctx: Context, web: FakeWeb, target: str
) -> None:
    web.dns["localhost.example"] = ["127.0.0.1"]
    web.pages["https://example.org/go"] = redirect(target)
    r = reg.call("fetch_url", {"url": "https://example.org/go"}, ctx)
    assert r.status == "error" and "local, private or reserved" in r.content
    assert [q.host for q in web.requests] == ["example.org"]  # the second never left


def test_redirects_are_followed_up_to_the_limit(
    reg: Registry, ctx: Context, web: FakeWeb
) -> None:
    for i in range(5):
        web.pages[f"https://example.org/r{i}"] = redirect(f"/r{i + 1}", 301)
    web.pages["https://example.org/r5"] = html("<p>arrived</p>")
    r = reg.call("fetch_url", {"url": "https://example.org/r0"}, ctx)
    assert r.status == "ok" and "URL: https://example.org/r5" in r.content
    assert "arrived" in r.content
    web.pages["https://example.org/r5"] = redirect("/r6")
    r = reg.call("fetch_url", {"url": "https://example.org/r0"}, ctx)
    assert r.status == "error" and "More than 5 redirects" in r.content


def test_a_redirect_to_another_public_host_is_resolved_and_pinned(
    reg: Registry, ctx: Context, web: FakeWeb
) -> None:
    web.dns["other.example"] = [PUBLIC2]
    web.pages["http://example.org/a"] = redirect("https://other.example/b")
    web.pages["https://other.example/b"] = html("<p>there</p>")
    r = reg.call("fetch_url", {"url": "http://example.org/a"}, ctx)
    assert r.status == "ok"
    assert web.lookups == ["example.org", "other.example"]
    assert [(q.host, q.ip) for q in web.requests] == [
        ("example.org", PUBLIC),
        ("other.example", PUBLIC2),
    ]


def test_the_next_address_is_tried_when_a_connection_fails(
    reg: Registry, ctx: Context, web: FakeWeb
) -> None:
    web.dns["example.org"] = [PUBLIC, "1.1.1.1"]

    def page(req: HttpRequest) -> HttpResponse:
        if req.ip == PUBLIC:
            raise ConnectionRefusedError("refused")
        return html("<p>second</p>")

    web.pages["https://example.org/"] = page
    r = reg.call("fetch_url", {"url": "https://example.org"}, ctx)
    assert r.status == "ok" and "second" in r.content
    assert [q.ip for q in web.requests] == [PUBLIC, "1.1.1.1"]


def test_page_types_sizes_and_encodings(
    reg: Registry, ctx: Context, web: FakeWeb
) -> None:
    web.pages["https://example.org/bin"] = HttpResponse(
        200, {"content-type": "application/octet-stream"}, b"\x00\x01"
    )
    r = reg.call("fetch_url", {"url": "https://example.org/bin"}, ctx)
    assert r.status == "error" and "not a text page" in r.content

    web.pages["https://example.org/big"] = HttpResponse(
        200, {"content-type": "text/plain"}, b"x" * 100, truncated=True
    )
    r = reg.call("fetch_url", {"url": "https://example.org/big"}, ctx)
    assert r.status == "ok" and "Only the first 2 MB" in r.content

    web.pages["https://example.org/gz"] = HttpResponse(
        200,
        {"content-type": "application/json", "content-encoding": "gzip"},
        gzip.compress(b'{"fox": "red"}'),
    )
    r = reg.call("fetch_url", {"url": "https://example.org/gz"}, ctx)
    assert r.status == "ok" and '{"fox": "red"}' in r.content

    web.pages["https://example.org/latin"] = HttpResponse(
        200,
        {"content-type": "text/html"},
        '<meta charset="iso-8859-1"><p>café</p>'.encode("latin-1"),
    )
    r = reg.call("fetch_url", {"url": "https://example.org/latin"}, ctx)
    assert "café" in r.content

    web.pages["https://example.org/gone"] = HttpResponse(
        410, {"content-type": "text/plain"}, b"gone away"
    )
    r = reg.call("fetch_url", {"url": "https://example.org/gone"}, ctx)
    assert r.status == "error" and "HTTP 410" in r.content and "gone away" in r.content


def test_a_gzip_bomb_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = HttpResponse(
        200, {"content-encoding": "gzip"}, gzip.compress(b"\x00" * 10_000_000)
    )
    data, cut = web_tools._decode(resp, 1000)
    assert len(data) == 1000 and cut


def test_html_text_keeps_the_whole_page_without_a_main() -> None:
    title, text, links = html_text.extract(
        "<title>T</title><p>One &amp; two</p><table><tr><td>a</td><td>b</td></tr></table>"
        '<a href="https://x.example/p">x</a><a href="mailto:a@b">m</a><pre>  keep\n  spaces</pre>',
        "https://example.org/",
    )
    assert title == "T"
    assert "One & two" in text and "| a | b" in text
    assert "  keep\n  spaces" in text
    assert links == ["https://x.example/p"]


def test_fetch_explains_the_url(reg: Registry) -> None:
    line = reg.get("fetch_url").describe({"url": "https://example.org/x"})
    assert "https://example.org/x" in line


# web_search


def wiki_answer(*titles: str) -> HttpResponse:
    data = {
        "query": {
            "search": [
                {
                    "title": t,
                    "snippet": f'<span class="searchmatch">{t}</span> &amp; more',
                }
                for t in titles
            ]
        }
    }
    return HttpResponse(
        200, {"content-type": "application/json"}, json.dumps(data).encode()
    )


def marginalia_answer(*urls: str) -> HttpResponse:
    data = {
        "license": "CC-BY-NC-SA 4.0",
        "results": [
            {"url": u, "title": f"Page {u}", "description": "text"} for u in urls
        ],
    }
    return HttpResponse(200, {}, json.dumps(data).encode())


def route(web: FakeWeb, answers: dict[str, HttpResponse]) -> None:
    def transport(req: HttpRequest) -> HttpResponse:
        web.requests.append(req)
        return answers.get(req.host, HttpResponse(500))

    web.transport = transport  # type: ignore[method-assign]


def test_search_asks_wikipedia_then_marginalia(ctx: Context, web: FakeWeb) -> None:
    route(
        web,
        {
            "en.wikipedia.org": wiki_answer("Red fox", "Arctic fox"),
            "api2.marginalia-search.com": marginalia_answer(
                "https://foxes.example/", "https://en.wikipedia.org/wiki/Red_fox"
            ),
        },
    )
    reg = build_registry(None, Settings(), net=web.net())
    r = reg.call("web_search", {"query": "red fox", "max_results": 5}, ctx)
    assert r.status == "ok"
    assert r.content.splitlines() == [
        "1. Red fox",
        "   https://en.wikipedia.org/wiki/Red_fox",
        "   Red fox & more [Wikipedia]",
        "2. Arctic fox",
        "   https://en.wikipedia.org/wiki/Arctic_fox",
        "   Arctic fox & more [Wikipedia]",
        "3. Page https://foxes.example/",
        "   https://foxes.example/",
        "   text [Marginalia]",
    ]
    wiki, marg = web.requests
    assert wiki.host == "en.wikipedia.org" and wiki.target.startswith("/w/api.php?")
    q = parse_qs(urlsplit(wiki.target).query)
    assert q["srsearch"] == ["red fox"] and q["list"] == ["search"]
    assert q["srlimit"] == ["5"]
    assert marg.target == "/search?query=red+fox&count=5"
    assert marg.headers["API-Key"] == "public"
    for req in (wiki, marg):
        assert web_tools.REPO_URL in req.headers["User-Agent"]
        assert req.ip is None  # fixed public hosts: no pinning needed


def test_search_reports_a_rate_limited_source(ctx: Context, web: FakeWeb) -> None:
    route(
        web,
        {
            "en.wikipedia.org": wiki_answer("Fox"),
            "api2.marginalia-search.com": HttpResponse(503),
        },
    )
    reg = build_registry(None, Settings(), net=web.net())
    r = reg.call("web_search", {"query": "fox"}, ctx)
    assert r.status == "ok" and "1. Fox" in r.content
    assert r.content.endswith(
        "Not available: Marginalia: rate limited (HTTP 503); try again later."
    )


def test_search_fails_when_every_source_fails(ctx: Context, web: FakeWeb) -> None:
    route(web, {})
    reg = build_registry(None, Settings(), net=web.net())
    r = reg.call("web_search", {"query": "fox"}, ctx)
    assert r.status == "error" and "Wikipedia: HTTP 500" in r.content


def test_searxng_comes_first_when_set(ctx: Context, web: FakeWeb) -> None:
    answer = {"results": [{"url": "https://s.example/", "title": "S", "content": "c"}]}
    route(
        web,
        {
            "127.0.0.1": HttpResponse(200, {}, json.dumps(answer).encode()),
            "de.wikipedia.org": wiki_answer("Fuchs"),
        },
    )
    settings = Settings(
        searxng_url="http://127.0.0.1:8888/searx",
        wikipedia_language="de",
        marginalia=False,
    )
    reg = build_registry(None, settings, net=web.net())
    r = reg.call("web_search", {"query": "fuchs"}, ctx)
    assert r.content.splitlines()[0] == "1. S"
    assert "https://de.wikipedia.org/wiki/Fuchs" in r.content
    assert [q.host for q in web.requests] == ["127.0.0.1", "de.wikipedia.org"]
    assert web.requests[0].port == 8888
    assert web.requests[0].target == "/searx/search?q=fuchs&format=json"
    line = reg.get("web_search").describe({"query": "fuchs"})
    assert line == "Sends the query 'fuchs' to 127.0.0.1:8888, de.wikipedia.org."


# The real transport, against a server on this Mac


class Handler(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[dict[str, str]]] = []

    def do_GET(self) -> None:
        Handler.seen.append(dict(self.headers))
        if self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            for _ in range(100):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.1)
            return
        body = b"y" * 5000
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Set-Cookie", "a=b")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[int]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Handler.seen = []
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_transport_connects_to_the_given_address_with_the_real_host_name(
    server: int,
) -> None:
    req = HttpRequest(
        "http", "pinned.example", server, "/a", {"User-Agent": "t"}, ip="127.0.0.1"
    )
    resp = net.http_transport(req)
    assert resp.status == 200 and resp.body == b"y" * 5000 and not resp.truncated
    assert resp.headers["set-cookie"] == "a=b"
    assert Handler.seen[0]["Host"] == f"pinned.example:{server}"
    assert "Cookie" not in Handler.seen[0] and "Authorization" not in Handler.seen[0]
    # a second request does not send the cookie back
    net.http_transport(req)
    assert "Cookie" not in Handler.seen[1]


def test_transport_stops_at_max_bytes(server: int) -> None:
    req = HttpRequest(
        "http", "x.example", server, "/", {}, ip="127.0.0.1", max_bytes=1000
    )
    resp = net.http_transport(req)
    assert len(resp.body) == 1000 and resp.truncated


def test_transport_deadline_and_abort(server: int) -> None:
    t0 = time.monotonic()
    req = HttpRequest(
        "http", "x.example", server, "/slow", {}, ip="127.0.0.1", timeout=0.5
    )
    with pytest.raises(TimeoutError):
        net.http_transport(req)
    assert time.monotonic() - t0 < 2

    abort = threading.Event()
    threading.Timer(0.3, abort.set).start()
    t0 = time.monotonic()
    req = HttpRequest(
        "http", "x.example", server, "/slow", {}, ip="127.0.0.1", abort=abort
    )
    with pytest.raises(InterruptedError):
        net.http_transport(req)
    assert time.monotonic() - t0 < 2


def test_fetch_url_with_the_real_transport_never_reaches_this_mac(
    server: int, ctx: Context
) -> None:
    """With the default transport, a name that points to 127.0.0.1 is refused before any connection."""

    def resolve(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    reg = build_registry(None, Settings(), net=Net(resolve=resolve))
    r = reg.call("fetch_url", {"url": f"http://rebind.example:{server}/"}, ctx)
    assert r.status == "error" and "local, private or reserved" in r.content
    assert Handler.seen == []


@pytest.mark.skipif(
    os.environ.get("MMW_LIVE") != "1", reason="live network test: set MMW_LIVE=1"
)
def test_live(ctx: Context) -> None:
    wiki = web_tools.search_wikipedia(Net(), "en", "Antikythera mechanism", 3, None)
    assert wiki and "Antikythera" in wiki[0][0]
    try:
        found = web_tools.search_marginalia(Net(), "Antikythera mechanism", 3, None)
        assert all(u.startswith("http") for _, u, _ in found)
    except web_tools.ToolError as e:  # the shared key is often rate limited
        assert "rate limited" in str(e)
    r = build_registry(None, Settings()).call(
        "fetch_url", {"url": "https://example.com"}, ctx
    )
    assert r.status == "ok" and "Example Domain" in r.content


def test_unsafe_characters_in_the_path_are_encoded(
    reg: Registry, ctx: Context, web: FakeWeb
) -> None:
    web.pages["https://example.org/a%20b?q=x%20y"] = html("<p>ok</p>")
    r = reg.call("fetch_url", {"url": "https://example.org/a b?q=x%20y"}, ctx)
    assert r.status == "ok"
    assert web.requests[0].target == "/a%20b?q=x%20y"


def test_a_connect_timeout_moves_on_to_the_next_address(
    reg: Registry, ctx: Context, web: FakeWeb
) -> None:
    web.dns["example.org"] = [PUBLIC2, PUBLIC]

    def page(req: HttpRequest) -> HttpResponse:
        if req.ip == PUBLIC2:
            raise TimeoutError("timed out")  # a black-holed IPv6 route
        return html("<p>over IPv4</p>")

    web.pages["https://example.org/"] = page
    r = reg.call("fetch_url", {"url": "https://example.org/"}, ctx)
    assert r.status == "ok" and "over IPv4" in r.content
    assert [q.ip for q in web.requests] == [PUBLIC2, PUBLIC]


@pytest.mark.parametrize("charset", ["punycode", "idna", "unicode_escape", "rot13"])
def test_odd_charsets_fall_back_to_utf8(
    reg: Registry, ctx: Context, web: FakeWeb, charset: str
) -> None:
    web.pages["https://example.org/c"] = HttpResponse(
        200,
        {"content-type": f"text/plain; charset={charset}"},
        "caf\u00e9 ".encode() * 200_000,
    )
    t0 = time.monotonic()
    r = reg.call("fetch_url", {"url": "https://example.org/c"}, ctx)
    assert r.status == "ok" and "caf\u00e9" in r.content
    assert time.monotonic() - t0 < 2
