"""Categorize what is still uncategorized, one merchant at a time, and send the answer back to Firefly III.

The list ("to categorize") holds payments on the bare Income/Misc default (nothing set in Firefly III, no rule matched) and AI guesses nobody
has confirmed yet, grouped by merchant (aicat.merchant_key), so one choice covers every payment from that store.

Each merchant gets a suggestion, best first:
  remembered  you chose it before for this merchant ("always", kept in Coinpile)
  history     what your other payments from this merchant already have (in Firefly III or Coinpile)
  ai          the AI's guess (AI categories, when switched on)

Applying a category:
  "Categories" sending on (Settings, Firefly III): every payment's category is set in Firefly III through its API; "always" also makes a
      Firefly III rule (rule group Coinpile), so the next ones the bank connector sends arrive categorized by Firefly III itself.
  sending off: kept in Coinpile only (it survives every read from Firefly III); "always" remembers the merchant, so new payments from it get
      the same category after each read.
A payment kept only in Coinpile (CSV import, hand entry) is set in Coinpile; "always" there makes a Coinpile rule."""
from collections import Counter

from coinpile import aicat, firefly, ledger, sync

DEFAULTS = ("Misc", "Income")


def key_of(t: dict) -> str:
    name, payee, money_in = aicat.merchant_key(t)
    return f"{name}|{payee}|{'in' if money_in else 'out'}"


def _rows(db) -> list[dict]:
    return db.all("""SELECT id, account_id, date, name, payee, amount, category, ai_category, plaid_category, source, ff_uncat, category_locked FROM transactions
                     WHERE coalesce(cat_ok,0)=0 AND (
                       (category IN ('Misc','Income') AND (ff_uncat=1 OR (category_locked=0 AND source != 'firefly')))
                       OR (ai_category IS NOT NULL AND category=ai_category))
                     ORDER BY date DESC, id""")


def _history(db) -> dict[str, str]:
    """For each merchant, the category most of its already-categorized payments have (a clear majority only)."""
    seen: dict[str, Counter] = {}
    for r in db.all("""SELECT name, payee, amount, category FROM transactions WHERE category NOT IN ('Misc','Income','Transfer') AND category IS NOT NULL
                       AND (ai_category IS NULL OR coalesce(cat_ok,0)=1)"""):
        seen.setdefault(key_of(r), Counter())[r["category"]] += 1
    out = {}
    for k, c in seen.items():
        (top, n), total = c.most_common(1)[0], sum(c.values())
        if n * 2 > total:
            out[k] = top
    return out


def queue(db, limit: int | None = None) -> dict:
    rows = _rows(db)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["ff_uncat"] == 1 or r["ai_category"] or ledger.rule_category(db, r["name"], r["amount"], r["plaid_category"]) is None:
            groups.setdefault(key_of(r), []).append(r)
    mem = {m["mkey"]: m["category"] for m in db.all("SELECT mkey, category FROM cat_memory")}
    hist = _history(db)
    valid = {c["name"] for c in db.all("SELECT name FROM categories")}
    items = []
    for k, rs in groups.items():
        ai = {r["ai_category"] for r in rs if r["ai_category"]}
        sug, src = None, None
        if mem.get(k) in valid:
            sug, src = mem[k], "remembered"
        elif hist.get(k) in valid:
            sug, src = hist[k], "history"
        elif (rc := ledger.rule_category(db, rs[0]["name"], rs[0]["amount"], rs[0]["plaid_category"])) in valid and rc != "Transfer":
            sug, src = rc, "rules"
        elif len(ai) == 1 and next(iter(ai)) in valid:
            sug, src = next(iter(ai)), "ai"
        items.append({"key": k, "name": rs[0]["name"], "payee": rs[0]["payee"], "direction": "in" if rs[0]["amount"] >= 0 else "out", "count": len(rs),
                      "total": round(sum(r["amount"] for r in rs), 2), "first": min(r["date"] for r in rs), "last": max(r["date"] for r in rs),
                      "examples": [{k2: r[k2] for k2 in ("id", "date", "name", "amount")} for r in rs[:3]],
                      "in_firefly": sum(1 for r in rs if r["id"].startswith("ff:")), "suggestion": sug, "suggested_by": src})
    items.sort(key=lambda i: (-i["count"], i["total"]))
    return {"merchants": len(items), "transactions": sum(i["count"] for i in items), "items": items[:limit] if limit else items}


