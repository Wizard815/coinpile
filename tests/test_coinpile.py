"""Coinpile reads Firefly III correctly, keeps each person's data apart, never edits what belongs to Firefly III, and drives the Plaid connector."""
import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from coinpile import firefly, web
from coinpile.db import DB
from tests.fake_firefly import FakeFirefly

TODAY = date(2026, 11, 10)
ALICE = "tok-alice-" + "x" * 30
BOB = "tok-bob-" + "y" * 30


def world():
    ff = FakeFirefly({ALICE: {"id": "1", "email": "alice@example.com"}, BOB: {"id": "2", "email": "bob@example.com"}})
    ff.acct("1", 1, "Huntington - Everyday Checking ••4821", balance="1200.50")
    ff.acct("1", 3, "Axos - High Yield ••0099", role="savingAsset", balance="5000")
    ff.acct("1", 5, "Capital One - Quicksilver ••0182", role="ccAsset", balance="-250")
    ff.acct("1", 7, "Car loan", type="liabilities", role=None, balance="0", debt_amount="-9000", liability_type="loan")
    ff.tx("1", 1, "opening balance", 1000, "Initial balance", 9, 1, "2026-01-01")
    ff.tx("1", 2, "deposit", 2500, "ACME PAYROLL", 90, 1, "2026-10-01", "Income")
    ff.tx("1", 3, "withdrawal", 82.4, "KROGER #66", 1, 91, "2026-10-03", "Groceries")
    ff.tx("1", 4, "withdrawal", 49.99, "AMAZON", 5, 92, "2026-10-06")
    ff.tx("1", 5, "transfer", 300, "To savings", 1, 3, "2026-10-07")
    for i, d in enumerate(("2026-08-05", "2026-09-05", "2026-10-05")):
        ff.tx("1", 10 + i, "withdrawal", 15.99, "NETFLIX.COM", 1, 93, d, "Subscriptions")
    ff.data["1"]["bills"].append({"type": "bills", "id": "1", "attributes": {"name": "Rent", "amount_avg": "1500", "repeat_freq": "monthly", "skip": 0, "date": "2026-01-01T00:00:00+00:00",
                                                                             "pay_dates": ["2026-12-01T00:00:00+00:00"], "active": True, "currency_code": "USD"}})
    ff.data["1"]["piggies"].append({"type": "piggy-banks", "id": "1", "attributes": {"name": "Vacation", "target_amount": "2000", "current_amount": "400", "active": True,
                                                                                    "target_date": "2027-06-01T00:00:00+00:00", "accounts": [{"account_id": "3", "name": "Axos", "current_amount": "400"}]}})
    ff.acct("2", 11, "Bob's checking", balance="77")
    return ff


def app(tmp_path, ff, connector_transport=None):
    return web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), connector_transport=connector_transport, today=lambda: TODAY)


def signed_in(a, token=ALICE):
    c = TestClient(a)
    r = c.post("/auth/token", json={"token": token})
    assert r.status_code == 200, r.text
    c.post("/api/refresh")
    return c


def test_the_local_copy_matches_firefly_iii():
    ff, db = world(), DB(":memory:")
    out = firefly.refresh(db, firefly.Firefly("https://firefly.example.com", ALICE, ff.transport()), TODAY)
    assert out["accounts"] == 4 and out["full"] and out["subscriptions"] == 1 and out["piggy_banks"] == 1
    a = {r["id"]: r for r in db.all("SELECT * FROM accounts")}
    assert (a["ff:1"]["institution"], a["ff:1"]["name"], a["ff:1"]["mask"], a["ff:1"]["kind"], a["ff:1"]["balance"]) == ("Huntington", "Everyday Checking", "4821", "cash", 1200.5)
    assert a["ff:3"]["subtype"] == "savings" and a["ff:5"]["kind"] == "credit" and a["ff:5"]["balance"] == 250          # a card's balance is what you owe
    assert a["ff:7"]["kind"] == "loan" and a["ff:7"]["balance"] == 9000
    t = {r["id"]: r for r in db.all("SELECT * FROM transactions")}
    assert "ff:1" not in t                                                                                               # an opening balance is not money that moved
    assert t["ff:2"]["amount"] == 2500 and t["ff:3"]["amount"] == -82.4 and t["ff:4"]["account_id"] == "ff:5" and t["ff:4"]["category"] == "Misc"
    assert t["ff:5:out"]["amount"] == -300 and t["ff:5:in"]["amount"] == 300 and t["ff:5:in"]["category"] == "Transfer"   # a transfer is both sides, never spending
    assert all(r["category_locked"] == 1 and r["source"] == "firefly" for r in t.values())
    s = db.one("SELECT * FROM subscriptions")
    assert (s["name"], s["amount"], s["every"], s["unit"], s["next_date"], s["source"]) == ("Rent", 1500, 1, "month", "2026-12-01", "firefly")
    assert firefly.piggies(db)[0]["accounts"][0]["account_id"] == "ff:3"
    # what changed in Firefly III follows: an edited amount, a deleted transaction, a removed account
    g = ff.data["1"]["groups"]
    g[2]["attributes"]["transactions"][0]["amount"] = "90.000000000000"
    del g[3]
    del ff.data["1"]["accounts"][3]
    firefly.refresh(db, firefly.Firefly("https://firefly.example.com", ALICE, ff.transport()), TODAY, full=True)
    t = {r["id"]: r for r in db.all("SELECT * FROM transactions")}
    assert t["ff:3"]["amount"] == -90 and "ff:4" not in t and not db.one("SELECT 1 FROM accounts WHERE id='ff:7'")


