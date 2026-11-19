"""The auto wallet (paper and live), its rules, and the advice for your own account."""
import json
import time
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from coinpile import traderules, trading, web
from tests.fake_mcp import MCP, FakeRobinhood
from tests.test_coinpile import BOB, RH, TODAY, signed_in, world


# ---------------------------------------------------------------- the rules on their own
def test_daily_loss_stop():
    assert traderules.daily_loss(1000, 975, 3)["ok"]
    v = traderules.daily_loss(1000, 969, 3)
    assert not v["ok"] and "down 3.1%" in v["reason"]
    assert not traderules.daily_loss(None, 900, 3)["ok"], "unknown value fails closed"
    assert traderules.daily_loss(None, None, 0)["ok"], "0 switches it off"


def test_day_trade_guard():
    assert not traderules.day_trade("sell", "nvda", {"NVDA"})["ok"]
    assert traderules.day_trade("buy", "NVDA", {"NVDA"})["ok"], "buying more is not a day trade"
    assert traderules.day_trade("sell", "NVDA", set())["ok"]
    assert traderules.day_trade("sell", "NVDA", {"NVDA"}, enabled=False)["ok"]


def test_policy_from_agentrisk():
    pos = [{"symbol": "AAPL", "quantity": 2, "price": 100}]
    ok = traderules.policy(traderules.DEFAULT_POLICY, 800, pos, "buy", "MSFT", 1, 100)
    assert ok["ok"], ok
    cap = traderules.policy(traderules.DEFAULT_POLICY, 800, pos, "buy", "MSFT", 5, 100)
    assert not cap["ok"] and not cap["ask"] and "20%" in cap["reason"]
    never = traderules.DEFAULT_POLICY.replace("never_trade: []", "never_trade: [GME]")
    assert "never-trade" in traderules.policy(never, 800, pos, "buy", "GME", 1, 10)["reason"]
    warn = traderules.DEFAULT_POLICY.replace("warn_list: []", "warn_list: [TSLA]")
    w = traderules.policy(warn, 800, pos, "buy", "TSLA", 1, 10)
    assert w["ask"] or w["ok"], w
    with pytest.raises(ValueError):
        traderules.parse_policy("limits: [1, 2")
    with pytest.raises(ValueError):
        traderules.parse_policy("limits:\n  max_single_position_pct: 500\n")
    assert not traderules.policy("nonsense: {", 0, [], "buy", "X", 1, 1)["ok"], "broken rules block"


def test_order_args_follow_the_tools_schema():
    tool = {"name": "place_equity_order", "inputSchema": {"required": ["symbol", "side", "quantity", "type"], "properties": {
        "symbol": {"type": "string"}, "side": {"enum": ["BUY", "SELL"]}, "quantity": {"type": "string"}, "type": {"enum": ["market"]}, "time_in_force": {"enum": ["gfd"]}}}}
    assert trading.order_args(tool, "buy", "SPY", 1.5) == {"symbol": "SPY", "side": "BUY", "quantity": "1.5", "type": "market", "time_in_force": "gfd"}
    with pytest.raises(ValueError, match="account_number"):
        trading.order_args({"inputSchema": {"required": ["account_number", "symbol", "quantity"], "properties": {"symbol": {}, "quantity": {}}}}, "buy", "SPY", 1)
    assert trading.guess_order_tool([{"name": "cancel_equity_order"}, {"name": "place_crypto_order"}, {"name": "place_equity_order"}]) == "place_equity_order"


# ---------------------------------------------------------------- the whole thing, through the API
class FakeTrader:
    """The trader container: a rating per symbol and prices you can move."""
    def __init__(self):
        self.ratings = {"NVDA": "Buy", "SPY": "Hold", "AAPL": "Hold"}
        self.prices = {"NVDA": 50.0, "SPY": 500.0, "AAPL": 200.0, "QQQ": 400.0}
        self.asked = []

    def transport(self):
        def h(req: httpx.Request):
            if req.url.path == "/health":
                return httpx.Response(200, json={"ok": True})
            if req.headers.get("x-internal-secret") != "s3cret":
                return httpx.Response(401)
            if req.url.path == "/quotes":
                syms = req.url.params["symbols"].split(",")
                return httpx.Response(200, json={"prices": {s: self.prices[s] for s in syms if s in self.prices}})
            if req.url.path == "/analyze":
                b = json.loads(req.content)
                self.asked.append(b)
                return httpx.Response(200, json={"symbol": b["symbol"], "rating": self.ratings.get(b["symbol"], "Hold"), "decision": f"Rating: {self.ratings.get(b['symbol'], 'Hold')}",
                                                 "trader_plan": f"plan for {b['symbol']}"})
            return httpx.Response(404)
        return httpx.MockTransport(h)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 11, 9, 50, tzinfo=trading.NY)        # a Wednesday, just after the 09:45 session

    def __call__(self):
        return self.t


