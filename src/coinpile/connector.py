"""The Plaid connector (plaid-firefly-connector) for one person: see their linked banks, pull them now, link a new one.

Coinpile talks to the connector's gateway as that person, with the person's name and their connector password or login token (kept encrypted).
Bank data itself still flows bank -> connector -> Firefly III -> Coinpile; this is only the remote control."""
import re

import httpx

from coinpile import netguard


class ConnectorError(Exception):
    pass


class Connector:
    def __init__(self, url: str, name: str, secret: str, transport=None):
        if not (url and name and secret):
            raise ConnectorError("the Plaid connector is not set up: enter its address, your name there and your password in Settings, Banks")
        self.url, self.name, self.secret = url.rstrip("/"), name.strip().lower(), secret
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", self.name):
            raise ConnectorError("your name at the connector is lowercase letters, digits, - and _ only")
        self.c = netguard.client(transport, timeout=60)
        self.token = secret if len(secret) >= 40 and " " not in secret else None     # a long random login token is used as is; anything else is a password

    def _login(self) -> None:
        try:
            r = self.c.post(f"{self.url}/login", json={"name": self.name, "password": self.secret})
        except httpx.HTTPError as e:
            raise ConnectorError(f"could not reach the Plaid connector ({type(e).__name__})")
        if r.status_code != 200:
            raise ConnectorError("the Plaid connector refused that name and password")
        self.token = r.json()["token"]

    def call(self, method: str, path: str, body: dict | None = None):
        for attempt in (0, 1):
            if not self.token:
                self._login()
            try:
                r = self.c.request(method, f"{self.url}/g/{self.name}/{path.lstrip('/')}", json=body, headers={"Authorization": f"Bearer {self.token}"})
            except httpx.HTTPError as e:
                raise ConnectorError(f"could not reach the Plaid connector ({type(e).__name__})")
            if r.status_code == 401 and attempt == 0:
                self.token = None                                   # a session ran out, or it was a password after all: sign in with it
                continue
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code >= 400:
                raise ConnectorError(data.get("detail") if isinstance(data.get("detail"), str) else f"the Plaid connector answered {r.status_code}")
            return data
        raise ConnectorError("the Plaid connector refused the sign-in")

    def banks(self) -> dict:
        """Linked banks and their accounts (names only: the connector's status page carries no balances worth trusting over Firefly III's)."""
        st = self.call("GET", "api/status")
        ff = self.call("GET", "api/firefly/status")
        items = [{"item_id": it.get("item_id"), "institution": it.get("institution"), "status": it.get("status"), "last_sync": it.get("last_sync"),
                  "webhook_live": it.get("webhook_live"), "schedule": it.get("schedule"), "schedule_effective": it.get("schedule_effective"),
                  "recurring": it.get("recurring"), "recurring_effective": it.get("recurring_effective"), "calls_per_day": it.get("calls_per_day"), "category": it.get("category"),
                  "accounts": [{"name": a.get("nickname") or a.get("name"), "mask": a.get("mask")} for a in it.get("accounts", [])]} for it in (st.get("plaid") or {}).get("items", [])]
        return {"items": items, "plaid_configured": (st.get("plaid") or {}).get("configured"), "plaid_env": (st.get("plaid") or {}).get("env"),
                "firefly": {k: ff.get(k) for k in ("configured", "sent", "waiting", "failed", "awaiting_approval", "last_ok", "last_error")}}

    def _item(self, item_id: str) -> str:
        from urllib.parse import quote
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item_id or ""):
            raise ConnectorError("not a bank link id")
        return quote(item_id, safe="")

    def set_item(self, item_id: str, patch: dict) -> dict:
        return self.call("PUT", f"api/plaid/items/{self._item(item_id)}", patch)

    def unlink(self, item_id: str) -> dict:
        return self.call("DELETE", f"api/plaid/items/{self._item(item_id)}")

    def usage(self) -> dict:
        u = self.call("GET", "api/plaid/usage")
        return {k: u.get(k) for k in ("month", "calls_month", "calls_today", "daily_cap", "items", "est_monthly_cost", "budget", "over_budget", "room_for_items", "per_item", "webhook_banks", "webhook_set")}

    def pull(self) -> dict:
        return self.call("POST", "sync", {})

    def link_token(self, item_id: str | None = None) -> dict:
        from urllib.parse import quote
        return self.call("POST", "plaid/link-token" + (f"?item_id={quote(item_id, safe='')}" if item_id else ""), {})

    def exchange(self, public_token: str, institution: str) -> dict:
        return self.call("POST", "plaid/exchange", {"public_token": public_token, "institution": institution})
