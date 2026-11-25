"""Web search for the agent through the person's own SearXNG, and reading a page it found.

SearXNG answers JSON only when its settings allow it (search.formats: [html, json]). Queries go to that server and from there to the search
engines it is set to use, so the agent is told never to put personal details (names, balances, account numbers) in a query.
A page is fetched only when the search just returned it, through the same guard as every other outbound call, and reduced to plain text."""
import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from coinpile import netguard

MAX_PAGE = 2_000_000
TEXT_LIMIT = 6000
TOTAL_SECONDS = 30


class SearchError(Exception):
    pass


def _ask(base_url: str, query: str, transport=None, time_range: str | None = None, insecure: bool = False) -> dict:
    """SearXNG's JSON answer, or a SearchError that says what to change."""
    if not base_url:
        raise SearchError("no SearXNG address is set (Settings, Agent models, Web search)")
    base = base_url.strip().rstrip("/")
    if base.endswith("/search"):
        base = base[:-7]                                  # the address of the search page pasted instead of the server's
    if not base.startswith(("http://", "https://")):
        raise SearchError("the SearXNG address starts with http:// or https:// (for example http://192.168.1.20:8080)")
    q = " ".join(str(query or "").split())[:300]
    if not q:
        raise SearchError("empty query")
    params = {"q": q, "format": "json", "safesearch": 1}
    if time_range in ("day", "week", "month", "year"):
        params["time_range"] = time_range
    host = urlparse(base).hostname or base
    try:
        with netguard.client(transport, timeout=20, verify=not insecure, follow_redirects=True) as c:
            r = c.get(base + "/search", params=params, headers={"Accept": "application/json", "User-Agent": "Coinpile"})
    except netguard.Blocked as e:
        raise SearchError(f"{e}. Coinpile runs in its own container: use the SearXNG container's name (on a shared Docker network) or your server's IP address, not localhost")
    except httpx.ConnectError as e:
        if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper():
            raise SearchError(f"{host}'s certificate is not trusted. Tick \"Trust this server's own certificate\" for a self-signed or home certificate, or use its http:// address")
        raise SearchError(f"could not connect to {host}: check the address and port, and that Coinpile's container can reach it (same Docker network, or your server's IP)")
    except httpx.TimeoutException:
        raise SearchError(f"{host} did not answer in 20 seconds")
    except httpx.HTTPError as e:
        raise SearchError(f"could not reach SearXNG ({type(e).__name__})")
    if r.status_code == 401:
        raise SearchError("SearXNG asks for a login: put it in the address, http://user:password@host:port")
    if r.status_code == 403:
        raise SearchError("SearXNG refused JSON: add json to search.formats in its settings.yml, then restart it")
    if r.status_code == 429:
        raise SearchError("SearXNG's limiter blocked Coinpile: set server.limiter: false in its settings.yml (fine for a private SearXNG), or let Coinpile's address through its bot detection")
    if r.status_code == 404:
        raise SearchError(f"{host} has no SearXNG search page at that address: use the address you open SearXNG at in a browser")
    if r.status_code >= 400:
        raise SearchError(f"SearXNG answered {r.status_code}")
    try:
        data = r.json()
        assert isinstance(data, dict)
    except (ValueError, AssertionError):
        raise SearchError("SearXNG did not answer in JSON: add json to search.formats in its settings.yml (or this address is not SearXNG)")
    return data


def probe(base_url: str, transport=None, insecure: bool = False, query: str = "current CD rates") -> dict:
    """The Test button: results, and which search engines answered or failed."""
    data = _ask(base_url, query, transport, insecure=insecure)
    rows = _rows(data.get("results") or [], 3)
    engines = sorted({e for x in data.get("results") or [] for e in (x.get("engines") or [x.get("engine")]) if e})
    down = [str(u[0]) + (f" ({u[1]})" if len(u) > 1 and u[1] else "") for u in data.get("unresponsive_engines") or [] if u]
    if not rows:
        raise SearchError("SearXNG answered but found nothing" + (f": these engines failed: {', '.join(down)}" if down else ": check that some search engines are enabled in it"))
    return {"ok": True, "results": rows, "engines": engines, "unresponsive": down}


