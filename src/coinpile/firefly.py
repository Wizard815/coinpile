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


def account_row(a: dict) -> dict | None:
    at = a["attributes"]
    if not at.get("active", True):
        return None
    inst, name, mask = split_name(at["name"])
    if at["type"] == "asset":
        kind, subtype = ROLE.get(at.get("account_role") or "", ("cash", "checking"))
        bal = _num(at.get("current_balance"))
        if kind == "credit":
            bal = -bal                                   # Firefly III: owed on a card is negative; here a card's balance is what you owe
    elif at["type"] in ("liabilities", "liability", "loan", "debt", "mortgage"):
        kind, subtype = "loan", at.get("liability_type") or "loan"
        bal = abs(_num(at.get("debt_amount")) or _num(at.get("current_balance")))
        if (at.get("liability_direction") or "credit") == "debit":
            kind, subtype = "cash", "owed to you"                # money someone owes you counts for you
    else:
        return None
    return {"id": f"ff:{a['id']}", "ff_id": str(a["id"]), "institution": inst, "name": name, "mask": mask, "kind": kind, "subtype": subtype,
            "balance": round(bal, 2), "currency": at.get("currency_code") or "USD", "notes": at.get("notes") or ""}


def sync_accounts(db, ff: Firefly) -> dict[str, dict]:
    rows = {}
    for t in ("asset", "liabilities"):
        for a in ff.pages("/accounts", type=t):
            r = account_row(a)
            if r:
                rows[r["ff_id"]] = r
    with db.tx() as c:
        for r in rows.values():
            c.execute("""INSERT INTO accounts(id,item_id,source,institution,name,kind,balance,currency,updated,subtype,mask) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET institution=excluded.institution,name=excluded.name,kind=excluded.kind,balance=excluded.balance,
                         currency=excluded.currency,updated=excluded.updated,subtype=excluded.subtype,mask=excluded.mask""",
                      (r["id"], None, "firefly", r["institution"], r["name"], r["kind"], r["balance"], r["currency"], sync.now(), r["subtype"], r["mask"]))
        gone = [x["id"] for x in db.all("SELECT id FROM accounts WHERE source='firefly'") if x["id"][3:] not in rows]
        for aid in gone:                                                     # removed or closed in Firefly III
            c.execute("DELETE FROM transactions WHERE account_id=?", (aid,))
            c.execute("DELETE FROM accounts WHERE id=?", (aid,))
    for r in rows.values():
        sync.record_balance(db, r["id"], r["name"], r["balance"], r["kind"])
    return rows


# ---------------------------------------------------------------- transactions
def tx_rows(group: dict, mine: set[str]) -> list[dict]:
    out = []
    for s in group["attributes"]["transactions"]:
        if (s.get("type") or "").lower() in ("opening balance", "liability credit"):
            continue                                    # a starting balance, not money that moved: the account balance already holds it
        jid, amt = s["transaction_journal_id"], abs(_num(s.get("amount")))
        src, dst = str(s.get("source_id") or ""), str(s.get("destination_id") or "")
        both = src in mine and dst in mine
        cat = s.get("category_name") or ("Transfer" if both or (s.get("type") or "").lower() == "reconciliation" else None)
        base = {"date": str(s["date"])[:10], "currency": s.get("currency_code") or "USD", "notes": s.get("notes") or "", "type": s.get("type"), "group": str(group["id"])}
        if src in mine:
            out.append({**base, "id": f"ff:{jid}" + (":out" if both else ""), "account_id": f"ff:{src}", "amount": -amt,
                        "name": s.get("description") or s.get("destination_name") or "", "category": cat or "Misc"})
        if dst in mine:
            out.append({**base, "id": f"ff:{jid}" + (":in" if both else ""), "account_id": f"ff:{dst}", "amount": amt,
                        "name": s.get("description") or s.get("source_name") or "", "category": cat or "Income"})
    return out


