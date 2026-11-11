#!/usr/bin/env python3
"""Build a sample-data copy of Coinpile's page: a stand-in Firefly III with five months of made-up money, Coinpile reads it, every screen's data is
recorded and embedded, and the page runs from that recording (clicks work, changes are not saved).

    python3 scripts/make_demo.py demo.html            # open demo.html?demo in a browser
    python3 scripts/make_demo.py demo.html --artifact # without the first lines a hosted viewer adds itself; always in sample mode

Nothing here is a real account."""
import json
import os
import random
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FIELD_KEY", Fernet.generate_key().decode())
os.environ.setdefault("UI_DIR", str(ROOT / "ui"))
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from coinpile import web  # noqa: E402
from tests.fake_firefly import FakeFirefly, add_history  # noqa: E402

TODAY = date.today()
ALICE, BOB = "tok-alice-" + "x" * 30, "tok-bob-" + "y" * 30
D = lambda n: (TODAY + timedelta(days=n)).isoformat()          # noqa: E731


def firefly() -> FakeFirefly:
    rnd = random.Random(7)
    ff = FakeFirefly({ALICE: {"id": "1", "email": "alice@example.com"}, BOB: {"id": "2", "email": "bob@example.com"}})
    ff.acct("1", 1, "Huntington - Everyday Checking ••4821", balance="2145.85")
    ff.acct("1", 2, "Huntington - Bills Checking ••7730", balance="812.40")
    ff.acct("1", 3, "Axos - High Yield Savings ••0099", role="savingAsset", balance="8374.63")
    ff.acct("1", 4, "Capital One - Quicksilver ••0182", role="ccAsset", balance="-952.80")
    ff.acct("1", 6, "Fidelity - 401k ••5555", balance="48250.00")
    ff.acct("1", 7, "Vanguard - Brokerage ••3141", balance="12780.40")
    ff.acct("1", 5, "Ally - Car loan", type="liabilities", role=None, balance="-11250", debt_amount="-11250", liability_type="loan")
    jid = [100]

    def t(kind, amount, desc, src, dst, day, cat=None):
        jid[0] += 1
        ff.tx("1", jid[0], kind, round(amount, 2), desc, src, dst, day.isoformat(), cat)
    start = TODAY - timedelta(days=150)
    d = start
    while d <= TODAY:
        if d.weekday() == 4 and (d - start).days % 14 < 7:
            t("deposit", 2480.00, "ACME CORP PAYROLL", 90, 1, d, "Salary")
        if d.day == 1:
            t("withdrawal", 1450.00, "OAKWOOD APARTMENTS", 2, 91, d, "Housing")
            t("transfer", 500.00, "To savings", 1, 3, d, None)
            t("withdrawal", 389.00, "ALLY AUTO PAYMENT", 1, 5, d, "Car")
        if d.day == 3:
            t("withdrawal", 15.49, "NETFLIX.COM", 4, 92, d, "Subscriptions")
        if d.day == 11:
            t("withdrawal", 16.99, "SPOTIFY USA", 4, 93, d, "Subscriptions")
        if d.day == 15:
            t("withdrawal", 24.99, "PLANET FITNESS", 1, 94, d, "Health")
            t("transfer", 900.00, "Capital One payment", 1, 4, d, None)
        if d.day == 20:
            t("withdrawal", round(rnd.uniform(58, 66), 2), "CITY WATER UTIL", 2, 95, d, "Bills")
            t("withdrawal", round(rnd.uniform(70, 140), 2), "AEP ELECTRIC", 2, 96, d, "Bills")
        if d.weekday() in (2, 6):
            t("withdrawal", round(rnd.uniform(45, 160), 2), rnd.choice(["KROGER #66", "ALDI 4411", "TRADER JOE'S"]), 4, 97, d, "Groceries")
        if d.weekday() == 5:
            t("withdrawal", round(rnd.uniform(18, 72), 2), rnd.choice(["CHIPOTLE", "PANERA BREAD", "LOCAL DINER", "THAI GARDEN"]), 4, 98, d, "Food")
        if d.day % 9 == 0:
            t("withdrawal", round(rnd.uniform(32, 55), 2), rnd.choice(["SHELL OIL", "SPEEDWAY"]), 1, 99, d, "Gas")
        if d.day in (6, 24):
            t("withdrawal", round(rnd.uniform(20, 120), 2), rnd.choice(["AMAZON.COM", "TARGET T-1234", "BEST BUY"]), 4, 100, d, "Shopping")
        if d.day == 27 and d.month % 2:
            t("withdrawal", 49.00, "MEAL KIT CO", 4, 101, d, "Food")
        d += timedelta(days=1)
    bills = [("Rent", "1450", "monthly", 1), ("Netflix", "15.49", "monthly", 3), ("Spotify", "16.99", "monthly", 11), ("Car insurance", "294", "quarterly", 18), ("Planet Fitness", "24.99", "monthly", 15)]
    for i, (name, amt, freq, day) in enumerate(bills):
        nxt = TODAY.replace(day=day) if TODAY.day < day else (TODAY.replace(day=1) + timedelta(days=32)).replace(day=day)
        ff.data["1"]["bills"].append({"type": "bills", "id": str(i + 1), "attributes": {"name": name, "amount_avg": amt, "amount_min": amt, "amount_max": amt, "repeat_freq": freq, "skip": 0,
                                                                                      "date": (TODAY - timedelta(days=200)).replace(day=day).isoformat() + "T00:00:00+00:00",
                                                                                      "pay_dates": [nxt.isoformat() + "T00:00:00+00:00"], "active": True, "currency_code": "USD"}})
    ff.data["1"]["piggies"].append({"type": "piggy-banks", "id": "1", "attributes": {"name": "Vacation", "target_amount": "2000", "current_amount": "650", "active": True, "save_per_month": "150",
                                                                                    "target_date": D(200) + "T00:00:00+00:00", "accounts": [{"account_id": "3", "name": "Axos - High Yield Savings ••0099", "current_amount": "650"}]}})
    for aid, (lo, hi) in {1: (1500, 2600), 2: (600, 1100), 3: (5800, 8374.63), 4: (-1400, -500)}.items():
        pts, n = {}, 150
        for k in range(n + 1):
            day = TODAY - timedelta(days=n - k)
            pts[day.isoformat()] = f"{lo + (hi - lo) * (k / n) + rnd.uniform(-120, 120) * (aid != 3):.2f}"
        add_history(ff, "1", aid, pts)
    add_history(ff, "1", 5, {(TODAY - timedelta(days=150 - 30 * k)).isoformat(): f"{-(12800 - 389 * k):.2f}" for k in range(5)})
    ff.acct("2", 11, "Axos - Joint Savings ••7711", role="savingAsset", balance="4000")
    return ff


