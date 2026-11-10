"""Small shared helpers: timestamps, money formatting, balance snapshots and the money-movement event log."""
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(v: float, cur: str = "USD") -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}" if cur == "USD" else f"{v:+,.2f} {cur}"


def record_balance(db, acct_id: str, name: str, balance: float | None, kind: str):
    """Snapshot a balance and emit an event when it changed."""
    if balance is None:
        return
    prev = db.one("SELECT balance FROM snapshots WHERE account_id=? ORDER BY ts DESC LIMIT 1", (acct_id,))
    if prev is not None and abs(prev["balance"] - balance) < 0.005:
        return
    ts = now()
    with db.tx() as c:
        c.execute("INSERT INTO snapshots VALUES(?,?,?)", (ts, acct_id, balance))
        if prev is not None:
            d = balance - prev["balance"]
            c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                      (f"bal:{acct_id}:{ts}", ts, "balance", f"{name}: balance {money(d)}", d, acct_id,
                       f"{prev['balance']:,.2f} -> {balance:,.2f}"))
