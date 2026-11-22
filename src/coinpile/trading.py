"""The auto wallet and trading advice.

Three sessions a day (morning, midday, afternoon; New York time) on days the US stock market is open: no sessions at weekends or on
market holidays, and none after 1 pm on early-close days (markethours.py). Each session:
  - Auto wallet: TradingAgents (the trader container, your own model) analyses the wallet's stocks and some of your watchlist,
    each decision becomes an order sized by your settings, every order passes traderules.check (daily loss stop, day-trade guard,
    your AgentRisk policy), and the order is filled in the paper wallet or placed in your Robinhood Agentic account.
  - Advice: the same analysis for the stocks in your main Robinhood account (and watchlist buys), written as ideas. Nothing is
    ever ordered in your main account.

You steer it: standing guidance, guidance and a mode for the next session only (auto, ask before each order, your own plan, or skip),
approve or reject orders that wait for you, and edit the rules. Paper first: the wallet starts simulated, with real prices, until
you switch it to live.

Only the person named in RH_OWNER can use it (as with the other Robinhood connectors)."""
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from coinpile import markethours, mcp, robinhood, settings as cfg, traderules

NY = ZoneInfo("America/New_York")
RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MODES = ("auto", "ask")                    # the standing mode of a session
NEXT_MODES = ("", "auto", "ask", "manual", "skip")
LATE = timedelta(minutes=90)               # a session missed by more than this (Coinpile was down) is skipped, not run late

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_config(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trade_paper(symbol TEXT PRIMARY KEY, quantity REAL NOT NULL, avg_price REAL);   -- symbol '$CASH' holds the cash
CREATE TABLE IF NOT EXISTS trade_days(day TEXT PRIMARY KEY, start_value REAL, stopped TEXT);
CREATE TABLE IF NOT EXISTS trade_sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, slot TEXT NOT NULL, mode TEXT NOT NULL, wallet TEXT NOT NULL,
    status TEXT NOT NULL, guidance TEXT DEFAULT '', summary TEXT DEFAULT '', started TEXT, finished TEXT);
