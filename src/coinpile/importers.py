"""CSV importers for accounts with no usable API: PayPal, Venmo, Cash App.

Honest scope: Venmo and Cash App have no public API for personal accounts, and PayPal's
Transaction Search API needs a business account. Download the CSV from each app and import it.
Signed amounts (positive = in). Re-importing is safe (stable ids, INSERT OR IGNORE).
"""
import csv
import hashlib
import io
import re
from datetime import datetime


AMOUNT = re.compile(r"^(?P<lp>\()?\s*(?P<s1>[-+\u2212])?\s*(?:[A-Z]{3}|[$€£¥₹]|US\$|C\$|A\$|kr|zł|Fr\.?|CHF)?\s*(?P<s2>[-+\u2212])?\s*"
                    r"(?P<n>\d[\d.,'\s\u00a0\u202f]*)\s*(?:[A-Z]{3}|[$€£¥₹]|kr|zł|CHF)?\s*(?P<s3>[-+\u2212])?\s*(?P<rp>\))?\s*(?P<crdr>CR|DR|Cr|Dr|cr|dr)?$")


class UnknownFormat(ValueError):
    """Not a PayPal, Venmo or Cash App export (it may still be a bank's own CSV)."""


def _num(s: str) -> float:
    """'$1,234.50', '-12.00', '$-12.00', '(12.00)', '12.00-', '12.00 DR', '1.234,56', '12,50 €' -> a signed number. An empty cell is 0.
    Anything else is not a number (ValueError, reported as a bad file). With both separators the last one is the decimal mark; a lone comma
    is the decimal mark unless exactly three digits follow it (1,234 is a thousand); a lone point is always the decimal mark."""
    s = (s or "").strip()
    if not s:
        return 0.0
    m = AMOUNT.match(s)
    if not m or bool(m["lp"]) != bool(m["rp"]) or sum(1 for k in ("s1", "s2", "s3") if m[k]) > 1:
        raise ValueError(f"not an amount: {s[:20]!r}")
    n = re.sub(r"[\s\u00a0\u202f']", "", m["n"])
    dot, comma = n.rfind("."), n.rfind(",")
    if dot >= 0 and comma >= 0:
        dec = "." if dot > comma else ","
    elif comma >= 0:
        dec = "," if n.count(",") == 1 and len(n) - comma - 1 != 3 else None
    elif n.count(".") > 1:
        dec = None                                       # 1.234.567: points grouping thousands
    else:
        dec = "."
    whole, _, frac = n.rpartition(dec) if dec and dec in n else (n, "", "")
    sep = {".": ",", ",": "."}.get(dec, "." if "." in whole else ",")
    if not re.fullmatch(r"\d+|\d{1,3}(?:" + re.escape(sep) + r"\d{3})+", whole) or (_ and not re.fullmatch(r"\d+", frac)):
        raise ValueError(f"not an amount: {s[:20]!r}")              # thousands come in groups of three, and only one decimal mark
    whole = whole.replace(sep, "")
    v = float(whole + ("." + frac if frac else ""))
    neg = bool(m["lp"]) or any((m[k] or "") in ("-", "\u2212") and m[k] for k in ("s1", "s2", "s3")) or (m["crdr"] or "").upper() == "DR"
    return -v if neg else v


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
                "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%Y%m%d")


def _date(s: str) -> str:
    """A date as YYYY-MM-DD. Accepts ISO dates (with or without a time), US 11/01/2026, European 01.11.2026 and spelled-out months (1 Nov 2026,
    Nov 1, 2026). Slashes are read the US way (month first). Raises ValueError when it is not a date."""
    s = (s or "").strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ]\d{1,2}:\d{2}", s)
    if m:
        s = m.group(1)                                   # a timestamp: its date part (time zones are not shifted, the bank's own day is kept)
    s = re.sub(r"\s+", " ", s)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"not a date: {s[:30]!r}")


