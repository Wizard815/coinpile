"""The last audit items: a lost answer from Firefly III never makes a duplicate, budgets that are not one calendar month, bills and income
Firefly III keeps in another currency, signing out everywhere, and what happens once Firefly III refuses Coinpile's sign-in."""
import time
from datetime import date

import httpx
from fastapi.testclient import TestClient

from coinpile import firefly, ledger, plan
from tests.audit_helpers import ALICE, alone, client, make_app, read, world
from tests.test_coinpile import BOB
from tests.test_pages_and_plans import rec

T = date(2026, 11, 10)


def start(tmp_path, ff=None, switches=()):
    ff = ff or world()
    a, _ = make_app(tmp_path, ff, T)
    c = client(a)
    for s in switches:
        assert c.put("/api/settings", json={s: "on"}).status_code == 200
    return ff, c, a


def lose_answers(ff, reads_too=False):
    """Firefly III does what it is asked, but the answer never arrives (a timeout). With reads_too it cannot be reached at all afterwards."""
    orig_write, orig_transport, state = ff.write, ff.transport, {"down": False}

    def write(uid, d, method, p, b):
        orig_write(uid, d, method, p, b)
        state["down"] = reads_too
        raise httpx.ReadTimeout("no answer")
    ff.write = write

    def back():
        ff.write = orig_write
        state["down"] = False
    return state, back


# ---------------------------------------------------------------- a lost answer is "unknown", not "failed"
def test_a_transaction_whose_answer_was_lost_is_not_recorded_twice(tmp_path):
    ff, c, a = start(tmp_path, switches=("ff_send_accounts",))
    body = {"account_id": "ff:1", "date": "2026-11-09", "name": "Farmers market", "amount": -12.5}
    mine = lambda: [g for g in ff.data["1"]["groups"] if g["attributes"]["transactions"][0]["description"] == "Farmers market"]      # noqa: E731
    state, back = lose_answers(ff)
    assert c.post("/api/transactions", json=body).status_code == 200 and len(mine()) == 1          # looked, found it: it was made
    back()
    assert c.post("/api/transactions", json=body).status_code == 200 and len(mine()) == 2          # a second one on purpose is a second one
    # Firefly III gone altogether after the write: said so, and the next try finds the first one instead of recording another
    body2 = {**body, "name": "Bakery", "amount": -4}
    bakery = lambda: [g for g in ff.data["1"]["groups"] if g["attributes"]["transactions"][0]["description"] == "Bakery"]             # noqa: E731
    state, back = lose_answers(ff)
    real = ff.transport()

    def flaky(req):
        if state.get("gone") and req.method == "GET":
            raise httpx.ConnectError("down")
        return real.handler(req)
    db = a.person_db("1")
    from coinpile import ffwrite
    w = ffwrite.Writer.__new__(ffwrite.Writer)
    w.db, w.ff = db, lambda: firefly.Firefly("https://firefly.example.com", ALICE, httpx.MockTransport(flaky))
    state["gone"] = True
    try:
        w.add_transaction("ff:1", "2026-11-09", "Bakery", -4.0, None)
        raise AssertionError("should have said Firefly III could not be reached")
    except firefly.Unreachable:
        pass
    assert len(bakery()) == 1 and db.one("SELECT COUNT(*) n FROM ff_pending")["n"] == 1
    back()
    state["gone"] = False
    assert c.post("/api/transactions", json=body2).status_code == 200
    assert len(bakery()) == 1 and db.one("SELECT COUNT(*) n FROM ff_pending")["n"] == 0


