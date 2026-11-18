"""Numbers an audit found wrong: bills already paid, refunds, month ends, other currencies, piggy banks, paydays, CDs."""
from datetime import date

from coinpile import agent, firefly, investments, ledger, plan, recur
from coinpile.db import DB
from tests.audit_helpers import ALICE, URL, alone, read, world

T = date(2026, 11, 10)


def test_w1_a_bill_already_paid_this_period_is_not_due_again():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="500")                       # 2000 before the rent went out on the 8th
    ff.tx("1", 1, "withdrawal", 1500, "Rent November", 1, 91, "2026-11-08", "Housing")
    ff.data["1"]["groups"][-1]["attributes"]["transactions"][0]["bill_id"] = "1"
    ff.data["1"]["bills"].append({"type": "bills", "id": "1", "attributes": {
        "name": "Rent", "amount_min": "1500", "amount_max": "1500", "repeat_freq": "monthly", "skip": 0, "date": "2026-01-12T00:00:00+00:00",
        "pay_dates": ["2026-11-12T00:00:00+00:00", "2026-12-12T00:00:00+00:00"],
        "paid_dates": [{"transaction_group_id": "1", "transaction_journal_id": "1", "date": "2026-11-08T00:00:00+00:00"}],
        "next_expected_match": "2026-12-12T00:00:00+00:00", "active": True, "currency_code": "USD"}})
    db = read(ff, T)
    up = ledger.upcoming(db, 14, T)
    p = ledger.project_balance(db, 14, T, include_income=False)
    print("upcoming", [(i["date"], i["amount"]) for i in up["items"]], "projection lowest", p["lowest"])
    assert up["total"] == 0, f"November rent is paid, yet {up['total']} is listed as due in the next 14 days"
    assert p["lowest"] == 500, f"lowest balance {p['lowest']}: the rent already paid is taken off a second time"


def test_w2_a_category_first_seen_on_a_refund_still_counts_as_spending():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="1000")
    ff.tx("1", 1, "deposit", 40, "BESTBUY REFUND", 90, 1, "2026-11-01", "Electronics")
    db = read(ff, T)
    ff.tx("1", 2, "withdrawal", 500, "BESTBUY TV", 1, 91, "2026-11-05", "Electronics")
    read(ff, T, db)
    spent = {r["category"]: r["spent"] for r in ledger.spending_by_category(db, "2026-11-01", "2026-11-30")}
    print("kind", db.one("SELECT kind FROM categories WHERE name='Electronics'")["kind"], "spending", spent, "forecast", ledger.spending_forecast(db, T)["spent_so_far"])
    assert spent.get("Electronics") == 460, f"the 500 TV (less the 40 that came back) is missing from spending by category: {spent}"


def test_w3_a_refund_takes_the_purchase_back_out_of_spending():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="1000")
    ff.tx("1", 1, "withdrawal", 200, "ZARA", 1, 91, "2026-11-03", "Clothes")
    ff.tx("1", 2, "deposit", 200, "ZARA", 91, 1, "2026-11-06", "Clothes")
    db = read(ff, T)
    spent = {r["category"]: r["spent"] for r in ledger.spending_by_category(db, "2026-11-01", "2026-11-30")}
    daily = {x["date"]: x for x in ledger.daily_totals(db, T, 10)}
    print("spending", spent, "daily 11-06", daily["2026-11-06"])
    assert spent.get("Clothes", 0) == 0, f"bought for 200, returned for 200: spending says {spent}"
    assert daily["2026-11-06"]["income"] == 0, "the refund is reported as income for the day"


def test_w4_paycheck_plan_keeps_the_real_paydays_at_a_month_end():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO incomes(name,amount,every,unit,next_date,active) VALUES('Salary',3000,1,'month','2026-01-31',1)")
        c.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,active) VALUES('Car insurance',400,'monthly',1,'month','2026-01-29',1)")
    a = agent.paycheck_plan(db, date(2026, 2, 10))
    b = agent.paycheck_plan(db, date(2026, 3, 1))
    print("28 Feb paycheck covers until", a["covers_until"], [x["date"] for x in a["bills_due_before_next"]], "| 31 Mar paycheck:", [x["date"] for x in b["bills_due_before_next"]])
    assert a["covers_until"] == "2026-03-31", f"the paycheck after 28 Feb lands on 31 Mar, not {a['covers_until']}"
    covered = {x["date"] for x in a["bills_due_before_next"]} | {x["date"] for x in b["bills_due_before_next"]}
    assert "2026-03-29" in covered, "the insurance due 29 Mar is in no paycheck's plan"