def main(out: str, artifact: bool):
    ff = firefly()
    hooks = httpx.MockTransport(lambda r: httpx.Response(200))
    conn = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=tempfile.mkdtemp(), firefly_transport=ff.transport(), webhook_transport=hooks,
                       connector_transport=conn, today=lambda: TODAY)
    c, bob = TestClient(a), TestClient(a)
    assert c.post("/auth/token", json={"token": ALICE}).status_code == 200 and bob.post("/auth/token", json={"token": BOB}).status_code == 200
    c.post("/api/refresh?full=true")
    bob.post("/api/refresh?full=true")
    post = lambda p, j: c.post(p, json=j).json()                # noqa: E731
    # what Coinpile keeps itself: CDs, goals, incomes, budgets, notes, a manual account, tokens, a webhook, models, a link with bob
    post("/api/savings", {"name": "Ally 12-month CD", "kind": "cd", "institution": "Ally", "balance": 10000, "apy": 4.8, "opened": D(-150), "maturity": D(215), "term_months": 12, "goal": 12000})
    post("/api/savings", {"name": "Huntington 6-month CD", "kind": "cd", "institution": "Huntington", "balance": 5000, "apy": 4.5, "opened": D(-160), "maturity": D(22)})
    g = post("/api/goals", {"name": "House down payment", "target": 20000, "priority": 1, "deadline": D(420), "monthly": 600, "note": "20% of a $100k condo"})
    places = {p["name"]: p["key"] for p in c.get("/api/goals").json()["places"]}
    c.post(f"/api/goals/{g['id']}/allocate", json={"place": places.get("Ally 12-month CD"), "amount": 9000})
    c.post(f"/api/goals/{g['id']}/allocate", json={"place": places.get("High Yield Savings"), "amount": 3000})
    post("/api/goals", {"name": "New laptop", "target": 1400, "priority": 3, "monthly": 200, "deadline": D(120)})
    inc = c.get("/api/incomes").json()
    for sg in inc.get("suggestions", [])[:1]:
        post("/api/incomes/accept", {"key": sg["key"], "name": "Acme paycheck"})
    for cat, amt in [("Food", 250), ("Groceries", 600), ("Gas", 160), ("Shopping", 200)]:
        c.put("/api/budgets", json={"category": cat, "monthly_amount": amt})
    c.put("/api/investments/ff:6", json={"investment": True, "category": "Retirement"})
    c.put("/api/investments/ff:7", json={"investment": True, "category": "Investment"})
    post("/api/accounts", {"name": "Cash wallet", "kind": "cash", "balance": 140, "min_balance": 20})
    post("/api/notes", {"title": "Goals for the year", "body": "- Keep checking above $500\n- Pay the card in full each month\n- Roll the 6-month CD into the house fund", "pinned": True})
    post("/api/tokens", {"name": "Home Assistant", "scopes": ["summary", "bills", "refresh"]})
    post("/api/webhooks", {"name": "Home Assistant", "url": "https://ha.example.com/api/webhook/coinpile", "events": ["transaction.new", "bill.due", "alert.new"]})
    post("/api/agent/providers", {"name": "Ollama (this machine)", "kind": "openai", "base_url": "http://192.168.1.20:11434/v1", "model": "llama3.1", "tools": True})
    c.put("/api/settings", json={"share": ["net_worth", "bills", "alerts", "savings", "spending"], "connector_url": "http://192.168.1.50:6421", "connector_name": "alice", "connector_token": "sample password 123"})
    c.post("/api/links", json={"peer": "bob"})
    bob.post("/api/links/alice/accept", json={})
    import time
    time.sleep(0.5)
    bob.put("/api/links/alice/shares", json={"kind": "account", "ref": "ff:11", "on": True})
    time.sleep(0.5)
    db = a.person_db("1")
    ts = TODAY.isoformat() + "T12:00:00+00:00"
    cid = db.conn.execute("INSERT INTO chats(title,provider_id,summary,created,updated) VALUES(?,?,?,?,?)", ("Bills for the next two months", 1, "", ts, ts)).lastrowid
    for role, text, trace in [("user", "How much will I pay in bills over the next 60 days?", []),
                              ("assistant", "About $3,550 across 9 payments. Rent is the biggest ($1,450 twice); car insurance ($294) is due in a few weeks.", [{"tool": "upcoming_bills", "args": {"days": 60}, "ok": True, "preview": ""}])]:
        db.conn.execute("INSERT INTO chat_messages(chat_id,role,content,trace,compacted,created) VALUES(?,?,?,?,0,?)", (cid, role, text, json.dumps(trace), ts))
    db.conn.commit()

    rec = {}
    get = lambda p: c.get(p).json()                            # noqa: E731
    rec["transactions"] = get("/api/transactions?limit=2000")
    for p in ["/api/overview", "/api/history", "/api/spending?", "/api/categories", "/api/accounts", "/api/subscriptions", "/api/suggestions", "/api/alerts", "/api/notes", "/api/budgets",
              "/api/links", "/api/links/people", "/api/requests", "/api/sharing", "/api/agent/providers", "/api/agent/presets", "/api/settings", "/api/category-rules", "/api/goals",
              "/api/incomes", "/api/tokens", "/api/forecast", "/api/maturities", "/api/notify/status", "/api/status", "/api/piggy-banks", "/api/webhooks", "/api/firefly/webhook", "/api/summary",
              "/api/chats", "/api/account-categories", "/api/robinhood/main", "/api/robinhood/agent", "/api/investments"]:
        rec[p] = get(p)
    for dd in list(range(1, 121)) + [365]:
        rec[f"/api/upcoming?days={dd}"] = get(f"/api/upcoming?days={dd}")
    for acc in rec["/api/accounts"]:
        rec[f"/api/accounts/{acc['id']}"] = get(f"/api/accounts/{acc['id']}")
    for ch in rec["/api/chats"]:
        rec[f"/api/chats/{ch['id']}"] = get(f"/api/chats/{ch['id']}")
    for w in rec["/api/webhooks"]["items"]:
        rec[f"/api/webhooks/{w['id']}/deliveries"] = get(f"/api/webhooks/{w['id']}/deliveries")
    for k in range(4):
        y, m = TODAY.year, TODAY.month - k
        while m < 1:
            y, m = y - 1, m + 12
        rec[f"/api/plan?month={y}-{m:02d}"] = get(f"/api/plan?month={y}-{m:02d}")
    rec["/api/banks"] = {"items": [{"item_id": "item-1", "institution": "Huntington", "status": None, "last_sync": time.time() - 3 * 3600, "webhook_live": True, "schedule": "auto", "schedule_effective": "daily",
                                    "recurring": "auto", "recurring_effective": True, "calls_per_day": 1.2, "category": "Checking",
                                    "accounts": [{"name": "Everyday Checking", "mask": "4821"}, {"name": "Bills Checking", "mask": "7730"}]},
                                   {"item_id": "item-2", "institution": "Capital One", "status": None, "last_sync": time.time() - 5 * 3600, "webhook_live": True, "schedule": "auto", "schedule_effective": "daily",
                                    "recurring": "off", "recurring_effective": False, "calls_per_day": 1.1, "category": None, "accounts": [{"name": "Quicksilver", "mask": "0182"}]},
                                   {"item_id": "item-3", "institution": "Axos", "status": None, "last_sync": time.time() - 40 * 3600, "webhook_live": False, "schedule": "weekly", "schedule_effective": "weekly",
                                    "recurring": "auto", "recurring_effective": False, "calls_per_day": 0.3, "category": "Savings", "accounts": [{"name": "High Yield Savings", "mask": "0099"}]}],
                         "plaid_configured": True, "plaid_env": "production",
                         "firefly": {"configured": True, "sent": 412, "waiting": 0, "failed": 0, "awaiting_approval": 0, "last_ok": TODAY.isoformat() + "T08:12:00", "last_error": None}}
    rec["/api/banks/usage"] = {"month": TODAY.strftime("%Y-%m"), "calls_month": 96, "calls_today": 4, "daily_cap": 300, "items": 3, "est_monthly_cost": 1.35, "budget": 5.0, "over_budget": False,
                               "room_for_items": 8, "per_item": 0.45, "webhook_banks": 2, "webhook_set": True}
    rec["/api/robinhood/main"] = {"source": "csv", "as_of": TODAY.isoformat(), "balances": {"net_transfers": 6000},
                                  "positions": [{"symbol": "VTI", "quantity": 18.5, "net_cost": 4400}, {"symbol": "AAPL", "quantity": 6, "net_cost": 1080}],
                                  "monthly": [{"month": (TODAY.replace(day=1) - timedelta(days=30 * k)).strftime("%Y-%m"), "deposits": 1000.0 if k % 2 == 0 else 500.0,
                                               "bought": 900.0 if k % 2 == 0 else 450.0, "sold": 0.0, "dividends": [0, 0, 11.42][k % 3], "fees": 0.0, "trades": 2} for k in range(6)]}
    rec["/api/status"]["robinhood"]["csv_files"] = ["import-2026-11-01.csv"]
    rec["/api/status"]["robinhood"]["mcp"] = True
    rec["/api/robinhood/agent"] = {"source": "robinhood-mcp", "as_of": TODAY.isoformat() + "T08:00:00+00:00", "balances": {"equity": 7340.25, "buying_power": 412.10},
                                   "positions": [{"symbol": "NVDA", "quantity": 10, "net_cost": 1200, "equity": 1450}, {"symbol": "VTI", "quantity": 20, "net_cost": 5000, "equity": 5890.25}]}
    rec["/api/investments"]["monthly"] = [{"month": (TODAY.replace(day=1) - timedelta(days=30 * k)).strftime("%Y-%m"), "value": round(61030.40 - 1450 * k + (230 if k % 2 else -120), 2)} for k in range(6, -1, -1)]
    for pr in rec["/api/investments"]["providers"]:
        if pr["id"] == "robinhood":
            pr["connected"] = True
    rec["/scan"] = c.post("/api/subscriptions/scan", json={}).json()
    rec["chatProvider"] = rec["/api/agent/providers"]["items"][0]["id"]
    up = rec["/api/upcoming?days=60"]
    rec["chat"] = {"reply": f"(Sample answer) Over the next 60 days you have {up['count']} payments totalling ${up['total']:,.2f}. "
                            "Rent is the biggest. Your checking stays above your $500 minimum the whole time if the paychecks land as usual.",
                   "trace": [{"tool": "upcoming_bills", "args": {"days": 60}, "ok": True, "preview": ""}, {"tool": "project_balance", "args": {"days": 60}, "ok": True, "preview": ""}],
                   "provider": {"id": rec["chatProvider"], "name": "Ollama (this machine)", "model": "llama3.1", "local": True}, "steps": 2}
    rec["models"] = {"models": [{"id": "llama3.1:8b", "context": None}, {"id": "qwen2.5:32b", "context": None}], "local": True}
    html = (ROOT / "ui" / "index.html").read_text()
    html = html.replace("window.GOBLIN_DEMO = null; /*DEMO*/", "window.GOBLIN_DEMO = " + json.dumps(rec, default=str).replace("</", "<\\/") + ";")
    if artifact:
        html = html.replace('if (new URLSearchParams(location.search).has("demo") && window.GOBLIN_DEMO)', "if (window.GOBLIN_DEMO)")
        html = "\n".join(html.split("\n")[2:])
    Path(out).write_text(html)
    print(f"wrote {out} ({len(html) // 1024} KB, {len(rec['transactions']['items'])} transactions)")


if __name__ == "__main__":
    main(sys.argv[1], "--artifact" in sys.argv)