def test_a_bill_and_a_piggy_bank_whose_answer_was_lost_are_not_made_twice(tmp_path):
    ff, c, a = start(tmp_path, switches=("ff_send_bills", "ff_send_goals", "ff_send_piggies"))
    state, back = lose_answers(ff)
    s = c.post("/api/subscriptions", json={"name": "Spotify", "amount": 11.99, "next_date": "2026-11-20", "cycle": "monthly"}).json()
    assert s["source"] == "firefly" and "not in Firefly III" not in (s.get("notes") or "")
    assert len([b for b in ff.data["1"]["bills"] if b["attributes"]["name"] == "Spotify"]) == 1
    # a piggy bank from the Piggy banks page
    assert c.post("/api/piggy-banks", json={"name": "Bike", "account_id": "ff:3", "target": 300}).status_code == 200
    assert len([p for p in ff.data["1"]["piggies"] if p["attributes"]["name"] == "Bike"]) == 1
    # a goal's piggy bank: the first try's answer is lost and Firefly III stays away; the next sync finds that piggy bank and keeps to it
    g = c.post("/api/goals", json={"name": "Laptop", "target": 1500}).json()
    c.post(f"/api/goals/{g['id']}/allocate", json={"amount": 300, "place": "a:ff:3"})
    back()
    c.post(f"/api/goals/{g['id']}/allocate", json={"amount": 350, "place": "a:ff:3"})
    laptops = [p for p in ff.data["1"]["piggies"] if p["attributes"]["name"] == "Laptop"]
    assert len(laptops) == 1 and float(laptops[0]["attributes"]["accounts"][0]["current_amount"]) == 350


# ---------------------------------------------------------------- budgets that are not one calendar month
class Pages:
    def __init__(self, limits, budgets):
        self.rows = {"/budget-limits": limits, "/budgets": budgets}

    def pages(self, path, **params):
        return iter(self.rows[path])


def test_a_budget_limit_counts_for_the_part_of_it_in_the_month():
    lim = lambda bid, a, b, amt: {"attributes": {"budget_id": bid, "start": a + "T00:00:00+00:00", "end": b + "T00:00:00+00:00", "amount": str(amt)}}      # noqa: E731
    bud = lambda bid, name, **kw: {"id": bid, "attributes": {"name": name, "active": True, "spent": [{"sum": "-100"}], **kw}}                              # noqa: E731
    ff = Pages([lim("1", "2026-11-01", "2026-11-30", 400), lim("2", "2026-01-01", "2026-12-31", 12000), lim("3", "2026-10-26", "2026-11-01", 70), lim("3", "2026-11-02", "2026-11-08", 70),
                lim("4", "nonsense", "2026-11-30", 50)],
               [bud("1", "Groceries"), bud("2", "Travel"), bud("3", "Fun"), bud("4", "Odd"), bud("5", "Lunch", auto_budget_type="reset", auto_budget_amount="50", auto_budget_period="weekly"),
                bud("6", "Rent", auto_budget_type="reset", auto_budget_amount="900", auto_budget_period="monthly"), bud("7", "No limit")])
    got = {b["name"]: b for b in firefly.budgets(ff, date(2026, 11, 1), date(2026, 11, 30))}
    assert got["Groceries"]["limit"] == 400 and got["Groceries"]["prorated"] is False              # exactly this month: all of it
    assert got["Travel"]["limit"] == round(12000 * 30 / 365, 2) and got["Travel"]["prorated"]      # a year's limit: November's days of it
    assert got["Fun"]["limit"] == 80 and got["Fun"]["prorated"]                                     # 1 day of the week that straddles the month, and a whole week
    assert got["Odd"]["limit"] == 50                                                               # dates that cannot be read: counted in full, as before
    assert got["Lunch"]["limit"] == round(50 * 30 / 7, 2) and got["Lunch"]["prorated"] and got["Rent"]["limit"] == 900 and not got["Rent"]["prorated"]
    assert got["No limit"]["limit"] is None and got["Travel"]["left"] == round(12000 * 30 / 365 - 100, 2)


