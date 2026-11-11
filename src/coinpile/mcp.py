"""A small, read-only MCP client (Model Context Protocol, streamable HTTP) with the MCP sign-in flow (OAuth 2.1 with PKCE and dynamic client
registration), used for Robinhood's official MCP server (Agentic Trading, https://agent.robinhood.com/mcp/trading).

Read-only by construction: `call()` refuses any tool that is not on the caller's allowlist, and Coinpile's Robinhood allowlist holds only get_* reads.
Nothing in Coinpile can place, cancel or change an order."""
import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode, urlparse

import httpx

from coinpile import netguard

PROTOCOL = "2025-06-18"


class MCPError(Exception):
    pass


class NeedsSignIn(MCPError):
    pass


MAX_BODY = 5_000_000


def _https(u, what: str) -> str:
    """Sign-in addresses a server hands us are opened in the person's browser or posted to: https only (a javascript: or http: one is refused)."""
    if not isinstance(u, str) or urlparse(u).scheme != "https" or not urlparse(u).hostname:
        raise MCPError(f"the MCP server gave an unsafe {what} address")
    return u


def _rpc_result(r: httpx.Response) -> dict:
    """A JSON-RPC answer, sent as plain JSON or as a server-sent event stream."""
    if len(r.content) > MAX_BODY:
        raise MCPError("the MCP server sent too much")
    if r.status_code == 401:
        raise NeedsSignIn("Robinhood needs you to sign in again")
    if r.status_code >= 400:
        raise MCPError(f"the MCP server answered {r.status_code}")
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        msg = None
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    m = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if "result" in m or "error" in m:
                    msg = m
        if msg is None:
            raise MCPError("the MCP server sent no answer")
    else:
        try:
            msg = r.json()
        except ValueError:
            raise MCPError("the MCP server did not answer in JSON")
    if msg.get("error"):
        raise MCPError(str(msg["error"].get("message") if isinstance(msg["error"], dict) else msg["error"])[:200])
    return msg.get("result") or {}


class Client:
    def __init__(self, url: str, token: str = "", transport=None, allow: set[str] | None = None):
        self.url, self.allow = url, set(allow or ())
        self.http = netguard.client(transport, timeout=30)
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._id, self.session = 0, None

    def _post(self, method: str, params: dict | None = None, notify: bool = False) -> dict:
        body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self._id += 1
            body["id"] = self._id
        h = dict(self.headers)
        if self.session:
            h["Mcp-Session-Id"] = self.session
        try:
            r = self.http.post(self.url, json=body, headers=h)
        except httpx.HTTPError as e:
            raise MCPError(f"could not reach the MCP server ({type(e).__name__})")
        if notify:
            return {}
        if r.headers.get("mcp-session-id"):
            self.session = r.headers["mcp-session-id"]
        return _rpc_result(r)

    def start(self) -> dict:
        info = self._post("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "Coinpile", "version": "1"}})
        self._post("notifications/initialized", notify=True)
        return info

    def tools(self) -> list[str]:
        if not self.session:
            self.start()
        return [t.get("name") for t in self._post("tools/list").get("tools", [])]

    def call(self, name: str, args: dict | None = None):
        """Call one READ tool. Anything not on the allowlist is refused here, before a request is made."""
        if name not in self.allow:
            raise PermissionError(f"'{name}' is not an allowed read tool")
        if not self.session:
            self.start()
        res = self._post("tools/call", {"name": name, "arguments": args or {}})
        if res.get("isError"):
            raise MCPError(f"{name} failed")
        if isinstance(res.get("structuredContent"), (dict, list)):
            return res["structuredContent"]
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return {"text": joined[:4000]}


# ---------------------------------------------------------------- signing in (MCP authorization: OAuth 2.1, PKCE, dynamic registration)
def discover(url: str, transport=None) -> dict:
    """Find where to sign in: the server's protected-resource metadata, then its authorization server's metadata."""
    http = netguard.client(transport, timeout=20)
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    meta_url = None
    try:
        r = http.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "Coinpile", "version": "1"}}},
                      headers={"Accept": "application/json, text/event-stream"})
        for part in r.headers.get("www-authenticate", "").split(","):
            if "resource_metadata" in part:
                meta_url = part.split("=", 1)[1].strip().strip('"')
    except httpx.HTTPError:
        pass
    candidates = [meta_url] if meta_url else []
    candidates += [f"{base}/.well-known/oauth-protected-resource{u.path}", f"{base}/.well-known/oauth-protected-resource"]
    resource, auth_server = url, None
    for c in candidates:
        try:
            r = http.get(c)
            if r.status_code == 200:
                d = r.json()
                resource = d.get("resource") or url
                if resource.rstrip("/") != url.rstrip("/") and not url.startswith(resource.rstrip("/") + "/"):
                    raise MCPError("the MCP server's sign-in information is for a different address")       # RFC 9728: the metadata must be for this server
                auth_server = (d.get("authorization_servers") or [None])[0]
                break
        except (httpx.HTTPError, ValueError, AttributeError):
            continue
    auth_server = _https((auth_server or base).rstrip("/"), "sign-in server")
    a = urlparse(auth_server)
    for c in [f"{a.scheme}://{a.netloc}/.well-known/oauth-authorization-server{a.path}", f"{a.scheme}://{a.netloc}/.well-known/openid-configuration{a.path}",
              f"{auth_server}/.well-known/oauth-authorization-server"]:
        try:
            r = http.get(c)
            if r.status_code == 200:
                m = r.json()
                if m.get("authorization_endpoint") and m.get("token_endpoint"):
                    reg = m.get("registration_endpoint")
                    return {"resource": resource, "issuer": m.get("issuer", auth_server), "authorization_endpoint": _https(m["authorization_endpoint"], "sign-in"),
                            "token_endpoint": _https(m["token_endpoint"], "token"), "registration_endpoint": _https(reg, "registration") if reg else None,
                            "scopes": [s for s in (m.get("scopes_supported") or []) if isinstance(s, str)]}
        except (httpx.HTTPError, ValueError):
            continue
    raise MCPError("could not find where to sign in to this MCP server")