def group_rows(db, key: str) -> list[dict]:
    return [r for r in _rows(db) if key_of(r) == key]


def apply(db, writer, key: str, category: str, always: bool = False) -> dict:
    """Set one merchant's category on every payment of it still in the list. writer: ffwrite.Writer, or None when Firefly III is only read."""
    category = (category or "").strip()[:60]
    if not category or category in ("Transfer",):
        raise ValueError("choose a category")
    rows = group_rows(db, key)
    if not rows:
        raise KeyError(key)
    send = bool(writer) and writer.on("categories")
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO categories(name,kind,builtin) VALUES(?,?,0)", (category, "income" if rows[0]["amount"] >= 0 else "expense"))
    sent, kept, failed, rule = 0, 0, [], None
    ruled = False
    for r in rows:
        if r["id"].startswith("ff:"):
            if send:
                try:
                    out = writer.set_category(r["id"], category, always and not ruled)
                except firefly.TokenRefused:
                    raise
                except (firefly.FireflyError, KeyError) as e:
                    failed.append({"id": r["id"], "why": str(e)[:200]})
                    continue
                rule = out.get("rule") or rule
                ruled = ruled or bool(always)
                with db.tx() as c:
                    c.execute("UPDATE transactions SET cat_ok=1, ai_category=NULL WHERE id=?", (r["id"],))
                sent += 1
            else:
                with db.tx() as c:                      # kept in Coinpile: ai_category is what survives a read of an uncategorized Firefly III payment
                    c.execute("UPDATE transactions SET category=?, ai_category=?, cat_ok=1 WHERE id=?", (category, category, r["id"]))
                kept += 1
        else:
            ledger.set_category(db, r["id"], category, always and not ruled)
            ruled = ruled or bool(always)
            with db.tx() as c:
                c.execute("UPDATE transactions SET cat_ok=1 WHERE id=?", (r["id"],))
            kept += 1
    if always and not send:
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO cat_memory(mkey, category, created) VALUES(?,?,?)", (key, category, sync.now()))
    return {"key": key, "category": category, "sent_to_firefly": sent, "kept_in_coinpile": kept, "failed": failed, "rule": rule,
            "remembered": bool(always and not send)}


def auto(db) -> int:
    """After a read from Firefly III: merchants you told Coinpile to always put somewhere get that category on their new payments (kept in
    Coinpile; with sending on, the Firefly III rule made then already does it in Firefly III). Then Coinpile's own rules and keyword list
    (Kroger is Groceries, Shell is Gas...) give an uncategorized Firefly III payment a guess: shown in Coinpile and listed to confirm, never
    sent to Firefly III until you apply it, and the AI is then only asked about what is left."""
    mem = {m["mkey"]: m["category"] for m in db.all("SELECT mkey, category FROM cat_memory")}
    n = 0
    for r in _rows(db):
        if r["source"] == "firefly" and r["ff_uncat"] == 1 and not r["ai_category"] and not mem.get(key_of(r)):
            g = ledger.rule_category(db, r["name"], r["amount"], r["plaid_category"])
            if g and g != "Transfer" and g != r["category"]:
                with db.tx() as c:
                    c.execute("UPDATE transactions SET category=?, ai_category=? WHERE id=? AND ff_uncat=1 AND ai_category IS NULL", (g, g, r["id"]))
            continue
        cat = mem.get(key_of(r))
        if cat and r["category"] != cat:
            with db.tx() as c:
                if r["source"] == "firefly":
                    n += c.execute("UPDATE transactions SET category=?, ai_category=?, cat_ok=1 WHERE id=? AND ff_uncat=1", (cat, cat, r["id"])).rowcount
                else:
                    n += c.execute("UPDATE transactions SET category=?, category_locked=1, cat_ok=1 WHERE id=?", (cat, r["id"])).rowcount
    return n
