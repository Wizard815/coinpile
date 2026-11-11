"""Correctness on real-world Firefly III data: several currencies, splits, transfers to cards and loans, missing fields and the subscription catcher."""


from coinpile import firefly, ledger
from coinpile.db import DB
from tests.fake_firefly import FakeFirefly, add_history
from tests.test_coinpile import ALICE, TODAY, app, signed_in, world

URL = "https://firefly.example.com"


def alone():
    return FakeFirefly({ALICE: {"id": "1", "email": "alice@example.com"}})


def read(ff, db=None, full=True):
    db = db or DB(":memory:")
    firefly.refresh(db, firefly.Firefly(URL, ALICE, ff.transport()), TODAY, full=full)
    return db


def split(ff, gid, legs, date_="2026-10-12"):
    """One Firefly III group with several journals. legs: (journal id, type, amount, description, source, destination, category)."""
    ff.data["1"]["groups"].append({"type": "transactions", "id": str(gid), "attributes": {"group_title": "Split", "transactions": [
        {"transaction_journal_id": str(j), "type": t, "amount": f"{a:.12f}", "description": d, "date": date_ + "T00:00:00+00:00", "source_id": str(s), "destination_id": str(dst),
         "source_name": "S", "destination_name": "D", "category_name": c, "currency_code": "USD", "notes": None} for j, t, a, d, s, dst, c in legs]}})


# ---------------------------------------------------------------- several currencies
def eur_world():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="1000", primary_currency_code="USD", pc_current_balance="1000")
    ff.acct("1", 2, "Sparkasse - Girokonto", balance="1000", currency_code="EUR", primary_currency_code="USD", pc_current_balance="1080")
    ff.acct("1", 3, "Euro loan", type="liabilities", role=None, balance="-500", debt_amount="500", pc_debt_amount="540", currency_code="EUR", primary_currency_code="USD",
            liability_type="loan")
    ff.tx("1", 1, "withdrawal", 50, "REWE", 2, 90, "2026-10-03", "Groceries")
    ff.data["1"]["groups"][-1]["attributes"]["transactions"][0].update(currency_code="EUR", pc_amount="54.000000000000", primary_currency_code="USD")
    ff.tx("1", 2, "withdrawal", 20, "KROGER", 1, 91, "2026-10-04", "Groceries")
    ff.tx("1", 3, "transfer", 108, "To Germany", 1, 2, "2026-10-05")           # USD out, EUR in: Firefly III keeps the EUR side as the foreign amount
    ff.data["1"]["groups"][-1]["attributes"]["transactions"][0].update(foreign_currency_code="EUR", foreign_amount="100.000000000000", pc_amount="108.000000000000")
    ff.data["1"]["bills"].append({"type": "bills", "id": "1", "attributes": {"name": "Gym (Berlin)", "amount_avg": "30", "pc_amount_avg": "32.40", "repeat_freq": "monthly", "skip": 0,
                                                                             "date": "2026-01-15T00:00:00+00:00", "pay_dates": [], "active": True, "currency_code": "EUR", "primary_currency_code": "USD"}})
    add_history(ff, "1", 2, {"2026-10-01": "900", "2026-10-05": "1000"})
    ff.history_pc = {"2": {"2026-10-01": "972", "2026-10-05": "1080"}}
    return ff


def test_a_eur_account_and_a_usd_account_add_up_in_the_primary_currency():
    db = read(eur_world())
    a = {r["id"]: r for r in db.all("SELECT * FROM accounts")}
    assert (a["ff:2"]["balance"], a["ff:2"]["currency"], a["ff:2"]["native_balance"], a["ff:2"]["native_currency"]) == (1080, "USD", 1000, "EUR")
    assert (a["ff:1"]["balance"], a["ff:1"]["native_currency"]) == (1000, None)
    assert (a["ff:3"]["balance"], a["ff:3"]["native_balance"]) == (540, 500)
    assert ledger.net_worth(db) == 1000 + 1080 - 540
    t = {r["id"]: r for r in db.all("SELECT * FROM transactions")}
    assert (t["ff:1"]["amount"], t["ff:1"]["native_amount"], t["ff:1"]["native_currency"]) == (-54, -50, "EUR")
    assert (t["ff:3:out"]["amount"], t["ff:3:out"]["native_currency"]) == (-108, None)
    assert (t["ff:3:in"]["amount"], t["ff:3:in"]["native_amount"], t["ff:3:in"]["native_currency"]) == (108, 100, "EUR")   # the EUR side gets its EUR amount
    assert {r["category"]: r["spent"] for r in ledger.spending_by_category(db)} == {"Groceries": 74}
    s = db.one("SELECT * FROM subscriptions")
    assert (s["amount"], s["currency"], s["native_amount"], s["native_currency"]) == (32.4, "USD", 30, "EUR")