def test_later_refreshes_only_read_recent_months_and_new_ones_show_as_movement():
    ff, db = world(), DB(":memory:")
    f = firefly.Firefly("https://firefly.example.com", ALICE, ff.transport())
    firefly.refresh(db, f, TODAY, now=1000.0)
    ff.tx("1", 50, "withdrawal", 12, "COFFEE", 1, 94, "2026-11-09")
    ff.calls.clear()
    out = firefly.refresh(db, f, TODAY, now=2000.0)
    assert not out["full"] and any(p == "/api/v1/transactions" and q.get("start") == "2026-07-13" for _, p, q in ff.calls)
    assert db.one("SELECT title FROM events WHERE id='tx:ff:50'")["title"] == "Huntington: COFFEE -$12.00"
    assert firefly.refresh(db, f, TODAY, now=2000.0 + 25 * 3600)["full"]                                             # and everything once a day


def test_sign_in_with_a_token_and_each_person_sees_only_their_own(tmp_path):
    ff = world()
    a = app(tmp_path, ff)
    anon = TestClient(a)
    assert anon.get("/api/overview").status_code == 401 and anon.get("/auth/me").json()["signed_in"] is False
    assert anon.post("/auth/token", json={"token": "nope-" + "z" * 30}).status_code == 401
    alice, bob = signed_in(a, ALICE), signed_in(a, BOB)
    assert alice.get("/auth/me").json()["email"] == "alice@example.com"
    assert {x["id"] for x in alice.get("/api/accounts").json()} == {"ff:1", "ff:3", "ff:5", "ff:7"}
    assert [x["id"] for x in bob.get("/api/accounts").json()] == ["ff:11"]
    bob.post("/api/notes", json={"title": "bob's secret plan", "body": "x"})
    assert "bob's secret plan" not in alice.get("/api/notes").text
    users = (tmp_path / "users.db").read_bytes()
    assert ALICE.encode() not in users and BOB.encode() not in users                                                   # tokens are stored encrypted
    assert alice.post("/auth/logout").status_code == 200 and alice.get("/api/overview").status_code == 401


