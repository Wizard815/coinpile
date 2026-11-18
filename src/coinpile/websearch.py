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


def search(base_url: str, query: str, transport=None, limit: int = 8, time_range: str | None = None) -> list[dict]:
    if not base_url:
        raise SearchError("no SearXNG address is set (Settings, Agent models, Web search)")
    q = " ".join(str(query or "").split())[:300]
    if not q:
        raise SearchError("empty query")
    params = {"q": q, "format": "json", "safesearch": 1}
    if time_range in ("day", "week", "month", "year"):
        params["time_range"] = time_range
    try:
        with netguard.client(transport, timeout=20) as c:
            r = c.get(base_url.rstrip("/") + "/search", params=params, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise SearchError(f"could not reach SearXNG ({type(e).__name__})")
    if r.status_code == 403:
        raise SearchError("SearXNG refused JSON: add json to search.formats in its settings.yml")
    if r.status_code >= 400:
        raise SearchError(f"SearXNG answered {r.status_code}")
    try:
        rows = r.json().get("results") or []
    except ValueError:
        raise SearchError("SearXNG did not answer in JSON (add json to search.formats in its settings.yml)")
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
