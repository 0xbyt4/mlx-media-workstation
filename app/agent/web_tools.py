"""The web tools (R2): web_search and fetch_url. Both send text off this Mac, so the user approves them (policy.py).

web_search asks, one after another (never in parallel): a SearXNG instance when [agent] searxng_url is set,
Wikipedia (MediaWiki search API) and Marginalia Search with the shared key "public". No scraping of search engines.
Every request names the app and its repository in the User-Agent, as Wikimedia's policy asks; no personal data.

fetch_url downloads one http(s) page as text. It refuses non-public addresses for every address of the host and
again after every redirect (redirects are followed by hand, at most MAX_REDIRECTS), connects to the address it
checked (app/agent/net.py), and stops at MAX_PAGE bytes and FETCH_SECONDS. It sends no cookies or credentials and
refuses URLs with a user name or password in them.
"""

import codecs
import html
import json
import re
import time
import tomllib
import zlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit

from app import config
from app.agent import html_text
from app.agent.file_tools import looks_text
from app.agent.net import (
    Blocked,
    HttpRequest,
    HttpResponse,
    Resolver,
    Transport,
    checked_addresses,
    http_transport,
    system_resolve,
)
from app.agent.policy import Risk
from app.agent.registry import Cancelled, Context, Tool, ToolError
from app.agent.settings import Settings

REPO_URL = "https://github.com/0xbyt4/mlx-media-workstation"
MAX_REDIRECTS = 5
MAX_PAGE = 2 * 1024 * 1024  # bytes of one page (after decompression too)
FETCH_SECONDS = 15.0  # for the whole fetch, redirects included
SEARCH_SECONDS = 10.0  # per search request
MAX_SEARCH_BYTES = 1024 * 1024
MAX_TRIES = 3  # addresses of one host tried in turn when a connection fails
REDIRECTS = {301, 302, 303, 307, 308}
MARGINALIA = "api2.marginalia-search.com"
TAG = re.compile(r"<[^>]+>")
TEXT_CODECS = (
    "utf-",
    "ascii",
    "latin",
    "iso8859",
    "cp",
    "mac-",
    "koi8",
    "euc",
    "shift_jis",
    "gb",
    "big5",
    "iso2022",
    "johab",
    "hz",
    "kz1048",
    "tis-620",
    "ptcp154",
)
# characters left as they are in a request target; others (spaces, control characters) are percent-encoded
URL_SAFE = "/?&=%:@!$'()*+,;~-._[]"


def _version() -> str:
    try:
        data = tomllib.loads((config.ROOT / "pyproject.toml").read_text())
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0"


USER_AGENT = f"MLXMediaWorkstation/{_version()} ({REPO_URL}) python-http.client"


@dataclass
class Net:
    """How the web tools reach the network. Tests pass fakes; nothing else changes."""

    resolve: Resolver = system_resolve
    transport: Transport = http_transport


@dataclass
class Page:
    url: str  # after redirects
    status: int
    content_type: str
    text: str
    truncated: bool


def _clean(snippet: str) -> str:
    return " ".join(html.unescape(TAG.sub("", snippet or "")).split())


def _host(parts: Any) -> str:
    host = parts.hostname
    if not host:
        raise ToolError("The URL has no host.")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ToolError(f"{host}: not a valid host name.") from None


def _decode(resp: HttpResponse, limit: int) -> tuple[bytes, bool]:
    """The body without its content encoding (gzip or deflate), cut at `limit` bytes."""
    coding = resp.headers.get("content-encoding", "identity").strip().lower()
    if coding in ("", "identity"):
        return resp.body, resp.truncated
    if coding not in ("gzip", "x-gzip", "deflate"):
        raise ToolError(
            f"The page uses the encoding {coding!r}, which this tool cannot read."
        )
    wbits = 16 + zlib.MAX_WBITS if "gzip" in coding else zlib.MAX_WBITS
    try:
        d = zlib.decompressobj(wbits)
        data = d.decompress(resp.body, limit)
    except zlib.error:
        raise ToolError("The page's compressed body is broken.") from None
    return data, resp.truncated or bool(d.unconsumed_tail)


def _charset(content_type: str, body: bytes) -> str:
    m = re.search(r"charset=[\"']?([\w.:-]+)", content_type, re.IGNORECASE)
    if not m:
        m = re.search(
            rb"<meta[^>]+charset=[\"']?([\w.:-]+)", body[:4096], re.IGNORECASE
        )
    name = m.group(1) if m else "utf-8"
    name = name.decode("ascii", "replace") if isinstance(name, bytes) else name
    try:
        codec = codecs.lookup(name).name
    except LookupError:
        return "utf-8"
    # only real text charsets: punycode and the escape codecs are slow or not charsets (a page could stall the tool)
    return codec if codec.startswith(TEXT_CODECS) else "utf-8"