def sync_transactions(db, ff: Firefly, accounts: dict, start: date | None, end: date) -> dict:
    """All transactions between start and end (all of them when start is None). Local rows in that window that Firefly III no longer has are removed."""
    mine, seen, n = set(accounts), set(), 0
    known = {r["id"] for r in db.all("SELECT id FROM transactions WHERE source='firefly'")}
    recent = (end - timedelta(days=45)).isoformat()
    params = {"end": end.isoformat(), "type": "all"}
    if start:
        params["start"] = start.isoformat()
    cats = set()
    groups = list(ff.pages("/transactions", **params))           # read everything first: the person's database is not held while Firefly III answers
    with db.tx() as c:
        for g in groups:
            for r in tx_rows(g, mine):
                seen.add(r["id"])
                cats.add(r["category"])
                c.execute("""INSERT INTO transactions(id,account_id,date,name,amount,currency,category,pending,source,category_locked,notes,ff_group) VALUES(?,?,?,?,?,?,?,0,'firefly',1,?,?)
                             ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,date=excluded.date,name=excluded.name,amount=excluded.amount,
                             currency=excluded.currency,category=excluded.category,notes=excluded.notes,ff_group=excluded.ff_group""",
                          (r["id"], r["account_id"], r["date"], r["name"][:255], r["amount"], r["currency"], r["category"], r["notes"], r["group"]))
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
        for cat in cats:
            c.execute("INSERT OR IGNORE INTO categories(name,kind,builtin) VALUES(?,?,0)", (cat, "income" if cat == "Income" else "transfer" if cat == "Transfer" else "expense"))
    return {"transactions": n, "removed": len(removed)}


# ---------------------------------------------------------------- bills (Firefly III's subscriptions)
FREQ = {"daily": (1, "day"), "weekly": (1, "week"), "monthly": (1, "month"), "quarterly": (3, "month"), "half-year": (6, "month"), "yearly": (1, "year")}


def sync_bills(db, ff: Firefly, today: date) -> int:
    seen = set()
    with db.tx() as c:
        for b in ff.pages("/bills", start=today.isoformat(), end=(today + timedelta(days=400)).isoformat()):
            at = b["attributes"]
            every, unit = FREQ.get(at.get("repeat_freq") or "monthly", (1, "month"))
            every *= int(at.get("skip") or 0) + 1
            anchor = date.fromisoformat(str(at["date"])[:10])
            nxt = next((str(p)[:10] for p in (at.get("pay_dates") or []) if str(p)[:10] >= today.isoformat()), None)
            if not nxt:
                n = recur.next_due(anchor, every, unit, today)
                nxt = (n or anchor).isoformat()
            amt = abs(_num(at.get("amount_avg")) or (_num(at.get("amount_min")) + _num(at.get("amount_max"))) / 2)
            key = f"ff-bill:{b['id']}"
            seen.add(key)
            row = (at["name"][:120], round(amt, 2), at.get("currency_code") or "USD", recur.label(every, unit), every, unit, nxt, int(bool(at.get("active", True))),
                   (str(at.get("end_date"))[:10] if at.get("end_date") else None), at.get("notes") or "")
            c.execute("""INSERT INTO subscriptions(name,amount,currency,cycle,every,unit,next_date,active,end_date,notes,source,plaid_stream_id,category,created)
                         VALUES(?,?,?,?,?,?,?,?,?,?,'firefly',?,'Subscriptions',?)
                         ON CONFLICT(plaid_stream_id) DO UPDATE SET name=excluded.name,amount=excluded.amount,currency=excluded.currency,cycle=excluded.cycle,
                         every=excluded.every,unit=excluded.unit,next_date=excluded.next_date,active=excluded.active,end_date=excluded.end_date,notes=excluded.notes""",
                      row + (key, sync.now()))
        for r in db.all("SELECT id, plaid_stream_id k FROM subscriptions WHERE source='firefly'"):
            if r["k"] not in seen:
                c.execute("DELETE FROM subscriptions WHERE id=?", (r["id"],))
    return len(seen)


# ---------------------------------------------------------------- piggy banks
def sync_piggies(db, ff: Firefly) -> int:
    rows = []
    for p in ff.pages("/piggy-banks"):
        at = p["attributes"]
        rows.append({"id": str(p["id"]), "name": at["name"], "target": _num(at.get("target_amount")) or None, "current": _num(at.get("current_amount")),
                     "target_date": str(at["target_date"])[:10] if at.get("target_date") else None, "active": bool(at.get("active", True)),
                     "accounts": [{"account_id": f"ff:{x['account_id']}", "name": x.get("name"), "amount": _num(x.get("current_amount"))} for x in at.get("accounts") or []],
                     "save_per_month": _num(at.get("save_per_month")) or None, "notes": at.get("notes") or ""})
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
        entries = sets[0].get("entries") or {}
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