def test_a_eur_accounts_history_is_in_the_primary_currency():
    ff = eur_world()
    orig = ff.transport()

    import httpx

    def h(req):
        r = orig.handle_request(req)
        if req.url.path.endswith("/chart/account/overview") and req.url.params.get("accounts[]") == "2":
            body = r.read()
            import json
            sets = json.loads(body)
            sets[0]["currency_code"], sets[0]["pc_entries"] = "EUR", {k + "T00:00:00+00:00": v for k, v in ff.history_pc["2"].items()}
            return httpx.Response(200, json=sets)
        return r
    db = DB(":memory:")
    firefly.refresh(db, firefly.Firefly(URL, ALICE, httpx.MockTransport(h)), TODAY, full=True)
    pts = [r["balance"] for r in db.all("SELECT balance FROM snapshots WHERE account_id='ff:2' AND ts LIKE '%T23:59:59Z' ORDER BY ts")]
    assert pts == [972, 1080]


def test_the_primary_currency_is_shown(tmp_path):
    ff = eur_world()
    for a in ff.data["1"]["accounts"]:
        a["attributes"]["primary_currency_code"] = "EUR"
    ff.data["1"]["accounts"][0]["attributes"].update(currency_code="USD", pc_current_balance="925.93")
    c = signed_in(app(tmp_path, ff))
    assert c.get("/api/status").json()["currency"] == "EUR" and c.get("/api/overview").json()["currency"] == "EUR"
    acc = {a["id"]: a for a in c.get("/api/accounts").json()}
    assert (acc["ff:1"]["balance"], acc["ff:1"]["native_currency"], acc["ff:2"]["balance"], acc["ff:2"]["native_currency"]) == (925.93, "USD", 1000, None)


def test_an_account_left_out_of_net_worth_in_firefly_is_left_out_here():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="1000")
    ff.acct("1", 2, "Kids' account", balance="400", include_net_worth=False)
    assert ledger.net_worth(read(ff)) == 1000


# ---------------------------------------------------------------- splits, cards and loans
def card_world():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="2000")
    ff.acct("1", 5, "Visa", role="ccAsset", balance="-300")
    ff.acct("1", 7, "Car loan", type="liabilities", role=None, balance="0", debt_amount="9000", liability_type="loan")
    ff.tx("1", 1, "withdrawal", 120, "AMAZON", 5, 90, "2026-10-02", "Shopping")              # bought on the card
    ff.tx("1", 2, "transfer", 120, "Visa payment", 1, 5, "2026-10-20", "Credit card")        # paid off from checking, with a category
    ff.tx("1", 3, "withdrawal", 350, "Car payment", 1, 7, "2026-10-21", "Car")               # paying the loan: Firefly III counts it as an expense
    ff.tx("1", 4, "withdrawal", 200, "Loan payment", 1, 7, "2026-10-22")                     # without a category
    return ff


def test_paying_a_card_or_a_loan_is_never_counted_twice():
    db = read(card_world())
    t = {r["id"]: r for r in db.all("SELECT * FROM transactions")}
    assert (t["ff:2:out"]["amount"], t["ff:2:in"]["amount"], t["ff:2:out"]["category"], t["ff:2:in"]["category"]) == (-120, 120, "Transfer", "Transfer")
    assert (t["ff:3:out"]["category"], t["ff:3:in"]["category"], t["ff:3:in"]["account_id"], t["ff:3:in"]["amount"]) == ("Car", "Transfer", "ff:7", 350)
    assert (t["ff:4:out"]["category"], t["ff:4:in"]["category"]) == ("Transfer", "Transfer")
    assert {r["category"]: r["spent"] for r in ledger.spending_by_category(db)} == {"Shopping": 120, "Car": 350}
    q = ledger.query_transactions(db)
    assert q["real_in"] == 0 and q["real_out"] == -470                     # nothing arriving on the card or the loan is income


