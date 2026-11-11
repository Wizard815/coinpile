"""Coinpile sends webhooks: when something happens in your money, Coinpile POSTs a small signed JSON message to addresses you choose
(Home Assistant, n8n, Node-RED, a chat webhook, your own script).

Events
  transaction.new   a new transaction (from Firefly III, a CSV import, or added by hand)
  balance.changed   an account's balance moved
  bill.due          a bill or subscription is due within its reminder days
  alert.new         something needs attention (overdrawn, below your minimum, a missed payment, a CD maturing...)
  goal.changed      a goal was added, funded, changed or closed
  refresh.done      Coinpile finished reading Firefly III (with the headline numbers)
  test              sent by the Test button

Each message:  {"id": "...", "event": "transaction.new", "at": "2026-11-11T10:00:00+00:00", "person": "alice", "data": {...}}
Signed like Firefly III and Stripe do it: header  Coinpile-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<body>" with the webhook's secret>.
A webhook set to "no details" sends only the event and counts: no names, no amounts.

Delivery: right away, then retried after 1, 5, 30, 120 and 720 minutes if the address does not answer 2xx. Nothing is ever sent anywhere you did not add."""
import hashlib
import hmac
import json
import secrets
import time
import uuid

import httpx

from coinpile import crypto, ledger, netguard, plan, settings as cfg, sync

EVENTS = {
    "transaction.new": "A new transaction",
    "balance.changed": "An account's balance moved",
    "bill.due": "A bill or subscription is due soon",
    "alert.new": "Something needs attention (overdrawn, under your minimum, a missed payment, a CD maturing...)",
    "goal.changed": "A goal was added, funded, changed or closed",
    "refresh.done": "Coinpile finished reading Firefly III (with the headline numbers)",
}
RETRY_MINUTES = [1, 5, 30, 120, 720]
MAX_HOOKS = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL, secret_enc TEXT NOT NULL, events TEXT NOT NULL,
  detail INTEGER DEFAULT 1, active INTEGER DEFAULT 1, created TEXT, last_status TEXT, last_at TEXT);
CREATE TABLE IF NOT EXISTS webhook_outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, hook_id INTEGER NOT NULL, event TEXT NOT NULL, body TEXT NOT NULL,
  tries INTEGER DEFAULT 0, next_at REAL NOT NULL, created TEXT, done INTEGER DEFAULT 0, status TEXT);
