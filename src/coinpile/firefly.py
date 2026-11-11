"""Read a person's Firefly III through its API and keep a local copy that the Coinpile screens and the agent work from.

Firefly III stays the source of truth. The copy uses the same tables the old Goblin Bank screens were built on, so the overview, forecasts,
subscription finder, planner and agent work unchanged. Rows that came from Firefly III carry source='firefly' and are refreshed from it;
what you add in Coinpile itself (goals, notes, savings you track by hand, chats) is kept as Coinpile's own.

Amounts are signed per account: positive = money in. A transfer between two of your accounts is two rows, one out and one in."""
import json
import re
from datetime import date, timedelta

import httpx

from coinpile import recur, settings as cfg, sync


class FireflyError(Exception):
    pass


class TokenRefused(FireflyError):
    pass


class Firefly:
    def __init__(self, url: str, token: str, transport=None):
        self.base = url.rstrip("/") + "/api/v1"
        self.c = httpx.Client(timeout=30, transport=transport, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json"})

    def get(self, path: str, **params):
        try:
            r = self.c.get(self.base + path, params=params)
        except httpx.HTTPError as e:
            raise FireflyError(f"could not reach Firefly III ({type(e).__name__})")
        if r.status_code in (401, 403):
            raise TokenRefused("Firefly III refused the sign-in: it has expired or was revoked. Sign in again.")
        if r.status_code >= 400:
            raise FireflyError(f"Firefly III answered {r.status_code} for {path}")
        try:
            return r.json()
        except ValueError:
            raise FireflyError("Firefly III sent something that is not JSON: is this the right address?")

    def send(self, method: str, path: str, body: dict | None = None) -> dict:
        """A change in Firefly III: only for the parts the person switched on, and only because of something they did in Coinpile (see ffwrite.py)."""
        try:
            r = self.c.request(method, self.base + path, json=body, headers={"Content-Type": "application/json"})
        except httpx.HTTPError as e:
            raise FireflyError(f"could not reach Firefly III ({type(e).__name__})")
        if r.status_code in (401, 403):
            raise TokenRefused("Firefly III refused the change: sign in again.")
        if r.status_code == 204 or not r.content:
            return {}
        try:
            d = r.json()
        except ValueError:
            d = {}
        if r.status_code >= 400:
            errs = d.get("errors") or {}
            first = next((v[0] for v in errs.values() if v), None) if isinstance(errs, dict) else None
            raise FireflyError(f"Firefly III did not accept it ({r.status_code}): {first or d.get('message') or ''}".rstrip(": "))
        return d

    def pages(self, path: str, **params):
        page = 1
        while True:
            d = self.get(path, page=page, limit=params.pop("limit", 200), **params)
            yield from d.get("data", [])
            p = (d.get("meta") or {}).get("pagination") or {}
            if page >= int(p.get("total_pages") or 1):
                return
            page += 1

    def me(self) -> dict:
        a = self.get("/about/user")["data"]
        return {"id": str(a["id"]), "email": a["attributes"].get("email", "")}

    def about(self) -> dict:
        return self.get("/about")["data"]


# ---------------------------------------------------------------- accounts
ROLE = {"defaultAsset": ("cash", "checking"), "sharedAsset": ("cash", "checking"), "savingAsset": ("cash", "savings"), "cashWalletAsset": ("cash", "cash"), "ccAsset": ("credit", "credit card")}
MASK = re.compile(r"\s*(?:••|\.\.\.|x{2,}|\*{2,})\s*(\d{2,6})\s*$", re.I)


def split_name(name: str) -> tuple[str, str, str | None]:
    """'Huntington - Everyday Checking ••4821' (how the Plaid connector names them) -> institution, name, last digits."""
    mask = None
    m = MASK.search(name)
    if m:
        mask, name = m.group(1), name[:m.start()]
    if " - " in name:
        inst, rest = name.split(" - ", 1)
        return inst.strip(), rest.strip() or name.strip(), mask
    return "Firefly III", name.strip(), mask


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _pc(at: dict, field: str, own: str | None, primary: str | None) -> tuple[float, str | None, bool]:
    """A money field of a Firefly III object in the person's primary currency when Firefly III gave it (its pc_* twin).
    Returns (value, the currency that value is in, whether it was converted). Without a pc_* value (the person has "convert to primary"
    off) the value stays in the object's own currency."""
    if primary and own and own != primary and at.get("pc_" + field) not in (None, ""):
        return _num(at.get("pc_" + field)), primary, True
    return _num(at.get(field)), own or primary, False


def account_row(a: dict, primary: str | None = None) -> dict | None:
    at = a.get("attributes") or {}
    if not at.get("active", True):
        return None
    primary = at.get("primary_currency_code") or primary
    own = at.get("currency_code") or primary or "USD"          # an account with no currency of its own is kept in the primary currency
    inst, name, mask = split_name(at.get("name") or f"Account {a.get('id')}")
    typ = at.get("type")
    if typ == "asset":
        kind, subtype = ROLE.get(at.get("account_role") or "", ("cash", "checking"))
        bal, cur, conv = _pc(at, "current_balance", own, primary)
        nat = _num(at.get("current_balance"))
        if kind == "credit":
            bal, nat = -bal, -nat                        # Firefly III: owed on a card is negative; here a card's balance is what you owe
    elif typ in ("liabilities", "liability", "loan", "debt", "mortgage"):
        kind, subtype = "loan", at.get("liability_type") or "loan"
        debt, cur, conv = _pc(at, "debt_amount", own, primary)
        cb, cur2, conv2 = _pc(at, "current_balance", own, primary)
        if not debt:
            debt, cur, conv = cb, cur2, conv2
        bal, nat = abs(debt), abs(_num(at.get("debt_amount")) or _num(at.get("current_balance")))
        if (at.get("liability_direction") or "credit") == "debit":
            kind, subtype = "cash", "owed to you"                # money someone owes you counts for you
    else:
        return None
    return {"id": f"ff:{a['id']}", "ff_id": str(a["id"]), "institution": inst, "name": name, "mask": mask, "kind": kind, "subtype": subtype,
            "balance": round(bal, 2), "currency": cur or "USD", "own_currency": own,
            "native_balance": round(nat, 2) if conv else None, "native_currency": own if conv else None,
            "in_net_worth": 0 if at.get("include_net_worth") is False else 1, "notes": at.get("notes") or ""}


def primary_currency(db) -> str:
    return cfg.get(db, "currency") or "USD"


def sync_accounts(db, ff: Firefly) -> dict[str, dict]:
    rows, raw = {}, []
    for t in ("asset", "liabilities"):
        raw += list(ff.pages("/accounts", type=t))
    primary = next((x["attributes"].get("primary_currency_code") for x in raw if (x.get("attributes") or {}).get("primary_currency_code")), None)
    for a in raw:
        r = account_row(a, primary)
        if r:
            rows[r["ff_id"]] = r
    if primary:
        cfg.set_value(db, "currency", primary)
    with db.tx() as c:
        for r in rows.values():
            c.execute("""INSERT INTO accounts(id,item_id,source,institution,name,kind,balance,currency,updated,subtype,mask,native_balance,native_currency,in_net_worth) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET institution=excluded.institution,name=excluded.name,kind=excluded.kind,balance=excluded.balance,
                         currency=excluded.currency,updated=excluded.updated,subtype=excluded.subtype,mask=excluded.mask,native_balance=excluded.native_balance,
                         native_currency=excluded.native_currency,in_net_worth=excluded.in_net_worth""",
                      (r["id"], None, "firefly", r["institution"], r["name"], r["kind"], r["balance"], r["currency"], sync.now(), r["subtype"], r["mask"],
                       r["native_balance"], r["native_currency"], r["in_net_worth"]))
        gone = [x["id"] for x in db.all("SELECT id FROM accounts WHERE source='firefly'") if x["id"][3:] not in rows]
        for aid in gone:                                                     # removed or closed in Firefly III
            c.execute("DELETE FROM transactions WHERE account_id=?", (aid,))
            c.execute("DELETE FROM snapshots WHERE account_id=?", (aid,))
            c.execute("DELETE FROM accounts WHERE id=?", (aid,))
    for r in rows.values():
        sync.record_balance(db, r["id"], r["name"], r["balance"], r["kind"])
    return rows


# ---------------------------------------------------------------- transactions
def _leg_money(s: dict, leg_cur: str | None, primary: str | None) -> tuple[float, str | None, float | None, str | None]:
    """One side of a journal: (amount in the primary currency where Firefly III gave a conversion, its currency, the account's own amount, its currency).
    Firefly III keeps `amount` in the currency of the journal (the source account's for a withdrawal or transfer, the destination's for a deposit) and,
    across currencies, `foreign_amount` in the other account's currency."""
    cur, fcur = s.get("currency_code") or primary, s.get("foreign_currency_code")
    has_foreign = fcur and s.get("foreign_amount") not in (None, "")
    if leg_cur and has_foreign and fcur == leg_cur and fcur != cur:
        nat, ncur, pc = abs(_num(s.get("foreign_amount"))), fcur, s.get("pc_foreign_amount")
    else:
        nat, ncur, pc = abs(_num(s.get("amount"))), cur, s.get("pc_amount")
    if not primary or not ncur or ncur == primary:
        return nat, ncur or primary, None, None
    if has_foreign and fcur == primary:
        return abs(_num(s.get("foreign_amount"))), primary, nat, ncur
    if cur == primary:
        return abs(_num(s.get("amount"))), primary, nat, ncur
    if pc not in (None, ""):
        return abs(_num(pc)), primary, nat, ncur
    return nat, ncur, None, None                         # no conversion from Firefly III: kept (and shown) in its own currency


def tx_rows(group: dict, mine, primary: str | None = None) -> list[dict]:
    """The rows one Firefly III transaction group gives: one per split and side that is one of your accounts.
    `mine` is the set of your Firefly III account ids, or {id: account row} so each side is read in that account's own currency."""
    out = []
    for s in (group.get("attributes") or {}).get("transactions") or []:
        typ = (s.get("type") or "").lower()
        if typ in ("opening balance", "liability credit"):
            continue                                    # a starting balance, not money that moved: the account balance already holds it
        jid = s["transaction_journal_id"]
        src, dst = str(s.get("source_id") or ""), str(s.get("destination_id") or "")
        both = src in mine and dst in mine
        # Between two of your own accounts (a transfer, paying a card, paying a loan) the money arriving is never income or spending. The side it
        # leaves keeps its category only for a withdrawal (Firefly III counts paying a liability as an expense); a transfer is never spending.
        own_cat = s.get("category_name") or None
        if both or typ == "transfer":                   # a transfer is between your own accounts even when one of them is closed (not read)
            out_cat = (own_cat if typ == "withdrawal" else None) or "Transfer"
        else:
            out_cat = own_cat or ("Transfer" if typ == "reconciliation" else "Misc")
        in_cat = "Transfer" if both or typ == "transfer" else own_cat or ("Transfer" if typ == "reconciliation" else "Income")
        base = {"date": str(s.get("date") or "")[:10], "notes": s.get("notes") or "", "type": s.get("type"), "group": str(group["id"]),
                "bill": str(s.get("subscription_id") or s.get("bill_id") or "") or None}
        cur_of = (lambda aid: (mine.get(aid) or {}).get("own_currency")) if isinstance(mine, dict) else (lambda aid: None)
        for aid, sign, suffix, cat, fallback in ((src, -1, ":out", out_cat, "destination_name"), (dst, 1, ":in", in_cat, "source_name")):
            if aid not in mine:
                continue
            amt, cur, nat, ncur = _leg_money(s, cur_of(aid), primary)
            out.append({**base, "id": f"ff:{jid}" + (suffix if both else ""), "account_id": f"ff:{aid}", "amount": sign * amt, "currency": cur or "USD",
                        "native_amount": sign * nat if nat is not None else None, "native_currency": ncur,
                        "name": s.get("description") or s.get(fallback) or "", "category": cat})
    return out


def sync_transactions(db, ff: Firefly, accounts: dict, start: date | None, end: date) -> dict:
    """All transactions between start and end (all of them when start is None). Local rows in that window that Firefly III no longer has are removed."""
    mine, seen, n = accounts, set(), 0
    primary = cfg.get(db, "currency") or None
    known = {r["id"] for r in db.all("SELECT id FROM transactions WHERE source='firefly'")}
    recent = (end - timedelta(days=45)).isoformat()
    params = {"end": end.isoformat(), "type": "all"}
    if start:
        params["start"] = start.isoformat()
    cats: dict[str, set] = {}
    groups = list(ff.pages("/transactions", **params))           # read everything first: the person's database is not held while Firefly III answers
    with db.tx() as c:
        for g in groups:
            for r in tx_rows(g, mine, primary):
                seen.add(r["id"])
                cats.setdefault(r["category"], set()).add("in" if r["amount"] > 0 and (r["type"] or "").lower() == "deposit" else "out")
                c.execute("""INSERT INTO transactions(id,account_id,date,name,amount,currency,category,pending,source,category_locked,notes,ff_group,native_amount,native_currency,ff_bill)
                             VALUES(?,?,?,?,?,?,?,0,'firefly',1,?,?,?,?,?)
                             ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,date=excluded.date,name=excluded.name,amount=excluded.amount,
                             currency=excluded.currency,category=excluded.category,notes=excluded.notes,ff_group=excluded.ff_group,native_amount=excluded.native_amount,
                             native_currency=excluded.native_currency,ff_bill=excluded.ff_bill""",
                          (r["id"], r["account_id"], r["date"], r["name"][:255], r["amount"], r["currency"], r["category"], r["notes"], r["group"],
                           r["native_amount"], r["native_currency"], r["bill"]))
                if r["id"] not in known and r["date"] >= recent:          # new and recent: it goes in the "latest movement" list and the feed
                    inst = (accounts.get(r["account_id"][3:]) or {}).get("institution", "")
                    c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)", (f"tx:{r['id']}", f"{r['date']}T00:00:00+00:00", "transaction",
                              f"{inst + ': ' if inst and inst != 'Firefly III' else ''}{r['name']} {sync.money(r['amount'])}", r["amount"], r["account_id"], "posted"))
                n += 1
        q, a = "SELECT id FROM transactions WHERE source='firefly' AND date<=?", [end.isoformat()]
        if start:
            q += " AND date>=?"
            a.append(start.isoformat())
        removed = [x["id"] for x in db.all(q, tuple(a)) if x["id"] not in seen]
        for tid in removed:
            c.execute("DELETE FROM transactions WHERE id=?", (tid,))
        for cat, ways in cats.items():                  # a new category only ever seen on deposits (Salary, Dividends...) is income, not spending
            kind = "income" if cat == "Income" or ways == {"in"} else "transfer" if cat == "Transfer" else "expense"
            c.execute("INSERT OR IGNORE INTO categories(name,kind,builtin) VALUES(?,?,0)", (cat, kind))
    return {"transactions": n, "removed": len(removed)}