def make(tmp_path, monkeypatch, rh=None):
    monkeypatch.setenv("RH_OWNER", "alice@example.com")
    monkeypatch.setenv("TRADER_URL", "http://trader:8100")
    monkeypatch.setenv("CONNECTOR_SECRET", "s3cret")
    tr, clock = FakeTrader(), Clock()
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), today=lambda: TODAY,
                       trader_transport=tr.transport(), trading_clock=clock, mcp_transport=rh.transport() if rh else None)
    return a, tr, clock


def run(c):
    assert c.post("/api/trading/run").status_code == 200
    for _ in range(200):
        st = c.get("/api/trading").json()
        if not st["running"] and st["sessions"] and st["sessions"][0]["status"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError("the session didn't finish")


def test_paper_wallet_sessions_rules_and_advice(tmp_path, monkeypatch):
    a, tr, clock = make(tmp_path, monkeypatch)
    c = signed_in(a)
    assert signed_in(a, BOB).get("/api/trading").status_code == 403, "only the Robinhood owner"
    c.post("/api/robinhood/csv-upload", content=RH)                          # your own account holds AAPL
    st = c.get("/api/trading").json()
    assert st["config"]["wallet"] == "paper" and not st["config"]["enabled"] and st["snapshot"]["cash"] == 1000
    assert c.put("/api/trading/config", json={"wallet": "live"}).status_code == 422, "real money needs confirm: LIVE"
    assert c.put("/api/trading/config", json={"policy": "limits: [oops"}).status_code == 422
    assert c.put("/api/trading/config", json={"enabled": True, "watchlist": "nvda, spy", "per_session": 2, "guidance": "prefer chips"}).status_code == 200

    st = run(c)
    o = st["orders"][0]
    assert (o["symbol"], o["side"], o["quantity"], o["status"]) == ("NVDA", "buy", 2.0, "filled"), "a Buy aims for 10% of the wallet"
    assert st["snapshot"]["cash"] == 900 and st["snapshot"]["value"] == 1000
    assert tr.asked[0]["guidance"] == "prefer chips" and tr.asked[0]["portfolio"]["cash"] == 1000
    ideas = {(i["account"], i["symbol"]): i for i in st["ideas"]}
    assert ideas[("main", "AAPL")]["action"] == "hold" and ideas[("main", "NVDA")]["action"] == "buy", "advice for your account, and a buy idea"
    assert ideas[("agent", "NVDA")]["rating"] == "Buy"

    # your plan for the next session: sell NVDA. Bought today, so the day-trade guard stops it.
    assert c.put("/api/trading/config", json={"next": {"mode": "manual", "plan": [{"side": "sell", "symbol": "NVDA", "quantity": "all"}]}}).status_code == 200
    st = run(c)
    o = st["orders"][0]
    assert o["status"] == "blocked" and "day trade" in o["reason"]
    assert st["config"]["next"]["mode"] == "", "the one-off plan is used up"

    # next day: ask before each order; NVDA is now a Sell
    clock.t = clock.t.replace(day=12)
    tr.ratings["NVDA"] = "Sell"
    c.put("/api/trading/config", json={"next": {"mode": "ask", "guidance": ""}})
    st = run(c)
    w = st["waiting"]
    assert [(x["symbol"], x["side"]) for x in w] == [("NVDA", "sell")]
    assert c.post(f"/api/trading/orders/{w[0]['id']}/approve").json()["order"]["status"] == "filled"
    assert c.post(f"/api/trading/orders/{w[0]['id']}/approve").status_code == 409, "only once"
    assert c.get("/api/trading").json()["snapshot"]["cash"] == 1000

    # your rules: never NVDA
    tr.ratings["NVDA"] = "Buy"
    c.put("/api/trading/config", json={"policy": traderules.DEFAULT_POLICY.replace("never_trade: []", "never_trade: [NVDA]")})
    st = run(c)
    assert st["orders"][0]["status"] == "blocked" and "never-trade" in st["orders"][0]["reason"]

    # the daily loss stop: the wallet holds NVDA, the price drops 50%
    c.put("/api/trading/config", json={"policy": traderules.DEFAULT_POLICY})
    clock.t = clock.t.replace(day=13)
    run(c)                                                                     # buys 2 NVDA at 50 (start of day: 1000)
    tr.prices["NVDA"], tr.ratings["NVDA"], tr.ratings["SPY"] = 10.0, "Hold", "Buy"     # the wallet is down 8%
    c.put("/api/trading/config", json={"daily_loss_pct": 5})
    st = run(c)
    spy = next(o for o in st["orders"] if o["symbol"] == "SPY")
    assert spy["status"] == "blocked" and "down 8.0%" in spy["reason"] and st["today"]["stopped"]

    # the Orb sees it once shared, read only
    orb = c.post("/api/tokens", json={"name": "Orb", "scopes": ["orb"]}).json()["token"]
    get = lambda: next(iter(TestClient(a).get("/api/v1/summary", headers={"Authorization": "Bearer " + orb}).json()["goblins"].values()))
    assert "trading" not in get()
    c.put("/api/settings", json={"share": ["trading"]})
    t = get()["trading"]
    assert t["wallet"] == "paper" and t["positions"][0]["symbol"] == "NVDA" and t["stopped"] and any(i["account"] == "main" for i in t["ideas"])


def test_sessions_run_on_schedule_once_each(tmp_path, monkeypatch):
    a, tr, clock = make(tmp_path, monkeypatch)
    c = signed_in(a)
    c.put("/api/trading/config", json={"enabled": True, "watchlist": ["SPY"]})
    import glob
    from coinpile.db import DB
    e = trading.Engine(DB(glob.glob(str(tmp_path / "people" / "*.db"))[0]), "alice@example.com", trading.Trader("http://trader:8100", "s3cret", tr.transport()), clock=clock)
    assert e.due_slot() == "09:45"
    e.run_session("09:45")
    assert e.due_slot() is None, "once per slot"
    clock.t = clock.t.replace(hour=11, minute=30)
    assert e.due_slot() is None, "missed by more than 90 minutes: skipped"
    clock.t = clock.t.replace(hour=12, minute=31)
    assert e.due_slot() == "12:30"
    clock.t = datetime(2026, 11, 14, 9, 50, tzinfo=trading.NY)
    assert e.due_slot() is None, "no sessions at the weekend"
    assert e.next_session().startswith("2026-11-16T09:45")
    assert trading.Engine(e.db, "bob@example.com", clock=clock).due_slot() is None, "nobody but the owner"


def test_live_wallet_through_robinhoods_mcp(tmp_path, monkeypatch):
    rh = FakeRobinhood()
    a, tr, clock = make(tmp_path, monkeypatch, rh)
    c = signed_in(a)
    c.put("/api/settings", json={"rh_mcp_url": MCP})
    assert c.put("/api/trading/config", json={"wallet": "live", "confirm": "LIVE"}).status_code == 409, "connect first"
    url = c.post("/api/trading/connect").json()["url"]
    back = rh.approve(url)
    assert rh.scopes[-1] == "read trade", "the trading sign-in asks for trading"
    r = c.get(back.replace("http://testserver", ""), follow_redirects=False)
    assert r.headers["location"].endswith("trading=connected#trading")
    # the read-only Robinhood connection is untouched, and its callback refuses a trading sign-in
    assert c.get("/api/trading").json()["live_connected"]
    ck = c.post("/api/trading/live/check").json()
    assert ck["chosen"] == "place_equity_order" and ck["would_send"] == {"symbol": "SPY", "side": "buy", "quantity": 1, "type": "market", "time_in_force": "gfd"}
    assert not rh.orders, "checking sends nothing"
    assert c.put("/api/trading/config", json={"wallet": "live"}).status_code == 422
    assert c.put("/api/trading/config", json={"order_tool": "transfer_funds"}).status_code == 422, "only an order tool"
    assert c.put("/api/trading/config", json={"wallet": "live", "confirm": "LIVE", "enabled": True, "watchlist": ["NVDA"], "per_session": 1}).status_code == 200
    tr.prices["VTI"] = 294.5
    tr.prices["NVDA"] = 145.0
    tr.ratings["VTI"] = "Hold"
    st = run(c)
    # the Agentic account: 412.10 cash, NVDA 10 and VTI 20 -> about 7,722; NVDA is already ~19% so a Buy (10%) adds nothing; nothing sent
    assert not rh.orders and st["snapshot"]["wallet"] == "live"
    tr.ratings["NVDA"], tr.ratings["VTI"] = "Hold", "Underweight"
    st = run(c)
    # Underweight: half of 20 VTI is $2,945, 38% of the wallet; the 20% cap per trade makes it 5.21 shares now, the rest in later sessions
    assert rh.orders == [{"symbol": "VTI", "side": "sell", "quantity": 5.2119, "type": "market", "time_in_force": "gfd"}]
    assert next(o for o in st["orders"] if o["symbol"] == "VTI")["broker_ref"] == "ord-1"
    assert c.post("/api/trading/disconnect").json() == {"connected": False}
    assert c.get("/api/trading").json()["config"]["wallet"] == "paper", "no sign-in, back to paper"