"""HTTP for the web tools: address checks, a resolver and a transport, all replaceable so tests need no network.

fetch_url (app/agent/web_tools.py) resolves a host once with `resolve`, refuses it unless EVERY address is public
(public_address), and then connects to one of those checked addresses with `transport` (HttpRequest.ip). The TLS
handshake still names and verifies the real host name (SNI and certificate check). So a DNS answer that changes
between the check and the connection (DNS rebinding) cannot point the request at this Mac or the local network.

The transport sends no cookies, no credentials and no proxy: http.client keeps no cookie jar and reads no proxy
settings. It asks for an uncompressed body ("Accept-Encoding: identity") and stops reading at max_bytes, at the
deadline, or when the abort event is set.
"""

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

NAT64 = ipaddress.ip_network("64:ff9b::/96")
CHUNK = 65536
CONNECT = 5.0  # seconds a TCP connect may take
WATCH = 0.1  # seconds between the watcher's checks of the deadline and the abort event


class Blocked(Exception):
    """A host or address the tools must not reach."""


@dataclass(frozen=True)
class HttpRequest:
    scheme: str  # "http" or "https"
    host: str  # the name for the Host header, SNI and the certificate check (ASCII, IDNA-encoded)
    port: int
    target: str  # path and query, starting with "/"
    headers: dict[str, str]
    ip: str | None = (
        None  # connect to this checked address; None = let the system resolve `host`
    )
    timeout: float = 15.0  # seconds for the whole request
    max_bytes: int = 2 * 1024 * 1024
    abort: threading.Event | None = None


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""
    truncated: bool = False  # the body was longer than max_bytes


Resolver = Callable[[str, int], list[str]]
Transport = Callable[[HttpRequest], HttpResponse]


def system_resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise Blocked(f"{host}: the name does not resolve ({e.strerror}).") from None
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def public_address(text: str) -> bool:
    """Whether an address is on the public internet: not loopback, private, link-local, multicast, reserved,
    unspecified, shared (CGNAT) or documentation space, also when an IPv4 address hides inside an IPv6 one."""
    try:
        a = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a in NAT64:
        # NAT64 (DNS64 networks): the IPv4 address inside decides; the prefix itself counts as reserved
        return public_address(str(ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)))
    if isinstance(a, ipaddress.IPv6Address):
        embedded = [a.ipv4_mapped, a.sixtofour]
        if a.teredo:
            embedded.append(a.teredo[1])
        if any(e is not None and not public_address(str(e)) for e in embedded):
            return False
        if a.is_site_local:
            return False
    return bool(
        a.is_global
        and not (
            a.is_private
            or a.is_loopback
            or a.is_link_local
            or a.is_multicast
            or a.is_reserved
            or a.is_unspecified
        )
    )


def checked_addresses(host: str, port: int, resolve: Resolver) -> list[str]:
    """The host's addresses when every one is public; Blocked otherwise (one private answer is enough)."""
    try:
        addresses = [str(ipaddress.ip_address(host))]  # an address literal: no lookup
    except ValueError:
        addresses = resolve(host, port)
    if not addresses:
        raise Blocked(f"{host}: no address.")
    bad = [a for a in addresses if not public_address(a)]
    if bad:
        raise Blocked(
            f"{host} resolves to a local, private or reserved address ({bad[0]}); "
            "the agent may reach only public internet addresses."
        )
    return addresses


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    """A TCP connection that waits at most CONNECT seconds (the watcher cannot end a pending connect)."""
    sock = socket.create_connection((address, port), min(timeout, CONNECT))
    sock.settimeout(timeout)
    return sock


class _Http(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str | None, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self.ip = ip

    def connect(self) -> None:
        self.sock = self.raw = _connect(self.ip or self.host, self.port, self.timeout)


class _Https(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str | None, timeout: float) -> None:
        super().__init__(
            host, port, timeout=timeout, context=ssl.create_default_context()
        )
        self.ip = ip

    def connect(self) -> None:
        sock = _connect(self.ip or self.host, self.port, self.timeout)
        # the certificate must match the host name, whichever address we connected to
        self.sock = self.raw = self._context.wrap_socket(
            sock, server_hostname=self.host, do_handshake_on_connect=False
        )
        self.sock.do_handshake()  # after self.raw is set, so the watcher can end a stalled handshake


def http_transport(req: HttpRequest) -> HttpResponse:
    """One GET request, no redirects followed. Raises OSError (also TimeoutError) or Blocked on failure.

    A watcher thread shuts the socket down when the deadline passes or the abort event is set, so a slow server
    or Stop ends a blocked read at once (within WATCH seconds)."""
    deadline = time.monotonic() + req.timeout
    cls = _Https if req.scheme == "https" else _Http
    conn = cls(req.host, req.port, req.ip, timeout=req.timeout)
    finished = threading.Event()
    why: list[str] = []

    def watch() -> None:
        while not finished.wait(WATCH):
            if req.abort is not None and req.abort.is_set():
                why.append("stopped")
            elif time.monotonic() > deadline:
                why.append("timeout")
            else:
                continue
            # conn.sock is None once a "close" response is read; its socket stays open until the read ends
            sock = getattr(conn, "raw", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return

    threading.Thread(target=watch, daemon=True).start()
    try:
        conn.request("GET", req.target, headers=req.headers)
        resp = conn.getresponse()
        headers: dict[str, str] = {}
        for name, value in resp.getheaders():
            key = name.lower()
            headers[key] = f"{headers[key]}, {value}" if key in headers else value
        body = bytearray()
        truncated = False
        while chunk := resp.read1(CHUNK):
            body += chunk
            if len(body) > req.max_bytes:
                del body[req.max_bytes :]
                truncated = True
                break
        if why:
            raise OSError(why[0])
        return HttpResponse(resp.status, headers, bytes(body), truncated)
    except (OSError, http.client.HTTPException) as e:
        if why == ["stopped"]:
            raise InterruptedError("stopped") from None
        if why == ["timeout"]:
            raise TimeoutError(
                f"no complete answer within {req.timeout:.0f} s"
            ) from None
        if isinstance(e, http.client.HTTPException):
            raise OSError(f"bad HTTP answer: {type(e).__name__}") from None
        raise
    finally:
        finished.set()
        conn.close()
