"""The Piggy banks page, subscriptions with a set number of payments, missing-subscription alerts and notes."""
import json
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


# ---------------------------------------------------------------- read from Firefly III instead of typed into Coinpile
def rec(rid, type_, title, amount, first, occ, unit="weekly", skip=0, n=None, until=None, src="1", dst="92"):
    return {"type": "recurrences", "id": str(rid), "attributes": {"type": type_, "title": title, "first_date": first + "T00:00:00+00:00", "nr_of_repetitions": n,
            "repeat_until": until, "active": True, "repetitions": [{"type": unit, "moment": "5", "skip": skip, "occurrences": [o + "T00:00:00+00:00" for o in occ]}],
            "transactions": [{"amount": str(amount), "currency_code": "USD", "source_id": src, "destination_id": dst, "category_name": "Shopping" if type_ == "withdrawal" else None}]}}


def test_recurring_transactions_in_firefly_become_subscriptions_and_income(tmp_path):
    ff = world()
    ff.data["1"]["recurrences"] = [
        rec(1, "withdrawal", "PayPal Pay in 4: headphones", 37.5, "2026-11-06", ["2026-11-20", "2026-12-04", "2026-12-18", "2027-01-01"], skip=1, n=4),     # Firefly III's own "4 times"
        rec(2, "deposit", "ACME paycheck", 2500, "2026-10-01", ["2026-12-01"], unit="monthly", src="90", dst="1"),
        rec(3, "withdrawal", "Rent", 1500, "2026-01-01", ["2026-12-01"], unit="monthly"),                                                              # also a bill: the bill wins
    ]
    c = signed_in(app(tmp_path, ff))
    subs = {s["name"]: s for s in c.get("/api/subscriptions").json()["items"]}
    p4 = subs["PayPal Pay in 4: headphones"]
    assert p4["source"] == "ff-recurring" and p4["label"] == "biweekly" and p4["account_id"] == "ff:1" and p4["category"] == "Shopping"
    assert p4["end_date"] == "2026-12-18" and p4["plan_total"] == 4 and p4["payments_left"] == 3 and p4["payment_no"] == 2
    assert [s["source"] for s in subs.values() if s["name"] == "Rent"] == ["firefly"]
    inc = next(i for i in c.get("/api/incomes").json()["items"] if i["name"] == "ACME paycheck")
    assert inc["source"] == "firefly" and inc["amount"] == 2500 and inc["account_id"] == "ff:1"
    assert c.delete(f"/api/incomes/{inc['id']}").status_code == 409                                                  # changed in Firefly III, not here
    assert c.delete(f"/api/subscriptions/{p4['id']}").status_code == 409
    # your own details stay in Coinpile, on Firefly III's own items too, and survive the next read
    body = {**{k: p4[k] for k in ("name", "amount", "next_date", "end_date", "notes", "every", "unit", "active")}, "category": "Shopping", "payer": "other", "payer_name": "Sam", "notify_days": 7}
    r = c.put(f"/api/subscriptions/{p4['id']}", json=body)
    assert r.status_code == 200 and r.json()["payer_name"] == "Sam" and r.json()["notify_days"] == 7
    assert c.put(f"/api/subscriptions/{p4['id']}", json={**body, "amount": 40}).status_code == 409                    # Firefly III's side is not changed from here
    ff.data["1"]["recurrences"] = []
    c.post("/api/refresh")
    assert "PayPal Pay in 4: headphones" not in {s["name"] for s in c.get("/api/subscriptions").json()["items"]}       # removed in Firefly III: gone here
    assert not any(i["source"] == "firefly" for i in c.get("/api/incomes").json()["items"])
    assert not [w for w in getattr(ff, "writes", [])]                                                                # and nothing was ever written