def url_host(url: object) -> str | None:
    """The host fetch_url would contact for this URL, as the network sees it (IDNA: "xn--..." for a non-ASCII name,
    so a look-alike letter shows), or None when the URL has no valid http(s) host."""
    try:
        parts = urlsplit(str(url))
        return _host(parts) if parts.scheme in ("http", "https") else None
    except (ToolError, ValueError):
        return None


def fetch(
    url: str,
    net: Net,
    abort: Any = None,
    max_bytes: int = MAX_PAGE,
    seconds: float = FETCH_SECONDS,
) -> tuple[str, HttpResponse]:
    """GET a public http(s) URL, following redirects by hand with the address check before each request."""
    deadline = time.monotonic() + seconds
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ToolError("Only http:// and https:// URLs can be fetched.")
        if parts.username is not None or parts.password is not None:
            raise ToolError("URLs with a user name or password are refused.")
        host = _host(parts)
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise ToolError("The URL has a bad port.") from None
        try:
            addresses = checked_addresses(host, port, net.resolve)
        except Blocked as e:
            raise ToolError(str(e)) from None
        target = quote(
            (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
            safe=URL_SAFE,
        )
        resp = None
        error: Exception | None = None
        for ip in addresses[:MAX_TRIES]:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ToolError(f"No complete answer within {seconds:.0f} s.")
            req = HttpRequest(
                parts.scheme,
                host,
                port,
                target,
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                    "Accept-Encoding": "identity",
                },
                ip=ip,
                timeout=left,
                max_bytes=max_bytes,
                abort=abort,
            )
            try:
                resp = net.transport(req)
                break
            except InterruptedError:
                raise Cancelled from None
            except TimeoutError as e:
                if time.monotonic() >= deadline:
                    raise ToolError(
                        f"No complete answer within {seconds:.0f} s."
                    ) from None
                error = e  # this address did not answer in time: try the next one
            except OSError as e:
                error = e
        if resp is None:
            raise ToolError(f"Could not connect to {host}: {error}")
        location = resp.headers.get("location")
        if resp.status in REDIRECTS and location:
            url = urljoin(url, location.strip())
            continue
        return url, resp
    raise ToolError(f"More than {MAX_REDIRECTS} redirects.")


def read_page(url: str, net: Net, abort: Any = None) -> Page:
    final, resp = fetch(url, net, abort)
    body, truncated = _decode(resp, MAX_PAGE)
    ctype = resp.headers.get("content-type", "").lower()
    mime = ctype.split(";", 1)[0].strip()
    is_html = mime in ("text/html", "application/xhtml+xml")
    texty = (
        is_html
        or mime.startswith("text/")
        or mime in ("application/json", "application/xml")
        or mime.endswith(("+json", "+xml"))
    )
    if not mime:
        texty = looks_text(body[:8192])
        is_html = texty and b"<html" in body[:4096].lower()
    if not texty:
        raise ToolError(f"{final} is not a text page ({mime or 'unknown type'}).")
    text = body.decode(_charset(ctype, body), errors="replace")
    if is_html:
        title, text, links = html_text.extract(text, final)
        if title:
            text = f"Title: {title}\n\n{text}"
        if links:
            text += "\n\nLinks:\n" + "\n".join(f"- {u}" for u in links)
    if resp.status >= 400:
        raise ToolError(f"{final} answered HTTP {resp.status}:\n{text[:1000]}")
    return Page(final, resp.status, mime, text, truncated)


def _get_json(
    net: Net, url: str, headers: dict[str, str], abort: Any, name: str
) -> Any:
    """GET a search API (no redirects, no address check: the host is fixed or set by the user)."""
    parts = urlsplit(url)
    req = HttpRequest(
        parts.scheme,
        _host(parts),
        parts.port or (443 if parts.scheme == "https" else 80),
        (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
        {"User-Agent": USER_AGENT, "Accept": "application/json", **headers},
        timeout=SEARCH_SECONDS,
        max_bytes=MAX_SEARCH_BYTES,
        abort=abort,
    )
    try:
        resp = net.transport(req)
    except InterruptedError:
        raise Cancelled from None
    except OSError as e:
        raise ToolError(f"{name}: {e or type(e).__name__}") from None
    if resp.status in (429, 503):
        said = " ".join(resp.body[:200].decode("utf-8", "replace").split())
        said = f": {said}" if said and not said.startswith(("<", "{")) else ""
        raise ToolError(
            f"{name}: rate limited (HTTP {resp.status}{said}); try again later."
        )
    if resp.status != 200:
        raise ToolError(f"{name}: HTTP {resp.status}.")
    try:
        return json.loads(resp.body)
    except ValueError:
        raise ToolError(f"{name}: the answer is not JSON.") from None


Hit = tuple[str, str, str]  # title, url, snippet


def search_wikipedia(net: Net, lang: str, query: str, n: int, abort: Any) -> list[Hit]:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": n,
        "srprop": "snippet",
        "format": "json",
        "formatversion": 2,
        "utf8": 1,
    }
    base = f"https://{lang}.wikipedia.org"
    data = _get_json(
        net, f"{base}/w/api.php?{urlencode(params)}", {}, abort, "Wikipedia"
    )
    if isinstance(data, dict) and "error" in data:
        info = data["error"].get("info") if isinstance(data["error"], dict) else ""
        raise ToolError(f"Wikipedia: {info or 'error'}")
    found = (
        (data.get("query") or {}).get("search") or [] if isinstance(data, dict) else []
    )
    return [
        (
            r["title"],
            f"{base}/wiki/{quote(r['title'].replace(' ', '_'))}",
            _clean(r.get("snippet", "")),
        )
        for r in found
        if isinstance(r, dict) and r.get("title")
    ]


