"""Sending things from Coinpile into Firefly III. Every part is OFF until the person switches it on (Settings, Firefly III), and nothing is sent except
because of something they did on a Coinpile screen.

  goals          -> piggy banks: each goal becomes a Firefly III piggy bank, and the money set aside for it on a Firefly III account becomes the piggy bank's amount there
  subscriptions  -> bills: a subscription added (or found by the subscription catcher and accepted) becomes a Firefly III bill; editing or deleting it changes the bill
  categories     -> a category set on a transaction is set in Firefly III; "always" also makes a Firefly III rule
  accounts       -> an account added in Coinpile is made in Firefly III, and transactions added to it, or imported from a bank CSV, go there too

What Firefly III has no place for stays in Coinpile: savings APY and maturity, notes, agent chats, nicknames, warnings, who pays a subscription, reminders.
After each change Coinpile reads Firefly III back, so the screens show what Firefly III holds."""
import json
import uuid
from datetime import date

from coinpile import firefly, importers, recur, settings as cfg

RULE_GROUP = "Coinpile"
PARTS = {"piggies": "ff_send_goals", "bills": "ff_send_bills", "categories": "ff_send_categories", "accounts": "ff_send_accounts"}


def bill_freq(every: int, unit: str) -> tuple[str, int] | None:
    """Coinpile's 'every N units' as Firefly III's repeat_freq + skip. None when Firefly III has no such cycle (it has no daily bills)."""
    if unit == "week":
        return "weekly", every - 1
    if unit == "month":
        return {3: ("quarterly", 0), 6: ("half-year", 0), 12: ("yearly", 0)}.get(every, ("monthly", every - 1))
    if unit == "year":
        return "yearly", every - 1
    return None