CREATE INDEX IF NOT EXISTS wo_due ON webhook_outbox(done, next_at);
CREATE TABLE IF NOT EXISTS webhook_seen(key TEXT PRIMARY KEY);
"""


def init(db) -> None:
    with db.tx() as c:
        c.executescript(SCHEMA)


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(ts if ts is not None else time.time())
    return f"t={ts},v1=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, header: str, tolerance: int = 600) -> bool:
    """For whoever receives Coinpile's webhooks (and for the tests): the same check Coinpile does on Firefly III's."""
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    want = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return abs(time.time() - ts) <= tolerance and hmac.compare_digest(want, parts.get("v1", ""))


# ---------------------------------------------------------------- the person's webhooks
def view(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "url": row["url"], "events": [e for e in row["events"].split(",") if e], "detail": bool(row["detail"]),
            "active": bool(row["active"]), "created": row["created"], "last_status": row["last_status"], "last_at": row["last_at"]}


def check_fields(url: str, events: list[str]) -> None:
    if not url.startswith(("http://", "https://")) or len(url) > 1000:
        raise ValueError("the address must start with http:// or https://")
    netguard.check(url)
    bad = [e for e in events if e not in EVENTS]
    if not events or bad:
        raise ValueError("choose at least one event" + (f" (unknown: {', '.join(bad)})" if bad else ""))


def add(db, name: str, url: str, events: list[str], detail: bool = True) -> dict:
    check_fields(url, events)
    if db.one("SELECT COUNT(*) n FROM webhooks")["n"] >= MAX_HOOKS:
        raise ValueError(f"at most {MAX_HOOKS} webhooks")
    secret = "whsec_" + secrets.token_urlsafe(24)
    with db.tx() as c:
        cur = c.execute("INSERT INTO webhooks(name,url,secret_enc,events,detail,created) VALUES(?,?,?,?,?,?)",
                        ((name or "Webhook").strip()[:60], url.strip(), crypto.encrypt(secret), ",".join(sorted(set(events))), int(detail), sync.now()))
    return {**view(db.one("SELECT * FROM webhooks WHERE id=?", (cur.lastrowid,))), "secret": secret, "note": "Copy the secret now; it is not shown again."}


def update(db, hid: int, **f) -> dict:
    row = db.one("SELECT * FROM webhooks WHERE id=?", (hid,))
    if not row:
        raise KeyError(hid)
    url = f.get("url") if f.get("url") is not None else row["url"]
    events = f.get("events") if f.get("events") is not None else row["events"].split(",")
    check_fields(url, events)
    with db.tx() as c:
        c.execute("UPDATE webhooks SET name=?, url=?, events=?, detail=?, active=? WHERE id=?",
                  ((f.get("name") or row["name"]).strip()[:60], url.strip(), ",".join(sorted(set(events))),
                   int(f["detail"]) if f.get("detail") is not None else row["detail"], int(f["active"]) if f.get("active") is not None else row["active"], hid))
    return view(db.one("SELECT * FROM webhooks WHERE id=?", (hid,)))


def remove(db, hid: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM webhook_outbox WHERE hook_id=?", (hid,))
        if not c.execute("DELETE FROM webhooks WHERE id=?", (hid,)).rowcount:
            raise KeyError(hid)


def deliveries(db, hid: int) -> list[dict]:
    return db.all("SELECT id, event, tries, status, created, done FROM webhook_outbox WHERE hook_id=? ORDER BY id DESC LIMIT 20", (hid,))


# ---------------------------------------------------------------- what happened
def queue(db, person: str, event: str, full: dict, minimal: dict, only_hook: int | None = None) -> int:
    """One event to every active webhook that wants it. A webhook without details gets `minimal` (no names, no amounts)."""
    n = 0
    for h in db.all("SELECT * FROM webhooks WHERE active=1" + (" AND id=?" if only_hook else ""), (only_hook,) if only_hook else ()):
        if event != "test" and event not in h["events"].split(","):
            continue
        body = json.dumps({"id": uuid.uuid4().hex, "event": event, "at": sync.now(), "person": person, "data": full if h["detail"] else minimal}, separators=(",", ":"))
        with db.tx() as c:
            c.execute("INSERT INTO webhook_outbox(hook_id,event,body,next_at,created) VALUES(?,?,?,?,?)", (h["id"], event, body, time.time(), sync.now()))
        n += 1
    return n


def _seen(db, key: str) -> bool:
    """True if this was already reported; marks it reported otherwise."""
    with db.tx() as c:
        return not c.execute("INSERT OR IGNORE INTO webhook_seen(key) VALUES(?)", (key,)).rowcount


def collect(db, person: str, today, refreshed: dict | None = None) -> int:
    """Look at what changed since last time and queue the events. Cheap when nothing changed; safe to call as often as you like.
    The first time a person adds a webhook, what already happened is not sent (only what happens from then on)."""
    if not db.one("SELECT 1 FROM webhooks WHERE active=1"):
        return 0
    n = 0
    start = int(cfg.get(db, "wh_cursor") or 0) if db.one("SELECT 1 FROM settings WHERE key='wh_cursor'") else None
    last = db.one("SELECT MAX(rowid) m FROM events")["m"] or 0
    if start is None:                                         # first run: begin from now
        cfg.set_value(db, "wh_cursor", str(last))
        for a in ledger.alerts(db, today):
            _seen(db, "alert:" + (a.get("key") or a["kind"] + ":" + a["title"]))
        for g in plan.goals_view(db, today)["items"]:
            _seen(db, f"goal:{g['id']}:{g['status']}:{g['allocated']}:{g['target']}")
        return 0
    accts = {a["id"]: a for a in db.all("SELECT id, name, nickname, institution, kind FROM accounts")}
    for e in db.all("SELECT rowid r, * FROM events WHERE rowid>? ORDER BY rowid LIMIT 500", (start,)):
        a = accts.get(e["account_id"]) or {}
        acct = {"id": e["account_id"], "name": a.get("nickname") or a.get("name"), "institution": a.get("institution"), "kind": a.get("kind")}
        if e["kind"] == "transaction":
            t = db.one("SELECT date, name, amount, category, currency FROM transactions WHERE id=?", (e["id"][3:],)) or {}
            full = {"account": acct, "date": t.get("date") or e["ts"][:10], "name": t.get("name") or e["title"], "amount": t.get("amount", e["amount"]),
                    "currency": t.get("currency") or "USD", "category": t.get("category")}
            n += queue(db, person, "transaction.new", full, {"account_kind": a.get("kind"), "direction": "in" if (e["amount"] or 0) > 0 else "out"})
        elif e["kind"] == "balance":
            bal = db.one("SELECT balance FROM snapshots WHERE account_id=? ORDER BY ts DESC, rowid DESC LIMIT 1", (e["account_id"],)) or {}
            n += queue(db, person, "balance.changed", {"account": acct, "change": e["amount"], "balance": bal.get("balance")}, {"account_kind": a.get("kind")})
        start = e["r"]
    cfg.set_value(db, "wh_cursor", str(start))
    for a in ledger.alerts(db, today):
        key = "alert:" + (a.get("key") or a["kind"] + ":" + a["title"] + ":" + str(a.get("date") or ""))
        if _seen(db, key):
            continue
        event = "bill.due" if a["kind"] == "due_soon" else "alert.new"
        full = {k: a.get(k) for k in ("kind", "severity", "title", "detail", "date") if a.get(k) is not None}
        n += queue(db, person, event, full, {"kind": a["kind"], "severity": a["severity"]})
    for g in plan.goals_view(db, today)["items"]:
        if _seen(db, f"goal:{g['id']}:{g['status']}:{g['allocated']}:{g['target']}"):
            continue
        full = {"id": g["id"], "name": g["name"], "target": g["target"], "set_aside": g["allocated"], "still_needed": g["shortfall"], "status": g["status"], "deadline": g.get("deadline")}
        n += queue(db, person, "goal.changed", full, {"id": g["id"], "status": g["status"], "pct": g["pct"]})
    if refreshed is not None:
        s = {"net_worth": ledger.net_worth(db), "accounts": refreshed.get("accounts"), "transactions_read": refreshed.get("transactions"), "full": refreshed.get("full")}
        n += queue(db, person, "refresh.done", s, {"accounts": refreshed.get("accounts")})
    return n


# ---------------------------------------------------------------- sending
def deliver(db, transport=None, limit: int = 50) -> dict:
    """Send what is due. Each message is tried at most len(RETRY_MINUTES)+1 times."""
    sent = failed = 0
    due = db.all("SELECT o.*, w.url, w.secret_enc FROM webhook_outbox o JOIN webhooks w ON w.id=o.hook_id WHERE o.done=0 AND o.next_at<=? AND w.active=1 ORDER BY o.id LIMIT ?",
                 (time.time(), limit))
    if not due:
        return {"sent": 0, "failed": 0}
    with netguard.client(transport, timeout=10, follow_redirects=False) as http:
        for o in due:
            body = o["body"].encode()
            try:
                r = http.post(o["url"], content=body, headers={"Content-Type": "application/json", "User-Agent": "Coinpile-Webhooks/1",
                                                                "Coinpile-Event": o["event"], "Coinpile-Signature": sign(crypto.decrypt(o["secret_enc"]), body)})
                ok, status = 200 <= r.status_code < 300, str(r.status_code)
            except httpx.HTTPError as e:
                ok, status = False, type(e).__name__
            tries = o["tries"] + 1
            with db.tx() as c:
                if ok:
                    c.execute("UPDATE webhook_outbox SET done=1, tries=?, status=? WHERE id=?", (tries, status, o["id"]))
                    sent += 1
                else:
                    gave_up = tries > len(RETRY_MINUTES)
                    c.execute("UPDATE webhook_outbox SET done=?, tries=?, status=?, next_at=? WHERE id=?",
                              (2 if gave_up else 0, tries, status, time.time() + 60 * RETRY_MINUTES[min(tries - 1, len(RETRY_MINUTES) - 1)], o["id"]))
                    failed += 1
                c.execute("UPDATE webhooks SET last_status=?, last_at=? WHERE id=?", (status, sync.now(), o["hook_id"]))
    with db.tx() as c:                                         # keep the log short
        c.execute("DELETE FROM webhook_outbox WHERE done<>0 AND id NOT IN (SELECT id FROM webhook_outbox ORDER BY id DESC LIMIT 500)")
    return {"sent": sent, "failed": failed}
