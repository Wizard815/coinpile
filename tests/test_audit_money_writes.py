"""What Coinpile sends to Firefly III, what a bad refresh leaves behind, and alerts: cases an audit found."""
from datetime import date

import httpx

from coinpile import ledger
from coinpile.db import DB
from tests.audit_helpers import alone, client, make_app, world

T = date(2026, 11, 10)


def start(tmp_path, ff=None, switches=()):
    ff = ff or world()
    a, _ = make_app(tmp_path, ff, T)
    c = client(a)
    for s in switches:
        assert c.put("/api/settings", json={s: "on"}).status_code == 200
    return ff, c


def test_f1_accepting_a_suggested_subscription_works(tmp_path):
    ff, c = start(tmp_path)
    sug = c.get("/api/suggestions").json()
    assert sug and sug[0]["key"] == "netflix|out"
    r = c.post("/api/suggestions/accept", json={"key": sug[0]["key"], "name": "Netflix"})
    print(r.status_code, r.text[:120])
    assert r.status_code == 200, f"accepting the suggestion answers {r.status_code}"


def test_f2_changing_the_reminder_on_a_firefly_bill_sends_nothing_else_to_firefly(tmp_path):
    ff = world()
    ff.data["1"]["bills"].append({"type": "bills", "id": "7", "attributes": {"name": "Electricity", "amount_min": "50", "amount_max": "150", "repeat_freq": "monthly", "skip": 0,
                                  "date": "2026-01-31T00:00:00+00:00", "pay_dates": ["2026-11-30T00:00:00+00:00"], "active": True, "currency_code": "USD", "notes": None}})
    ff, c = start(tmp_path, ff, ("ff_send_bills",))
    s = next(x for x in c.get("/api/subscriptions").json()["items"] if x["name"] == "Electricity")
    body = {"name": s["name"], "amount": s["amount"], "next_date": s["next_date"], "every": s["every"], "unit": s["unit"], "category": s["category"], "notes": s["notes"] or "",
            "active": True, "notify_days": 7}                                    # only the reminder changes: a Coinpile-only detail
    ff.writes = []
    assert c.put(f"/api/subscriptions/{s['id']}", json=body).status_code == 200
    b = next(x for x in ff.data["1"]["bills"] if x["id"] == "7")["attributes"]
    print("sent:", ff.writes, "\nbill now:", b["amount_min"], b["amount_max"], b["date"])
    assert (float(b["amount_min"]), float(b["amount_max"])) == (50, 150), f"the bill's range 50-150 became {b['amount_min']}-{b['amount_max']} in Firefly III"
    assert b["date"][:10] == "2026-01-31", f"the bill's date moved from the 31st to {b['date'][:10]} in Firefly III"


def test_f3_a_goal_has_one_piggy_bank_after_firefly_was_down_for_a_moment(tmp_path):
    ff, c = start(tmp_path, switches=("ff_send_goals",))
    g = c.post("/api/goals", json={"name": "Laptop", "target": 1500}).json()
    assert c.post(f"/api/goals/{g['id']}/allocate", json={"amount": 300, "place": "a:ff:3"}).status_code == 200
    orig = ff.write
    ff.write = lambda uid, d, method, p, b: httpx.Response(503, json={"message": "Service Unavailable"})       # every change fails for a moment
    c.post(f"/api/goals/{g['id']}/allocate", json={"amount": 500, "place": "a:ff:3"})
    ff.write = orig
    c.post(f"/api/goals/{g['id']}/allocate", json={"amount": 600, "place": "a:ff:3"})
    c.post("/api/refresh")
    pigs = [(p["id"], p["attributes"]["name"], float(p["attributes"]["current_amount"])) for p in ff.data["1"]["piggies"]]
    gv = c.get("/api/goals").json()
    print(pigs, "allocated", gv["allocated"], "free", gv["free"])
    assert [p for p in pigs if p[1] == "Laptop"] == [p for p in pigs if p[1] == "Laptop"][:1], f"Firefly III now has two Laptop piggy banks: {pigs}"
    assert gv["allocated"] == 1000.0, f"earmarked is {gv['allocated']}: 600 for the goal + 400 Vacation = 1000; the orphaned 300 piggy bank is counted on top"