def detect(headers: list[str]) -> str | None:
    h = {x.strip().lower() for x in headers}
    if {"transaction id", "transaction type", "net amount"} <= h:
        return "cashapp"
    if {"datetime", "from", "to", "note"} <= h:
        return "venmo"
    if {"gross", "net"} <= h or ({"name", "type", "status"} <= h and "gross" in h):
        return "paypal"
    return None


def parse(text: str, provider: str | None = None) -> tuple[str, list[dict]]:
    text = text.lstrip("\ufeff")
    if provider == "venmo":  # Venmo exports have preamble lines before the real header row
        lines = text.splitlines()
        i = next((n for n, l in enumerate(lines) if "Datetime" in l and "Note" in l), 0)
        text = "\n".join(lines[i:])
    rows = _rows(text)
    if not rows:
        return provider or "unknown", []
    prov = provider or detect(list(rows[0].keys()))
    if not prov:
        raise UnknownFormat("unrecognized CSV format (expected PayPal, Venmo or Cash App export)")
    out = []
    for n, r in enumerate(rows, start=2):
        r = {k.strip(): (v or "").strip() if isinstance(v, str) else "" for k, v in r.items() if isinstance(k, str) and k}
        try:
            row = _p2p_row(prov, r)
        except ValueError as e:
            raise ValueError(f"row {n}: {e}")
        if row:
            out.append(row)
    return prov, out


def _rows(text: str) -> list[dict]:
    try:
        return list(csv.DictReader(io.StringIO(text.replace("\x00", ""))))
    except csv.Error as e:
        raise ValueError(f"this is not a readable CSV file ({e})")


def _p2p_row(prov: str, r: dict) -> dict | None:
    if prov == "paypal":
        if r.get("Status", "Completed") not in ("Completed", ""):
            return None
        amt, name, date = _num(r.get("Net") or r.get("Gross", "0")), r.get("Name") or r.get("Type", ""), _date(r.get("Date", ""))
        tid = r.get("Transaction ID", "")
    elif prov == "venmo":
        if not r.get("Datetime") or r.get("Status") not in ("Complete", ""):
            return None
        amt = _num(r.get("Amount (total)") or r.get("Amount", "0"))
        name = (r.get("To") if amt < 0 else r.get("From")) or r.get("Type", "")
        name = f"{name}: {r.get('Note', '')}".strip(": ")
        date, tid = _date(r["Datetime"]), r.get("ID", "")
    else:  # cashapp
        amt, name, date = _num(r.get("Net Amount") or r.get("Amount", "0")), r.get("Notes") or r.get("Transaction Type", ""), _date(r.get("Date", ""))
        tid = r.get("Transaction ID", "")
    tid = tid or hashlib.sha256(f"{date}|{name}|{amt}".encode()).hexdigest()[:16]
    return {"id": f"{prov}:{tid}", "date": date, "name": name or prov, "amount": amt}


def store(db, provider: str, rows: list[dict]) -> int:
    acct = f"manual:{provider}"
    from coinpile.sync import money, now
    from coinpile import ledger
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO accounts(id,source,institution,name,kind,balance,updated) VALUES(?,?,?,?,?,?,?)",
                  (acct, "csv", provider.title(), f"{provider.title()} (CSV import)", "cash", None, now()))
    added = 0
    for r in rows:
        with db.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO transactions(id,account_id,date,name,amount,source,category) VALUES(?,?,?,?,?,?,?)",
                            (r["id"], acct, r["date"], r["name"], r["amount"], "csv", ledger.categorize(db, r["name"], r["amount"])))
            if cur.rowcount:
                added += 1
                c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                          (f"tx:{r['id']}", f"{r['date']}T00:00:00+00:00", "transaction",
                           f"{provider.title()}: {r['name']} {money(r['amount'])}", r["amount"], acct, "imported"))
    return added


