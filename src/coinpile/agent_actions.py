"""Changes the agent may ask for. Reading is always allowed; every change here is only a request until the person answers yes.

Each action has: a description and argument schema for the model, prepare(ctx, args) -> (clean args, one plain sentence saying exactly what
will happen, where: "firefly" or "coinpile"), and run(ctx, args) -> result. prepare refuses anything invalid before the person is asked, so a
yes always means the change described. The person's yes is the consent: an approved action runs even when the matching "send to Firefly III"
switch is off (those switches are about what Coinpile does on its own)."""
import json
import math
from datetime import date, timedelta

from coinpile import firefly, recur, sync

STR, NUM, INT, DATE, BOOL = {"type": "string"}, {"type": "number"}, {"type": "integer"}, {"type": "string", "description": "YYYY-MM-DD"}, {"type": "boolean"}


class Refused(ValueError):
    pass


def S(props, req=()):
    return {"type": "object", "properties": props, "required": list(req)}


def money(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise Refused("an amount must be a number")
    if isinstance(x, bool) or not math.isfinite(v) or abs(v) > 1e12:
        raise Refused("an amount must be an ordinary number")
    return round(v, 2)


def txt(s, n: int) -> str:
    """Text from the model, as it is stored and as it is shown in the question: one line, no runs of blank space, and never so long that
    it pushes the rest of the sentence out of sight."""
    return " ".join(str(s or "").split())[:n]


def today(ctx) -> date:
    f = getattr(ctx, "today", None)
    return f() if callable(f) else date.today()


def day(s, what="date") -> str:
    try:
        return date.fromisoformat(str(s)[:10]).isoformat()
    except ValueError:
        raise Refused(f"{what} must be YYYY-MM-DD")


def acct(ctx, aid, firefly_only=True) -> dict:
    a = ctx.store.one("SELECT * FROM accounts WHERE id=?", (str(aid or ""),))
    if not a or (firefly_only and not str(a["id"]).startswith("ff:")):
        raise Refused("unknown account (use an id from list_accounts" + (" that starts with ff:" if firefly_only else "") + ")")
    return a


def holds_money(a: dict) -> dict:
    if a.get("kind") != "cash":
        raise Refused("a piggy bank lives in a bank account that holds money (not a card, loan or investment account)")
    return a


def label(a: dict) -> str:
    return (a.get("nickname") or a["name"]) + (f" ({a['institution']})" if a.get("institution") and a["institution"] not in (a.get("nickname") or a["name"]) else "")


def piggy(ctx, pid) -> dict:
    row = ctx.store.one("SELECT data FROM ff_piggy WHERE id=?", (str(pid or ""),))
    if not row:
        raise Refused("unknown piggy bank (use piggy_id from list_goals)")
    return json.loads(row["data"])


def hook(ctx, name):
    f = getattr(ctx, name, None)
    if not f:
        raise Refused("that is not available here")
    return f


def after_firefly(ctx):
    w = getattr(ctx, "writer", None)
    if w and getattr(w, "after", None):
        try:
            w.after()                          # read Firefly III back so the screens show the change at once
        except firefly.FireflyError:
            pass


# ---------------------------------------------------------------- the actions
def p_note(ctx, a):
    title, body = txt(a.get("title"), 120), str(a.get("body", ""))[:20000]
    if not title:
        raise Refused("a note needs a title")
    pid = str(a.get("piggy_id") or "") or None
    p = piggy(ctx, pid) if pid else None
    return {"title": title, "body": body, "piggy_id": pid}, f"Save a note \"{title}\"" + (f" on the piggy bank {p['name']}" if p else "") + f" ({len(body)} characters" + (f", starting: \"{txt(body, 160)}\"" if body.strip() else "") + ")", "coinpile"


def r_note(ctx, a):
    ts = sync.now()
    with ctx.store.tx() as c:
        cur = c.execute("INSERT INTO notes(title,body,author,pinned,piggy_id,created,updated) VALUES(?,?,?,?,?,?,?)", (a["title"], a["body"], "agent", 0, a["piggy_id"], ts, ts))
    return {"note_id": cur.lastrowid}


def p_sub(ctx, a):
    every, unit = recur.resolve(a.get("every"), a.get("unit"), a.get("cycle") or (None if a.get("every") else "monthly"))
    name, amt = txt(a.get("name"), 120), money(a.get("amount"))
    if not name or amt <= 0:
        raise Refused("a subscription needs a name and an amount above 0")
    clean = {k: txt(a[k], 500 if k == "notes" else 80) for k in ("payer_name", "category", "payment_method", "notes") if a.get(k) not in (None, "")}
    if a.get("payer") not in (None, ""):
        if a["payer"] not in ("me", "other"):
            raise Refused("payer is me or other")
        clean["payer"] = a["payer"]
    ac = acct(ctx, a["account_id"], firefly_only=False) if a.get("account_id") not in (None, "") else None
    if ac:
        clean["account_id"] = ac["id"]
    if a.get("end_date") not in (None, ""):
        clean["end_date"] = day(a["end_date"], "end_date")
    clean.update({"name": name, "amount": amt, "every": every, "unit": unit, "next_date": day(a.get("next_date") or today(ctx).isoformat(), "next_date"),
                  "in_firefly": bool(a.get("in_firefly"))})
    if clean.get("end_date") and clean["end_date"] < clean["next_date"]:
        raise Refused("end_date is before next_date")
    where = "a Coinpile subscription" + (" and a bill in Firefly III" if clean["in_firefly"] else "")
    more = ([f"paid by {clean.get('payer_name') or 'someone else'}"] if clean.get("payer") == "other" else []) + ([f"from {label(ac)}"] if ac else []) \
        + [f"{k.replace('_', ' ')} {clean[k]}" for k in ("category", "payment_method") if clean.get(k)] + ([f"ends {clean['end_date']}"] if clean.get("end_date") else []) \
        + ([f"notes: \"{txt(clean['notes'], 120)}\""] if clean.get("notes") else [])
    return (clean, f"Add {where}: {amt:,.2f} {recur.label(every, unit)} for \"{name}\", next on {clean['next_date']}" + ("".join(", " + m for m in more)),
            "firefly" if clean["in_firefly"] else "coinpile")


def r_sub(ctx, a):
    a = dict(a)
    in_ff = a.pop("in_firefly", False)
    made = hook(ctx, "create_sub")(a)
    if in_ff:
        ctx.writer.sub_created(made["id"])
        after_firefly(ctx)
    return {"subscription_id": made["id"], "in_firefly": in_ff}


def p_tx(ctx, a):
    ac = acct(ctx, a.get("account_id"))
    amt = money(a.get("amount"))
    if not amt:
        raise Refused("amount cannot be 0 (negative = money out, positive = money in)")
    desc = txt(a.get("description"), 255)
    if not desc:
        raise Refused("a description is needed")
    d = day(a.get("date") or today(ctx).isoformat())
    cat = txt(a.get("category"), 60) or None
    return ({"account_id": ac["id"], "amount": amt, "description": desc, "date": d, "category": cat},
            f"Record in Firefly III: {'paid' if amt < 0 else 'received'} {abs(amt):,.2f} \"{desc}\" on {d}, account {label(ac)}" + (f", category {cat}" if cat else ""), "firefly")


def r_tx(ctx, a):
    r = ctx.writer.add_transaction(a["account_id"], a["date"], a["description"], a["amount"], a["category"])
    after_firefly(ctx)
    return {"firefly_group": str((r.get("data") or {}).get("id") or "")}


def p_cat(ctx, a):
    t = ctx.store.one("SELECT * FROM transactions WHERE id=?", (str(a.get("transaction_id") or ""),))
    if not t or not t.get("ff_group"):
        raise Refused("unknown transaction (use an id from search_transactions)")
    cat = txt(a.get("category"), 60)
    if not cat:
        raise Refused("a category is needed")
    always = bool(a.get("always"))
    return ({"transaction_id": t["id"], "category": cat, "always": always},
            f"In Firefly III, set the category of \"{t['name']}\" ({t['amount']:,.2f} on {t['date']}) to {cat}" + (" and make a rule so future ones get it too" if always else ""), "firefly")


def r_cat(ctx, a):
    out = ctx.writer.set_category(a["transaction_id"], a["category"], a["always"])
    return {"category": out["category"], "rule": out.get("rule")}


def p_merchant(ctx, a):
    from coinpile import categorize
    key = str(a.get("key") or "")
    rows = categorize.group_rows(ctx.store, key)
    if not rows:
        raise Refused("nothing to categorize for that key (use a key from uncategorized_merchants)")
    cat = txt(a.get("category"), 60)
    if not cat or cat == "Transfer":
        raise Refused("a category is needed")
    always = bool(a.get("always"))
    w = getattr(ctx, "writer", None)
    send = bool(w) and w.on("categories") and any(r["id"].startswith("ff:") for r in rows)
    where = "in Firefly III" if send else "in Coinpile"
    total = sum(r["amount"] for r in rows)
    return ({"key": key, "category": cat, "always": always},
            f"{where[0].upper() + where[1:]}, set the category of {len(rows)} payment{'s' if len(rows) != 1 else ''} from \"{txt(rows[0]['name'], 60)}\" ({total:,.2f} in all) to {cat}"
            + ((" and make a Firefly III rule so future ones get it too" if send else " and remember it for this merchant") if always else ""), "firefly" if send else "coinpile")


def r_merchant(ctx, a):
    from coinpile import categorize
    w = getattr(ctx, "writer", None)
    out = categorize.apply(ctx.store, w if (w and w.on("categories")) else None, a["key"], a["category"], a["always"])
    return {k: out[k] for k in ("category", "sent_to_firefly", "kept_in_coinpile", "failed", "rule", "remembered")}


def _bank_problems(ctx) -> dict:
    try:
        return hook(ctx, "bank_problems")()
    except Refused:
        raise
    except Exception as e:
        raise Refused(f"could not reach the Plaid connector: {getattr(e, 'detail', None) or e}"[:200])


def p_bank_pull(ctx, a):
    banks = _bank_problems(ctx)["banks"]
    b = next((x for x in banks if x["item_id"] == str(a.get("item_id") or "")), None) if a.get("item_id") else (banks[0] if len(banks) == 1 else None)
    if not b:
        raise Refused("which bank? give item_id from bank_problems" if banks else "no bank is linked at the connector")
    full = bool(a.get("full"))
    return ({"item_id": b["item_id"], "full": full},
            f"Pull {b['bank']} now at the Plaid connector" + (" (full: ask the bank for fresh data, which Plaid bills per call, and read its whole history again; nothing is doubled)" if full else " (what is new since the last pull)")
            + ", and send what changed to Firefly III", "connector")


def r_bank_pull(ctx, a):
    out = hook(ctx, "bank_pull")(a["item_id"], a["full"])
    try:
        hook(ctx, "refresh_now")()                     # read the result into Coinpile
    except Exception:
        pass
    return {k: out.get(k) for k in ("ok", "error", "added", "modified", "removed", "firefly")}


def p_bank_match(ctx, a):
    d = next((x for x in _bank_problems(ctx)["balance_differs"] if x["account_id"] == str(a.get("account_id") or "")), None)
    if not d:
        raise Refused("that account's Firefly III balance does not differ (give account_id from bank_problems' balance_differs)")
    owed = d.get("kind") in ("credit", "loan")
    bank = -abs(float(d["bank_says"] or 0)) if owed else float(d["bank_says"] or 0)
    return ({"account_id": d["account_id"]},
            f"Match {d['account']} in Firefly III to the bank: Firefly III says {float(d['firefly_says'] or 0):,.2f}, the bank {bank:,.2f}. "
            "Its opening balance moves by the difference; no transaction is added", "firefly")


def r_bank_match(ctx, a):
    out = hook(ctx, "bank_match")(a["account_id"])
    try:
        hook(ctx, "refresh_now")()
    except Exception:
        pass
    return {"changed": out.get("changed"), "difference": out.get("difference")}


def p_piggy_new(ctx, a):
    name = txt(a.get("name"), 255)
    if not name:
        raise Refused("a piggy bank needs a name")
    ac = holds_money(acct(ctx, a.get("account_id")))
    target = money(a["target"]) if a.get("target") not in (None, "") else None
    start = money(a.get("start_amount") or 0)
    if (target is not None and target < 0) or start < 0:
        raise Refused("amounts cannot be negative")
    if start > (ac.get("balance") or 0):
        raise Refused(f"{label(ac)} holds less than that starting amount")
    td = day(a["target_date"], "target_date") if a.get("target_date") else None
    clean = {"name": name, "account_id": ac["id"], "target": target, "target_date": td, "start_amount": start, "notes": str(a.get("notes") or "")[:2000]}
    return clean, (f"Make a piggy bank in Firefly III: \"{name}\" in {label(ac)}" + (f", target {target:,.2f}" if target else "") + (f" by {td}" if td else "")
                   + (f", starting with {start:,.2f}" if start else "") + (f", notes: \"{txt(clean['notes'], 120)}\"" if clean["notes"].strip() else "")), "firefly"


def r_piggy_new(ctx, a):
    return hook(ctx, "piggy_new")(a)


def p_piggy_edit(ctx, a):
    p = piggy(ctx, a.get("piggy_id"))
    ch = {}
    if a.get("name"):
        ch["name"] = txt(a["name"], 255)
    if a.get("target") not in (None, ""):
        ch["target"] = money(a["target"])
        if ch["target"] < 0:
            raise Refused("a target cannot be negative")
    if a.get("target_date") not in (None, ""):
        ch["target_date"] = day(a["target_date"], "target_date")
    if a.get("notes") is not None:
        ch["notes"] = str(a["notes"])[:2000]
    if not ch:
        raise Refused("nothing to change")
    words = ", ".join(f"{k.replace('_', ' ')} to {v:,.2f}" if isinstance(v, float) else f"{k.replace('_', ' ')} to \"{txt(v, 160)}\"" for k, v in ch.items())
    return {"piggy_id": p["id"], **ch}, f"In Firefly III, change the piggy bank {p['name']}: {words}", "firefly"


def r_piggy_edit(ctx, a):
    a = dict(a)
    return hook(ctx, "piggy_edit")(a.pop("piggy_id"), a)


def p_piggy_money(ctx, a):
    p = piggy(ctx, a.get("piggy_id"))
    ac = acct(ctx, a.get("account_id") or (p["accounts"][0]["account_id"] if p["accounts"] else ""))
    amt = money(a.get("amount"))
    if not amt:
        raise Refused("amount cannot be 0")
    if amt > 0 and amt > (ac.get("balance") or 0):
        raise Refused(f"{label(ac)} holds less than that")
    if amt < 0 and -amt > (p.get("current") or 0) + 0.005:
        raise Refused(f"the piggy bank holds only {(p.get('current') or 0):,.2f}")
    return ({"piggy_id": p["id"], "account_id": ac["id"], "amount": amt},
            f"In Firefly III, {'put' if amt > 0 else 'take'} {abs(amt):,.2f} {'into' if amt > 0 else 'out of'} the piggy bank {p['name']} (held in {label(ac)})", "firefly")


def r_piggy_money(ctx, a):
    return hook(ctx, "piggy_money")(a["piggy_id"], a["account_id"], a["amount"])


def p_budget(ctx, a):
    name = txt(a.get("name"), 100)
    amt = money(a.get("amount"))
    if not name or amt <= 0:
        raise Refused("a budget needs a name and an amount above 0")
    m = str(a.get("month") or today(ctx).isoformat()[:7])
    first = date.fromisoformat(day(m + "-01", "month"))
    return {"name": name, "amount": amt, "month": first.isoformat()[:7]}, f"In Firefly III, set the budget \"{name}\" to {amt:,.2f} for {first.strftime('%B %Y')} (made if it does not exist)", "firefly"


def r_budget(ctx, a):
    first = date.fromisoformat(a["month"] + "-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return firefly.set_budget_limit(ctx.writer.ff(), a["name"], a["amount"], first, last)


def p_cd(ctx, a):
    ac = acct(ctx, a.get("account_id"), firefly_only=False)
    from coinpile.api_money import SAVINGS_KINDS
    kind = str(a.get("savings_kind") or "cd")
    if kind not in SAVINGS_KINDS:
        raise Refused(f"savings_kind is one of {', '.join(SAVINGS_KINDS)}")
    clean = {"account_id": ac["id"], "savings_kind": kind}
    if a.get("apy") not in (None, ""):
        clean["apy"] = money(a["apy"])
        if not 0 <= clean["apy"] <= 100:
            raise Refused("apy is a yearly percentage")
    for f in ("opened", "maturity"):
        if a.get(f):
            clean[f] = day(a[f], f)
    if a.get("term_months"):
        try:
            clean["term_months"] = int(a["term_months"])
        except (TypeError, ValueError):
            raise Refused("term_months is a whole number")
        if not 0 < clean["term_months"] <= 1200:
            raise Refused("term_months is a whole number of months")
    bits = [f"{k.replace('_', ' ')} {v}" for k, v in clean.items() if k not in ("account_id", "savings_kind")]
    return clean, f"Mark {label(ac)} as {kind.replace('_', ' ')} in Coinpile" + (f" ({', '.join(bits)})" if bits else ""), "coinpile"


def r_cd(ctx, a):
    with ctx.store.tx() as c:
        if not c.execute("UPDATE accounts SET sv_kind=?, savings_pool=1 WHERE id=?", (a["savings_kind"], a["account_id"])).rowcount:
            raise Refused("that account is no longer there")
        for f in ("apy", "opened", "maturity", "term_months"):
            if f in a:
                c.execute(f"UPDATE accounts SET {f}=? WHERE id=?", (a[f], a["account_id"]))
    return {"account_id": a["account_id"]}


ACTIONS = {
    "write_note": ("Save a note (marked as written by the agent). Give piggy_id when it is about one goal.", S({"title": STR, "body": STR, "piggy_id": STR}, ["title", "body"]), p_note, r_note),
    "add_subscription": ("Add a subscription/recurring bill. in_firefly true also makes it a bill in Firefly III. Needs name, amount and an interval (cycle such as monthly, or every + unit).",
                         S({"name": STR, "amount": NUM, "cycle": STR, "every": INT, "unit": {"type": "string", "enum": list(recur.UNITS)}, "next_date": DATE, "account_id": STR,
                            "payer": {"type": "string", "enum": ["me", "other"]}, "payer_name": STR, "category": STR, "payment_method": STR, "end_date": DATE, "notes": STR, "in_firefly": BOOL}, ["name", "amount"]), p_sub, r_sub),
    "record_transaction": ("Record a transaction in Firefly III (cash spending, a payment the bank has not sent). amount: negative = money out, positive = money in. account_id from list_accounts (ff:...).",
                           S({"account_id": STR, "amount": NUM, "description": STR, "date": DATE, "category": STR}, ["account_id", "amount", "description"]), p_tx, r_tx),
    "set_transaction_category": ("Change a transaction's category in Firefly III (transaction_id from search_transactions). always=true also makes a Firefly III rule for future ones.",
                                 S({"transaction_id": STR, "category": STR, "always": BOOL}, ["transaction_id", "category"]), p_cat, r_cat),
    "categorize_merchant": ("Set the category of every uncategorized payment from one merchant (key from uncategorized_merchants). Goes to Firefly III when the user sends categories "
                            "there, else kept in Coinpile. always=true also makes a Firefly III rule (or remembers the merchant in Coinpile) for future payments.",
                            S({"key": STR, "category": STR, "always": BOOL}, ["key", "category"]), p_merchant, r_merchant),
    "create_piggy_bank": ("Make a piggy bank (a goal) in Firefly III: name, the Firefly III account that holds the money, optional target, target_date, start_amount, notes.",
                          S({"name": STR, "account_id": STR, "target": NUM, "target_date": DATE, "start_amount": NUM, "notes": STR}, ["name", "account_id"]), p_piggy_new, r_piggy_new),
    "update_piggy_bank": ("Change a piggy bank's name, target, target_date or notes in Firefly III (piggy_id from list_goals).",
                          S({"piggy_id": STR, "name": STR, "target": NUM, "target_date": DATE, "notes": STR}, ["piggy_id"]), p_piggy_edit, r_piggy_edit),
    "move_piggy_bank_money": ("Put money into (positive amount) or take it out of (negative) a piggy bank in Firefly III. Some Firefly III versions refuse this; then the user does it in Firefly III.",
                              S({"piggy_id": STR, "amount": NUM, "account_id": STR}, ["piggy_id", "amount"]), p_piggy_money, r_piggy_money),
    "set_budget": ("Set a Firefly III budget's amount for a month (YYYY-MM, default this month); the budget is made if it does not exist.",
                   S({"name": STR, "amount": NUM, "month": STR}, ["name", "amount"]), p_budget, r_budget),
    "update_bank": ("Pull one bank now at the Plaid connector and send what changed to Firefly III (item_id from bank_problems). full=true asks the bank for fresh data first "
                    "(Plaid bills it) and reads the whole history again; use it only when a quick pull did not help.", S({"item_id": STR, "full": {"type": "boolean"}}), p_bank_pull, r_bank_pull),
    "match_bank_balance": ("Make an account's Firefly III balance equal the bank's by moving its opening balance (account_id from bank_problems' balance_differs). Try update_bank first.",
                           S({"account_id": STR}, ["account_id"]), p_bank_match, r_bank_match),
    "mark_account_as_savings": ("Mark an account as a CD (or savings, money_market, bond, hsa, 529...) in Coinpile with its APY, opened, maturity and term.",
                                S({"account_id": STR, "savings_kind": STR, "apy": NUM, "opened": DATE, "maturity": DATE, "term_months": INT}, ["account_id"]), p_cd, r_cd),
}
