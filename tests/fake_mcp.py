"""A stand-in for Robinhood's MCP server and its sign-in (OAuth 2.1 + PKCE + dynamic registration), shaped like the MCP spec."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx

MCP = "https://agent.example.com/mcp/trading"


class FakeRobinhood:
    def __init__(self):
        self.calls, self.challenges, self.codes, self.tokens, self.refresh = [], {}, {}, set(), {}
        self.n = 0

    def approve(self, authorize_url: str) -> str:
        """What the person does on Robinhood's sign-in page: returns the callback URL with a code."""
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        assert q["code_challenge_method"] == "S256" and q["resource"] == MCP
        self.n += 1
        code = f"code{self.n}"
        self.codes[code] = q["code_challenge"]
        return f"{q['redirect_uri']}?code={code}&state={q['state']}"

    def transport(self):
        def h(req: httpx.Request):
            u, p = str(req.url), req.url.path
            if u.startswith(MCP):
                tok = req.headers.get("authorization", "").removeprefix("Bearer ")
                if tok not in self.tokens:
                    return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://agent.example.com/.well-known/oauth-protected-resource/mcp/trading"'})
                m = json.loads(req.content)
                self.calls.append(m.get("method") + (":" + m["params"]["name"] if m.get("method") == "tools/call" else ""))
                if m["method"] == "initialize":
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}}, headers={"Mcp-Session-Id": "s1"})
                if "id" not in m:
                    return httpx.Response(202)
                if m["method"] == "tools/list":
                    names = ["get_accounts", "get_portfolio", "get_equity_positions", "get_crypto_positions", "place_equity_order", "cancel_equity_order"]
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": n} for n in names]}})
                name = m["params"]["name"]
                data = {"get_portfolio": {"portfolio": {"market_value": "7340.25", "buying_power": "412.10"}},
                        "get_equity_positions": {"results": [{"symbol": "NVDA", "quantity": "10", "average_buy_price": "120", "market_value": "1450"},
                                                             {"symbol": "VTI", "quantity": "20", "average_buy_price": "250", "market_value": "5890.25"}]},
                        "get_crypto_positions": {"results": []}}.get(name, {})
                body = {"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": json.dumps(data)}]}}
                if name == "get_equity_positions":                 # sent as a server-sent event stream, as streamable HTTP may
                    return httpx.Response(200, text="event: message\ndata: " + json.dumps(body) + "\n\n", headers={"content-type": "text/event-stream"})
                return httpx.Response(200, json=body)
            if u == "https://agent.example.com/.well-known/oauth-protected-resource/mcp/trading":
                return httpx.Response(200, json={"resource": MCP, "authorization_servers": ["https://auth.example.com"]})
            if u == "https://auth.example.com/.well-known/oauth-authorization-server":
                return httpx.Response(200, json={"issuer": "https://auth.example.com", "authorization_endpoint": "https://auth.example.com/authorize",
                                                 "token_endpoint": "https://auth.example.com/token", "registration_endpoint": "https://auth.example.com/register",
                                                 "scopes_supported": ["read", "trade"]})
            if u == "https://auth.example.com/register":
                b = json.loads(req.content)
                assert b["token_endpoint_auth_method"] == "none"
                return httpx.Response(201, json={"client_id": "coinpile-client-1"})
            if u == "https://auth.example.com/token":
                f = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
                if f["grant_type"] == "authorization_code":
                    ch = self.codes.pop(f["code"], None)
                    want = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                    if ch != want:
                        return httpx.Response(400, json={"error": "invalid_grant"})
                elif f["grant_type"] == "refresh_token":
                    if f["refresh_token"] not in self.refresh:
                        return httpx.Response(400, json={"error": "invalid_grant"})
                self.n += 1
                tok, ref = f"rh-access-{self.n}-" + "x" * 20, f"rh-refresh-{self.n}"
                self.tokens.add(tok)
                self.refresh[ref] = tok
                return httpx.Response(200, json={"access_token": tok, "refresh_token": ref, "expires_in": 3600, "token_type": "Bearer"})
            return httpx.Response(404)
        return httpx.MockTransport(h)