CREATE TABLE IF NOT EXISTS trade_ideas(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts TEXT, account TEXT NOT NULL, symbol TEXT NOT NULL,
    rating TEXT, action TEXT, summary TEXT, decision TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS trade_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts TEXT, day TEXT, wallet TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, quantity REAL NOT NULL, price REAL, source TEXT, rating TEXT, status TEXT NOT NULL, reason TEXT DEFAULT '', broker_ref TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS trade_sessions_slot ON trade_sessions(day, slot) WHERE slot != 'now';
"""

DEFAULTS = {
    "enabled": False,                 # the auto wallet trades at all
    "wallet": "paper",                # paper | live
    "paper_cash": 1000.0,
    "sessions": ["09:45", "12:30", "15:30"],
    "mode": "auto",                   # auto | ask (every session)
    "guidance": "",                   # standing guidance, in every session
    "next": {"mode": "", "guidance": "", "plan": []},   # for the next session only
    "watchlist": ["SPY", "QQQ"],
    "per_session": 3,                 # watchlist symbols looked at per session (rotating), on top of what the wallet holds
    "advice": True,
    "advice_per_session": 3,          # main-account holdings looked at per session (rotating)
    "buy_pct": 10.0,                  # a Buy aims for this % of the wallet in the stock; Overweight half of it
    "fractional": True,
    "daily_loss_pct": 3.0,
    "pdt_guard": True,
    "policy": traderules.DEFAULT_POLICY,
    "order_tool": "",                 # live: the Robinhood MCP tool that places an order (found on connect; you can change it)
    "extra_closed": [],               # one-off market closures the holiday rules can't know (YYYY-MM-DD)
}
TOKEN_KEYS = ("rh_trade_token", "rh_trade_refresh", "rh_trade_expires", "rh_trade_client", "rh_trade_meta")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db) -> None:
    with db.tx() as c:
        c.executescript(SCHEMA)


def owner_ok(email: str) -> bool:
    owner = os.environ.get("RH_OWNER", "").strip().lower()
    return bool(owner) and owner == (email or "").strip().lower()


# ---------------------------------------------------------------- settings
def config(db) -> dict:
    ensure(db)
    row = db.one("SELECT data FROM trade_config WHERE id=1")
    saved = json.loads(row["data"]) if row else {}
    return {**DEFAULTS, **saved, "next": {**DEFAULTS["next"], **(saved.get("next") or {})}}


def _symbols(v, what: str) -> list[str]:
    items = v if isinstance(v, list) else [x for x in re.split(r"[,\s]+", str(v or "")) if x]
    out = []
    for x in items:
        s = str(x).strip().upper()
        if not SYMBOL.match(s):
            raise ValueError(f"{what}: '{x}' is not a stock symbol")
        if s not in out:
            out.append(s)
    return out[:50]


def _plan(v) -> list[dict]:
    """Your own orders for the next session: [{side, symbol, quantity}] (quantity 'all' sells the whole position)."""
    out = []
    for p in (v or [])[:20]:
        side = str(p.get("side", "")).lower()
        if side not in ("buy", "sell"):
            raise ValueError("each planned order needs side buy or sell")
        sym = _symbols([p.get("symbol")], "plan")[0]
        q = p.get("quantity")
        if side == "sell" and str(q).lower() == "all":
            out.append({"side": side, "symbol": sym, "quantity": "all"})
            continue
        try:
            q = float(q)
        except (TypeError, ValueError):
            raise ValueError(f"plan: the quantity for {sym} must be a number" + (" or all" if side == "sell" else ""))
        if not (0 < q < 1e6):
            raise ValueError(f"plan: the quantity for {sym} must be above 0")
        out.append({"side": side, "symbol": sym, "quantity": q})
    return out


def save_config(db, patch: dict) -> dict:
    c = config(db)
    for k, v in patch.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown trading setting {k!r}")
        if k in ("enabled", "advice", "fractional", "pdt_guard"):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        elif k == "wallet":
            if v not in ("paper", "live"):
                raise ValueError("wallet must be paper or live")
        elif k == "mode":
            if v not in MODES:
                raise ValueError("mode must be auto or ask")
        elif k == "sessions":
            if not isinstance(v, list) or not (1 <= len(v) <= 6) or any(not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(t)) for t in v):
                raise ValueError("sessions are 1 to 6 times like 09:45 (New York time)")
            v = sorted(set(v))
        elif k == "watchlist":
            v = _symbols(v, "watchlist")
        elif k in ("per_session", "advice_per_session"):
            if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= 20):
                raise ValueError(f"{k} must be a whole number from 0 to 20")
        elif k in ("paper_cash", "buy_pct", "daily_loss_pct"):
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            lim = {"paper_cash": (1, 1e8), "buy_pct": (0.5, 100), "daily_loss_pct": (0, 100)}[k]
            if not (lim[0] <= v <= lim[1]):
                raise ValueError(f"{k} must be between {lim[0]:g} and {lim[1]:g}")
        elif k == "guidance":
            v = str(v or "")[:2000]
        elif k == "policy":
            traderules.parse_policy(str(v))
            v = str(v)
        elif k == "order_tool":
            v = str(v or "").strip()
            if v and not re.match(r"^[A-Za-z0-9_.\-]{1,80}$", v):
                raise ValueError("order_tool must be a tool name")
            if v and (not re.search(r"order", v, re.I) or re.search(r"cancel|transfer|withdraw|deposit|option|crypto|stake|lend", v, re.I)):
                raise ValueError("order_tool must be Robinhood's stock order tool (its name has 'order' in it; never a transfer, cancel, options or crypto tool)")
        elif k == "extra_closed":
            items = v if isinstance(v, list) else [x for x in re.split(r"[,\s]+", str(v or "")) if x]
            if any(not re.match(r"^\d{4}-\d{2}-\d{2}$", str(x)) for x in items):
                raise ValueError("extra closed days are dates like 2027-01-09")
            v = sorted(set(map(str, items)))[-50:]
        elif k == "next":
            n = v or {}
            if n.get("mode", "") not in NEXT_MODES:
                raise ValueError("next.mode must be auto, ask, manual, skip or empty")
            v = {"mode": n.get("mode", ""), "guidance": str(n.get("guidance") or "")[:2000], "plan": _plan(n.get("plan"))}
            if v["mode"] == "manual" and not v["plan"]:
                raise ValueError("a manual session needs at least one planned order")
        c[k] = v
    with db.tx() as x:
        x.execute("INSERT OR REPLACE INTO trade_config(id,data) VALUES(1,?)", (json.dumps(c),))
    return c


# ---------------------------------------------------------------- the trader container (TradingAgents)
class Trader:
    def __init__(self, url: str | None = None, secret: str | None = None, transport=None):
        self.url = (url if url is not None else os.environ.get("TRADER_URL", "")).rstrip("/")
        self.secret = secret if secret is not None else os.environ.get("CONNECTOR_SECRET", "")
        self.c = httpx.Client(timeout=httpx.Timeout(1800, connect=10), transport=transport)

    def _h(self):
        return {"X-Internal-Secret": self.secret}

    def up(self) -> str:
        if not self.url:
            return "not deployed"
        try:
            return "up" if self.c.get(f"{self.url}/health", timeout=5).status_code == 200 else "error"
        except httpx.HTTPError:
            return "down"

    def analyze(self, symbol: str, portfolio: dict, guidance: str) -> dict:
        r = self.c.post(f"{self.url}/analyze", headers=self._h(), json={"symbol": symbol, "date": datetime.now(NY).date().isoformat(), "portfolio": portfolio, "guidance": guidance})
        if r.status_code != 200:
            raise RuntimeError(f"the trader answered {r.status_code}: {r.text[:200]}")
        return r.json()

    def quotes(self, symbols) -> dict[str, float]:
        syms = sorted({s for s in symbols if SYMBOL.match(s)})
        if not syms:
            return {}
        r = self.c.get(f"{self.url}/quotes", headers=self._h(), params={"symbols": ",".join(syms)}, timeout=60)
        r.raise_for_status()
        return {k: float(v) for k, v in (r.json().get("prices") or {}).items() if v}


# ---------------------------------------------------------------- wallets
class PaperWallet:
    kind = "paper"

    def __init__(self, db, start_cash: float):
        self.db = db
        ensure(db)
        if not db.one("SELECT 1 FROM trade_paper WHERE symbol='$CASH'"):
            with db.tx() as c:
                c.execute("INSERT INTO trade_paper(symbol,quantity,avg_price) VALUES('$CASH',?,NULL)", (float(start_cash),))

    def holdings(self) -> tuple[float, list[dict]]:
        rows = self.db.all("SELECT * FROM trade_paper")
        cash = next((r["quantity"] for r in rows if r["symbol"] == "$CASH"), 0.0)
        return cash, [{"symbol": r["symbol"], "quantity": r["quantity"], "avg_price": r["avg_price"]} for r in rows if r["symbol"] != "$CASH" and r["quantity"] > 1e-9]

    def fill(self, side: str, symbol: str, qty: float, price: float) -> str:
        cash, pos = self.holdings()
        held = next((p for p in pos if p["symbol"] == symbol), None)
        cost = qty * price
        with self.db.tx() as c:
            if side == "buy":
                if cost > cash + 1e-6:
                    raise RuntimeError("not enough cash in the paper wallet")
                q0, a0 = (held["quantity"], held["avg_price"] or price) if held else (0.0, price)
                c.execute("INSERT OR REPLACE INTO trade_paper(symbol,quantity,avg_price) VALUES(?,?,?)", (symbol, q0 + qty, (q0 * a0 + cost) / (q0 + qty)))
                c.execute("UPDATE trade_paper SET quantity=quantity-? WHERE symbol='$CASH'", (cost,))
            else:
                if not held or qty > held["quantity"] + 1e-9:
                    raise RuntimeError(f"the paper wallet doesn't hold that much {symbol}")
                left = held["quantity"] - qty
                if left <= 1e-9:
                    c.execute("DELETE FROM trade_paper WHERE symbol=?", (symbol,))
                else:
                    c.execute("UPDATE trade_paper SET quantity=? WHERE symbol=?", (left, symbol))
                c.execute("UPDATE trade_paper SET quantity=quantity+? WHERE symbol='$CASH'", (cost,))
        return "paper"

    def reset(self, start_cash: float) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM trade_paper")
            c.execute("INSERT INTO trade_paper(symbol,quantity,avg_price) VALUES('$CASH',?,NULL)", (float(start_cash),))


def order_args(tool: dict, side: str, symbol: str, qty: float) -> dict:
    """What to send to Robinhood's order tool, read from the tool's own input schema: a market order, good for the day.
    Robinhood's schema isn't published, so the usual names are matched; anything required that can't be filled stops the order."""
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    required = set((tool.get("inputSchema") or {}).get("required") or [])
    out = {}

    def pick(names):
        return next((n for n in names if n in props), None)

    def enum_pick(name, wanted):
        opts = [str(o) for o in (props[name].get("enum") or [])]
        if not opts:
            return wanted[0]
        return next((o for w in wanted for o in opts if o.lower() == w), None)

    k = pick(("symbol", "ticker", "instrument_symbol", "stock_symbol"))
    if k:
        out[k] = symbol
    k = pick(("side", "action", "direction", "order_side"))
    if k:
        v = enum_pick(k, [side])
        if v is None:
            raise ValueError(f"the order tool's '{k}' doesn't offer {side}")
        out[k] = v
    k = pick(("quantity", "qty", "shares", "share_quantity"))
    if k:
        out[k] = qty if props[k].get("type") != "string" else f"{qty:g}"
    k = pick(("type", "order_type"))
    if k:
        v = enum_pick(k, ["market"])
        if v is None:
            raise ValueError(f"the order tool's '{k}' has no market order")
        out[k] = v
    k = pick(("time_in_force", "tif", "duration"))
    if k:
        v = enum_pick(k, ["gfd", "day"])
        if v is None:
            raise ValueError(f"the order tool's '{k}' has no day order")
        out[k] = v
    missing = [r for r in required if r not in out]
    if missing:
        raise ValueError(f"the order tool needs {', '.join(sorted(missing))}, which Coinpile doesn't know how to fill; pick another tool or tell Coinpile's maintainer")
    if not any(x in out for x in ("symbol", "ticker", "instrument_symbol", "stock_symbol")) or not any(x in out for x in ("quantity", "qty", "shares", "share_quantity")):
        raise ValueError("the order tool has no symbol or quantity field Coinpile recognises")
    return out


def guess_order_tool(tools: list[dict]) -> str:
    names = [t["name"] for t in tools]
    for pat in (r"^place_(equity|stock)_order$", r"^(place|submit|create)_(equity_|stock_)?order$", r"^(place|submit|create).*order", r"^(buy|sell)_(stock|equity)$"):
        hit = [n for n in names if re.search(pat, n, re.I) and not re.search(r"cancel|crypto|option", n, re.I)]
        if hit:
            return hit[0]
    return ""


class LiveWallet:
    """Your Robinhood Agentic account, through Robinhood's MCP with the trading sign-in."""
    kind = "live"

    def __init__(self, db, transport=None, order_tool: str = ""):
        self.db, self.transport, self.order_tool = db, transport, order_tool
        self._client = None

    def token(self) -> str:
        tok = cfg.get(self.db, "rh_trade_token")
        if not tok:
            return ""
        exp = float(cfg.get(self.db, "rh_trade_expires") or 0)
        if exp and exp - time.time() < 120 and cfg.get(self.db, "rh_trade_refresh") and cfg.get(self.db, "rh_trade_meta"):
            d = mcp.exchange(json.loads(cfg.get(self.db, "rh_trade_meta")), cfg.get(self.db, "rh_trade_client"),
                             {"grant_type": "refresh_token", "refresh_token": cfg.get(self.db, "rh_trade_refresh")}, self.transport)
            save_tokens(self.db, d)
            return d["access_token"]
        return tok

    def client(self) -> mcp.Client:
        if self._client is None:
            tok = self.token()
            if not tok:
                raise mcp.NeedsSignIn("connect Robinhood for trading first (Trading, Live wallet)")
            allow = set(robinhood.READ_TOOLS) | ({self.order_tool} if self.order_tool else set())
            self._client = mcp.Client(cfg.get(self.db, "rh_mcp_url") or robinhood.MCP_URL, tok, self.transport, allow)
        return self._client

    def holdings(self) -> tuple[float, list[dict]]:
        c = self.client()
        port = c.call("get_portfolio")
        cash = robinhood._find(port, ("buying_power", "cash", "withdrawable_amount"))
        pos = robinhood.positions_from(c.call("get_equity_positions"))
        if cash is None:
            raise RuntimeError("Robinhood didn't say how much cash the Agentic account has")
        return cash, [{"symbol": p["symbol"].upper(), "quantity": p["quantity"], "avg_price": (p["net_cost"] / p["quantity"]) if p.get("net_cost") and p["quantity"] else None} for p in pos]

    def tool(self) -> dict:
        if not self.order_tool:
            raise RuntimeError("no order tool is chosen (Trading, Live wallet)")
        t = next((t for t in self.client().tool_list() if t["name"] == self.order_tool), None)
        if not t:
            raise RuntimeError(f"Robinhood has no tool named {self.order_tool}")
        return t

    def fill(self, side: str, symbol: str, qty: float, price: float) -> str:
        res = self.client().call(self.order_tool, order_args(self.tool(), side, symbol, qty))
        o = res.get("order") if isinstance(res, dict) and isinstance(res.get("order"), dict) else res
        ref = (o.get("id") or o.get("order_id")) if isinstance(o, dict) else None
        return str(ref or "placed")[:80]


def save_tokens(db, d: dict) -> None:
    cfg.set_value(db, "rh_trade_token", d["access_token"])
    if d.get("refresh_token"):
        cfg.set_value(db, "rh_trade_refresh", d["refresh_token"])
    cfg.set_value(db, "rh_trade_expires", str(time.time() + int(d["expires_in"])) if d.get("expires_in") else "")


def forget_tokens(db) -> None:
    for k in TOKEN_KEYS:
        cfg.set_value(db, k, "")


# ---------------------------------------------------------------- sessions
def _round_qty(q: float, fractional: bool) -> float:
    return math.floor(q * 10000) / 10000 if fractional else float(math.floor(q))


class Engine:
    def __init__(self, db, email: str = "", trader: Trader | None = None, mcp_transport=None, notify=None, clock=None):
        self.db, self.email = db, email
        self.trader = trader or Trader()
        self.mcp_transport = mcp_transport
        self.notify = notify or (lambda text: None)
        self.clock = clock or (lambda: datetime.now(NY))
        self.lock = threading.Lock()
        ensure(db)

    @property
    def allowed(self) -> bool:
        return owner_ok(self.email)

    def wallet(self, c: dict | None = None):
        c = c or config(self.db)
        return LiveWallet(self.db, self.mcp_transport, c["order_tool"]) if c["wallet"] == "live" else PaperWallet(self.db, c["paper_cash"])

    # ---- the schedule
    def due_slot(self) -> str | None:
        c = config(self.db)
        if not self.allowed or not (c["enabled"] or c["advice"]):
            return None
        now = self.clock()
        shut = markethours.close_time(now.date(), c["extra_closed"])
        if not shut:
            return None                                   # weekend or market holiday
        day = now.date().isoformat()
        done = {r["slot"] for r in self.db.all("SELECT slot FROM trade_sessions WHERE day=?", (day,))}
        for slot in c["sessions"]:
            h, m = map(int, slot.split(":"))
            at = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if at.time() >= shut:
                continue                                  # after an early close
            if at <= now < at + LATE and slot not in done:
                return slot
        return None

    def market(self, c: dict | None = None) -> dict:
        """Today's market: open or not (and why), and when it closes."""
        c = c or config(self.db)
        now = self.clock()
        why, shut = markethours.closed(now.date(), c["extra_closed"]), markethours.close_time(now.date(), c["extra_closed"])
        return {"day": now.date().isoformat(), "open_today": not why, "closed_because": why, "closes": shut.strftime("%H:%M") if shut else None,
                "early_close": shut == markethours.EARLY_CLOSE, "open_now": bool(shut) and markethours.OPEN <= now.time() < shut}

    def next_session(self) -> str | None:
        c = config(self.db)
        now = self.clock()
        for d in range(0, 15):
            day = (now + timedelta(days=d))
            shut = markethours.close_time(day.date(), c["extra_closed"])
            if not shut:
                continue
            for slot in c["sessions"]:
                h, m = map(int, slot.split(":"))
                at = day.replace(hour=h, minute=m, second=0, microsecond=0)
                if at > now and at.time() < shut:
                    return at.isoformat(timespec="minutes")
        return None

    # ---- the wallet's state, priced
    def snapshot(self, wallet=None, prices: dict | None = None) -> dict:
        wallet = wallet or self.wallet()
        cash, pos = wallet.holdings()
        if prices is None:
            prices = self.trader.quotes([p["symbol"] for p in pos]) if pos else {}
        rows = []
        for p in pos:
            price = prices.get(p["symbol"]) or p.get("avg_price")
            rows.append({**p, "price": price, "value": round(p["quantity"] * price, 2) if price else None})
        known = all(r["value"] is not None for r in rows)
        return {"wallet": wallet.kind, "cash": round(cash, 2), "positions": rows, "value": round(cash + sum(r["value"] or 0 for r in rows), 2) if known else None}

    def _day_start(self, day: str, value: float | None) -> float | None:
        row = self.db.one("SELECT start_value FROM trade_days WHERE day=?", (day,))
        if row and row["start_value"]:
            return row["start_value"]
        if value:
            with self.db.tx() as c:
                c.execute("INSERT OR REPLACE INTO trade_days(day,start_value,stopped) VALUES(?,?,NULL)", (day, value))
        return value

    def _bought_today(self, day: str, wallet: str) -> set[str]:
        return {r["symbol"] for r in self.db.all("SELECT symbol FROM trade_orders WHERE day=? AND wallet=? AND side='buy' AND status='filled'", (day, wallet))}

    # ---- one session
    def run_session(self, slot: str = "now") -> dict:
        if not self.allowed:
            raise PermissionError("trading is only for the Robinhood owner (RH_OWNER)")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("a session is already running")
        try:
            return self._run(slot)
        finally:
            self.lock.release()

    def _run(self, slot: str) -> dict:
        c = config(self.db)
        nxt = c["next"]
        mode = nxt["mode"] or c["mode"]
        guidance = "\n".join(x for x in (c["guidance"].strip(), nxt["guidance"].strip()) if x)
        day = self.clock().date().isoformat()
        with self.db.tx() as x:
            cur = x.execute("INSERT INTO trade_sessions(day,slot,mode,wallet,status,guidance,started) VALUES(?,?,?,?,?,?,?)",
                            (day, slot, mode, c["wallet"], "running", guidance, now_iso()))
            sid = cur.lastrowid
        # the one-off settings are used up by this session
        save_config(self.db, {"next": {"mode": "", "guidance": "", "plan": []}})
        notes, orders = [], []
        try:
            if mode == "skip":
                notes.append("Skipped (you asked).")
            else:
                cache: dict[str, dict] = {}
                if c["enabled"]:
                    orders = self._auto(sid, c, mode, guidance, nxt["plan"], day, cache, notes)
                if c["advice"]:
                    self._advise(sid, c, guidance, cache, notes)
            status = "done"
        except Exception as e:
            notes.append(f"Stopped: {e}")
            status = "failed"
        summary = " ".join(notes)[:2000]
        with self.db.tx() as x:
            x.execute("UPDATE trade_sessions SET status=?, summary=?, finished=? WHERE id=?", (status, summary, now_iso(), sid))
        self._tell(sid, c, summary, orders)
        return {"session": sid, "status": status, "summary": summary, "orders": orders}

    def _analysis(self, sym: str, book: dict, guidance: str, cache: dict) -> dict:
        if sym not in cache:
            try:
                cache[sym] = self.trader.analyze(sym, book, guidance)
            except Exception as e:
                cache[sym] = {"symbol": sym, "rating": "", "error": str(e)[:300]}
        return cache[sym]

    def _rotate(self, items: list[str], n: int) -> list[str]:
        """n of items, carrying on from where the last session stopped, so the whole list is covered over the day."""
        if not items or n <= 0:
            return []
        row = self.db.one("SELECT COUNT(*) AS n FROM trade_sessions WHERE status!='running'")
        start = ((row["n"] if row else 0) * n) % len(items)
        return [items[(start + i) % len(items)] for i in range(min(n, len(items)))]

    def _auto(self, sid: int, c: dict, mode: str, guidance: str, plan: list, day: str, cache: dict, notes: list) -> list[dict]:
        mk = self.market(c)
        if not mk["open_now"]:
            notes.append(f"The market is closed ({mk['closed_because'] or 'outside trading hours'}): no orders.")
            return []
        wallet = self.wallet(c)
        snap = self.snapshot(wallet)
        start = self._day_start(day, snap["value"])
        held = [p["symbol"] for p in snap["positions"]]
        if mode == "manual":
            intents = []
            for p in plan:
                pos = next((x for x in snap["positions"] if x["symbol"] == p["symbol"]), None)
                q = (pos["quantity"] if pos else 0) if p["quantity"] == "all" else p["quantity"]
                if q:
                    intents.append({"symbol": p["symbol"], "side": p["side"], "quantity": q, "source": "your plan", "rating": ""})
            notes.append(f"Your plan: {len(intents)} order(s).")
        else:
            look = held + [s for s in self._rotate([s for s in c["watchlist"] if s not in held], c["per_session"])]
            book = {"cash": snap["cash"], "positions": [{"ticker": p["symbol"], "quantity": p["quantity"], "average_price": p.get("avg_price")} for p in snap["positions"]]}
            intents = []
            for sym in look:
                a = self._analysis(sym, book, guidance, cache)
                self._idea(sid, "agent", a, held_qty=next((p["quantity"] for p in snap["positions"] if p["symbol"] == sym), 0))
                if a.get("rating") in RATINGS:
                    intents += self._size(a["rating"], sym, snap, c)
            notes.append(f"Looked at {len(look)} stock(s) for the {c['wallet']} wallet.")
        prices = self.trader.quotes({i["symbol"] for i in intents} | set(held)) if intents else {}
        snap = self.snapshot(wallet, prices) if intents else snap
        out = []
        for i in sorted(intents, key=lambda i: i["side"] != "sell"):            # sells first, so their cash can fund the buys
            out.append(self._order(sid, c, wallet, i, prices, day, start, mode))
            snap = self.snapshot(wallet, prices)
        filled = [o for o in out if o["status"] == "filled"]
        waiting = [o for o in out if o["status"] == "asked"]
        blocked = [o for o in out if o["status"] == "blocked"]
        notes.append(f"{len(filled)} order(s) done, {len(waiting)} waiting for you, {len(blocked)} stopped by the rules." if out else "No orders.")
        return out

    def _size(self, rating: str, sym: str, snap: dict, c: dict) -> list[dict]:
        """How much to buy or sell for a rating. Each order stays under your rules' cap per trade, so a big exit happens over several sessions."""
        pos = next((p for p in snap["positions"] if p["symbol"] == sym), None)
        try:
            cap_pct = (traderules.parse_policy(c["policy"]).get("order_rules") or {}).get("max_order_pct_of_portfolio")
        except ValueError:
            cap_pct = None
        cap = snap["value"] * float(cap_pct) / 100 * 0.99 if cap_pct and snap["value"] else None
        if rating in ("Sell", "Underweight"):
            if not pos:
                return []
            q = pos["quantity"] if rating == "Sell" else _round_qty(pos["quantity"] / 2, c["fractional"])
            if cap and pos.get("price") and q * pos["price"] > cap:
                q = _round_qty(cap / pos["price"], c["fractional"])
            return [{"symbol": sym, "side": "sell", "quantity": q, "source": "agent", "rating": rating}] if q > 0 else []
        if rating in ("Buy", "Overweight") and snap["value"]:
            target = snap["value"] * c["buy_pct"] / 100 * (1 if rating == "Buy" else 0.5)
            have = (pos or {}).get("value") or 0
            want = min(target - have, snap["cash"], cap if cap else float("inf"))
            return [{"symbol": sym, "side": "buy", "value": want, "source": "agent", "rating": rating}] if want >= 1 else []
        return []

    def _order(self, sid: int, c: dict, wallet, i: dict, prices: dict, day: str, start: float | None, mode: str) -> dict:
        sym, side = i["symbol"], i["side"]
        price = prices.get(sym)
        o = {"symbol": sym, "side": side, "quantity": i.get("quantity") or 0, "price": price, "source": i["source"], "rating": i.get("rating", ""), "status": "", "reason": ""}
        if not price:
            o.update(status="failed", reason="no price for it right now")
        else:
            if "value" in i:
                o["quantity"] = _round_qty(i["value"] / price, c["fractional"])
            if o["quantity"] <= 0:
                o.update(status="failed", reason="too small to buy even one share" if not c["fractional"] else "too small")
        if not o["status"]:
            o = self._checked(c, wallet, o, prices, day, start, mode)
        with self.db.tx() as x:
            cur = x.execute("INSERT INTO trade_orders(session_id,ts,day,wallet,symbol,side,quantity,price,source,rating,status,reason,broker_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sid, now_iso(), day, wallet.kind, sym, side, o["quantity"], price, o["source"], o["rating"], o["status"], o["reason"], o.get("broker_ref")))
            o["id"] = cur.lastrowid
        return o

    def _checked(self, c: dict, wallet, o: dict, prices: dict, day: str, start: float | None, mode: str, approved: bool = False) -> dict:
        price = prices[o["symbol"]]
        snap = self.snapshot(wallet, prices)
        v = traderules.check(side=o["side"], symbol=o["symbol"], quantity=o["quantity"], price=price, cash=snap["cash"],
                             positions=[{"symbol": p["symbol"], "quantity": p["quantity"], "price": p["price"]} for p in snap["positions"]],
                             policy_text=c["policy"], start_value=start, value_now=snap["value"], daily_loss_pct=c["daily_loss_pct"],
                             bought_today=self._bought_today(day, wallet.kind), pdt_guard=c["pdt_guard"])
        if not v["ok"]:
            if v["rule"] == "daily_loss" and "down" in v["reason"]:
                with self.db.tx() as x:
                    x.execute("UPDATE trade_days SET stopped=? WHERE day=?", (v["reason"], day))
            return {**o, "status": "asked" if v["ask"] and not approved else "blocked", "reason": v["reason"]}
        if mode == "ask" and not approved:
            return {**o, "status": "asked", "reason": v["reason"] or "waiting for you"}
        try:
            ref = wallet.fill(o["side"], o["symbol"], o["quantity"], price)
            return {**o, "status": "filled", "reason": v["reason"], "broker_ref": ref}
        except Exception as e:
            return {**o, "status": "failed", "reason": str(e)[:300]}

    # ---- you decide
    def decide(self, order_id: int, approve: bool) -> dict:
        if not self.allowed:
            raise PermissionError("trading is only for the Robinhood owner (RH_OWNER)")
        with self.lock:
            o = self.db.one("SELECT * FROM trade_orders WHERE id=?", (order_id,))
            if not o or o["status"] != "asked":
                raise LookupError("that order isn't waiting for you")
            c = config(self.db)
            if not approve:
                with self.db.tx() as x:
                    x.execute("UPDATE trade_orders SET status='rejected', reason='you said no' WHERE id=?", (order_id,))
                return {**o, "status": "rejected"}
            if o["wallet"] != c["wallet"]:
                raise LookupError("this order was for the other wallet")
            mk = self.market(c)
            if not mk["open_now"]:
                raise LookupError(f"the market is closed ({mk['closed_because'] or 'outside trading hours'}); approve it when it's open")
            wallet = self.wallet(c)
            day = self.clock().date().isoformat()
            _, held = wallet.holdings()
            prices = self.trader.quotes({o["symbol"]} | {p["symbol"] for p in held})
            price = prices.get(o["symbol"])
            if not price:
                raise RuntimeError("no price for it right now; try again")
            snap = self.snapshot(wallet, prices)
            r = self._checked(c, wallet, {k: o[k] for k in ("symbol", "side", "quantity", "source", "rating")} | {"status": "", "reason": ""},
                              prices, day, self._day_start(day, snap["value"]), "auto", approved=True)
            with self.db.tx() as x:
                x.execute("UPDATE trade_orders SET status=?, reason=?, price=?, day=?, broker_ref=? WHERE id=?", (r["status"], r["reason"], price, day, r.get("broker_ref"), order_id))
            return {**o, **r}

    # ---- advice for your own account
    def _advise(self, sid: int, c: dict, guidance: str, cache: dict, notes: list) -> None:
        mine = self.db.all("SELECT symbol, quantity, net_cost FROM holdings WHERE account_id='rh:main' AND quantity > 0 ORDER BY symbol")
        syms = self._rotate([h["symbol"] for h in mine if SYMBOL.match(h["symbol"])], c["advice_per_session"])
        book = {"positions": [{"ticker": h["symbol"], "quantity": h["quantity"], "average_price": (h["net_cost"] / h["quantity"]) if h["net_cost"] and h["quantity"] else None} for h in mine]}
        for sym in syms:
            a = self._analysis(sym, book, guidance, cache)
            self._idea(sid, "main", a, held_qty=next((h["quantity"] for h in mine if h["symbol"] == sym), 0))
        buys = [s for s, a in cache.items() if a.get("rating") == "Buy" and s not in {h["symbol"] for h in mine}]
        for sym in buys:
            self._idea(sid, "main", cache[sym], held_qty=0)
        notes.append(f"Ideas for your account: {len(syms) + len(buys)}.")

    def _idea(self, sid: int, account: str, a: dict, held_qty: float) -> None:
        r = a.get("rating") or ""
        action = {"Buy": "add" if held_qty else "buy", "Overweight": "add a little" if held_qty else "consider a small buy", "Hold": "hold" if held_qty else "wait",
                  "Underweight": "trim" if held_qty else "skip", "Sell": "sell" if held_qty else "avoid"}.get(r, "")
        with self.db.tx() as x:
            x.execute("INSERT INTO trade_ideas(session_id,ts,account,symbol,rating,action,summary,decision,error) VALUES(?,?,?,?,?,?,?,?,?)",
                      (sid, now_iso(), account, a.get("symbol", ""), r, action, (a.get("trader_plan") or "")[:1500], (a.get("decision") or "")[:6000], a.get("error")))

    def _tell(self, sid: int, c: dict, summary: str, orders: list[dict]) -> None:
        lines = [f"Trading session ({c['wallet']}): {summary}"]
        for o in orders[:10]:
            lines.append(f"- {o['side']} {o['quantity']:g} {o['symbol']} @ {o['price'] or '?'}: {o['status']}" + (f" ({o['reason']})" if o["reason"] else ""))
        ideas = self.db.all("SELECT symbol, action FROM trade_ideas WHERE session_id=? AND account='main' AND action != ''", (sid,))
        if ideas:
            lines.append("Your account: " + ", ".join(f"{i['symbol']} {i['action']}" for i in ideas[:10]))
        try:
            self.notify("\n".join(lines))
        except Exception:
            pass

    # ---- what the page shows
    def status(self, prices: bool = True) -> dict:
        c = config(self.db)
        out = {"allowed": self.allowed, "config": c, "trader": self.trader.up() if prices else None, "next_session": self.next_session(),
               "live_connected": bool(cfg.get(self.db, "rh_trade_token")), "running": self.lock.locked(), "market": self.market(c)}
        try:
            out["snapshot"] = self.snapshot() if prices else None
        except Exception as e:
            out["snapshot"], out["snapshot_error"] = None, str(e)[:300]
        day = self.clock().date().isoformat()
        d = self.db.one("SELECT * FROM trade_days WHERE day=?", (day,))
        out["today"] = {"day": day, "start_value": d["start_value"] if d else None, "stopped": d["stopped"] if d else None}
        out["sessions"] = self.db.all("SELECT * FROM trade_sessions ORDER BY id DESC LIMIT 20")
        out["waiting"] = self.db.all("SELECT * FROM trade_orders WHERE status='asked' ORDER BY id")
        out["orders"] = self.db.all("SELECT * FROM trade_orders ORDER BY id DESC LIMIT 50")
        out["ideas"] = self.db.all("SELECT i.* FROM trade_ideas i JOIN (SELECT account, symbol, MAX(id) AS id FROM trade_ideas GROUP BY account, symbol) m ON m.id=i.id ORDER BY i.account, i.symbol")
        return out