def test_w5_a_cd_at_5_percent_apy_earns_5_percent_in_a_year():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO savings(name,kind,balance,apy,maturity) VALUES('CD','cd',10000,5.0,'2027-11-10')")       # 365 days from T
    v = ledger.savings_summary(db, T)["items"][0]
    print("yearly_interest", v["yearly_interest"], "value_at_maturity", v["value_at_maturity"])
    assert v["value_at_maturity"] == 10500.0, f"10,000 at 5.00% APY for 365 days is 10,500.00, not {v['value_at_maturity']}"


def test_w6_money_owed_to_you_is_not_cash_for_paying_bills():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="300")
    ff.acct("1", 2, "Loan to Bob", type="liabilities", role=None, balance="5000", debt_amount="5000", liability_type="loan", liability_direction="credit")      # "I am owed this debt"
    db = read(ff, T)
    with db.tx() as c:
        c.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,active) VALUES('Rent',1000,'monthly',1,'month','2026-11-15',1)")
    p = ledger.project_balance(db, 30, T, include_income=False)
    print("start", p["start"], "lowest", p["lowest"])
    assert p["start"] == 300 and p["lowest"] == -700, f"projection starts at {p['start']} and never goes below {p['lowest']}: Bob's debt is treated as money in the bank"


def test_w7_other_currencies_are_not_added_as_if_they_were_dollars():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="1000", primary_currency_code="USD")
    ff.acct("1", 2, "Yen account", balance="500000", currency_code="JPY", primary_currency_code="USD")      # Firefly III with "convert to primary currency" off: no pc_ values
    ff.tx("1", 1, "withdrawal", 12000, "Tokyo dinner", 2, 91, "2026-11-03", "Food")
    ff.data["1"]["groups"][-1]["attributes"]["transactions"][0]["currency_code"] = "JPY"
    ff.tx("1", 2, "withdrawal", 30, "Diner", 1, 92, "2026-11-04", "Food")
    db = read(ff, T)
    nw, spent = ledger.net_worth(db), ledger.spending_by_category(db, "2026-11-01", "2026-11-30")[0]["spent"]
    print("net worth", nw, "food", spent)
    assert nw == 1000.0, "net worth adds 500,000 yen to 1,000 dollars and calls it 501,000"
    assert spent == 30.0, "12,000 yen and 30 dollars are added up as 12,030"
    note = [a for a in ledger.alerts(db, T) if a["kind"] == "other_currency"]
    assert len(note) == 1 and "JPY" in note[0]["title"], "what is left out of the totals is said"


def test_w8_a_subscription_in_another_currency_is_not_summed_as_dollars(tmp_path):
    from tests.audit_helpers import client, make_app
    a, _ = make_app(tmp_path, world(), T)
    c = client(a)
    before = c.get("/api/subscriptions").json()["monthly_total"]
    r = c.post("/api/subscriptions", json={"name": "Tokyo gym", "amount": 9000, "currency": "JPY", "next_date": "2026-12-01", "cycle": "monthly"})
    assert r.status_code == 422 and "USD" in r.json()["detail"]                 # everything is added up in one currency: it is entered in that one
    after = c.get("/api/subscriptions").json()["monthly_total"]
    assert after == before, "9,000 yen a month is added to the dollar total as 9,000"


def test_w9_piggy_bank_money_is_still_set_aside_when_the_account_is_followed_by_a_savings_item(tmp_path):
    from tests.audit_helpers import client, make_app
    a, _ = make_app(tmp_path, world(), T)
    c = client(a)
    g0 = c.get("/api/goals").json()
    c.post("/api/savings", json={"name": "Axos HYSA", "kind": "savings", "apy": 4.2, "account_id": "ff:3"})
    g1 = c.get("/api/goals").json()
    print("free before", g0["free"], "in piggy", g0["in_piggy_banks"], "| after", g1["free"], "in piggy", g1["in_piggy_banks"])
    assert g1["free"] == g0["free"] == 4600.0, f"free savings went from {g0['free']} to {g1['free']}: the 400 in the Vacation piggy bank is counted as free"


def test_w10_two_paychecks_on_the_same_day_both_count():
    rows = []
    for i, dt in enumerate(["2026-08-14", "2026-08-28", "2026-09-11", "2026-09-25", "2026-10-09", "2026-10-23", "2026-11-06"]):
        rows += [{"id": f"a{i}", "account_id": "x", "date": dt, "name": "ACME PAYROLL", "amount": 1500.0}, {"id": f"b{i}", "account_id": "x", "date": dt, "name": "ACME PAYROLL", "amount": 1500.0}]
    found = recur.detect(rows, T)
    print([(r["amount"], r["label"], r["occurrences"]) for r in found])
    assert found and found[0]["amount"] == 3000.0, f"3,000 lands every two weeks; the detector (and the balance projection) uses {found[0]['amount'] if found else 'nothing'}"