def test_a_transfer_to_a_closed_account_is_not_spending():
    ff = card_world()
    ff.acct("1", 8, "Old savings", role="savingAsset", balance="0", active=False)
    ff.tx("1", 9, "transfer", 500, "To old savings", 1, 8, "2026-10-25", "Savings goal")
    ff.tx("1", 10, "transfer", 80, "From old savings", 8, 1, "2026-10-26")
    db = read(ff)
    t = {r["id"]: r for r in db.all("SELECT * FROM transactions")}
    assert (t["ff:9"]["amount"], t["ff:9"]["category"], t["ff:10"]["amount"], t["ff:10"]["category"]) == (-500, "Transfer", 80, "Transfer")
    assert "Savings goal" not in {r["category"] for r in ledger.spending_by_category(db)}


def test_a_split_withdrawal_is_one_row_per_split_and_rereading_is_stable():
    ff = card_world()
    split(ff, 40, [(41, "withdrawal", 30, "Groceries part", 1, 90, "Groceries"), (42, "withdrawal", 20, "Household part", 1, 90, "Shopping"),
                   (43, "transfer", 10, "Pocket money", 1, 5, None)])
    db = read(ff)
    db = read(ff, db)
    rows = db.all("SELECT * FROM transactions WHERE ff_group='40' ORDER BY id")
    assert [(r["id"], r["amount"], r["category"]) for r in rows] == [("ff:41", -30, "Groceries"), ("ff:42", -20, "Shopping"), ("ff:43:in", 10, "Transfer"), ("ff:43:out", -10, "Transfer")]
    spent = {r["category"]: r["spent"] for r in ledger.spending_by_category(db, "2026-10-12", "2026-10-12")}
    assert spent == {"Groceries": 30, "Shopping": 20}
    del ff.data["1"]["groups"][-1]["attributes"]["transactions"][1:]                          # the other splits are removed in Firefly III
    read(ff, db, full=False)
    assert [r["id"] for r in db.all("SELECT id FROM transactions WHERE ff_group='40'")] == ["ff:41"]


def test_a_repeating_transfer_with_a_category_is_not_a_subscription():
    ff = card_world()
    ff.acct("1", 3, "Savings", role="savingAsset", balance="100")
    for i, d in enumerate(("2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01")):
        ff.tx("1", 60 + i, "transfer", 300, "Monthly savings", 1, 3, d, "Savings")
        ff.tx("1", 70 + i, "transfer", 120, "Visa autopay", 1, 5, d, "Credit card")
    db = read(ff)
    assert not [s for s in ledger.suggestions(db, TODAY) if s["source"] == "detected"]


# ---------------------------------------------------------------- missing and odd Firefly III data
def test_missing_and_odd_firefly_data_never_breaks_a_page(tmp_path):
    ff = world()
    d = ff.data["1"]
    ff.acct("1", 20, None, balance=None)
    d["accounts"][-1]["attributes"].pop("currency_code")
    ff.acct("1", 22, "Mortgage", type="liabilities", role=None, balance=None, debt_amount=None, liability_type="mortgage")
    ff.tx("1", 60, "withdrawal", 5, "", 1, 95, "2026-10-20")
    d["groups"][-1]["attributes"]["transactions"][0].update(description=None, destination_name=None, amount=None, currency_code=None)
    d["bills"].append({"type": "bills", "id": "2", "attributes": {"name": "Old gym", "amount_min": "25", "amount_max": "25", "amount_avg": None, "repeat_freq": "weekly", "skip": None,
                                                                  "date": "2025-01-01T00:00:00+00:00", "end_date": "2025-06-01T00:00:00+00:00", "pay_dates": None, "active": True,
                                                                  "currency_code": None, "notes": None}})
    d["bills"].append({"type": "bills", "id": "3", "attributes": {"name": None, "date": None}})
    d["bills"].append({"type": "bills", "id": "4", "attributes": {"name": "No date", "date": None, "amount_avg": "9", "repeat_freq": None}})
    d["piggies"].append({"type": "piggy-banks", "id": "2", "attributes": {"name": "Empty", "target_amount": None, "current_amount": None, "active": True, "target_date": None, "accounts": None}})
    d["piggies"].append({"type": "piggy-banks", "id": "3", "attributes": {"name": None, "accounts": [{"name": "no id"}]}})
    c = signed_in(app(tmp_path, ff))
    r = c.post("/api/refresh?full=true")
    assert r.status_code == 200, r.text
    acc = {a["id"]: a for a in c.get("/api/accounts").json()}
    assert acc["ff:20"]["name"] == "Account 20" and acc["ff:20"]["balance"] == 0 and acc["ff:22"]["balance"] == 0
    subs = {s["name"]: s for s in c.get("/api/subscriptions").json()["items"]}
    assert subs["Old gym"]["next_due"] is None and subs["Old gym"]["monthly"] == 0                  # ended: costs nothing a month any more
    assert c.get("/api/overview").json()["subscriptions_monthly"] == 1500 + subs["No date"]["monthly"]
    for p in ("/api/overview", "/api/transactions", "/api/accounts/ff:20", "/api/piggy-banks", "/api/forecast", "/api/plan", "/api/goals", "/api/upcoming?days=400",
              "/api/alerts", "/api/suggestions", "/api/summary", "/api/history", "/api/projection", "/api/spending", "/subscriptions.ics"):
        assert c.get(p).status_code == 200, p


