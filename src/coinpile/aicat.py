"""AI guesses for transactions no rule could place: a fallback under the rules, never over them, and off unless switched on.

Which transactions: ones still on the bare Income/Misc default. A transaction from Firefly III qualifies only when Firefly III itself has no
category on it (anything set there, by you or a Firefly III rule, is never touched); one kept in Coinpile only (CSV import, hand entry) only when
you have not set its category yourself and no rule, bank category or keyword matched.

How: many transactions go to the model in ONE message (BATCH at a time), with the exact category names Coinpile has; the model answers a
category or null for each. The same merchant is asked once (see merchant_key) and every one of its transactions gets that answer, so it is
always placed the same way; a merchant the model already placed in an earlier run gets that same category without asking again. Anything that is not exactly one of those names is thrown away. The guess is kept in ai_category; it shows as the
transaction's category in Coinpile and survives later reads from Firefly III, but is never sent to Firefly III.

What the model sees: the transaction's description, payee, amount and the bank's own category hint. Not account names, not ids (it gets
numbers 1..n). A provider that is not on your machine or network is only used when you ticked "allow cloud" on it, as for the chat."""
import json
import re
import time

from coinpile import crypto, ledger, netguard, settings as cfg

BATCH = 40              # tested: 40 short lines fit a small 8k-context local model with room for the answer; bigger batches gain little
AUTO_MAX = 80           # at most this many per refresh (two calls), so a refresh stays quick and cheap
TIMEOUT = 90


def categories(db) -> list[str]:
    return [r["name"] for r in db.all("SELECT name FROM categories ORDER BY name")]


def provider(db) -> tuple[dict, str] | None:
    """The model to ask (the chat's default, else the first one), or None: none set up, or a cloud one without "allow cloud"."""
    from coinpile.agent import is_local
    pid = cfg.get(db, "agent_provider")
    row = (db.one("SELECT * FROM providers WHERE id=?", (int(pid),)) if pid and str(pid).isdigit() else None) or db.one("SELECT * FROM providers ORDER BY id LIMIT 1")
    if not row or not (is_local(row["base_url"]) or row["allow_cloud"]):
        return None
    return row, (crypto.decrypt(row["key_enc"]) if row["key_enc"] else "")


def candidates(db, only_new: bool, limit: int | None = None) -> list[dict]:
    """Transactions on the bare default that may get a guess. only_new: ones not looked at before (the automatic run after a refresh)."""
    rows = db.all("""SELECT id, name, payee, amount, plaid_category, source, category, category_locked, ff_uncat, ai_category FROM transactions
                     WHERE category IN ('Misc','Income') AND ai_category IS NULL AND (ff_uncat=1 OR (category_locked=0 AND source != 'firefly'))"""
                  + (" AND coalesce(ai_seen,0)=0" if only_new else "") + " ORDER BY date DESC, id")
    out = [r for r in rows if r["ff_uncat"] == 1 or ledger.rule_category(db, r["name"], r["amount"], r["plaid_category"]) is None]
    return out[:limit] if limit else out


def merchant_key(t: dict) -> tuple:
    """Transactions that are the same merchant the same way round: description and payee with case, spacing and digit runs (store numbers,
    reference numbers, dates) ignored, and money in kept apart from money out."""
    def norm(v):
        return re.sub(r"\s+", " ", re.sub(r"\d+", "#", str(v or "").lower())).strip()
    return norm(t.get("name")), norm(t.get("payee")), (t.get("amount") or 0) >= 0


def known(db) -> dict[tuple, str]:
    """Merchants the model already placed and whose guess still stands: {merchant_key: category}. Only a category every such transaction
    agrees on counts."""
    seen: dict[tuple, set] = {}
    for r in db.all("SELECT name, payee, amount, ai_category FROM transactions WHERE ai_category IS NOT NULL AND category=ai_category"):
        seen.setdefault(merchant_key(r), set()).add(r["ai_category"])
    valid = set(categories(db))
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1 and next(iter(v)) in valid}


def _prompt(cats: list[str], batch: list[dict]) -> tuple[str, str]:
    system = ("You sort bank transactions into a person's budget categories. Use ONLY these category names, spelled exactly: "
              + json.dumps(cats) + ". Money out (negative amount) is spending; money in (positive) is usually Income or a refund. "
              "If you cannot tell, answer null for that line: a wrong guess is worse than none. Reply with one JSON object and nothing else, "
              "mapping each line number to a category name or null, e.g. {\"1\": \"Groceries\", \"2\": null}.")
    lines = []
    for i, t in enumerate(batch, 1):
        desc = re.sub(r"\s+", " ", str(t["name"] or ""))[:80]
        payee = re.sub(r"\s+", " ", str(t.get("payee") or ""))[:60]
        hint = f" | bank says {t['plaid_category']}" if t.get("plaid_category") else ""
        lines.append(f"{i}. {desc}" + (f" | payee {payee}" if payee and payee.lower() != desc.lower() else "") + f" | {t['amount']:+.2f}{hint}")
    return system, "\n".join(lines)


