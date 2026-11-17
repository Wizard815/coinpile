"""Bad input that used to end in an error page or leave pages failing: now refused, or read as nothing."""
import json
from datetime import date

import pytest

from tests.audit_helpers import client, make_app, world

T = date(2026, 11, 10)
PAGES = ["/api/overview", "/api/accounts", "/api/subscriptions", "/api/savings", "/api/goals", "/api/plan", "/api/incomes", "/api/alerts", "/api/transactions", "/api/projection",
         "/api/forecast", "/api/history", "/api/maturities", "/api/afford?amount=10"]


def start(tmp_path, ff=None):
    a, _ = make_app(tmp_path, ff or world(), T)
    return client(a)                                   # server errors come back as 500 instead of raising


def broken(c):
    return [p for p in PAGES if c.get(p).status_code != 200]


CASES = [
    ("subscription amount 1e308", "post", "/api/subscriptions", {"name": "x", "amount": 1e308, "next_date": "2026-12-01", "cycle": "monthly"}),
    ("account balance 1e308", "post", "/api/accounts", {"name": "Wallet", "balance": 1e308, "coinpile_only": True}),
    ("income 1e308 weekly", "post", "/api/incomes", {"name": "Job", "amount": 1e308, "next_date": "2026-11-15", "cycle": "weekly"}),
    ("savings 100% APY maturing 9999-12-31", "post", "/api/savings", {"name": "CD", "balance": 10, "apy": 100, "maturity": "9999-12-31"}),
    ("afford amount=nan", "get", "/api/afford?amount=nan", None),
    ("afford amount=inf", "get", "/api/afford?amount=inf", None),
    ("plan month 9999-12", "get", "/api/plan?month=9999-12", None),
    ("budgets month 9999-12", "get", "/api/budgets?month=9999-12", None),
    ("firefly budgets month 9999-12", "get", "/api/firefly/budgets?month=9999-12", None),
    ("dismiss proposal:abc", "post", "/api/suggestions/dismiss", {"key": "proposal:abc"}),
    ("transactions offset 1e23", "get", "/api/transactions?offset=99999999999999999999999", None),
    ("delete goal id 1e23", "delete", "/api/goals/99999999999999999999999", None),
    ("account term_months 1e30", "put", "/api/accounts/ff:3", {"term_months": 10 ** 30}),
]


@pytest.mark.parametrize("label,method,path,body", CASES, ids=[c[0] for c in CASES])
def test_bad_input_is_refused_not_a_500(tmp_path, label, method, path, body):
    c = start(tmp_path)
    r = getattr(c, method)(path, **({"content": json.dumps(body), "headers": {"content-type": "application/json"}} if body is not None else {}))
    br = broken(c)
    print(label, "->", r.status_code, "pages failing afterwards:", br)
    assert r.status_code != 500, f"{label}: 500"
    assert not br, f"{label}: answered {r.status_code}, and afterwards these pages fail: {br}"


@pytest.mark.parametrize("field,where", [("Infinity", "account"), ("Infinity", "transaction"), ("NaN", "piggy"), ("NaN", "bill")])
def test_a_non_number_from_firefly_does_not_break_pages(tmp_path, field, where):
    ff = world()
    d = ff.data["1"]
    if where == "account":
        d["accounts"][0]["attributes"]["current_balance"] = field
    elif where == "transaction":
        d["groups"][2]["attributes"]["transactions"][0]["amount"] = field
    elif where == "piggy":
        d["piggies"][0]["attributes"]["current_amount"] = field
    else:
        d["bills"][0]["attributes"]["amount_avg"] = field
    c = start(tmp_path, ff)
    r = c.post("/api/refresh?full=true")
    br = [p for p in PAGES + ["/api/piggy-banks/overview"] if c.get(p).status_code != 200]
    print(where, field, "refresh", r.status_code, "failing:", br)
    assert r.status_code == 200 and not br, f"{where} amount {field!r} from Firefly III: refresh {r.status_code}, pages failing {br}"