def test_f4_adding_money_to_a_piggy_bank_in_another_currency_sends_that_currency(tmp_path):
    ff = alone()
    ff.acct("1", 1, "Checking", balance="1000", primary_currency_code="USD")
    ff.acct("1", 2, "Sparkasse - Tagesgeld", role="savingAsset", balance="2000", currency_code="EUR", primary_currency_code="USD", pc_current_balance="2160")
    ff.acct("1", 4, "Sparkasse - Giro", role="defaultAsset", balance="500", currency_code="EUR", primary_currency_code="USD", pc_current_balance="540")
    ff.data["1"]["piggies"].append({"type": "piggy-banks", "id": "1", "attributes": {
        "name": "Urlaub", "target_amount": "1000", "pc_target_amount": "1080", "current_amount": "400", "pc_current_amount": "432", "currency_code": "EUR", "primary_currency_code": "USD",
        "active": True, "target_date": None, "accounts": [{"account_id": "2", "name": "Tagesgeld", "current_amount": "300", "pc_current_amount": "324"},
                                                          {"account_id": "4", "name": "Giro", "current_amount": "100", "pc_current_amount": "108"}]}})
    ff, c = start(tmp_path, ff, ("ff_send_piggies",))
    ff.writes = []
    r = c.post("/api/piggy-banks/1/money", json={"account_id": "ff:2", "amount": 10})
    # shown here in dollars, kept there in euros: the amounts cannot be sent back as they are shown, so nothing is sent at all
    assert r.status_code == 422 and "EUR" in r.json()["detail"] and not ff.writes


def test_f5_one_empty_account_list_from_firefly_does_not_wipe_what_coinpile_keeps(tmp_path):
    ff, c = start(tmp_path)
    c.put("/api/accounts/ff:3", json={"savings_kind": "cd", "apy": 4.5, "maturity": "2027-03-01", "nickname": "House fund", "min_balance": 100})
    keep = ff.data["1"]["accounts"]
    ff.data["1"]["accounts"] = []                       # Firefly III answers 200 with no accounts once (a restore, a cache or proxy hiccup)
    r = c.post("/api/refresh")
    assert r.status_code == 502 and "no accounts" in r.json()["detail"]         # said plainly, and nothing is deleted
    assert next(a for a in c.get("/api/accounts").json() if a["id"] == "ff:3")["nickname"] == "House fund"
    ff.data["1"]["accounts"] = keep
    c.post("/api/refresh?full=true")
    a3 = next(a for a in c.get("/api/accounts").json() if a["id"] == "ff:3")
    got = {k: a3[k] for k in ("sv_kind", "apy", "maturity", "nickname", "min_balance")}
    print(got)
    assert got == {"sv_kind": "cd", "apy": 4.5, "maturity": "2027-03-01", "nickname": "House fund", "min_balance": 100}, f"the CD details and nickname are gone: {got}"


def test_f6_an_inactive_bill_does_not_hide_an_active_recurring_payment_of_the_same_name(tmp_path):
    ff = world()
    ff.data["1"]["bills"].append({"type": "bills", "id": "9", "attributes": {"name": "Spotify", "amount_min": "11.99", "amount_max": "11.99", "repeat_freq": "monthly", "skip": 0,
                                  "date": "2025-01-05T00:00:00+00:00", "pay_dates": [], "active": False, "currency_code": "USD"}})
    ff.data["1"]["recurrences"] = [{"type": "recurrences", "id": "5", "attributes": {"type": "withdrawal", "title": "Spotify", "first_date": "2026-06-05T00:00:00+00:00", "active": True,
                                    "repetitions": [{"type": "monthly", "skip": 0, "occurrences": ["2026-12-05T00:00:00+00:00"]}], "transactions": [{"amount": "11.99", "source_id": "1", "currency_code": "USD"}]}}]
    ff, c = start(tmp_path, ff)
    up = [i["name"] for i in c.get("/api/upcoming?days=30").json()["items"]]
    print([(s["name"], s["source"], s["active"]) for s in c.get("/api/subscriptions").json()["items"]], up)
    assert "Spotify" in up, "the Spotify payment on 5 Dec is in nobody's list: the switched-off bill of the same name hides the live recurring transaction"