def test_w11_investment_value_by_month_carries_a_month_without_a_reading():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO snapshots VALUES('2026-07-15T10:00:00+00:00','rh:main',1000)")
        c.execute("INSERT INTO snapshots VALUES('2026-10-02T10:00:00+00:00','rh:main',1300)")
    m = investments.monthly(db, ["rh:main"])
    print(m)
    assert [x["month"] for x in m] == ["2026-07", "2026-08", "2026-09", "2026-10"], "August and September are missing (the docstring says they carry the last known value)"


def test_w12_a_coinpile_subscription_and_the_same_firefly_bill_are_not_both_counted(tmp_path):
    from tests.audit_helpers import client, make_app
    ff = world()
    a, _ = make_app(tmp_path, ff, T)
    c = client(a)
    c.post("/api/subscriptions", json={"name": "Netflix", "amount": 15.99, "next_date": "2026-12-05", "cycle": "monthly"})
    ff.data["1"]["bills"].append({"type": "bills", "id": "2", "attributes": {"name": "Netflix", "amount_min": "15.99", "amount_max": "15.99", "repeat_freq": "monthly", "skip": 0,
                                                                             "date": "2026-01-05T00:00:00+00:00", "pay_dates": ["2026-12-05T00:00:00+00:00"], "active": True, "currency_code": "USD"}})
    c.post("/api/refresh")
    s = c.get("/api/subscriptions").json()
    n = [(x["name"], x["source"]) for x in s["items"] if x["name"] == "Netflix"]
    print(n, "monthly total", s["monthly_total"])
    assert s["monthly_total"] == 1515.99, f"Netflix is counted twice: monthly total {s['monthly_total']} (rent 1500 + 15.99 expected)"


# ---------------------------------------------------------------- an end date at the end of time
def test_a_subscription_that_never_ends_is_listed_at_once():
    import time
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,end_date,active) VALUES('Gym',30,'monthly',1,'month','2026-12-01','9999-12-31',1)")
        c.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,end_date,active) VALUES('Paper',2,'daily',1,'day','2026-11-01','9999-12-31',1)")
    t = time.time()
    subs = ledger.subscriptions(db, T)
    assert time.time() - t < 1 and [s["next_due"] for s in subs if s["name"] == "Gym"] == ["2026-12-01"]
    assert ledger.upcoming(db, 30, T)["count"] > 0 and ledger.alerts(db, T) is not None
    assert len(recur.occurrences(date(2026, 1, 1), 1, "day", date(2026, 1, 1), date.max)) == recur.MAX_DATES          # a range nobody can read is cut off
    assert recur.occurrences(date(9999, 11, 30), 1, "month", date(9999, 11, 1), date.max, date.max)[-1] == date(9999, 12, 30)


def test_payments_left_are_counted_without_listing_them():
    n, last = recur.count_until(date(2026, 12, 1), 1, "month", T, date(2027, 2, 28))
    assert (n, last) == (3, date(2027, 2, 1))
    assert recur.count_until(date(2026, 12, 1), 1, "month", T, date(2026, 11, 30)) == (0, None)                    # ends before it starts
    assert recur.count_until(date(2026, 8, 1), 1, "month", T, date(2026, 10, 15)) == (0, date(2026, 10, 1))          # all in the past: none left, the last was 1 Oct
    assert recur.count_until(date(2026, 1, 31), 1, "month", T, date(2027, 1, 31))[0] == 3                           # 30 Nov, 31 Dec, 31 Jan
    n, _ = recur.count_until(date(2026, 12, 1), 1, "day", T, date.max)
    assert n > 2_900_000                                                                                            # worked out, not built


def test_nothing_is_due_before_the_next_payment_date():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO subscriptions(name,amount,cycle,every,unit,next_date,active) VALUES('Insurance',90,'monthly',1,'month','2027-03-15',1)")
    assert ledger.subscriptions(db, T)[0]["next_due"] == "2027-03-15" and ledger.upcoming(db, 60, T)["count"] == 0


def test_a_loan_marked_i_owe_this_debt_is_a_loan_and_one_marked_i_am_owed_is_yours():
    """Firefly III 6.7.9's own labels: liability_direction "debit" = "I owe this debt to somebody else", "credit" = "I am owed this debt"."""
    ff = alone()
    ff.acct("1", 1, "Checking", balance="300")
    ff.acct("1", 2, "Car loan", type="liabilities", role=None, balance="-13637.35", debt_amount="13637.35", liability_type="loan", liability_direction="debit")
    ff.acct("1", 3, "Loan to Bob", type="liabilities", role=None, balance="500", debt_amount="500", liability_type="loan", liability_direction="credit")
    db = read(ff, T)
    k = {r["name"]: (r["kind"], r["balance"]) for r in db.all("SELECT name, kind, balance FROM accounts")}
    assert k["Car loan"] == ("loan", 13637.35) and k["Loan to Bob"][0] == "cash"
    assert not any("overdrawn" in a["title"] for a in ledger.overview(db, 30, T)["alerts"])
