"""A small stand-in for the read side of the Firefly III API (v1), shaped like Firefly III 6.7 answers."""
import json

import httpx


class FakeFirefly:
    def __init__(self, users=None):
        self.users = users or {"tok-alice-" + "x" * 30: {"id": "1", "email": "alice@example.com"}}
        self.data = {uid["id"]: {"accounts": [], "groups": [], "bills": [], "piggies": []} for uid in self.users.values()}
        self.calls = []

    def acct(self, uid, id, name, type="asset", role="defaultAsset", balance="0", **kw):
        self.data[uid]["accounts"].append({"type": "accounts", "id": str(id), "attributes": {"name": name, "type": type, "account_role": role, "current_balance": balance,
                                                                                          "currency_code": "USD", "active": True, **kw}})

    def tx(self, uid, jid, type, amount, desc, src, dst, date, category=None):
        self.data[uid]["groups"].append({"type": "transactions", "id": str(jid), "attributes": {"transactions": [{
            "transaction_journal_id": str(jid), "type": type, "amount": f"{amount:.12f}", "description": desc, "date": date + "T00:00:00+00:00",
            "source_id": str(src), "destination_id": str(dst), "source_name": "S", "destination_name": "D", "category_name": category, "currency_code": "USD", "notes": None}]}})

    def _next(self):
        self.n = getattr(self, "n", 1000) + 1
        return str(self.n)

    def write(self, uid, d, method, p, b):
        """Just enough of Firefly III's write side for what Coinpile can send, with its duplicate check."""
        self.writes = getattr(self, "writes", [])
        self.writes.append((method, p, b))
        parts = p.strip("/").split("/")
        if p == "/transactions" and method == "POST":
            leg = b["transactions"][0]
            if b.get("error_if_duplicate_hash") and any(t["attributes"]["transactions"][0].get("external_id") == leg.get("external_id") for t in d["groups"]):
                return httpx.Response(422, json={"message": "Duplicate of transaction #1.", "errors": {"transactions.0.description": ["Duplicate of transaction #1."]}})
            self.tx(uid, self._next(), leg["type"], float(leg["amount"]), leg["description"], leg.get("source_id") or "900", leg.get("destination_id") or "901", leg["date"], leg.get("category_name"))
            d["groups"][-1]["attributes"]["transactions"][0]["external_id"] = leg.get("external_id")
            return httpx.Response(200, json={"data": d["groups"][-1]})
        if parts[0] == "transactions" and method == "PUT":
            g = next(x for x in d["groups"] if x["id"] == parts[1])
            if len(g["attributes"]["transactions"]) > 1 and not b.get("group_title"):
                return httpx.Response(422, json={"message": "A group title is mandatory when there is more than one transaction.", "errors": {"group_title": ["mandatory"]}})
            keep = {str(ch["transaction_journal_id"]) for ch in b["transactions"]}
            g["attributes"]["transactions"] = [t for t in g["attributes"]["transactions"] if t["transaction_journal_id"] in keep]     # like Firefly III: splits left out are deleted
            for ch in b["transactions"]:
                t = next(x for x in g["attributes"]["transactions"] if x["transaction_journal_id"] == str(ch["transaction_journal_id"]))
                t.update({k: v for k, v in ch.items() if k == "category_name"})
            return httpx.Response(200, json={"data": g})
        if p == "/rule-groups" and method == "POST":
            g = {"type": "rule_groups", "id": self._next(), "attributes": {"title": b["title"]}}
            d.setdefault("rule_groups", []).append(g)
            return httpx.Response(200, json={"data": g})
        if p == "/rules" and method == "POST":
            d.setdefault("rules", []).append(b)
            return httpx.Response(200, json={"data": {"id": self._next(), "attributes": b}})
        if p in ("/bills", "/piggy-banks") and method == "POST":
            key = "bills" if p == "/bills" else "piggies"
            att = {**b, "amount_avg": b.get("amount_min"), "date": (b.get("date") or "2026-01-01") + "T00:00:00+00:00", "pay_dates": [], "active": True}
            if key == "piggies":
                att = {"name": b["name"], "target_amount": b["target_amount"], "active": True, "target_date": b.get("target_date"),
                       "accounts": [{"account_id": a["account_id"], "name": "x", "current_amount": a["current_amount"]} for a in b["accounts"]],
                       "current_amount": str(sum(float(a["current_amount"]) for a in b["accounts"]))}
            row = {"type": key, "id": self._next(), "attributes": att}
            d[key].append(row)
            return httpx.Response(200, json={"data": row})
        if parts[0] in ("bills", "piggy-banks"):
            key = "bills" if parts[0] == "bills" else "piggies"
            row = next((x for x in d[key] if x["id"] == parts[1]), None)
            if not row:
                return httpx.Response(404, json={"message": "No query results"})
            if method == "DELETE":
                d[key].remove(row)
                return httpx.Response(204)
            if key == "piggies":
                if getattr(self, "piggy_update_breaks", False):
                    return httpx.Response(500, json={"message": "Internal Firefly III Exception: bcsub(): Argument #2 ($num2) must be of type string, int given"})
                row["attributes"].update({"name": b["name"], "target_amount": b["target_amount"], "accounts": [{"account_id": a["account_id"], "name": "x", "current_amount": a["current_amount"]} for a in b["accounts"]],
                                          "current_amount": str(sum(float(a["current_amount"]) for a in b["accounts"]))})
            else:
                row["attributes"].update({**b, "amount_avg": b["amount_min"], "date": b["date"] + "T00:00:00+00:00"})
            return httpx.Response(200, json={"data": row})
        if p == "/accounts" and method == "POST":
            if any(a["attributes"]["name"] == b["name"] for a in d["accounts"]):
                return httpx.Response(422, json={"message": "x", "errors": {"name": ["This account name is already in use."]}})
            self.acct(uid, self._next(), b["name"], type=b["type"], role=b.get("account_role"), balance=b.get("opening_balance") or "0")
            return httpx.Response(200, json={"data": d["accounts"][-1]})
        return httpx.Response(404, json={"message": "not in the fake"})

    def transport(self):
        def h(req: httpx.Request):
            self.calls.append((req.method, req.url.path, dict(req.url.params)))
            tok = req.headers.get("authorization", "").removeprefix("Bearer ")
            u = self.users.get(tok)
            if not u:
                return httpx.Response(401, json={"message": "Unauthenticated."})
            d, p, q = self.data[u["id"]], req.url.path.removeprefix("/api/v1"), req.url.params
            if req.method != "GET":
                return self.write(u["id"], d, req.method, p, json.loads(req.content) if req.content else {})
            if p == "/about/user":
                return httpx.Response(200, json={"data": {"type": "users", "id": u["id"], "attributes": {"email": u["email"]}}})
            if p == "/accounts":
                t = q.get("type")
                rows = [a for a in d["accounts"] if a["attributes"]["type"] == t]
            elif p.startswith("/transactions/"):
                g = next((x for x in d["groups"] if x["id"] == p.split("/")[2]), None)
                return httpx.Response(200, json={"data": g}) if g else httpx.Response(404, json={"message": "No query results"})
            elif p == "/transactions":
                rows = [g for g in d["groups"] if (not q.get("start") or g["attributes"]["transactions"][0]["date"][:10] >= q["start"]) and g["attributes"]["transactions"][0]["date"][:10] <= q["end"]]
            elif p == "/bills":
                rows = d["bills"]
            elif p == "/chart/account/overview":
                aid = q.get("accounts[]")
                pts = getattr(self, "history", {}).get((u["id"], aid), {})
                name = next((a["attributes"]["name"] for a in d["accounts"] if a["id"] == aid), "?")
                return httpx.Response(200, json=[{"label": name, "currency_code": "USD", "period": "1D", "entries": {k + "T00:00:00+00:00": v for k, v in pts.items()}}])
            elif p == "/rule-groups":
                rows = d.setdefault("rule_groups", [])
            elif p == "/piggy-banks":
                rows = d["piggies"]
            else:
                return httpx.Response(404, json={"message": "not here"})
            per, page = int(q.get("limit", 50)), int(q.get("page", 1))
            chunk = rows[(page - 1) * per: page * per]
            return httpx.Response(200, json={"data": chunk, "meta": {"pagination": {"total": len(rows), "total_pages": max(1, -(-len(rows) // per)), "current_page": page}}})
        return httpx.MockTransport(h)


def add_history(ff, uid, account_id, points: dict):
    """Day -> balance, as Firefly III's /chart/account/overview gives it for one account."""
    ff.history = getattr(ff, "history", {})
    ff.history[(uid, str(account_id))] = points