def test_f7_plan_size_set_on_a_firefly_recurring_transaction_survives_a_refresh(tmp_path):
    ff = world()
    ff.data["1"]["recurrences"] = [{"type": "recurrences", "id": "5", "attributes": {"type": "withdrawal", "title": "Sofa instalment", "first_date": "2026-06-05T00:00:00+00:00", "active": True,
                                    "repetitions": [{"type": "monthly", "skip": 0, "occurrences": ["2026-12-05T00:00:00+00:00"]}], "transactions": [{"amount": "99", "source_id": "1", "currency_code": "USD"}]}}]
    ff, c = start(tmp_path, ff)
    s = next(x for x in c.get("/api/subscriptions").json()["items"] if x["name"] == "Sofa instalment")
    body = {"name": s["name"], "amount": s["amount"], "next_date": s["next_date"], "every": s["every"], "unit": s["unit"], "category": s["category"], "notes": s["notes"] or "", "active": True,
            "plan_total": 12, "account_id": "ff:5", "notify_days": 5}
    r = c.put(f"/api/subscriptions/{s['id']}", json=body)
    assert r.status_code == 200 and r.json()["plan_total"] == 12 and r.json()["account_id"] == "ff:5"
    c.post("/api/refresh")
    s = next(x for x in c.get("/api/subscriptions").json()["items"] if x["name"] == "Sofa instalment")
    print({k: s[k] for k in ("plan_total", "account_id", "notify_days")})
    assert (s["plan_total"], s["account_id"]) == (12, "ff:5"), f"after the next refresh plan size is {s['plan_total']} and 'paid from' is {s['account_id']}"


def test_f8_a_new_subscription_is_not_called_missing_before_its_first_payment(tmp_path):
    ff = world()
    ff.tx("1", 70, "withdrawal", 12, "COFFEE", 1, 94, "2026-11-09")
    ff, c = start(tmp_path, ff)
    assert c.post("/api/subscriptions", json={"name": "New gym", "amount": 45, "next_date": "2026-12-01", "cycle": "monthly", "account_id": "ff:1"}).status_code == 200
    al = [(a["severity"], a["title"]) for a in c.get("/api/alerts").json() if a["kind"] == "missed_payment"]
    print(al)
    assert not al, f"added today, first payment on 1 Dec, already: {al}"


def test_f9_a_cd_maturing_today_is_not_also_called_idle_and_two_cds_have_their_own_alerts():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO savings(name,kind,balance,apy,maturity) VALUES('CD A','cd',1000,4.0,'2026-11-10')")
    kinds = [(a["kind"], a["title"]) for a in ledger.alerts(db, T)]
    print(kinds)
    db2 = DB(":memory:")
    with db2.tx() as c:
        c.execute("INSERT INTO savings(name,kind,balance,apy,maturity) VALUES('CD A','cd',1000,4.0,'2026-11-20')")
        c.execute("INSERT INTO savings(name,kind,balance,apy,maturity) VALUES('CD B','cd',2000,4.0,'2026-11-20')")
    keys = [a["key"] for a in ledger.alerts(db2, T) if a["kind"] == "maturing"]
    print(keys)
    assert len(set(keys)) == 2, f"both CDs share the alert key {keys[0]!r}: marking one handled hides the other"
    assert [k for k, _ in kinds] == ["maturing"], f"a CD maturing today gets two alerts: {kinds}"


