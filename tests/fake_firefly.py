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

    def transport(self):
        def h(req: httpx.Request):
            self.calls.append((req.method, req.url.path, dict(req.url.params)))
            tok = req.headers.get("authorization", "").removeprefix("Bearer ")
            u = self.users.get(tok)
            if not u:
                return httpx.Response(401, json={"message": "Unauthenticated."})
            d, p, q = self.data[u["id"]], req.url.path.removeprefix("/api/v1"), req.url.params
            if p == "/about/user":
                return httpx.Response(200, json={"data": {"type": "users", "id": u["id"], "attributes": {"email": u["email"]}}})
            if p == "/accounts":
                t = q.get("type")
                rows = [a for a in d["accounts"] if a["attributes"]["type"] == t]
            elif p == "/transactions":
                rows = [g for g in d["groups"] if (not q.get("start") or g["attributes"]["transactions"][0]["date"][:10] >= q["start"]) and g["attributes"]["transactions"][0]["date"][:10] <= q["end"]]
            elif p == "/bills":
                rows = d["bills"]
            elif p == "/piggy-banks":
                rows = d["piggies"]
            else:
                return httpx.Response(404, json={"message": "not here"})
            per, page = int(q.get("limit", 50)), int(q.get("page", 1))
            chunk = rows[(page - 1) * per: page * per]
            return httpx.Response(200, json={"data": chunk, "meta": {"pagination": {"total": len(rows), "total_pages": max(1, -(-len(rows) // per)), "current_page": page}}})
        return httpx.MockTransport(h)