def test_coinpile_never_changes_firefly_iii(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    assert c.put("/api/transactions/ff:3/category", json={"category": "Food"}).status_code == 409
    sid = c.get("/api/subscriptions").json()["items"][0]["id"]
    assert c.delete(f"/api/subscriptions/{sid}").status_code == 409
    assert c.delete("/api/accounts/ff:1").status_code == 409 and c.put("/api/accounts/ff:1", json={"balance": 5}).status_code == 409
    assert c.put("/api/accounts/ff:1", json={"nickname": "Bills"}).status_code == 200                                   # a nickname is Coinpile's own
    assert c.post("/api/goals", json={"name": "Car", "target": 1000}).status_code == 200
    assert c.post("/api/refresh").status_code == 200 and c.get("/api/accounts/ff:1").json()["account"]["nickname"] == "Bills"     # and survives a refresh
    assert {m for m, _, _ in ff.calls} == {"GET"}                                                                   # only ever reads (nothing is switched on)


def test_a_change_from_another_site_is_refused(tmp_path):
    c = signed_in(app(tmp_path, world()))
    assert c.post("/api/notes", json={"title": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/notes", json={"title": "x"}, headers={"Origin": "http://testserver"}).status_code == 200


def test_an_expired_token_is_reported_and_forget_me_deletes_everything(tmp_path):
    ff = world()
    a = app(tmp_path, ff)
    c = signed_in(a)
    del ff.users[ALICE]                                                                                                   # revoked in Firefly III
    r = c.post("/api/refresh")
    assert r.status_code == 409 and "Sign in again" in r.json()["detail"] and c.get("/auth/me").json()["problem"] == "sign in again"
    assert c.get("/api/status").json()["firefly"]["last_error"]
    assert c.post("/api/forget").json() == {"forgotten": True}
    assert c.get("/api/overview").status_code == 401 and not list((tmp_path / "people").glob("*.db"))


def test_settings_only_take_what_a_person_may_set(tmp_path):
    c = signed_in(app(tmp_path, world()))
    assert c.put("/api/settings", json={"plaid_secret": "x"}).status_code == 422
    r = c.put("/api/settings", json={"connector_url": "http://192.168.1.50:6421", "connector_name": "alice", "connector_token": "a long enough password"}).json()
    assert r["connector_token"] == {"set": True} and "a long enough password" not in json.dumps(r)
    assert c.put("/api/settings", json={"connector_url": "ftp://x"}).status_code == 422


def fake_connector():
    sessions, log = {}, []

    def h(req: httpx.Request):
        log.append((req.method, req.url.path))
        if req.url.path == "/login":
            b = json.loads(req.content)
            if (b["name"], b["password"]) != ("alice", "a long enough password"):
                return httpx.Response(401, json={"detail": "wrong name or password"})
            sessions["sess_1"] = "alice"
            return httpx.Response(200, json={"token": "sess_1"})
        if req.headers.get("authorization") != "Bearer sess_1":
            return httpx.Response(401, json={"detail": "unauthorized"})
        p = req.url.path.removeprefix("/g/alice/")
        if p == "api/status":
            return httpx.Response(200, json={"plaid": {"configured": True, "env": "production", "items": [{"item_id": "i1", "institution": "Huntington", "accounts": [{"name": "Checking", "mask": "4821", "balance": 1}]}]}})
        if p == "api/firefly/status":
            return httpx.Response(200, json={"configured": True, "sent": 10, "waiting": 0, "failed": 0, "awaiting_approval": 1, "token_set": True})
        if p == "sync":
            return httpx.Response(200, json={"firefly": {"created": 2}})
        if p == "plaid/link-token":
            return httpx.Response(200, json={"link_token": "link-production-abc"})
        if p == "plaid/exchange":
            return httpx.Response(200, json={"item_id": "i2", "institution": json.loads(req.content)["institution"]})
        return httpx.Response(404, json={"detail": "no"})
    return httpx.MockTransport(h), log


def test_the_plaid_connector_can_be_driven_from_coinpile(tmp_path):
    t, log = fake_connector()
    c = signed_in(app(tmp_path, world(), connector_transport=t))
    assert c.get("/api/banks").status_code == 502                                                                       # not set up yet: a clear message
    c.put("/api/settings", json={"connector_url": "http://connector:8000", "connector_name": "alice", "connector_token": "a long enough password"})
    b = c.get("/api/banks").json()
    assert b["items"][0]["institution"] == "Huntington" and b["items"][0]["accounts"] == [{"name": "Checking", "mask": "4821"}] and b["firefly"]["awaiting_approval"] == 1
    assert "balance" not in json.dumps(b["items"])
    assert c.post("/api/banks/link-token", json={}).json()["link_token"] == "link-production-abc"
    assert c.post("/api/banks/exchange", json={"public_token": "public-x", "institution": "PayPal"}).json()["institution"] == "PayPal"
    assert c.post("/api/banks/pull").json()["pulled"] is True
    assert log.count(("POST", "/login")) == 1                                                                           # one sign-in, then the session is reused
    c.put("/api/settings", json={"connector_token": "wrong password here"})
    assert "refused" in c.get("/api/banks").json()["detail"]


def test_the_page_and_health(tmp_path):
    c = TestClient(app(tmp_path, world()))
    assert c.get("/health").json()["ok"] and "Coinpile" in c.get("/").text
    assert c.get("/auth/firefly").status_code == 404                                                                    # no OAuth client configured: token sign-in only


def test_sign_in_with_firefly_starts_with_pkce(tmp_path):
    a = web.create_app(firefly_url="http://firefly:8080", public_firefly_url="https://ff.example.com", data_dir=str(tmp_path), client_id="abc-123", public_url="https://coins.example.com")
    c = TestClient(a)
    assert c.get("/auth/me").json()["oauth"] is True
    loc = c.get("/auth/firefly", follow_redirects=False).headers["location"]
    assert loc.startswith("https://ff.example.com/oauth/authorize?") and "code_challenge_method=S256" in loc and "redirect_uri=https%3A%2F%2Fcoins.example.com%2Fauth%2Fcallback" in loc
    assert c.get("/auth/callback?code=x&state=forged", follow_redirects=False).headers["location"] == "/?signin=failed"


def test_the_agent_answers_from_the_firefly_iii_copy(tmp_path):
    from devtools.fake_llm import openai_server
    log = []
    script = [[("list_accounts", {}), ("search_transactions", {"q": "netflix", "direction": "out"}), ("list_subscriptions", {}), ("savings_summary", {}),
               ("find_recurring_payments", {}), ("write_note", {"title": "Plan", "body": "Cancel Netflix?"})]]
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), llm_transport=openai_server(script, log), today=lambda: TODAY)
    c = signed_in(a)
    assert c.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True}).status_code == 200
    r = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "where does my money go?"}]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [t["tool"] for t in out["trace"]] == [s[0] for s in script[0]] and all(t["ok"] for t in out["trace"])
    got = {t["tool"]: json.loads(m["content"]) for t, m in zip(out["trace"], [m for m in log[1]["body"]["messages"] if m["role"] == "tool"])}
    assert {x["id"] for x in got["list_accounts"]} == {"ff:1", "ff:3", "ff:5", "ff:7"}
    assert got["search_transactions"]["total"] == 3 and got["search_transactions"]["sum_out"] == -47.97
    assert [s["name"] for s in got["list_subscriptions"]] == ["Rent"] and got["savings_summary"]["total_savings"] == 5000
    assert any("netflix" in s["name"].lower() for s in got["find_recurring_payments"])                                 # the subscription catcher works on Firefly III data
    assert got["write_note"]["saved"] and c.get("/api/notes").json()[0]["author"] == "agent"


def test_net_worth_has_a_past_from_firefly_iii(tmp_path):
    from tests.fake_firefly import add_history
    ff = world()
    add_history(ff, "1", 1, {"2026-01-01": "0", "2026-01-02": "500", "2026-01-03": "500", "2026-06-01": "1200.50"})
    add_history(ff, "1", 5, {"2026-01-01": "0", "2026-03-01": "-250"})                    # a card: owed, so it lowers net worth
    c = signed_in(app(tmp_path, ff))
    h = {x["date"]: x["net_worth"] for x in c.get("/api/history").json()}
    assert h["2026-01-02"] == 500 and h["2026-03-01"] == 250 and h["2026-06-01"] == 950.5 and "2026-01-01" not in h
    c.post("/api/refresh?full=true")                                                      # reading it again does not double it
    assert {x["date"]: x["net_worth"] for x in c.get("/api/history").json()}["2026-06-01"] == 950.5