# ---------------------------------------------------------------- bills (Firefly III's subscriptions)
FREQ = {"daily": (1, "day"), "weekly": (1, "week"), "monthly": (1, "month"), "quarterly": (3, "month"), "half-year": (6, "month"), "yearly": (1, "year")}


def bill_row(b: dict, today: date, primary: str | None = None) -> tuple | None:
    """A Firefly III bill as a subscription row, or None when it has no name. Missing amounts, dates or frequency fall back to sensible defaults."""
    at = b.get("attributes") or {}
    if not at.get("name"):
        return None
    every, unit = FREQ.get(at.get("repeat_freq") or "monthly", (1, "month"))
    try:
        every *= max(0, int(at.get("skip") or 0)) + 1
    except (TypeError, ValueError):
        pass
    try:
        anchor = date.fromisoformat(str(at.get("date"))[:10])
    except ValueError:
        anchor = today
    end = str(at.get("end_date"))[:10] if at.get("end_date") else None
    nxt = next((str(p)[:10] for p in (at.get("pay_dates") or []) if str(p)[:10] >= today.isoformat()), None)
    if not nxt:
        n = recur.next_due(anchor, every, unit, today)
        nxt = (n or anchor).isoformat()
        if end and nxt > end:
            nxt = end                                        # an ended bill: its last date, not a date after the end
    primary = at.get("primary_currency_code") or primary
    own = at.get("currency_code") or primary or "USD"
    amt, cur, conv = _pc(at, "amount_avg", own, primary)
    if not amt:
        lo, cur, conv = _pc(at, "amount_min", own, primary)
        hi, _, _ = _pc(at, "amount_max", own, primary)
        amt = (lo + hi) / 2 if lo and hi else lo or hi
    nat = abs(_num(at.get("amount_avg")) or (_num(at.get("amount_min")) + _num(at.get("amount_max"))) / 2) if conv else None
    return (str(at["name"])[:120], round(abs(amt), 2), cur or "USD", recur.label(every, unit), every, unit, nxt, int(bool(at.get("active", True))), end, at.get("notes") or "",
            round(nat, 2) if nat is not None else None, own if conv else None)


