"""The Piggy banks page, subscriptions with a set number of payments, missing-subscription alerts and notes."""
from datetime import date

from coinpile import ledger
from coinpile.db import DB
from tests.test_coinpile import TODAY, app, signed_in, world


# ---------------------------------------------------------------- piggy banks
def test_the_piggy_bank_page_reads_firefly_and_changes_nothing_until_switched_on(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    v = c.get("/api/piggy-banks/overview").json()
    assert [p["name"] for p in v["items"]] == ["Vacation"] and v["total"] == 400 and v["targets"] == 2000 and v["still_needed"] == 1600
    assert v["items"][0]["left"] == 1600 and v["items"][0]["link"].endswith("/piggy-banks/show/1") and v["can_change"] is False
    assert [a["id"] for a in v["accounts"]] and all(a["id"].startswith("ff:") for a in v["accounts"])
    writes = len(getattr(ff, "writes", []))
    assert c.post("/api/piggy-banks", json={"name": "Tires", "account_id": "ff:3"}).status_code == 409
    assert c.put("/api/piggy-banks/1", json={"name": "Trip"}).status_code == 409
    assert c.post("/api/piggy-banks/1/money", json={"account_id": "ff:3", "amount": 50}).status_code == 409
    assert len(getattr(ff, "writes", [])) == writes                                                       # nothing was sent


def test_with_the_switch_on_piggy_banks_are_made_edited_and_filled_in_firefly(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    c.put("/api/settings", json={"ff_send_piggies": "on"})
    assert c.post("/api/piggy-banks", json={"name": "Tires", "account_id": "ff:99"}).status_code == 422         # not one of their accounts
    assert c.post("/api/piggy-banks", json={"name": "Tires", "account_id": "ff:7"}).status_code == 422          # the car loan is not where money is kept
    r = c.post("/api/piggy-banks", json={"name": "Tires", "account_id": "ff:3", "target": 800, "target_date": "2027-03-01", "start_amount": 100, "notes": "winter set"})
    assert r.status_code == 200, r.text
    made = next(p for p in ff.data["1"]["piggies"] if p["attributes"]["name"] == "Tires")
    assert made["attributes"]["accounts"][0]["current_amount"] == "100.00" and made["attributes"]["target_amount"] == "800.00"
    v = c.get("/api/piggy-banks/overview").json()
    assert {p["name"] for p in v["items"]} == {"Vacation", "Tires"} and v["total"] == 500                       # read back at once
    assert c.put("/api/piggy-banks/1", json={"name": "Trip to Maine", "target": 2500}).status_code == 200
    assert ff.data["1"]["piggies"][0]["attributes"]["name"] == "Trip to Maine" and ff.data["1"]["piggies"][0]["attributes"]["target_amount"] == "2500.00"
    r = c.post("/api/piggy-banks/1/money", json={"account_id": "ff:3", "amount": 150})
    assert r.status_code == 200 and r.json()["current"] == 550
    assert c.post("/api/piggy-banks/1/money", json={"account_id": "ff:3", "amount": -600}).status_code == 422    # cannot take out more than is in it
    assert c.post("/api/piggy-banks/1/money", json={"account_id": "ff:3", "amount": 5000}).status_code == 422    # nor overfill it
    ev = c.get("/api/piggy-banks/1/events").json()
    assert ev and ev[-1]["amount"] == 150
    ff.piggy_update_breaks = True                                                                                # Firefly III 6.7.9's own bug
    r = c.post("/api/piggy-banks/1/money", json={"account_id": "ff:3", "amount": 10})
    assert r.status_code == 502 and "in Firefly III itself" in r.json()["detail"]
    assert c.put("/api/piggy-banks/1", json={"notes": "still works"}).status_code == 200                         # names, targets and notes still change


def test_a_goals_piggy_bank_is_left_to_the_goal(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    c.put("/api/settings", json={"ff_send_goals": "on", "ff_send_piggies": "on"})
    g = c.post("/api/goals", json={"name": "Roof", "target": 3000}).json()
    c.post(f"/api/goals/{g['id']}/allocate", json={"place": "a:ff:3", "amount": 1000})
    c.post("/api/refresh")
    roof = next(p for p in c.get("/api/piggy-banks/overview").json()["items"] if p["name"] == "Roof")
    assert roof["goal"] == "Roof"
    r = c.post(f"/api/piggy-banks/{roof['id']}/money", json={"account_id": "ff:3", "amount": 5})
    assert r.status_code == 409 and "Savings" in r.json()["detail"]


# ---------------------------------------------------------------- subscriptions with an end
def test_pay_in_4_counts_down_and_is_kept_when_finished(tmp_path):
    c = signed_in(app(tmp_path, world()))
    s = c.post("/api/subscriptions", json={"name": "PayPal Pay in 4: headphones", "amount": 37.5, "next_date": "2026-11-20", "cycle": "biweekly", "times_left": 3, "plan_total": 4}).json()
    assert s["end_date"] == "2026-12-18" and s["payments_left"] == 3 and s["last_payment"] == "2026-12-18" and s["payment_no"] == 2
    up = c.get("/api/upcoming?days=90").json()["items"]
    assert [i["date"] for i in up if i["name"].startswith("PayPal")] == ["2026-11-20", "2026-12-04", "2026-12-18"]       # three, then gone
    db = DB(":memory:")
    later = ledger.sub_view({**s, "active": 1}, date(2026, 12, 10))
    assert later["payments_left"] == 1 and later["payment_no"] == 4
    done = ledger.sub_view({**s, "active": 1}, date(2027, 1, 5))
    assert done["ended"] and done["payments_left"] == 0 and done["last_payment"] == "2026-12-18" and done["monthly"] == 0
    assert any(x["id"] == s["id"] for x in c.get("/api/subscriptions").json()["items"])                            # kept, to look back on
    assert c.post("/api/subscriptions", json={"name": "x", "amount": 1, "next_date": "2026-11-20", "cycle": "monthly", "times_left": 0}).status_code == 422
    db.conn.close()


def test_every_kind_of_repeat_is_understood():
    from coinpile import recur
    for cycle, n in (("daily", 1), ("weekly", 7), ("biweekly", 14), ("fourweekly", 28)):
        e, u = recur.resolve(None, None, cycle)
        assert (recur.shift(date(2026, 1, 1), e, u, 1) - date(2026, 1, 1)).days == n
    for cycle, months in (("monthly", 1), ("bimonthly", 2), ("quarterly", 3), ("semiannual", 6), ("yearly", 12), ("biennial", 24)):
        e, u = recur.resolve(None, None, cycle)
        d = recur.shift(date(2026, 1, 31), e, u, 1)
        assert (d.year - 2026) * 12 + d.month - 1 == months                                                       # the 31st clamps to the month's end


# ---------------------------------------------------------------- missing subscriptions
def test_a_subscription_that_did_not_show_up_is_called_out_even_without_an_account(tmp_path):
    ff = world()                                                     # NETFLIX.COM 15.99 on Aug 5, Sep 5, Oct 5
    ff.tx("1", 40, "withdrawal", 20, "SHELL OIL", 1, 94, "2026-11-09")      # transactions run past Nov 5, so a Nov 5 charge would have been seen
    c = signed_in(app(tmp_path, ff))
    s = c.post("/api/subscriptions", json={"name": "Netflix", "amount": 15.99, "next_date": "2026-08-05", "cycle": "monthly"}).json()
    miss = [a for a in c.get("/api/alerts").json() if a["kind"] == "missed_payment"]
    assert len(miss) == 1 and miss[0]["sub_id"] == s["id"] and miss[0]["title"].startswith("Missing subscription: Netflix (15.99)") and "2026-11-05" in miss[0]["detail"]
    # a name the bank never showed is not called missing for ever
    c.post("/api/subscriptions", json={"name": "Gym membership", "amount": 30, "next_date": "2026-08-01", "cycle": "monthly"})
    assert not any("Gym" in a["title"] for a in c.get("/api/alerts").json())
    # ended plans are not missing either
    c.put(f"/api/subscriptions/{s['id']}", json={**{k: s[k] for k in ("name", "amount", "next_date", "category")}, "cycle": "monthly", "end_date": "2026-10-05"})
    assert not [a for a in c.get("/api/alerts").json() if a["kind"] == "missed_payment"]


# ---------------------------------------------------------------- notes
def test_notes_have_tags_and_stay_in_order(tmp_path):
    c = signed_in(app(tmp_path, world()))
    a = c.post("/api/notes", json={"title": "Taxes", "body": "- [ ] W-2\n- [x] 1099", "tags": " Taxes, house ,taxes"}).json()
    assert a["tags"] == "taxes,house"
    c.post("/api/notes", json={"title": "Pinned", "pinned": True})
    assert [n["title"] for n in c.get("/api/notes").json()] == ["Pinned", "Taxes"]
    assert c.put(f"/api/notes/{a['id']}", json={"title": " ", "body": ""}).status_code == 422
    assert c.put(f"/api/notes/{a['id']}", json={"title": "Taxes 2026", "body": "- [x] W-2", "tags": ""}).json()["tags"] == ""