def _answer(text: str) -> dict:
    """The first JSON object in the reply (models wrap it in prose or code fences); {} when there is none."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except (ValueError, RecursionError):
        return {}
    return d if isinstance(d, dict) else {}


def ai_categorize(db, prov: dict, key: str, transactions: list[dict], transport=None) -> tuple[dict[str, str], int]:
    """{transaction id: category} for the ones placed with a valid name, and how many model calls it took. One line per merchant; a
    merchant placed before is not asked again. Raises on a model server that cannot be reached or answers an error, so the caller can say so."""
    from coinpile.agent import _call_anthropic, _call_openai
    cats = categories(db)
    valid = set(cats)
    before = known(db)
    groups: dict[tuple, list[dict]] = {}
    for t in transactions:
        groups.setdefault(merchant_key(t), []).append(t)
    out = {t["id"]: before[k] for k, ts in groups.items() if k in before for t in ts}
    ask = [ts for k, ts in groups.items() if k not in before]
    call = _call_anthropic if prov["kind"] == "anthropic" else _call_openai
    calls = 0
    if not ask:
        return out, calls
    with netguard.client(transport, timeout=TIMEOUT) as http:
        for i in range(0, len(ask), BATCH):
            batch = ask[i:i + BATCH]
            system, user = _prompt(cats, [ts[0] for ts in batch])
            calls += 1
            reply = call(http, prov, key, system, [{"role": "user", "content": user}], None)
            got = _answer(reply.get("content") or "")
            for n, ts in enumerate(batch, 1):
                v = got.get(str(n))
                if isinstance(v, str) and v in valid:              # exactly one of the person's categories; anything else is dropped
                    for t in ts:
                        out[t["id"]] = v
    return out, calls


def apply(db, guesses: dict[str, str], looked_at: list[str]) -> int:
    """Keep the guesses (only on rows that still qualify) and mark every row that was looked at, guessed or not."""
    n = 0
    with db.tx() as c:
        for tid, cat in guesses.items():
            n += c.execute("""UPDATE transactions SET ai_category=?, category=? WHERE id=? AND category IN ('Misc','Income')
                              AND (ff_uncat=1 OR (category_locked=0 AND source != 'firefly'))""", (cat, cat, tid)).rowcount
        for tid in looked_at:
            c.execute("UPDATE transactions SET ai_seen=1 WHERE id=?", (tid,))
    return n


def run(db, transport=None, only_new: bool = False) -> dict:
    """One pass. Nothing is sent anywhere unless ai_categorize is on and a usable model is set up. only_new is the automatic run after a
    refresh: the first one after switching it on just marks what is already there as looked at, so the history is never re-sent."""
    if cfg.get(db, "ai_categorize") != "on":
        if only_new and cfg.get(db, "ai_categorize_since"):
            cfg.set_value(db, "ai_categorize_since", "")
        return {"skipped": "AI categories are off (Settings)"}
    if only_new and cfg.get(db, "ai_categorize_auto") != "on":
        if cfg.get(db, "ai_categorize_since"):
            cfg.set_value(db, "ai_categorize_since", "")         # switched off: switching on again starts from then, not from now
        return {"skipped": "automatic AI categories are off"}
    if only_new and not cfg.get(db, "ai_categorize_since"):
        with db.tx() as c:
            c.execute("UPDATE transactions SET ai_seen=1")
        cfg.set_value(db, "ai_categorize_since", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return {"asked": 0, "set": 0, "calls": 0, "note": "from now on new transactions are looked at; use Re-run on all for the history"}
    p = provider(db)
    if not p:
        return {"skipped": "no model to ask (add one in Settings, Models; a cloud one needs 'allow cloud')"}
    rows = candidates(db, only_new, AUTO_MAX if only_new else None)
    if not rows:
        return {"asked": 0, "set": 0, "calls": 0}
    guesses, calls = ai_categorize(db, p[0], p[1], rows, transport)
    return {"asked": len(rows), "set": apply(db, guesses, [r["id"] for r in rows]), "calls": calls}