def register(meta: dict, redirect_uri: str, transport=None) -> str:
    """Dynamic client registration: Coinpile introduces itself and gets a client id (no secret: a public client using PKCE)."""
    if not meta.get("registration_endpoint"):
        raise MCPError("this MCP server does not let apps register themselves; paste a token instead")
    try:
        r = netguard.client(transport, timeout=20).post(_https(meta["registration_endpoint"], "registration"), json={
        "client_name": "Coinpile (read-only)", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    except httpx.HTTPError as e:
        raise MCPError(f"could not reach the MCP server's registration ({type(e).__name__})")
    if r.status_code not in (200, 201):
        raise MCPError(f"registering with the MCP server failed ({r.status_code})")
    return r.json()["client_id"]


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def authorize_url(meta: dict, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state, "code_challenge": challenge,
         "code_challenge_method": "S256", "resource": meta["resource"]}
    q["scope"] = " ".join(read_scopes(meta.get("scopes") or [])) or None
    if not q["scope"]:
        del q["scope"]
    return meta["authorization_endpoint"] + ("&" if "?" in meta["authorization_endpoint"] else "?") + urlencode(q)


def read_scopes(scopes: list[str]) -> list[str]:
    """Only scopes that are plainly read-only: 'read', or ending in ':read' / '.read' / '_read', and never one naming write, trade or order."""
    out = []
    for s in scopes:
        low = s.lower()
        if any(w in low for w in ("write", "trade", "order", "transfer", "withdraw", "admin")):
            continue
        if low == "read" or low.endswith((":read", ".read", "_read", "/read")) or low in ("openid", "offline_access"):
            out.append(s)
    return out


def exchange(meta: dict, client_id: str, data: dict, transport=None) -> dict:
    try:
        r = netguard.client(transport, timeout=20).post(_https(meta["token_endpoint"], "token"), data={**data, "client_id": client_id, "resource": meta["resource"]},
                                                        headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise MCPError(f"could not reach Robinhood's sign-in ({type(e).__name__})")
    if r.status_code != 200:
        raise NeedsSignIn(f"Robinhood refused the sign-in ({r.status_code})")
    try:
        d = r.json()
    except ValueError:
        raise MCPError("Robinhood's sign-in did not answer in JSON")
    if not isinstance(d, dict) or not d.get("access_token"):
        raise MCPError("Robinhood's sign-in sent no access token")
    return d