def test_details_on_a_firefly_bill_are_kept_here_without_writing_to_firefly(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    rent = next(s for s in c.get("/api/subscriptions").json()["items"] if s["name"] == "Rent")
    body = {**{k: rent[k] for k in ("name", "amount", "next_date", "end_date", "notes", "every", "unit", "active")}, "category": "Subscriptions", "account_id": "ff:1", "notify_days": 5}
    assert c.put(f"/api/subscriptions/{rent['id']}", json=body).status_code == 200
    c.post("/api/refresh")
    rent = next(s for s in c.get("/api/subscriptions").json()["items"] if s["name"] == "Rent")
    assert rent["account_id"] == "ff:1" and rent["notify_days"] == 5
    assert c.put(f"/api/subscriptions/{rent['id']}", json={**body, "active": False}).status_code == 409
    assert not getattr(ff, "writes", [])


def test_firefly_budgets_show_their_limit_and_spending(tmp_path):
    ff = world()
    ff.data["1"]["budgets"] = [{"type": "budgets", "id": "1", "attributes": {"name": "Groceries", "active": True, "spent": [{"sum": "-82.40", "currency_code": "USD"}]}},
                               {"type": "budgets", "id": "2", "attributes": {"name": "Fun", "active": True, "spent": [], "auto_budget_type": "reset", "auto_budget_amount": "100"}},
                               {"type": "budgets", "id": "3", "attributes": {"name": "Old", "active": False, "spent": []}}]
    ff.data["1"]["budget-limits"] = [{"type": "budget_limits", "id": "9", "attributes": {"budget_id": "1", "amount": "400", "start": "2026-11-01T00:00:00+00:00"}}]
    c = signed_in(app(tmp_path, ff))
    b = c.get("/api/firefly/budgets?month=2026-11").json()
    assert [x["name"] for x in b["items"]] == ["Groceries", "Fun"]
    g = b["items"][0]
    assert g["limit"] == 400 and g["spent"] == 82.4 and g["left"] == 317.6 and b["items"][1]["limit"] == 100 and b["items"][1]["auto"] == "reset"
    assert b["limit"] == 500 and b["link"].endswith("/budgets")
    assert c.get("/api/firefly/budgets?month=nope").status_code == 422


def test_a_payment_firefly_linked_to_the_bill_counts_as_paid(tmp_path):
    ff = world()
    for i, dt in enumerate(("2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01")):
        ff.tx("1", 60 + i, "withdrawal", 1500, "ONLINE PMT 88213 LANDLORD LLC", 1, 95, dt)                            # the bank's wording, nothing like "Rent"
        ff.data["1"]["groups"][-1]["attributes"]["transactions"][0]["bill_id"] = "1"
    ff.tx("1", 70, "withdrawal", 20, "SHELL OIL", 1, 94, "2026-11-09")
    c = signed_in(app(tmp_path, ff))
    assert not [a for a in c.get("/api/alerts").json() if a["kind"] == "missed_payment"]
    ff.data["1"]["groups"] = [g for g in ff.data["1"]["groups"] if g["id"] != "63"]                                     # November's payment never came
    c.post("/api/refresh?full=true")
    miss = [a for a in c.get("/api/alerts").json() if a["kind"] == "missed_payment"]
    assert len(miss) == 1 and "Rent" in miss[0]["title"] and "2026-11-01" in miss[0]["detail"]


# ---------------------------------------------------------------- CDs are accounts; goals are piggy banks; notes go with them
def test_an_account_can_be_a_cd_and_keeps_that_through_refreshes(tmp_path):
    c = signed_in(app(tmp_path, world()))
    assert c.put("/api/accounts/ff:3", json={"savings_kind": "cd", "apy": 4.5, "opened": "2026-05-01", "maturity": "2027-05-01", "term_months": 12}).status_code == 200
    c.post("/api/refresh?full=true")
    p = next(x for x in c.get("/api/goals").json()["places"] if x["account_id"] == "ff:3")
    assert p["kind"] == "cd" and p["apy"] == 4.5 and p["maturity"] == "2027-05-01" and p["term_months"] == 12 and p["value_at_maturity"] > 5000
    assert [m["name"] for m in c.get("/api/maturities").json()] == [p["name"]]
    # any cash account can be counted and named, the checking one included
    assert c.put("/api/accounts/ff:1", json={"savings_kind": "money_market"}).status_code == 200
    assert any(x["account_id"] == "ff:1" and x["kind"] == "money_market" for x in c.get("/api/goals").json()["places"])
    assert c.put("/api/accounts/ff:1", json={"savings_mode": "no", "savings_kind": ""}).status_code == 200
    assert not any(x["account_id"] == "ff:1" for x in c.get("/api/goals").json()["places"])
    assert c.put("/api/accounts/ff:3", json={"savings_kind": "jar"}).status_code == 422
    assert c.put("/api/accounts/ff:3", json={"maturity": "soon"}).status_code == 422


def test_piggy_banks_are_the_goals_with_their_notes_for_the_agent(tmp_path):
    from devtools.fake_llm import openai_server
    ff = world()                                   # Vacation: 400 of 2000 by 2027-06-01
    ff.data["1"]["piggies"][0]["attributes"]["save_per_month"] = "228.57"
    log, script = [], [[("list_goals", {}), ("write_note", {"title": "Vacation plan", "body": "- [ ] book flights", "piggy_id": "1"})]]
    a = __import__("coinpile.web", fromlist=["x"]).create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), llm_transport=openai_server(script, log), today=lambda: TODAY)
    c = signed_in(a)
    n = c.post("/api/notes", json={"title": "Where to go", "body": "Maine or Oregon", "piggy_id": "1"}).json()
    assert n["piggy_id"] == "1" and c.post("/api/notes", json={"title": "x", "piggy_id": "99"}).status_code == 404
    over = c.get("/api/piggy-banks/overview").json()["items"][0]
    assert [x["title"] for x in over["my_notes"]] == ["Where to go"]
    pl = c.get("/api/plan?month=2026-11").json()
    assert pl["goals_month"] == 228.57                                                                        # Firefly III's own monthly amount for the piggy bank
    assert c.get("/api/goals").json()["needed"] == 1600
    c.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True})
    out = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "how is my vacation fund?"}]}).json()
    got = {t["tool"]: json.loads(m["content"]) for t, m in zip(out["trace"], [m for m in log[1]["body"]["messages"] if m["role"] == "tool"])}
    g = got["list_goals"]["goals_are_piggy_banks"][0]
    assert g["name"] == "Vacation" and g["still_needed"] == 1600 and [x["title"] for x in g["my_notes"]] == ["Where to go"]
    assert got["write_note"]["piggy_id"] == "1" and {x["title"] for x in c.get("/api/piggy-banks/overview").json()["items"][0]["my_notes"]} == {"Where to go", "Vacation plan"}
    assert "add_goal" not in {t["name"] for t in c.get("/api/agent/tools").json()}                         # goals are made in Firefly III now
    chat = c.post("/api/chats", json={"title": "About Vacation", "links": [{"kind": "piggy", "ref_id": 1}]}).json()
    assert chat["links"] == [{"kind": "piggy", "ref_id": 1, "title": "Vacation"}]