# ---------------------------------------------------------------- a bank's own CSV, into an account you track by hand
DATE_COLS = ("posted date", "post date", "posting date", "trans. date", "transaction date", "date")
DESC_COLS = ("description", "payee", "memo", "name", "details", "transaction description")


def _col(headers: dict, names: tuple) -> str | None:
    return next((headers[n] for n in names if n in headers), None)


def parse_bank(text: str, flip: bool = False) -> tuple[list[dict], float | None]:
    """Read a CSV downloaded from a bank or card site (Capital One, Discover, Huntington, Axos...). The columns are found by name:
    a date, a description, and either one Amount column or Debit/Credit columns. `flip` is for files where purchases are positive (typical for credit cards).
    Returns the rows (positive = money in) and, if the file has a running Balance column, the balance on its newest row."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    i = next((n for n, l in enumerate(lines) if any(d in l.lower() for d in DATE_COLS) and ("," in l or "\t" in l)), 0)      # some banks put notes above the header
    rows = _rows("\n".join(lines[i:]))
    if not rows:
        return [], None
    heads = {k.strip().lower(): k for k in rows[0].keys() if k}
    dcol, ncol = _col(heads, DATE_COLS), _col(heads, DESC_COLS)
    acol, debit, credit, bcol = heads.get("amount"), heads.get("debit"), heads.get("credit"), heads.get("balance")
    if not dcol or not ncol or not (acol or debit or credit):
        raise ValueError("could not find a date, a description and an amount in this file")
    out, seen, newest = [], {}, None
    for n, r in enumerate(rows, start=i + 2):
        cell = lambda col: (r.get(col) or "").strip() if col and isinstance(r.get(col), str) else ""
        if not re.search(r"\d", cell(dcol)):
            continue                                     # a blank line or a note like "Total" or "Ending balance"
        try:
            date = _date(cell(dcol))
            name = cell(ncol)
            if acol and cell(acol):
                amt = _num(cell(acol)) * (-1 if flip else 1)
            else:
                amt = abs(_num(cell(credit))) - abs(_num(cell(debit)))
            row_bal = _num(cell(bcol)) if bcol and cell(bcol) else None
        except ValueError as e:
            raise ValueError(f"row {n}: {e}")
        key = (date, name, round(amt, 2))
        seen[key] = seen.get(key, 0) + 1            # two identical purchases on one day are both real: number them
        tid = hashlib.sha256(f"{date}|{name}|{amt:.2f}|{seen[key]}".encode()).hexdigest()[:20]
        out.append({"id": f"bank:{tid}", "date": date, "name": name or "(no description)", "amount": round(amt, 2)})
        if row_bal is not None and (newest is None or date >= newest[0]):
            newest = (date, row_bal)
    return out, (newest[1] if newest else None)


def store_bank(db, account_id: str, rows: list[dict], balance: float | None = None) -> int:
    from coinpile import ledger
    from coinpile.sync import money, now, record_balance
    acct = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    added = 0
    for r in rows:
        with db.tx() as c:
            tid = f"{r['id']}@{account_id}"                    # the same line in two accounts' files is two transactions
            cur = c.execute("INSERT OR IGNORE INTO transactions(id,account_id,date,name,amount,source,category) VALUES(?,?,?,?,?,?,?)",
                            (tid, account_id, r["date"], r["name"], r["amount"], "csv", ledger.categorize(db, r["name"], r["amount"], None, acct["kind"])))
            if cur.rowcount:
                added += 1
                c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)", (f"tx:{tid}", f"{r['date']}T00:00:00+00:00", "transaction", f"{acct['name']}: {r['name']} {money(r['amount'])}", r["amount"], account_id, "imported"))
    if balance is not None:
        with db.tx() as c:
            c.execute("UPDATE accounts SET balance=?, updated=? WHERE id=?", (balance, now(), account_id))
        record_balance(db, account_id, acct["name"], balance, acct["kind"])
    return added