class Writer:
    def __init__(self, db, ff_factory, after, today=date.today):
        self.db, self.ff, self.after, self.today = db, ff_factory, after, today          # after(): read Firefly III back into the local copy

    def on(self, part: str) -> bool:
        return cfg.get(self.db, PARTS[part]) == "on"

    # ---------------- transactions and categories
    def set_category(self, tid: str, category: str, make_rule: bool) -> dict:
        row = self.db.one("SELECT * FROM transactions WHERE id=?", (tid,))
        if not row or not row.get("ff_group"):
            raise KeyError(tid)
        f = self.ff()
        jid = tid.split(":")[1]
        # Firefly III deletes any split of a group that is left out of an update, so every split goes back, unchanged except this one's category.
        grp = f.get(f"/transactions/{row['ff_group']}")["data"]["attributes"]
        splits = grp["transactions"]
        if not any(str(x["transaction_journal_id"]) == jid for x in splits):
            raise KeyError(tid)
        f.send("PUT", f"/transactions/{row['ff_group']}", {"apply_rules": False, **({"group_title": grp.get("group_title") or row["name"][:255]} if len(splits) > 1 else {}), "transactions": [
            {"transaction_journal_id": str(x["transaction_journal_id"]), **({"category_name": category} if str(x["transaction_journal_id"]) == jid else {})} for x in splits]})
        out = {"id": tid, "category": category, "updated": 1, "in_firefly": True}
        if make_rule:
            words = recur.norm(row["name"]) or row["name"]
            try:
                self._make_rule(f, words, category)
            except firefly.FireflyError as e:
                if "already in use" not in str(e):
                    raise                                       # the same rule exists already: fine
            out["rule"] = f"Firefly III rule: description contains '{words[:60]}', set category {category}"
        with self.db.tx() as c:
            c.execute("UPDATE transactions SET category=? WHERE id=?", (category, tid))
            c.execute("INSERT OR IGNORE INTO categories(name,kind,builtin) VALUES(?,?,0)", (category, "expense"))
        return out

    def _make_rule(self, f, words: str, category: str) -> None:
        f.send("POST", "/rules", {"title": f"{words[:60]} is {category}", "rule_group_id": self._rule_group(f), "trigger": "store-journal", "active": True, "strict": True,
                                  "stop_processing": False, "triggers": [{"type": "description_contains", "value": words[:255], "active": True, "stop_processing": False}],
                                  "actions": [{"type": "set_category", "value": category, "active": True, "stop_processing": False}]})

    def _rule_group(self, f) -> str:
        for g in f.pages("/rule-groups"):
            if g["attributes"].get("title") == RULE_GROUP:
                return str(g["id"])
        return str(f.send("POST", "/rule-groups", {"title": RULE_GROUP, "description": "Rules made from Coinpile", "active": True})["data"]["id"])

    def add_transaction(self, account_id: str, day: str, name: str, amount: float, category: str | None, external_id: str | None = None, check_duplicate: bool = False) -> dict:
        fid = account_id.split(":", 1)[1]
        leg = {"type": "deposit" if amount > 0 else "withdrawal", "date": day, "amount": f"{abs(amount):.2f}", "description": name[:255] or "(no description)",
               "external_id": external_id or f"coinpile:{uuid.uuid4().hex[:16]}", "tags": ["coinpile"]}
        if category:
            leg["category_name"] = category
        if amount > 0:
            leg.update({"destination_id": fid, "source_name": name[:255] or "(unknown)"})
        else:
            leg.update({"source_id": fid, "destination_name": name[:255] or "(unknown)"})
        return self.ff().send("POST", "/transactions", {"error_if_duplicate_hash": check_duplicate, "apply_rules": True, "transactions": [leg]})

    def import_csv(self, account_id: str, text: str, flip: bool) -> dict:
        try:
            source, rows = importers.parse(text)                 # a PayPal, Venmo or Cash App export
            bal = None
        except importers.UnknownFormat:                          # a bad row in one of those is reported as it is, not retried as a bank file
            source = "bank"
            rows, bal = importers.parse_bank(text, flip)
        added = skipped = 0
        errors = []
        for r in rows:
            try:
                self.add_transaction(account_id, r["date"], r["name"], r["amount"], None, external_id="coinpile-csv:" + r["id"].replace(":", "-"), check_duplicate=True)
                added += 1
            except firefly.FireflyError as e:
                if "uplicate" in str(e):
                    skipped += 1                                 # already in Firefly III
                else:
                    errors.append(str(e))
                    if len(errors) >= 5:
                        break
        self.after()
        return {"format": source, "rows": len(rows), "added": added, "already_there": skipped, "errors": errors, "balance_in_file": bal}

    # ---------------- accounts
    def add_account(self, name: str, institution: str | None, kind: str, balance: float | None) -> str:
        full = f"{institution} - {name}" if institution and institution.lower() not in ("manual", "") else name
        today = self.today().isoformat()
        body = {"name": full[:1024], "currency_code": cfg.get(self.db, "currency") or "USD", "active": True, "include_net_worth": True}
        if kind == "credit":
            body.update({"type": "asset", "account_role": "ccAsset", "credit_card_type": "monthlyFull", "monthly_payment_date": today,
                         "opening_balance": f"{-abs(balance or 0):.2f}", "opening_balance_date": today})
        elif kind == "loan":
            body.update({"type": "liabilities", "liability_type": "loan", "liability_direction": "credit", "opening_balance": f"{-abs(balance or 0):.2f}",
                         "opening_balance_date": today, "interest": "0", "interest_period": "monthly"})
        else:
            body.update({"type": "asset", "account_role": "defaultAsset", "opening_balance": f"{balance or 0:.2f}", "opening_balance_date": today})
        d = self.ff().send("POST", "/accounts", body)
        self.after()
        return f"ff:{d['data']['id']}"

    # ---------------- subscriptions <-> bills
    def _bill_body(self, s: dict) -> dict | None:
        fr = bill_freq(int(s.get("every") or 1), s.get("unit") or "month")
        if not fr:
            return None
        native = s.get("native_currency") and s.get("native_amount") is not None
        if native and s.get("source") == "firefly" and s.get("id"):
            # a bill in another currency: Coinpile shows it converted; Firefly III keeps its own amount and currency unless the amount was changed here
            old = s.get("_stored_amount")
            if old is None or abs(float(old) - float(s["amount"])) < 0.005:
                amt_v, cur = float(s["native_amount"]), s["native_currency"]
            else:
                amt_v, cur = float(s["amount"]), s.get("currency") or "USD"
        else:
            amt_v, cur = float(s["amount"]), s.get("currency") or "USD"
        amt = f"{abs(amt_v):.2f}"
        body = {"name": s["name"][:255], "amount_min": amt, "amount_max": amt, "date": s["next_date"], "repeat_freq": fr[0], "skip": fr[1],
                "currency_code": cur, "active": bool(s.get("active", 1)), "notes": s.get("notes") or None}
        if s.get("end_date"):
            body["end_date"] = s["end_date"]
        return body

    def sub_created(self, sid: int) -> None:
        s = self.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
        body = s and self._bill_body(s)
        if not body:
            return                                              # Firefly III has no daily bills: it stays a Coinpile subscription
        d = self.ff().send("POST", "/bills", body)
        with self.db.tx() as c:
            c.execute("UPDATE subscriptions SET source='firefly', plaid_stream_id=? WHERE id=?", (f"ff-bill:{d['data']['id']}", sid))

    def sub_updated(self, sid: int, before: dict | None = None) -> None:
        s = self.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
        if s is not None:
            s = {**s, "_stored_amount": (before or {}).get("amount")}
        if not s:
            return
        if s["source"] != "firefly":
            self.sub_created(sid)                           # a Coinpile-only subscription edited with "send subscriptions" on becomes a bill now
            return
        body = self._bill_body(s)
        if not body:
            raise firefly.FireflyError("Firefly III bills cannot repeat daily: choose weekly or longer")
        self.ff().send("PUT", f"/bills/{s['plaid_stream_id'].split(':')[1]}", body)

    def sub_deleted(self, row: dict) -> None:
        if row and row.get("source") == "firefly" and (row.get("plaid_stream_id") or "").startswith("ff-bill:"):
            self.ff().send("DELETE", f"/bills/{row['plaid_stream_id'].split(':')[1]}")

    # ---------------- goals <-> piggy banks
    def sync_goals(self) -> dict:
        """Every active goal with money set aside on a Firefly III account is a Firefly III piggy bank holding that money there.
        A goal that closes, or is deleted, takes its piggy bank with it. Only piggy banks Coinpile made are ever changed."""
        f, made, changed, removed = self.ff(), 0, 0, 0
        rows = self.db.all("SELECT goal_id, piggy_id, sig FROM ff_goal_piggy")
        links, sigs = {r["goal_id"]: r["piggy_id"] for r in rows}, {r["goal_id"]: r["sig"] for r in rows}
        goals = {g["id"]: g for g in self.db.all("SELECT * FROM goals WHERE status='active' AND coalesce(shared_state,'') <> 'offered'")}
        start = self.today().isoformat()
        for gid, g in goals.items():
            accts = {}
            for a in self.db.all("SELECT place, amount FROM allocations WHERE goal_id=? AND amount>0 AND place LIKE 'a:ff:%'", (gid,)):
                fid = a["place"].split(":")[2]
                accts[fid] = round(accts.get(fid, 0) + a["amount"], 2)
            body = {"name": g["name"][:255], "target_amount": f"{g['target']:.2f}", "start_date": (g.get("created") or start)[:10], "target_date": g.get("deadline"),
                    "notes": "Kept in step by Coinpile (its goal of the same name).", "accounts": [{"account_id": k, "current_amount": f"{v:.2f}"} for k, v in accts.items()],
                    "transaction_currency_code": cfg.get(self.db, "currency") or "USD"}
            sig = json.dumps(body, sort_keys=True)
            if gid in links:
                if sigs.get(gid) == sig:
                    continue                                    # nothing changed for this goal
                try:
                    f.send("PUT", f"/piggy-banks/{links[gid]}", body)
                    with self.db.tx() as c:
                        c.execute("UPDATE ff_goal_piggy SET sig=? WHERE goal_id=?", (sig, gid))
                    changed += 1
                    continue
                except firefly.FireflyError as e:
                    # Firefly III 6.7 can fail to change a piggy bank's amount (an internal error). This piggy bank is Coinpile's own, so replace it instead.
                    try:
                        f.send("DELETE", f"/piggy-banks/{links[gid]}")
                    except firefly.FireflyError:
                        pass
                    with self.db.tx() as c:
                        c.execute("DELETE FROM ff_goal_piggy WHERE goal_id=?", (gid,))
            if not accts:
                continue                                        # nothing on a Firefly III account yet: a piggy bank needs one
            d = f.send("POST", "/piggy-banks", body)
            with self.db.tx() as c:
                c.execute("INSERT OR REPLACE INTO ff_goal_piggy(goal_id, piggy_id, sig) VALUES(?,?,?)", (gid, str(d["data"]["id"]), sig))
            made += 1
        for gid, pid in links.items():
            if gid not in goals:                                # closed or deleted in Coinpile
                try:
                    f.send("DELETE", f"/piggy-banks/{pid}")
                except firefly.FireflyError:
                    pass
                with self.db.tx() as c:
                    c.execute("DELETE FROM ff_goal_piggy WHERE goal_id=?", (gid,))
                removed += 1
        return {"made": made, "changed": changed, "removed": removed}