def search_marginalia(net: Net, query: str, n: int, abort: Any) -> list[Hit]:
    url = f"https://{MARGINALIA}/search?{urlencode({'query': query, 'count': n})}"
    data = _get_json(net, url, {"API-Key": "public"}, abort, "Marginalia")
    found = data.get("results") or [] if isinstance(data, dict) else []
    return [
        (r.get("title") or r["url"], r["url"], _clean(r.get("description", "")))
        for r in found
        if isinstance(r, dict) and isinstance(r.get("url"), str)
    ]


def search_searxng(net: Net, base: str, query: str, n: int, abort: Any) -> list[Hit]:
    url = f"{base}/search?{urlencode({'q': query, 'format': 'json'})}"
    data = _get_json(net, url, {}, abort, "SearXNG")
    found = data.get("results") or [] if isinstance(data, dict) else []
    return [
        (r.get("title") or r["url"], r["url"], _clean(r.get("content", "")))
        for r in found[:n]
        if isinstance(r, dict) and isinstance(r.get("url"), str)
    ]


def search_hosts(settings: Settings) -> list[str]:
    hosts = []
    if settings.searxng_url:
        hosts.append(urlsplit(settings.searxng_url).netloc)
    hosts.append(f"{settings.wikipedia_language}.wikipedia.org")
    if settings.marginalia:
        hosts.append(MARGINALIA)
    return hosts


def build(settings: Settings, net: Net) -> list[Tool]:
    def web_search(args: dict[str, Any], ctx: Context) -> str:
        query, n = args["query"], args["max_results"]
        sources = []
        if settings.searxng_url:
            sources.append(
                (
                    "SearXNG",
                    lambda: search_searxng(
                        net, settings.searxng_url, query, n, ctx.abort
                    ),
                )
            )
        sources.append(
            (
                "Wikipedia",
                lambda: search_wikipedia(
                    net, settings.wikipedia_language, query, n, ctx.abort
                ),
            )
        )
        if settings.marginalia:
            sources.append(
                ("Marginalia", lambda: search_marginalia(net, query, n, ctx.abort))
            )
        hits: list[tuple[str, Hit]] = []
        errors: list[str] = []
        for (
            name,
            run,
        ) in sources:  # one after another: no parallel requests to these services
            ctx.check_abort()
            try:
                hits += [(name, h) for h in run()]
            except ToolError as e:
                errors.append(str(e))
        seen: set[str] = set()
        lines = []
        for name, (title, url, snippet) in hits:
            if url in seen or len(seen) >= n:
                continue
            seen.add(url)
            lines.append(f"{len(seen)}. {title}\n   {url}\n   {snippet} [{name}]")
        if not lines and errors:
            raise ToolError("Search failed. " + " ".join(errors))
        text = "\n".join(lines) or f"No results for {query!r}."
        if errors:
            text += "\n\nNot available: " + " ".join(errors)
        return text

    def fetch_url(args: dict[str, Any], ctx: Context) -> str:
        page = read_page(args["url"], net, ctx.abort)
        head = f"URL: {page.url}"
        if page.truncated:
            head += f"\n[Only the first {MAX_PAGE // 1024 // 1024} MB of the page was read.]"
        return f"{head}\n\n{page.text}"

    return [
        Tool(
            name="web_search",
            description=(
                "Search the web: Wikipedia plus a small independent web index"
                + (" and a general search engine" if settings.searxng_url else "")
                + ". Returns titles, URLs and snippets. The query leaves this Mac; never put private text in it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 300},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 8,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=Risk.R2,
            run=web_search,
            network=True,
            untrusted_output=True,
            explain=lambda a: (
                f"Sends the query {a.get('query', '')!r} to {', '.join(search_hosts(settings))}."
            ),
        ),
        Tool(
            name="fetch_url",
            description=(
                "Download one public web page (http or https) and return its readable text and links. The URL "
                "leaves this Mac; never put private text in it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1, "maxLength": 2048}
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            risk=Risk.R2,
            run=fetch_url,
            network=True,
            untrusted_output=True,
            explain=lambda a: (
                f"Downloads {a.get('url', '')} (the site sees this Mac's IP address)."
            ),
            host=lambda a: url_host(a.get("url", "")),
        ),
    ]