def search(base_url: str, query: str, transport=None, limit: int = 8, time_range: str | None = None, insecure: bool = False) -> list[dict]:
    return _rows(_ask(base_url, query, transport, time_range, insecure).get("results") or [], limit)


def _rows(rows: list, limit: int) -> list[dict]:
    out, seen = [], set()
    for x in rows:
        url = str(x.get("url") or "")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append({"title": str(x.get("title") or "")[:200], "url": url, "snippet": " ".join(str(x.get("content") or "").split())[:400],
                    "site": urlparse(url).hostname or "", "published": str(x.get("publishedDate") or "")[:10] or None})
        if len(out) >= limit:
            break
    return out


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def _body(r, deadline: float) -> bytes:
    """The page's bytes, never more than MAX_PAGE in memory and never past the deadline. The body is read as it is sent (still compressed)
    and unpacked here with a limit, so a small compressed page cannot unfold into gigabytes."""
    import time
    import zlib
    enc = (r.headers.get("content-encoding") or "").strip().lower()
    if enc not in ("", "identity", "gzip", "x-gzip", "deflate"):
        raise SearchError("the page is packed in a way that cannot be read safely")
    unpack = zlib.decompressobj(47) if enc in ("gzip", "x-gzip") else zlib.decompressobj() if enc == "deflate" else None
    if r.is_stream_consumed:                      # a ready-made answer (a stand-in transport in tests): it is in memory already
        if len(r.content) > MAX_PAGE:
            raise SearchError("the page is too large")
        return r.content
    out, raw = bytearray(), 0
    for chunk in r.iter_raw(16384):
        if time.monotonic() > deadline:
            raise SearchError("the page took too long")
        raw += len(chunk)
        if unpack:
            try:
                while chunk:
                    out += unpack.decompress(chunk, MAX_PAGE + 1 - len(out))
                    chunk = unpack.unconsumed_tail
                    if len(out) > MAX_PAGE:
                        break
            except zlib.error:
                raise SearchError("the page could not be unpacked")
        else:
            out += chunk
        if len(out) > MAX_PAGE or raw > MAX_PAGE:
            raise SearchError("the page is too large")
    return bytes(out)


def read_page(url: str, transport=None) -> dict:
    """A page a web search found. It came from the internet, so it (and anything it redirects to) may only be on the public internet."""
    import time
    if urlparse(url).scheme not in ("http", "https"):
        raise SearchError("only web pages can be read")
    deadline = time.monotonic() + TOTAL_SECONDS
    try:
        with netguard.client(transport, public_only=True, timeout=10, max_redirects=5) as c:
            with c.stream("GET", url, headers={"Accept": "text/html,text/plain;q=0.9", "Accept-Encoding": "gzip, deflate", "User-Agent": "Coinpile/1 (personal finance assistant)"},
                          follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise SearchError(f"the page answered {r.status_code}")
                ctype = r.headers.get("content-type", "")
                if "html" not in ctype and not ctype.startswith("text/"):
                    raise SearchError(f"not a web page ({ctype.split(';')[0] or 'unknown type'})")
                raw, final, charset = _body(r, deadline), str(r.url), r.charset_encoding or "utf-8"
    except (httpx.HTTPError, netguard.Blocked) as e:
        raise SearchError(f"could not read the page ({type(e).__name__})")
    try:
        body = raw.decode(charset, "replace")
    except LookupError:
        body = raw.decode("utf-8", "replace")
    if "html" in ctype:
        p = _Text()
        p.feed(body)
        text, title = "".join(p.parts), p.title
    else:
        text, title = body, ""
    text = re.sub(r"[ \t\r\f\v]+", " ", html.unescape(text))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return {"url": final, "title": " ".join(title.split())[:200], "text": text[:TEXT_LIMIT], "truncated": len(text) > TEXT_LIMIT}
