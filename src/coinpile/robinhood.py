"""Robinhood in Coinpile. Firefly III cannot hold stock positions, so this lives in Coinpile only.

Main account: Robinhood's own activity-report CSV, uploaded on the Robinhood tab and parsed here (no account access at all), or the optional
live connector container. Agent account: the optional rh-agent connector container (Robinhood's official MCP, read tools only).
Each account's total shows up as an investment account in Coinpile, so net worth includes it."""
import os

import httpx

from coinpile import sync
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
    def __init__(self, db, csv_dir: str, transport=None):
        self.db, self.csv_dir = db, csv_dir
        self.urls = {"agent": os.environ.get("RH_AGENT_URL", ""), "live": os.environ.get("RH_LIVE_URL", "")}
        self.secret = os.environ.get("CONNECTOR_SECRET", "")
        self.c = httpx.Client(timeout=20, transport=transport)

    def fetch(self, kind: str):
        out = {}
        if kind == "csv":
            if not (os.path.isdir(self.csv_dir) and os.listdir(self.csv_dir)):
                return None
            con = CsvConnector(self.csv_dir)
            for ep in ("balances", "positions", "history"):
                try:
                    out[ep] = getattr(con, ep)()
                    out["as_of"], out["source"] = now_iso(), con.name
                except Exception as e:
                    out[ep] = None
                    out.setdefault("errors", []).append(f"{ep}: {type(e).__name__}")
            return out
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
        fname = f"import-{sync.now().replace(':', '').replace('+', 'Z')}.csv"
        with open(os.path.join(self.csv_dir, fname), "wb") as f:
            f.write(raw)
        return fname