def test_f10_moving_money_to_a_closed_goal_changes_nothing(tmp_path):
    ff, c = start(tmp_path)
    g1 = c.post("/api/goals", json={"name": "A", "target": 500}).json()["id"]
    g2 = c.post("/api/goals", json={"name": "B", "target": 500}).json()["id"]
    c.post(f"/api/goals/{g1}/allocate", json={"amount": 300})
    c.post(f"/api/goals/{g2}/close", json={"status": "done"})
    r = c.post("/api/goals/move", json={"from_id": g1, "to_id": g2, "amount": 200})
    left = {g["id"]: g["allocated"] for g in c.get("/api/goals").json()["items"]}[g1]
    print(r.status_code, r.text, "A has", left)
    assert r.status_code == 409 and left == 300, f"the move was refused ({r.status_code}) but goal A went from 300 to {left}"


def test_f11_a_date_typed_without_dashes_is_refused_or_stored_as_a_date(tmp_path):
    ff, c = start(tmp_path)
    own = c.post("/api/accounts", json={"name": "Wallet", "balance": 50, "coinpile_only": True}).json()["id"]
    r = c.post("/api/transactions", json={"account_id": own, "date": "20261109", "name": "Lunch", "amount": -18, "category": "Food"})
    spent = {x["category"]: x["spent"] for x in c.get("/api/spending?start=2026-11-01&end=2026-11-30").json()}
    print(r.status_code, r.json().get("date"), spent)
    assert r.status_code == 422 or spent.get("Food") == 18, f"accepted with date {r.json().get('date')!r}, then missing from November spending: {spent}"


def test_f12_a_piggy_bank_cannot_hold_more_than_its_account(tmp_path):
    ff = world()
    ff.data["1"]["piggies"].append({"type": "piggy-banks", "id": "2", "attributes": {"name": "No target", "target_amount": None, "current_amount": "0", "active": True, "target_date": None,
                                                                                    "accounts": [{"account_id": "1", "name": "Checking", "current_amount": "0"}]}})
    ff, c = start(tmp_path, ff, ("ff_send_piggies",))
    r = c.post("/api/piggy-banks/2/money", json={"account_id": "ff:1", "amount": 50000})          # the checking account holds 1,200.50
    print(r.status_code, r.text[:120])
    assert r.status_code == 422, f"50,000 was set aside on an account that holds 1,200.50 (answer {r.status_code})"


def test_subscriptions_made_before_the_switch_was_on_can_be_sent_as_bills_later(tmp_path):
    ff, c = start(tmp_path)
    add = lambda name, every, unit: c.post("/api/subscriptions", json={"name": name, "amount": 12.5, "next_date": "2026-12-01", "every": every, "unit": unit}).json()   # noqa: E731
    gym, daily = add("Gym", 1, "month"), add("Coffee", 1, "day")
    assert not [b for b in ff.data["1"]["bills"] if b["attributes"]["name"] == "Gym"]          # the switch was off: Coinpile only
    assert c.post("/api/firefly/sync-subscriptions").status_code == 409                          # nothing goes without the switch
    assert c.put("/api/settings", json={"ff_send_bills": "on"}).status_code == 200
    r = c.post("/api/firefly/sync-subscriptions").json()
    assert r["sent"] == 1 and r["names"] == ["Gym"] and r["daily"] == ["Coffee"] and not r["failed"]
    gyms = [b for b in ff.data["1"]["bills"] if b["attributes"]["name"] == "Gym"]
    assert len(gyms) == 1 and float(gyms[0]["attributes"]["amount_min"]) == 12.5
    assert c.post("/api/firefly/sync-subscriptions").json()["sent"] == 0                         # pressing it again makes no second bill
    assert len([b for b in ff.data["1"]["bills"] if b["attributes"]["name"] == "Gym"]) == 1
    assert len([s for s in c.get("/api/subscriptions").json()["items"] if s["name"] == "Gym"]) == 1   # and Coinpile shows it once
