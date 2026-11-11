"""Robinhood in Coinpile. Firefly III cannot hold stock positions, so this lives in Coinpile only.

Main account: Robinhood's own activity-report CSV, uploaded on the Robinhood tab and parsed here (no account access at all), or the optional
live connector container. Agent account: the optional rh-agent connector container (Robinhood's official MCP, read tools only).
Each account's total shows up as an investment account in Coinpile, so net worth includes it."""
import os

import httpx

from coinpile import mcp, settings as cfg, sync

MCP_URL = "https://agent.robinhood.com/mcp/trading"
# The only Robinhood MCP tools Coinpile may ever call: reads. Order, cancel, watchlist and alert tools are not here, so they cannot be called.
READ_TOOLS = {"get_accounts", "get_portfolio", "get_equity_positions", "get_crypto_positions", "get_option_positions", "get_realized_pnl"}


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _find(d, keys, depth: int = 0):
    """The first of `keys` anywhere in a nested answer (the server's exact layout is not published, so look for the usual names)."""
    if depth > 12:
        return None
    if isinstance(d, dict):
        for k in keys:
            if k in d and _num(d[k]) is not None:
                return _num(d[k])
        for v in d.values():
            x = _find(v, keys, depth + 1)
            if x is not None:
                return x
    if isinstance(d, list):
        for v in d:
            x = _find(v, keys, depth + 1)
            if x is not None:
                return x
    return None


def _rows(d, depth: int = 0) -> list[dict]:
    """The longest list of objects in an answer: that is the positions."""
    best = []
    if depth > 12:
        return best
    if isinstance(d, list) and d and all(isinstance(x, dict) for x in d):
        best = d
    if isinstance(d, dict):
        for v in d.values():
            r = _rows(v, depth + 1)
            if len(r) > len(best):
                best = r
    elif isinstance(d, list):
        for v in d:
            r = _rows(v, depth + 1)
            if len(r) > len(best):
                best = r
    return best


def positions_from(d) -> list[dict]:
    out = []
    for p in _rows(d):
        inst = p.get("instrument") if isinstance(p.get("instrument"), dict) else {}
        sym = p.get("symbol") or p.get("ticker") or p.get("instrument_symbol") or inst.get("symbol") or (p.get("currency") or {}).get("code") if isinstance(p.get("currency"), dict) else (p.get("symbol") or p.get("ticker") or p.get("instrument_symbol") or inst.get("symbol"))
        qty = _num(p.get("quantity") or p.get("shares") or p.get("qty"))
        if not sym or qty is None:
            continue
        avg = _num(p.get("average_buy_price") or p.get("average_cost") or p.get("avg_cost") or p.get("cost_basis_per_share"))
        val = _num(p.get("market_value") or p.get("equity") or p.get("value"))
        price = _num(p.get("price") or p.get("last_price") or p.get("current_price"))
        out.append({"symbol": str(sym)[:12], "quantity": qty, "net_cost": round(avg * qty, 2) if avg is not None else None,
                    "equity": round(val if val is not None else (price * qty if price is not None else 0), 2) if (val is not None or price is not None) else None})
    return out
from coinpile.connectors.base import now_iso
from coinpile.connectors.rh_csv import CsvConnector


def equity(bal) -> float | None:
    if isinstance(bal, dict):
        for k in ("equity", "total_equity", "portfolio_value", "market_value", "total"):
            try:
                if bal.get(k) is not None:
                    return float(bal[k])
            except (TypeError, ValueError):
                pass
    return None


