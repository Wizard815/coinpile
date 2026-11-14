"""Changes the agent may ask for. Reading is always allowed; every change here is only a request until the person answers yes.

Each action has: a description and argument schema for the model, prepare(ctx, args) -> (clean args, one plain sentence saying exactly what
will happen, where: "firefly" or "coinpile"), and run(ctx, args) -> result. prepare refuses anything invalid before the person is asked, so a
yes always means the change described. The person's yes is the consent: an approved action runs even when the matching "send to Firefly III"
switch is off (those switches are about what Coinpile does on its own)."""
import json
from datetime import date, timedelta

from coinpile import firefly, recur, sync

STR, NUM, INT, DATE, BOOL = {"type": "string"}, {"type": "number"}, {"type": "integer"}, {"type": "string", "description": "YYYY-MM-DD"}, {"type": "boolean"}


class Refused(ValueError):
    pass


def S(props, req=()):
    return {"type": "object", "properties": props, "required": list(req)}


def money(x) -> float:
    try:
        v = round(float(x), 2)
    except (TypeError, ValueError):
        raise Refused("an amount must be a number")
    return v


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
    title, body = str(a.get("title", "")).strip()[:120], str(a.get("body", ""))[:20000]
    if not title:
        raise Refused("a note needs a title")
    pid = str(a.get("piggy_id") or "") or None
    p = piggy(ctx, pid) if pid else None
    return {"title": title, "body": body, "piggy_id": pid}, f"Save a note \"{title}\"" + (f" on the piggy bank {p['name']}" if p else "") + f" ({len(body)} characters)", "coinpile"


def r_note(ctx, a):
    ts = sync.now()
    with ctx.store.tx() as c:
        cur = c.execute("INSERT INTO notes(title,body,author,pinned,piggy_id,created,updated) VALUES(?,?,?,?,?,?,?)", (a["title"], a["body"], "agent", 0, a["piggy_id"], ts, ts))
    return {"note_id": cur.lastrowid}


def p_sub(ctx, a):
    every, unit = recur.resolve(a.get("every"), a.get("unit"), a.get("cycle") or (None if a.get("every") else "monthly"))
    name, amt = str(a.get("name", "")).strip(), money(a.get("amount"))
    if not name or amt <= 0:
        raise Refused("a subscription needs a name and an amount above 0")
    clean = {k: a[k] for k in ("account_id", "payer", "payer_name", "category", "payment_method", "end_date", "notes") if a.get(k) not in (None, "")}
    clean.update({"name": name, "amount": amt, "every": every, "unit": unit, "next_date": day(a.get("next_date") or date.today().isoformat(), "next_date"),
                  "in_firefly": bool(a.get("in_firefly"))})
    where = "a Coinpile subscription" + (" and a bill in Firefly III" if clean["in_firefly"] else "")
    return clean, f"Add {where}: {name}, {amt:,.2f} {recur.label(every, unit)}, next on {clean['next_date']}", "firefly" if clean["in_firefly"] else "coinpile"


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
    desc = str(a.get("description", "")).strip()[:255]
    if not desc:
        raise Refused("a description is needed")
    d = day(a.get("date") or date.today().isoformat())
    cat = str(a.get("category") or "").strip()[:60] or None
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
    cat = str(a.get("category") or "").strip()[:60]
    if not cat:
        raise Refused("a category is needed")
    always = bool(a.get("always"))
    return ({"transaction_id": t["id"], "category": cat, "always": always},
            f"In Firefly III, set the category of \"{t['name']}\" ({t['amount']:,.2f} on {t['date']}) to {cat}" + (" and make a rule so future ones get it too" if always else ""), "firefly")


def r_cat(ctx, a):
    out = ctx.writer.set_category(a["transaction_id"], a["category"], a["always"])
    return {"category": out["category"], "rule": out.get("rule")}