def test_home_assistant_tokens_read_only_what_they_may(tmp_path):
    a = app(tmp_path, world())
    c = signed_in(a)
    t = c.post("/api/tokens", json={"name": "Home Assistant", "scopes": ["summary", "refresh"]}).json()
    assert t["token"].startswith("cp_1_")
    ha = TestClient(a)                                                                           # no browser session: only the token
    A = {"Authorization": "Bearer " + t["token"]}
    s = ha.get("/api/v1/me/summary", headers=A).json()
    assert s["net_worth"] == round(1200.5 + 5000 - 250 - 9000, 2) and s["subscriptions_monthly"] == 1500 and s["read_from_firefly_at"]
    flat = ha.get("/api/v1/me/ha", headers=A).json()
    assert all(not isinstance(v, (dict, list)) for v in flat.values()) and flat["next_bill_name"] == "Rent"
    assert ha.get("/api/v1/me/transactions", headers=A).status_code == 403                       # not granted
    assert ha.get("/api/v1/me/summary?refresh=1", headers=A).json()["refreshed"] is True
    assert ha.get("/api/v1/me/summary", headers={"Authorization": "Bearer cp_1_wrong"}).status_code == 401
    assert ha.get("/api/v1/me/summary", headers={"Authorization": "Bearer cp_99_" + "x" * 40}).status_code == 401
    assert ha.get("/api/overview", headers=A).status_code == 401                                 # a token is never a login
    assert ha.get("/api/v1/summary", headers=A).status_code == 403                               # the Orb needs its own scope
    orb = c.post("/api/tokens", json={"name": "Orb", "scopes": ["orb"]}).json()["token"]
    c.put("/api/settings", json={"share": ["net_worth", "bill_list"]})
    o = ha.get("/api/v1/summary", headers={"Authorization": "Bearer " + orb}).json()["goblins"]["alice"]
    assert set(o) == {"name", "net_worth", "bill_list"} and o["bill_list"][0]["name"] == "Rent"
    c.delete(f"/api/tokens/{t['id']}")
    assert ha.get("/api/v1/me/summary", headers=A).status_code == 401
    assert t["token"].encode() not in (tmp_path / "people" / "1.db").read_bytes()               # only a hash is kept


def test_two_coinpile_people_link_and_share_a_firefly_account_and_a_goal(tmp_path):
    import time
    a = app(tmp_path, world())
    alice, bob = signed_in(a, ALICE), signed_in(a, BOB)
    assert alice.get("/api/links/people").json()["people"] == ["bob"]
    assert alice.post("/api/links", json={"peer": "bob"}).status_code == 200
    assert bob.get("/api/requests").json()["links"] == ["alice"]
    assert bob.post("/api/links/alice/accept", json={}).status_code == 200
    def wait(cond):
        for _ in range(40):
            if cond():
                return True
            time.sleep(0.05)
        return False
    assert wait(lambda: alice.get("/api/links").json()["links"][0]["status"] == "active")
    # alice lets bob see her Firefly III checking account; nothing else
    assert alice.put("/api/links/bob/shares", json={"kind": "account", "ref": "ff:1", "on": True}).status_code == 200
    assert wait(lambda: bob.get("/api/sharing").json()[0]["from_them"])
    seen = bob.get("/api/sharing").json()[0]["from_them"]
    assert [(x["ref"], x["name"], x["balance"]) for x in seen] == [("ff:1", "Everyday Checking", 1200.5)]
    assert "Axos" not in bob.get("/api/sharing").text and "KROGER" not in bob.get("/api/sharing").text            # never other accounts or transactions
    # a shared goal, with money set aside on both sides
    g = alice.post("/api/goals", json={"name": "Trip", "target": 3000}).json()
    assert alice.post(f"/api/goals/{g['id']}/share", json={"peers": ["bob"]}).status_code == 200
    assert wait(lambda: bob.get("/api/requests").json()["goals"])
    bg = bob.get("/api/requests").json()["goals"][0]
    assert bob.post(f"/api/goals/{bg['id']}/accept", json={}).status_code == 200
    assert alice.post(f"/api/goals/{g['id']}/notes", json={"text": "Flights are cheaper in May"}).status_code == 200
    assert wait(lambda: "Flights are cheaper in May" in bob.get("/api/goals").text)
    # stopping the share takes the account away again
    alice.put("/api/links/bob/shares", json={"kind": "account", "ref": "ff:1", "on": False})
    assert wait(lambda: not bob.get("/api/sharing").json()[0]["from_them"])


def test_money_in_firefly_piggy_banks_is_not_free(tmp_path):
    c = signed_in(app(tmp_path, world()))
    v = c.get("/api/goals").json()
    assert v["total"] == 5000 and v["in_piggy_banks"] == 400 and v["allocated"] == 400 and v["free"] == 4600     # Vacation holds 400 of the savings account
    assert v["places"][0]["earmarked"] == 400 and v["piggy_banks"][0]["name"] == "Vacation"
    g = c.post("/api/goals", json={"name": "Car", "target": 10000, "auto_allocate": True}).json()
    assert g["allocated"] == 4600                                                                                  # a goal can take only what is really free
    assert c.get("/api/afford?amount=100").json()["free_savings"] == 0