def test_an_ended_bill_is_not_due_and_costs_nothing_a_month():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,end_date,active) VALUES('Old',20,'monthly',1,'month','2026-01-05','2026-06-05',1)")
    s = ledger.subscriptions(db, TODAY)[0]
    assert s["next_due"] is None and s["monthly"] == 0
    assert ledger.overview(db, 30, TODAY)["subscriptions_monthly"] == 0


# ---------------------------------------------------------------- the subscription catcher on Firefly III data
def catcher_world():
    ff = card_world()
    for i, (d, a) in enumerate((("2026-06-14", 54.10), ("2026-07-14", 61.75), ("2026-08-13", 58.20), ("2026-09-14", 49.80), ("2026-10-14", 57.00))):
        ff.tx("1", 100 + i, "withdrawal", a, "VERIZON WIRELESS", 1, 96, d, "Bills")
    for i, d in enumerate(("2026-07-03", "2026-08-03", "2026-09-03", "2026-10-03")):
        ff.tx("1", 110 + i, "withdrawal", 12.99, "SPOTIFY P1A2B3", 5, 97, d)
    return ff


def test_the_catcher_finds_a_monthly_charge_whose_amount_moves_a_little():
    db = read(catcher_world())
    found = {s["name"]: s for s in ledger.suggestions(db, TODAY) if s["source"] == "detected"}
    assert "Verizon Wireless" in found and found["Verizon Wireless"]["unit"] == "month" and found["Verizon Wireless"]["next_date"] == "2026-11-14"
    assert "Spotify P1A2B3" in found
    assert not any("Visa" in n or "Car" in n or "Loan" in n for n in found)


def test_a_firefly_bill_is_not_suggested_again_even_under_another_name(tmp_path):
    ff = catcher_world()
    ff.data["1"]["bills"].append({"type": "bills", "id": "9", "attributes": {"name": "Phone", "amount_avg": "55", "repeat_freq": "monthly", "skip": 0, "date": "2026-01-14T00:00:00+00:00",
                                                                             "pay_dates": ["2026-11-14T00:00:00+00:00"], "active": True, "currency_code": "USD"}})
    ff.data["1"]["bills"].append({"type": "bills", "id": "10", "attributes": {"name": "Spotify", "amount_avg": "12.99", "repeat_freq": "monthly", "skip": 0, "date": "2026-01-03T00:00:00+00:00",
                                                                              "pay_dates": [], "active": True, "currency_code": "USD"}})
    for g in ff.data["1"]["groups"]:
        s = g["attributes"]["transactions"][0]
        if s["description"] == "VERIZON WIRELESS":
            s.update(subscription_id="9", subscription_name="Phone", bill_id="9", bill_name="Phone")
    c = signed_in(app(tmp_path, ff))
    names = [s["name"] for s in c.get("/api/suggestions").json() if s.get("source") == "detected"]
    assert names == []
    scan = c.post("/api/subscriptions/scan", json={}).json()
    assert scan["found"] == []