# ---------------------------------------------------------------- bills and income in a currency Firefly III does not convert
def test_bills_and_income_in_another_currency_are_shown_but_not_added_up():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="1000", primary_currency_code="USD")
    ff.data["1"]["bills"] = [{"type": "bills", "id": "1", "attributes": {"name": "Tokyo gym", "amount_min": "9000", "amount_max": "9000", "repeat_freq": "monthly", "skip": 0, "currency_code": "JPY",
                                                                       "primary_currency_code": "USD", "date": "2026-01-15T00:00:00+00:00", "pay_dates": ["2026-11-15T00:00:00+00:00"], "active": True}},
                             {"type": "bills", "id": "2", "attributes": {"name": "Rent", "amount_min": "1500", "amount_max": "1500", "repeat_freq": "monthly", "skip": 0, "currency_code": "USD",
                                                                       "date": "2026-01-20T00:00:00+00:00", "pay_dates": ["2026-11-20T00:00:00+00:00"], "active": True}}]
    yen = rec(1, "deposit", "Tokyo salary", 300000, "2026-01-25", ["2026-11-25"], unit="monthly", src="90", dst="1")
    yen["attributes"]["transactions"][0]["currency_code"] = "JPY"
    ff.data["1"]["recurrences"] = [yen, rec(2, "deposit", "ACME paycheck", 2500, "2026-01-01", ["2026-12-01"], unit="monthly", src="90", dst="1")]
    db = read(ff, T)
    subs = {s["name"]: s for s in ledger.subscriptions(db, T)}
    assert subs["Tokyo gym"]["other_currency"] == "JPY" and subs["Tokyo gym"]["monthly"] == 0 and subs["Rent"]["monthly"] == 1500 and subs["Rent"]["other_currency"] is None
    up = ledger.upcoming(db, 30, T)
    assert [i["name"] for i in up["items"]] == ["Rent"] and up["total"] == 1500
    inc = {i["name"]: i for i in plan.incomes(db, T)}
    assert inc["ACME paycheck"]["active"] and not inc["Tokyo salary (JPY, not converted)"]["active"]
    note = [a for a in ledger.alerts(db, T) if a["kind"] == "other_currency"]
    assert len(note) == 1 and "1 bill" in note[0]["title"] and "1 income" in note[0]["title"] and "JPY" in note[0]["title"]


# ---------------------------------------------------------------- signing out everywhere, and a sign-in Firefly III no longer honours
def test_sign_out_everywhere_ends_every_session_and_can_end_api_tokens(tmp_path):
    ff, c, a = start(tmp_path)
    other = TestClient(a)                                                       # the same person in a second browser
    assert other.post("/auth/token", json={"token": ALICE}).status_code == 200 and other.get("/api/overview").status_code == 200
    bob = TestClient(a)
    assert bob.post("/auth/token", json={"token": BOB}).status_code == 200
    tok = c.post("/api/tokens", json={"name": "HA", "scopes": ["summary"]}).json()["token"]
    H = {"Authorization": "Bearer " + tok}
    assert TestClient(a).post("/auth/logout/everywhere", json={}).status_code == 401             # not signed in: nothing to do
    r = c.post("/auth/logout/everywhere", json={})
    assert r.status_code == 200 and r.json()["sessions_ended"] == 2 and r.json()["api_tokens_revoked"] == 0
    assert c.get("/api/overview").status_code == 401 and other.get("/api/overview").status_code == 401 and bob.get("/api/overview").status_code == 200
    assert TestClient(a).get("/api/v1/me/summary", headers=H).status_code == 200               # tokens are their own thing unless asked
    assert other.post("/auth/token", json={"token": ALICE}).status_code == 200
    assert other.post("/auth/logout/everywhere", json={"api_tokens": True}).json()["api_tokens_revoked"] == 1
    assert TestClient(a).get("/api/v1/me/summary", headers=H).status_code == 401


def test_once_firefly_refuses_the_sign_in_sessions_and_tokens_do_not_go_on_for_weeks(tmp_path):
    from coinpile import web
    ff, c, a = start(tmp_path)
    tok = c.post("/api/tokens", json={"name": "HA", "scopes": ["summary"]}).json()["token"]
    H = {"Authorization": "Bearer " + tok}
    month = lambda: a.users.db.one("SELECT MAX(expires) e FROM sessions WHERE user_id='1'")["e"] - time.time()      # noqa: E731
    assert month() > 20 * 86400
    a.users.problem("1", "sign in again")                                        # what a refresh does when Firefly III answers 401
    assert month() <= web.PROBLEM_HOURS * 3600 + 5
    assert c.get("/api/overview").status_code == 200 and TestClient(a).get("/api/v1/me/summary", headers=H).status_code == 200      # a day's grace
    with a.users.db.tx() as x:
        x.execute("UPDATE users SET problem_since=? WHERE id='1'", (time.time() - web.PROBLEM_HOURS * 3600 - 60,))
    r = TestClient(a).get("/api/v1/me/summary", headers=H)
    assert r.status_code == 401 and "sign in to Coinpile again" in r.json()["detail"]
    fresh = TestClient(a)
    assert fresh.post("/auth/token", json={"token": ALICE}).status_code == 200  # signing in again with a token Firefly III accepts clears it
    assert a.users.get("1")["problem"] is None and month() > 20 * 86400
    assert TestClient(a).get("/api/v1/me/summary", headers=H).status_code == 200