RH = """Activity Date,Process Date,Settle Date,Instrument,Description,Trans Code,Quantity,Price,Amount
1/2/2026,1/2/2026,1/4/2026,AAPL,Apple,Buy,10,$100.00,($1000.00)
1/3/2026,1/3/2026,1/5/2026,AAPL,Apple,Sell,4,$110.00,$440.00
1/1/2026,1/1/2026,1/1/2026,,ACH Deposit,ACH,,,"$2,000.00"
"""


def test_robinhood_lives_in_coinpile(tmp_path):
    c = signed_in(app(tmp_path, world()))
    before = c.get("/api/overview").json()["net_worth"]
    assert "error" in c.get("/api/robinhood/main").json()
    assert c.post("/api/robinhood/csv-upload", content="not,a,report").status_code == 422
    assert c.post("/api/robinhood/csv-upload", content=RH).status_code == 200
    m = c.get("/api/robinhood/main").json()
    assert {p["symbol"]: p["quantity"] for p in m["positions"]}["AAPL"] == 6
    assert c.get("/api/status").json()["robinhood"]["csv_files"]
    # an activity report has no account value in it, so it does not invent one; a connector that reports one (live, agent) adds an investment account
    assert not any(a["id"] == "rh:main" for a in c.get("/api/accounts").json()) and c.get("/api/overview").json()["net_worth"] == before


def test_bank_and_paypal_csvs_go_into_coinpile_accounts_never_into_firefly(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    pp = 'Date,Time,TimeZone,Name,Type,Status,Currency,Gross,Fee,Net,Transaction ID\n11/02/2026,10:00:00,PST,Etsy Shop,Payment,Completed,USD,-25.00,0.00,-25.00,7AB12345\n'
    r = c.post("/api/import/csv", content=pp).json()
    assert r["provider"] == "paypal" and r["added"] == 1 and c.post("/api/import/csv", content=pp).json()["added"] == 0
    jar = c.post("/api/accounts", json={"name": "Cash jar", "kind": "cash", "balance": 20}).json()
    csv = "Date,Description,Amount\n2026-11-01,GAS STATION,-40.00\n"
    assert c.post(f"/api/import/bank-csv?account_id={jar['id']}", content=csv).json()["added"] == 1
    assert c.post("/api/import/bank-csv?account_id=ff:1", content=csv).status_code == 409
    assert {m for m, _, _ in ff.calls} == {"GET"}


def test_a_robinhood_connector_with_a_value_adds_an_investment_account(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_AGENT_URL", "http://rh-agent:8000")
    monkeypatch.setenv("RH_OWNER", "alice@example.com")                                           # the connector is alice's Robinhood account
    rh = httpx.MockTransport(lambda req: httpx.Response(200, json={"data": {"equity": 1234.5} if req.url.path == "/balances" else [], "as_of": "2026-11-10T00:00:00+00:00", "source": "mcp"}))
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), robinhood_transport=rh, today=lambda: TODAY)
    c = signed_in(a)
    acct = next(x for x in c.get("/api/accounts").json() if x["id"] == "rh:agent")
    assert acct["kind"] == "investment" and acct["balance"] == 1234.5 and acct["source"] == "robinhood"
    bob = signed_in(a, BOB)
    assert "error" in bob.get("/api/robinhood/agent").json() and not any(x["id"].startswith("rh:") for x in bob.get("/api/accounts").json())   # never someone else's portfolio