def p_piggy_new(ctx, a):
    name = str(a.get("name", "")).strip()[:255]
    if not name:
        raise Refused("a piggy bank needs a name")
    ac = acct(ctx, a.get("account_id"))
    target = money(a["target"]) if a.get("target") not in (None, "") else None
    start = money(a.get("start_amount") or 0)
    if (target is not None and target < 0) or start < 0:
        raise Refused("amounts cannot be negative")
    td = day(a["target_date"], "target_date") if a.get("target_date") else None
    clean = {"name": name, "account_id": ac["id"], "target": target, "target_date": td, "start_amount": start, "notes": str(a.get("notes") or "")[:2000]}
    return clean, (f"Make a piggy bank in Firefly III: \"{name}\" in {label(ac)}" + (f", target {target:,.2f}" if target else "") + (f" by {td}" if td else "")
                   + (f", starting with {start:,.2f}" if start else "")), "firefly"


def r_piggy_new(ctx, a):
    return hook(ctx, "piggy_new")(a)


def p_piggy_edit(ctx, a):
    p = piggy(ctx, a.get("piggy_id"))
    ch = {}
    if a.get("name"):
        ch["name"] = str(a["name"]).strip()[:255]
    if a.get("target") not in (None, ""):
        ch["target"] = money(a["target"])
    if a.get("target_date") not in (None, ""):
        ch["target_date"] = day(a["target_date"], "target_date")
    if a.get("notes") is not None:
        ch["notes"] = str(a["notes"])[:2000]
    if not ch:
        raise Refused("nothing to change")
    words = ", ".join(f"{k.replace('_', ' ')} to {v:,.2f}" if isinstance(v, float) else f"{k.replace('_', ' ')} to \"{v}\"" for k, v in ch.items())
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
    return ({"piggy_id": p["id"], "account_id": ac["id"], "amount": amt},
            f"In Firefly III, {'put' if amt > 0 else 'take'} {abs(amt):,.2f} {'into' if amt > 0 else 'out of'} the piggy bank {p['name']} (held in {label(ac)})", "firefly")


def r_piggy_money(ctx, a):
    return hook(ctx, "piggy_money")(a["piggy_id"], a["account_id"], a["amount"])


def p_budget(ctx, a):
    name = str(a.get("name", "")).strip()[:100]
    amt = money(a.get("amount"))
    if not name or amt <= 0:
        raise Refused("a budget needs a name and an amount above 0")
    m = str(a.get("month") or date.today().isoformat()[:7])
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
        clean["term_months"] = int(a["term_months"])
    bits = [f"{k.replace('_', ' ')} {v}" for k, v in clean.items() if k not in ("account_id", "savings_kind")]
    return clean, f"Mark {label(ac)} as {kind.replace('_', ' ')} in Coinpile" + (f" ({', '.join(bits)})" if bits else ""), "coinpile"


def r_cd(ctx, a):
    with ctx.store.tx() as c:
        c.execute("UPDATE accounts SET sv_kind=?, savings_pool=1 WHERE id=?", (a["savings_kind"], a["account_id"]))
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
    "create_piggy_bank": ("Make a piggy bank (a goal) in Firefly III: name, the Firefly III account that holds the money, optional target, target_date, start_amount, notes.",
                          S({"name": STR, "account_id": STR, "target": NUM, "target_date": DATE, "start_amount": NUM, "notes": STR}, ["name", "account_id"]), p_piggy_new, r_piggy_new),
    "update_piggy_bank": ("Change a piggy bank's name, target, target_date or notes in Firefly III (piggy_id from list_goals).",
                          S({"piggy_id": STR, "name": STR, "target": NUM, "target_date": DATE, "notes": STR}, ["piggy_id"]), p_piggy_edit, r_piggy_edit),
    "move_piggy_bank_money": ("Put money into (positive amount) or take it out of (negative) a piggy bank in Firefly III. Some Firefly III versions refuse this; then the user does it in Firefly III.",
                              S({"piggy_id": STR, "amount": NUM, "account_id": STR}, ["piggy_id", "amount"]), p_piggy_money, r_piggy_money),
    "set_budget": ("Set a Firefly III budget's amount for a month (YYYY-MM, default this month); the budget is made if it does not exist.",
                   S({"name": STR, "amount": NUM, "month": STR}, ["name", "amount"]), p_budget, r_budget),
    "mark_account_as_savings": ("Mark an account as a CD (or savings, money_market, bond, hsa, 529...) in Coinpile with its APY, opened, maturity and term.",
                                S({"account_id": STR, "savings_kind": STR, "apy": NUM, "opened": DATE, "maturity": DATE, "term_months": INT}, ["account_id"]), p_cd, r_cd),
}
