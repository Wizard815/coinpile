"""CSV imports: amounts and dates in the formats banks and payment apps really use, and bad rows reported (422) instead of a server error."""
import pytest

from coinpile import importers
from tests.test_coinpile import app, signed_in, world


# ---------------------------------------------------------------- CSV numbers and dates
@pytest.mark.parametrize("text,value", [
    ("$1,234.50", 1234.5), ("1.234,56", 1234.56), ("12,50 €", 12.5), ("€ 1.234,00", 1234), ("1,234", 1234), ("0,99", 0.99), ("1 234,56", 1234.56),
    ("-$12.00", -12), ("$-12.00", -12), ("(12.00)", -12), ("12.00-", -12), ("12.00 DR", -12), ("12.00 CR", 12), ("+5.00", 5), ("", 0), ("1.234.567,89", 1234567.89),
    ("1,234,567.89", 1234567.89), ("-1.234,56 €", -1234.56),
])
def test_csv_amounts_in_every_common_format(text, value):
    assert importers._num(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["abc", "12.34.56", "1-2", "$", "--"])
def test_what_is_not_an_amount_is_refused(text):
    with pytest.raises(ValueError):
        importers._num(text)


@pytest.mark.parametrize("text", ["2026-11-01", "11/01/2026", "01.11.2026", "1 Nov 2026", "Nov 1, 2026", "2026-11-01T10:20:30Z", "11/01/26", "2026/11/01", "1-Nov-2026",
                                  "2026-11-01 10:20:30 PST", "November 1, 2026"])
def test_csv_dates_in_every_common_format(text):
    assert importers._date(text) == "2026-11-01"


def test_a_bad_row_in_a_csv_is_reported_not_a_server_error(tmp_path):
    c = signed_in(app(tmp_path, world()))
    bad_amount = "Date,Name,Type,Status,Gross,Net,Transaction ID\n11/01/2026,Bob,Payment,Completed,abc,abc,T1\n"
    r = c.post("/api/import/csv", content=bad_amount, headers={"Content-Type": "text/csv"})
    assert r.status_code == 422 and "row 2" in r.json()["detail"]
    bad_date = "Date,Name,Type,Status,Gross,Net,Transaction ID\nsoon,Bob,Payment,Completed,1.00,1.00,T1\n"
    r = c.post("/api/import/csv", content=bad_date, headers={"Content-Type": "text/csv"})
    assert r.status_code == 422 and "date" in r.json()["detail"]
    r = c.post("/api/import/csv", content="a,b\n\x00\x00,1\n", headers={"Content-Type": "text/csv"})
    assert r.status_code == 422
    good = "Date,Name,Type,Status,Gross,Net,Transaction ID\n01.11.2026,Bob,Payment,Completed,\"-1.234,56\",\"-1.234,56\",T1\n"
    r = c.post("/api/import/csv", content=good, headers={"Content-Type": "text/csv"})
    assert r.status_code == 200 and r.json()["added"] == 1
    t = c.get("/api/transactions?q=bob").json()["items"][0]
    assert (t["date"], t["amount"]) == ("2026-11-01", -1234.56)


def test_a_bank_csv_with_european_numbers_and_a_bad_row(tmp_path):
    rows, bal = importers.parse_bank("Date,Description,Amount,Balance\n01.11.2026,REWE,\"-12,50\",\"1.000,00\"\n1 Nov 2026,Pay,\"2.500,00\",\"3.487,50\"\n")
    assert [(r["date"], r["amount"]) for r in rows] == [("2026-11-01", -12.5), ("2026-11-01", 2500)] and bal == 3487.5
    with pytest.raises(ValueError, match="row 2"):
        importers.parse_bank("Date,Description,Amount\n11/01/2026,X,lots\n")


def test_a_bad_row_in_a_paypal_file_sent_to_firefly_says_which_row(tmp_path):
    c = signed_in(app(tmp_path, world()))
    c.put("/api/settings", json={"ff_send_accounts": "on"})
    paypal = "Date,Name,Type,Status,Gross,Net,Transaction ID\n11/01/2026,Bob,Payment,Completed,1.00,1.00,T1\n11/02/2026,Ann,Payment,Completed,lots,lots,T2\n"
    r = c.post("/api/import/bank-csv?account_id=ff:1", content=paypal)
    assert r.status_code == 422 and "row 3" in r.json()["detail"]
