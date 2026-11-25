"""Categorize: what is still uncategorized, by merchant, sent back to Firefly III (or kept in Coinpile), and the agent's part in it."""
from datetime import date
from types import SimpleNamespace

from coinpile import agent_actions, categorize
from tests.audit_helpers import alone, client, make_app, read

T = date(2026, 11, 10)


def shop_world():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="3000")
    ff.tx("1", 11, "withdrawal", 80.10, "KROGER #123", 1, 90, "2026-10-02")
    ff.tx("1", 12, "withdrawal", 64.00, "KROGER #123", 1, 90, "2026-10-09")
    ff.tx("1", 13, "withdrawal", 91.30, "Kroger  #771", 1, 90, "2026-10-16")
    ff.tx("1", 14, "withdrawal", 50.00, "KROGER #555", 1, 90, "2026-09-20", "Groceries")      # categorized in Firefly III already
    ff.tx("1", 21, "withdrawal", 40.00, "SHELL OIL 5544", 1, 91, "2026-10-03")
    ff.tx("1", 22, "withdrawal", 38.50, "SHELL OIL 5544", 1, 91, "2026-10-20")
    ff.tx("1", 31, "withdrawal", 12.00, "ODD SHOP", 1, 92, "2026-10-05")
    ff.tx("1", 41, "deposit", 1500, "ACME PAYROLL", 93, 1, "2026-10-15", "Salary")
    return ff


def cat_in_firefly(ff, jid):
    for g in ff.data["1"]["groups"]:
        for t in g["attributes"]["transactions"]:
            if t["transaction_journal_id"] == str(jid):
                return t.get("category_name")


def test_one_choice_per_merchant_goes_to_firefly_with_a_rule(tmp_path):
    ff = shop_world()
    a, _ = make_app(tmp_path, ff, T)
    c = client(a)
    assert c.put("/api/settings", json={"ff_send_categories": "on"}).status_code == 200
    q = c.get("/api/categorize").json()
    assert q["sends_to_firefly"] is True and q["merchants"] == 3 and q["transactions"] == 6
    kroger = q["items"][0]
    assert kroger["count"] == 3 and kroger["total"] == -235.4 and kroger["suggestion"] == "Groceries" and kroger["suggested_by"] == "history"
    assert not [i for i in q["items"] if "PAYROLL" in i["name"]]                                   # categorized in Firefly III: not in the list
    r = c.post("/api/categorize/apply", json={"key": kroger["key"], "category": "Groceries", "always": True}).json()
    assert r["sent_to_firefly"] == 3 and not r["failed"] and "rule" in r and r["rule"]
    assert [cat_in_firefly(ff, j) for j in (11, 12, 13)] == ["Groceries"] * 3
    rules = ff.data["1"].get("rules", [])
    assert len(rules) == 1 and rules[0]["actions"][0]["value"] == "Groceries"                      # one rule, not one per payment
    c.post("/api/refresh")
    q2 = c.get("/api/categorize").json()
    assert q2["merchants"] == 2 and all("KROGER" not in i["name"].upper() for i in q2["items"])
    assert c.post("/api/categorize/apply", json={"key": kroger["key"], "category": "Groceries"}).status_code == 404


def test_kept_in_coinpile_when_not_sending_and_remembered_for_new_payments(tmp_path):
    ff = shop_world()
    a, _ = make_app(tmp_path, ff, T)
    c = client(a)
    shell = next(i for i in c.get("/api/categorize").json()["items"] if "SHELL" in i["name"])
    assert c.get("/api/categorize").json()["sends_to_firefly"] is False
    r = c.post("/api/categorize/apply", json={"key": shell["key"], "category": "Gas", "always": True}).json()
    assert r["kept_in_coinpile"] == 2 and r["sent_to_firefly"] == 0 and r["remembered"] is True
    assert cat_in_firefly(ff, 21) is None and not ff.data["1"].get("rules")                       # Firefly III untouched
    c.post("/api/refresh")
    assert {r["category"]: r["spent"] for r in c.get("/api/spending").json()}.get("Gas") == 78.5       # still Gas after reading Firefly III again
    ff.tx("1", 23, "withdrawal", 41.25, "SHELL OIL 5544", 1, 91, "2026-11-05")                    # a new one arrives
    out = c.post("/api/refresh").json()
    assert out.get("remembered_categories") == 1
    names = {i["name"] for i in c.get("/api/categorize").json()["items"]}
    assert not any("SHELL" in n for n in names)
    sp = {r["category"]: r["spent"] for r in c.get("/api/spending").json()}
    assert sp.get("Gas") == 119.75


def test_the_agent_proposes_a_merchant_and_nothing_changes_before_yes(tmp_path):
    ff = shop_world()
    db = read(ff, T)
    key = next(i["key"] for i in categorize.queue(db)["items"] if "SHELL" in i["name"])
    ctx = SimpleNamespace(store=db, writer=None)
    clean, summary, target = agent_actions.p_merchant(ctx, {"key": key, "category": "Gas", "always": True})
    assert target == "coinpile" and "2 payments" in summary and "Gas" in summary and "remember" in summary
    assert categorize.queue(db)["merchants"] == 3                                                  # only asked
    out = agent_actions.r_merchant(ctx, clean)
    assert out["kept_in_coinpile"] == 2 and categorize.queue(db)["merchants"] == 2
    try:
        agent_actions.p_merchant(ctx, {"key": "nope|x|out", "category": "Gas"})
        assert False
    except agent_actions.Refused:
        pass


def test_a_payment_arriving_on_a_loan_is_never_income_or_left_to_categorize():
    ff = alone()
    ff.acct("1", 1, "Checking", balance="3000")
    ff.acct("1", 7, "Car loan", type="liabilities", role=None, balance="-9000", debt_amount="9000", liability_type="loan")
    ff.tx("1", 51, "deposit", 350, "Monthly payment", 94, 7, "2026-10-21")       # the bank's own record of a payment, without the checking side
    db = read(ff, T)
    row = db.one("SELECT category, ff_uncat FROM transactions WHERE account_id='ff:7'")
    assert row["category"] == "Transfer" and row["ff_uncat"] == 0
    assert categorize.queue(db)["merchants"] == 0


def test_well_known_names_are_placed_by_keywords_without_any_model():
    ff = shop_world()
    db = read(ff, T)
    categorize.auto(db)
    q = {i["name"]: i for i in categorize.queue(db)["items"]}
    shell = next(i for n, i in q.items() if "SHELL" in n)
    assert shell["suggestion"] == "Gas" and shell["suggested_by"] == "rules"
    assert q["ODD SHOP"]["suggestion"] is None                                   # left for you, the AI or the agent
    assert {r["category"] for r in db.all("SELECT category FROM transactions WHERE name LIKE 'SHELL%'")} == {"Gas"}    # shown in Coinpile now
    assert cat_in_firefly(ff, 21) is None                                         # but not sent until applied