def sync_bills(db, ff: Firefly, today: date) -> int:
    seen = set()
    primary = cfg.get(db, "currency") or None
    bills = list(ff.pages("/bills", start=today.isoformat(), end=(today + timedelta(days=400)).isoformat()))
    with db.tx() as c:
        for b in bills:
            row = bill_row(b, today, primary)
            if not row:
                continue
            key = f"ff-bill:{b['id']}"
            seen.add(key)
            c.execute("""INSERT INTO subscriptions(name,amount,currency,cycle,every,unit,next_date,active,end_date,notes,native_amount,native_currency,source,plaid_stream_id,category,created)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'firefly',?,'Subscriptions',?)
                         ON CONFLICT(plaid_stream_id) DO UPDATE SET name=excluded.name,amount=excluded.amount,currency=excluded.currency,cycle=excluded.cycle,
                         every=excluded.every,unit=excluded.unit,next_date=excluded.next_date,active=excluded.active,end_date=excluded.end_date,notes=excluded.notes,
                         native_amount=excluded.native_amount,native_currency=excluded.native_currency""",
                      row + (key, sync.now()))
        for r in db.all("SELECT id, plaid_stream_id k FROM subscriptions WHERE source='firefly'"):
            if r["k"] not in seen:
                c.execute("DELETE FROM subscriptions WHERE id=?", (r["id"],))
    return len(seen)