def test_nothing_is_sent_to_firefly_iii_until_each_part_is_switched_on(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    assert c.get("/api/status").json()["sends"] == {"piggies": False, "bills": False, "categories": False, "accounts": False}
    g = c.post("/api/goals", json={"name": "Roof", "target": 3000}).json()
    c.post(f"/api/goals/{g['id']}/allocate", json={"place": "a:ff:3", "amount": 1000})
    c.post("/api/subscriptions", json={"name": "Hulu", "amount": 9.99, "next_date": "2026-11-25", "cycle": "monthly"})
    assert c.put("/api/transactions/ff:3/category", json={"category": "Food"}).status_code == 409
    assert c.post("/api/transactions", json={"account_id": "ff:1", "date": "2026-11-09", "name": "x", "amount": -1}).status_code == 409
    assert c.post("/api/accounts", json={"name": "Jar", "kind": "cash", "balance": 5}).json()["source"] == "manual"
    assert not getattr(ff, "writes", [])


def test_switched_on_parts_go_into_firefly_iii(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    c.put("/api/settings", json={"ff_send_goals": "on", "ff_send_bills": "on", "ff_send_categories": "on", "ff_send_accounts": "on"})
    # goals -> piggy banks, and the money is not counted twice
    g = c.post("/api/goals", json={"name": "Roof", "target": 3000}).json()
    c.post(f"/api/goals/{g['id']}/allocate", json={"place": "a:ff:3", "amount": 1000})
    pig = next(p for p in ff.data["1"]["piggies"] if p["attributes"]["name"] == "Roof")
    assert pig["attributes"]["accounts"] == [{"account_id": "3", "name": "x", "current_amount": "1000.00"}] and pig["attributes"]["target_amount"] == "3000.00"
    c.post("/api/refresh")
    v = c.get("/api/goals").json()
    assert v["in_piggy_banks"] == 400 and v["allocated"] == 1400 and v["free"] == 3600                # Vacation (Firefly III's own) + Roof (the goal itself), once each
    ff.piggy_update_breaks = True                                                                     # Firefly III 6.7's piggy-bank amount bug: replaced instead
    c.post(f"/api/goals/{g['id']}/allocate", json={"place": "a:ff:3", "amount": 1500})
    assert [p["attributes"]["accounts"][0]["current_amount"] for p in ff.data["1"]["piggies"] if p["attributes"]["name"] == "Roof"] == ["1500.00"]
    c.post(f"/api/goals/{g['id']}/close", json={})
    assert "Roof" not in [p["attributes"]["name"] for p in ff.data["1"]["piggies"]] and "Vacation" in [p["attributes"]["name"] for p in ff.data["1"]["piggies"]]
    # subscriptions -> bills (Coinpile's own extras stay), editing and deleting follow; daily stays in Coinpile
    s = c.post("/api/subscriptions", json={"name": "Spotify", "amount": 11.99, "next_date": "2026-11-20", "cycle": "monthly", "notify_days": 5}).json()
    bill = next(b for b in ff.data["1"]["bills"] if b["attributes"]["name"] == "Spotify")
    assert s["source"] == "firefly" and bill["attributes"]["repeat_freq"] == "monthly"
    c.put(f"/api/subscriptions/{s['id']}", json={"name": "Spotify", "amount": 16.99, "next_date": "2026-11-20", "cycle": "monthly", "notify_days": 5})
    assert bill["attributes"]["amount_min"] == "16.99"
    c.post("/api/refresh")
    mine = [x for x in c.get("/api/subscriptions").json()["items"] if x["name"] == "Spotify"]
    assert len(mine) == 1 and mine[0]["notify_days"] == 5
    assert c.post("/api/subscriptions", json={"name": "Parking", "amount": 2, "next_date": "2026-11-12", "every": 1, "unit": "day"}).json()["source"] == "manual"
    c.delete(f"/api/subscriptions/{s['id']}")
    assert "Spotify" not in [b["attributes"]["name"] for b in ff.data["1"]["bills"]]
    # categories, with a Firefly III rule
    assert c.put("/api/transactions/ff:3/category", json={"category": "Food", "make_rule": True}).json()["in_firefly"]
    assert ff.data["1"]["groups"][2]["attributes"]["transactions"][0]["category_name"] == "Food" and ff.data["1"]["rules"][0]["actions"][0]["value"] == "Food"
    # accounts, transactions, a bank CSV twice
    acc = c.post("/api/accounts", json={"name": "Everyday", "institution": "Ally", "kind": "cash", "balance": 100}).json()
    assert acc["id"].startswith("ff:") and ff.data["1"]["accounts"][-1]["attributes"]["name"] == "Ally - Everyday"
    assert c.post("/api/accounts", json={"name": "Jar", "kind": "cash", "coinpile_only": True}).json()["source"] == "manual"
    assert c.post("/api/transactions", json={"account_id": acc["id"], "date": "2026-11-09", "name": "Coffee", "amount": -4.5}).json()["id"].startswith("ff:")
    csv = "Date,Description,Amount\n2026-11-01,GAS STATION,-40.00\n2026-11-02,GAS STATION,-40.00\n"
    one = c.post(f"/api/import/bank-csv?account_id={acc['id']}", content=csv).json()
    two = c.post(f"/api/import/bank-csv?account_id={acc['id']}", content=csv).json()
    assert (one["added"], two["added"], two["already_there"]) == (2, 0, 2)


def test_a_signed_firefly_webhook_makes_coinpile_read_at_once(tmp_path):
    import hashlib, hmac, time as _t
    ff = world()
    a = app(tmp_path, ff)
    c = signed_in(a)
    url = c.get("/api/firefly/webhook").json()["url"]
    path = url.split("testserver", 1)[1]
    c.put("/api/settings", json={"ff_webhook_secret": "whsec-123"})
    body = b'{"trigger":"STORE_TRANSACTION","content":{}}'
    ts = int(_t.time())
    sig = hmac.new(b"whsec-123", f"{ts}.".encode() + body, hashlib.sha3_256).hexdigest()
    hook = TestClient(a)                                                                         # Firefly III has no Coinpile session
    assert hook.post(path, content=body, headers={"Signature": f"t={ts},v1=deadbeef"}).status_code == 401
    assert hook.post(path, content=body, headers={"Signature": f"t={ts - 3600},v1=" + hmac.new(b"whsec-123", f"{ts - 3600}.".encode() + body, hashlib.sha3_256).hexdigest()}).status_code == 401
    assert hook.post("/hooks/firefly/not-a-key", content=body, headers={"Signature": f"t={ts},v1={sig}"}).status_code == 404
    ff.tx("1", 60, "withdrawal", 7.25, "WEBHOOK LUNCH", 1, 95, "2026-11-10")
    assert hook.post(path, content=body, headers={"Signature": f"t={ts},v1={sig}"}).status_code == 200
    for _ in range(80):
        if c.get("/api/transactions?q=webhook").json()["items"]:
            break
        _t.sleep(0.1)
    assert c.get("/api/transactions?q=webhook").json()["items"][0]["amount"] == -7.25 and c.get("/api/firefly/webhook").json()["last"]



def test_a_person_never_gets_the_containers_or_anyone_elses_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CALDAV_URL", "https://dav.example.com/operator")
    monkeypatch.setenv("CALDAV_USER", "admin")
    monkeypatch.setenv("CALDAV_PASSWORD", "S3cret-admin")
    sent = []
    dav = httpx.MockTransport(lambda req: sent.append(req.headers.get("authorization")) or httpx.Response(207))
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), caldav_transport=dav, today=lambda: TODAY)
    bob = signed_in(a, BOB)
    assert bob.get("/api/settings").json()["caldav_url"] == "" and bob.get("/api/settings").json()["caldav_user"] == ""
    bob.put("/api/settings", json={"caldav_url": "https://attacker.example/x"})
    bob.post("/api/settings/test-caldav")
    assert not any(h and "S3cret" in __import__("base64").b64decode(h.split()[1]).decode() for h in sent if h and h.startswith("Basic"))