class Robinhood:
    def __init__(self, db, csv_dir: str, transport=None, email: str = "", mcp_transport=None):
        """The live and agent connectors are one Robinhood account: they are used only for the person named in RH_OWNER (their Firefly III email).
        Everyone else gets the CSV upload only, so nobody is ever shown someone else's portfolio."""
        self.db, self.csv_dir = db, csv_dir
        owner = os.environ.get("RH_OWNER", "").strip().lower()
        mine = bool(owner) and owner == (email or "").strip().lower()
        self.urls = {"agent": os.environ.get("RH_AGENT_URL", "") if mine else "", "live": os.environ.get("RH_LIVE_URL", "") if mine else ""}
        self.secret = os.environ.get("CONNECTOR_SECRET", "")
        self.c = httpx.Client(timeout=20, transport=transport)
        self.mcp_transport = mcp_transport

    # ---------------- Robinhood's official MCP (the person's own agentic account, signed in on Robinhood's site)
    def mcp_token_set(self) -> bool:
        return bool(cfg.get(self.db, "rh_mcp_token"))

    def mcp_token(self) -> str:
        tok = cfg.get(self.db, "rh_mcp_token")
        if not tok:
            return ""
        import time
        exp = float(cfg.get(self.db, "rh_mcp_expires") or 0)
        if exp and exp - time.time() < 120 and cfg.get(self.db, "rh_mcp_refresh") and cfg.get(self.db, "rh_mcp_meta"):    # mcp.exchange raises only MCPError
            import json
            d = mcp.exchange(json.loads(cfg.get(self.db, "rh_mcp_meta")), cfg.get(self.db, "rh_mcp_client"),
                             {"grant_type": "refresh_token", "refresh_token": cfg.get(self.db, "rh_mcp_refresh")}, self.mcp_transport)
            self.save_tokens(d)
            return d["access_token"]
        return tok

    def save_tokens(self, d: dict) -> None:
        import time
        cfg.set_value(self.db, "rh_mcp_token", d["access_token"])
        if d.get("refresh_token"):
            cfg.set_value(self.db, "rh_mcp_refresh", d["refresh_token"])
        cfg.set_value(self.db, "rh_mcp_expires", str(time.time() + int(d.get("expires_in") or 3600)) if d.get("expires_in") else "")

    def mcp_client(self):
        tok = self.mcp_token()
        return mcp.Client(cfg.get(self.db, "rh_mcp_url") or MCP_URL, tok, self.mcp_transport, READ_TOOLS) if tok else None

    def fetch_mcp(self):
        try:
            c = self.mcp_client()
        except mcp.MCPError as e:
            return {"error": str(e), "source": "robinhood-mcp"}
        if not c:
            return None
        out = {"source": "robinhood-mcp", "as_of": sync.now()}
        try:
            port = c.call("get_portfolio")
            out["balances"] = {"equity": _find(port, ("market_value", "equity", "total_equity", "portfolio_value", "total_value")),
                               "buying_power": _find(port, ("buying_power", "cash", "withdrawable_amount"))}
        except (mcp.MCPError, PermissionError) as e:
            out.setdefault("errors", []).append(f"portfolio: {e}")
        pos = []
        for tool in ("get_equity_positions", "get_crypto_positions"):
            try:
                pos += positions_from(c.call(tool))
            except (mcp.MCPError, PermissionError) as e:
                if isinstance(e, mcp.NeedsSignIn):
                    raise
                out.setdefault("errors", []).append(f"{tool.split('_')[1]}: {e}")
        out["positions"] = pos
        if (out.get("balances") or {}).get("equity") is None and pos and all(p.get("equity") is not None for p in pos):
            out.setdefault("balances", {})["equity"] = round(sum(p["equity"] for p in pos), 2)
        return out

    def fetch(self, kind: str):
        out = {}
        if kind == "csv":
            if not (os.path.isdir(self.csv_dir) and os.listdir(self.csv_dir)):
                return None
            con = CsvConnector(self.csv_dir)
            for ep in ("balances", "positions", "monthly"):
                try:
                    out[ep] = getattr(con, ep)()
                    out["as_of"], out["source"] = now_iso(), con.name
                except Exception as e:
                    out[ep] = None
                    out.setdefault("errors", []).append(f"{ep}: {type(e).__name__}")
            return out
        if kind == "agent" and cfg.get(self.db, "rh_mcp_token"):
            try:
                return self.fetch_mcp()                  # the person's own Robinhood, signed in from Coinpile
            except mcp.MCPError as e:
                return {"error": str(e), "source": "robinhood-mcp"}
        base = self.urls.get(kind)
        if not base:
            return None
        for ep in ("balances", "positions", "history"):
            try:
                r = self.c.get(f"{base}/{ep}", headers={"X-Internal-Secret": self.secret})
                r.raise_for_status()
                j = r.json()
                out[ep], out["as_of"], out["source"] = j["data"], j["as_of"], j["source"]
            except (httpx.HTTPError, ValueError, KeyError) as e:
                out[ep] = None
                out.setdefault("errors", []).append(f"{ep}: {type(e).__name__}")
        return out

    def main(self, pref: str = "auto"):
        order = {"auto": ["live", "csv"], "live": ["live"], "csv": ["csv"]}.get(pref, ["live", "csv"])
        for src in order:
            d = self.fetch(src)
            if d and d.get("positions") is not None:
                return d
        return None

    def probe(self, kind: str) -> str:
        if kind == "agent" and cfg.get(self.db, "rh_mcp_token"):
            return "up"                                   # signed in to Robinhood's MCP from Coinpile
        if not self.urls.get(kind):
            return "not deployed"
        try:
            return "up" if self.c.get(f"{self.urls[kind]}/health").status_code == 200 else "error"
        except httpx.HTTPError:
            return "down"

    def snapshot(self, pref: str = "auto") -> dict:
        got = {}
        for acct, label, data in (("rh:main", "Robinhood (main)", self.main(pref)), ("rh:agent", "Robinhood (agent)", self.fetch("agent"))):
            eq = equity(data.get("balances")) if data else None
            if eq is None:
                continue
            with self.db.tx() as c:
                c.execute("""INSERT INTO accounts(id,source,institution,name,kind,balance,updated) VALUES(?,?,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET balance=excluded.balance,updated=excluded.updated""", (acct, "robinhood", "Robinhood", label, "investment", eq, sync.now()))
            sync.record_balance(self.db, acct, label, eq, "investment")
            got[acct] = eq
        return got

    def save_csv(self, raw: bytes) -> str:
        head = raw[:2000].decode("utf-8", "replace")
        if "Trans Code" not in head or "Activity Date" not in head:
            raise ValueError("not a Robinhood activity report (expected Activity Date and Trans Code columns)")
        os.makedirs(self.csv_dir, exist_ok=True)
        if len(os.listdir(self.csv_dir)) >= 200:
            raise ValueError("there are already 200 reports here; delete old ones first")
        fname = f"import-{sync.now().replace(':', '').replace('+', 'Z')}-{os.urandom(3).hex()}.csv"
        with open(os.path.join(self.csv_dir, fname), "wb") as f:
            f.write(raw)
        return fname