# ---------------------------------------------------------------- piggy banks
def piggy_row(p: dict, primary: str | None = None) -> dict:
    at = p.get("attributes") or {}
    primary = at.get("primary_currency_code") or primary
    own = at.get("currency_code") or primary or "USD"
    target, cur, _ = _pc(at, "target_amount", own, primary)
    accts = []
    for x in at.get("accounts") or []:
        if not isinstance(x, dict) or not x.get("account_id"):
            continue
        accts.append({"account_id": f"ff:{x['account_id']}", "name": x.get("name") or "", "amount": _pc(x, "current_amount", own, primary)[0]})
    return {"id": str(p["id"]), "name": at.get("name") or f"Piggy bank {p['id']}", "target": target or None, "current": _pc(at, "current_amount", own, primary)[0],
            "currency": cur or "USD", "target_date": str(at["target_date"])[:10] if at.get("target_date") else None, "active": bool(at.get("active", True)),
            "accounts": accts, "save_per_month": _pc(at, "save_per_month", own, primary)[0] or None, "notes": at.get("notes") or ""}


def sync_piggies(db, ff: Firefly) -> int:
    primary = cfg.get(db, "currency") or None
    rows = [piggy_row(p, primary) for p in ff.pages("/piggy-banks")]
    with db.tx() as c:
        c.execute("DELETE FROM ff_piggy")
        c.executemany("INSERT INTO ff_piggy(id, data) VALUES(?,?)", [(r["id"], json.dumps(r)) for r in rows])
    return len(rows)