def test_a_handle_is_never_handed_to_someone_new(tmp_path):
    ff = FakeFirefly({ALICE: {"id": "1", "email": "alice@example.com"}, BOB: {"id": "2", "email": "bob@example.com"}, "tok-mallory-" + "m" * 30: {"id": "3", "email": "alice@evil.example"}})
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), today=lambda: TODAY)
    alice, bob = signed_in(a, ALICE), signed_in(a, BOB)
    alice.post("/api/links", json={"peer": "bob"})
    bob.post("/api/links/alice/accept", json={})
    import time as _t
    _t.sleep(0.3)
    assert alice.post("/api/forget").json() == {"forgotten": True}
    _t.sleep(0.3)
    assert bob.get("/api/links").json()["links"] == []                                           # bob was told alice is gone
    mallory = signed_in(a, "tok-mallory-" + "m" * 30)
    assert mallory.get("/api/links").json()["me"] != "alice" and "alice" not in bob.get("/api/links/people").json()["people"]


def test_oauth_can_only_be_finished_by_the_browser_that_started_it(tmp_path):
    a = web.create_app(firefly_url="http://firefly:8080", data_dir=str(tmp_path), client_id="abc-123", public_url="http://testserver")
    attacker = TestClient(a)
    loc = attacker.get("/auth/firefly", follow_redirects=False).headers["location"]
    state = dict(x.split("=", 1) for x in loc.split("?", 1)[1].split("&"))["state"]
    victim = TestClient(a)
    assert victim.get(f"/auth/callback?code=x&state={state}", follow_redirects=False).headers["location"] == "/?signin=failed"


def test_large_bodies_are_refused_without_reading_them_all(tmp_path):
    c = signed_in(app(tmp_path, world()))
    big = b"x" * (web.MAX_BODY + 10)
    assert c.post("/api/import/csv", content=big).status_code == 413
    def chunks():
        for _ in range(8):
            yield b"y" * 1_000_000
    assert c.post("/api/import/csv", content=chunks()).status_code == 413
    assert c.get("/health").headers["x-frame-options"] == "DENY"


def test_setting_one_splits_category_keeps_the_other_splits(tmp_path):
    ff = world()
    ff.tx("1", 70, "withdrawal", 60, "COSTCO food", 1, 96, "2026-11-08", None)
    ff.data["1"]["groups"][-1]["attributes"]["transactions"].append({**ff.data["1"]["groups"][-1]["attributes"]["transactions"][0], "transaction_journal_id": "71", "amount": "40.0", "description": "COSTCO tires"})
    c = signed_in(app(tmp_path, ff))
    c.put("/api/settings", json={"ff_send_categories": "on"})
    assert c.put("/api/transactions/ff:70/category", json={"category": "Groceries"}).status_code == 200
    splits = {t["transaction_journal_id"]: t for t in ff.data["1"]["groups"][-1]["attributes"]["transactions"]}
    assert set(splits) == {"70", "71"} and splits["70"]["category_name"] == "Groceries" and splits["71"]["category_name"] is None


@pytest.mark.parametrize("url,ok", [("http://127.0.0.1:11434/v1", False), ("http://localhost:8000", False), ("http://169.254.169.254/latest/meta-data", False),
                                    ("http://[::1]:80", False), ("http://[::ffff:127.0.0.1]/", False), ("http://0.0.0.0/", False), ("http://metadata.google.internal/", False),
                                    ("http://192.168.1.50:11434/v1", True), ("http://10.0.0.5:6421", True), ("https://203.0.113.9/v1", True)])
def test_typed_addresses_cannot_reach_the_server_itself_or_metadata(url, ok):
    from coinpile import netguard
    if ok:
        netguard.check(url)
    else:
        with pytest.raises(netguard.Blocked):
            netguard.check(url)


def test_the_connector_name_cannot_steer_the_path(tmp_path):
    c = signed_in(app(tmp_path, world(), connector_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    c.put("/api/settings", json={"connector_url": "http://connector:8000", "connector_name": "../admin/p/bob", "connector_token": "a long enough password"})
    assert "lowercase" in c.get("/api/banks").json()["detail"]


def hook_receiver(status=200):
    got = []

    def h(req: httpx.Request):
        got.append({"url": str(req.url), "event": req.headers.get("coinpile-event"), "sig": req.headers.get("coinpile-signature"), "body": req.content})
        return httpx.Response(status)
    return httpx.MockTransport(h), got


def test_coinpile_sends_signed_webhooks_for_what_happens(tmp_path):
    from coinpile import webhooks
    ff = world()
    t, got = hook_receiver()
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), webhook_transport=t, today=lambda: TODAY)
    c = signed_in(a)
    assert c.post("/api/webhooks", json={"name": "HA", "url": "ftp://x", "events": ["transaction.new"]}).status_code == 422
    assert c.post("/api/webhooks", json={"name": "HA", "url": "http://127.0.0.1:8123/api/webhook/x", "events": ["transaction.new"]}).status_code == 422     # not the server itself
    h = c.post("/api/webhooks", json={"name": "Home Assistant", "url": "http://ha.local:8123/api/webhook/coinpile", "events": ["transaction.new", "balance.changed", "goal.changed", "refresh.done"]}).json()
    assert h["secret"].startswith("whsec_") and "secret" not in c.get("/api/webhooks").text
    # the test button
    assert c.post(f"/api/webhooks/{h['id']}/test").json() == {"ok": True, "status": "200"}
    m = json.loads(got[-1]["body"])
    assert m["event"] == "test" and m["person"] == "alice" and webhooks.verify(h["secret"], got[-1]["body"], got[-1]["sig"])
    assert not webhooks.verify("whsec_wrong", got[-1]["body"], got[-1]["sig"])
    # what already happened before the webhook existed is not sent; a new transaction is
    got.clear()
    ff.tx("1", 80, "withdrawal", 12.5, "PIZZA PLACE", 1, 97, "2026-11-10", "Food")
    ff.data["1"]["accounts"][0]["attributes"]["current_balance"] = "1188.00"
    c.post("/api/refresh")
    events = [json.loads(g["body"]) for g in got]
    tx = next(e for e in events if e["event"] == "transaction.new")
    assert tx["data"]["name"] == "PIZZA PLACE" and tx["data"]["amount"] == -12.5 and tx["data"]["account"]["institution"] == "Huntington"
    assert any(e["event"] == "balance.changed" and e["data"]["balance"] == 1188.0 for e in events), [e["data"] for e in events if e["event"] == "balance.changed"]
    assert any(e["event"] == "refresh.done" for e in events) and all(webhooks.verify(h["secret"], g["body"], g["sig"]) for g in got)
    assert sum(1 for e in events if e["event"] == "transaction.new") == 1                   # the old ones were not replayed
    got.clear()
    c.post("/api/refresh")
    assert not [g for g in got if g["event"] == "transaction.new"]                           # nothing new, nothing sent
    g = c.post("/api/goals", json={"name": "Trip", "target": 900}).json()
    for _ in range(40):
        if any(x["event"] == "goal.changed" for x in got):
            break
        __import__("time").sleep(0.05)
    assert json.loads(next(x for x in got if x["event"] == "goal.changed")["body"])["data"]["name"] == "Trip"


def test_a_webhook_without_details_sends_no_names_or_amounts(tmp_path):
    ff = world()
    t, got = hook_receiver()
    c = signed_in(web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), webhook_transport=t, today=lambda: TODAY))
    c.post("/api/webhooks", json={"name": "Lamp", "url": "https://hooks.example.com/x", "events": ["transaction.new"], "detail": False})
    ff.tx("1", 81, "withdrawal", 99.99, "SECRET SHOP", 1, 98, "2026-11-10")
    c.post("/api/refresh")
    body = got[-1]["body"].decode()
    assert "SECRET SHOP" not in body and "99.99" not in body and json.loads(body)["data"] == {"account_kind": "cash", "direction": "out"}


def test_failed_webhooks_are_retried_then_given_up(tmp_path, monkeypatch):
    from coinpile import webhooks
    import time as _t
    ff = world()
    t, got = hook_receiver(status=500)
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), webhook_transport=t, today=lambda: TODAY)
    c = signed_in(a)
    h = c.post("/api/webhooks", json={"url": "https://hooks.example.com/x", "events": ["refresh.done"]}).json()
    assert c.post(f"/api/webhooks/{h['id']}/test").json() == {"ok": False, "status": "500"}
    db = a.person_db("1")
    clock = [_t.time()]
    monkeypatch.setattr(webhooks.time, "time", lambda: clock[0])
    for _ in range(10):
        clock[0] += 13 * 3600
        webhooks.deliver(db, t)
    row = db.one("SELECT tries, done FROM webhook_outbox WHERE event='test'")
    assert row == {"tries": len(webhooks.RETRY_MINUTES) + 1, "done": 2} and len(got) == len(webhooks.RETRY_MINUTES) + 1
    assert c.get(f"/api/webhooks/{h['id']}/deliveries").json()[0]["status"] == "500"
    assert c.get("/api/webhooks").json()["items"][0]["last_status"] == "500"


def test_csvs_dropped_in_the_imports_folder_are_imported(tmp_path, monkeypatch):
    a = app(tmp_path, world())
    c = signed_in(a)
    folder = tmp_path / "people" / "1-imports"
    folder.mkdir(parents=True)
    (folder / "paypal.csv").write_text('Date,Time,TimeZone,Name,Type,Status,Currency,Gross,Fee,Net,Transaction ID\n11/02/2026,10:00:00,PST,Etsy Shop,Payment,Completed,USD,-25.00,0.00,-25.00,7AB12345\n')
    (folder / "junk.csv").write_text("nothing,useful\n1,2\n")
    assert a.scan_drop_folder("1") == 1
    assert (folder / "done" / "paypal.csv").exists() and (folder / "failed" / "junk.csv").exists()
    assert any(t["name"] == "Etsy Shop" for t in c.get("/api/transactions?limit=100").json()["items"])