def piggies(db) -> list[dict]:
    return [json.loads(r["data"]) for r in db.all("SELECT data FROM ff_piggy ORDER BY id")]


# ---------------------------------------------------------------- balance history (for the net worth chart)
HISTORY_DAYS = 730
MARK = "T23:59:59Z"          # backfilled day-end balances carry this; live snapshots use the +00:00 form, so a re-read replaces only its own rows


def sync_history(db, ff: Firefly, accounts: dict, today: date, days: int = HISTORY_DAYS) -> int:
    """Firefly III knows every account's balance on every day. Keep one row per account per day the balance changed, so net worth has a past from the first day."""
    start, n = today - timedelta(days=days), 0
    for fid, a in accounts.items():
        try:
            sets = ff.get("/chart/account/overview", start=start.isoformat(), end=today.isoformat(), period="1D", **{"accounts[]": fid})
        except FireflyError:
            continue                                         # an older Firefly III without this chart: the chart simply starts today
        if not sets:
            continue
        ds = sets[0] if isinstance(sets, list) and sets and isinstance(sets[0], dict) else {}
        entries = ds.get("entries") or {}
        if a.get("native_currency") and isinstance(ds.get("pc_entries"), dict) and ds["pc_entries"]:
            entries = ds["pc_entries"]                       # an account in another currency: its history in the primary currency, like its balance
        rows, prev = [], None
        for day, v in sorted(entries.items() if isinstance(entries, dict) else []):
            bal = _num(v)
            bal = -bal if a["kind"] == "credit" else abs(bal) if a["kind"] == "loan" else bal
            bal = round(bal, 2)
            if prev is None and bal == 0:
                continue                                     # before the account existed
            if bal != prev:
                rows.append((str(day)[:10] + MARK, a["id"], bal))
                prev = bal
        with db.tx() as c:
            c.execute("DELETE FROM snapshots WHERE account_id=? AND ts LIKE ?", (a["id"], "%" + MARK))
            c.executemany("INSERT INTO snapshots VALUES(?,?,?)", rows)
        n += len(rows)
    return n


# ---------------------------------------------------------------- one refresh
FULL_EVERY = 24 * 3600


def refresh(db, ff: Firefly, today: date, full: bool = False, now: float | None = None) -> dict:
    """Bring the local copy up to date. A full pass (every transaction) on the first run and once a day; otherwise the last 120 days, which is where edits happen."""
    import time
    now = now or time.time()
    last_full = float(cfg.get(db, "ff_last_full") or 0)
    full = full or not last_full or now - last_full > FULL_EVERY
    accounts = sync_accounts(db, ff)
    start = None if full else today - timedelta(days=120)
    tx = sync_transactions(db, ff, accounts, start, today + timedelta(days=30))
    out = {"accounts": len(accounts), **tx, "subscriptions": sync_bills(db, ff, today), "piggy_banks": sync_piggies(db, ff), "full": full}
    if full:
        out["history_points"] = sync_history(db, ff, accounts, today)
    cfg.set_value(db, "ff_last_refresh", sync.now())
    if full:
        cfg.set_value(db, "ff_last_full", str(now))
    return out
