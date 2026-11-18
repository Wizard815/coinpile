"""The agent and the Plaid connector: it can see what is wrong (sign-in needed, failed pulls, balances that differ, held batches) and ask to pull a bank
or match a balance; both happen only on the user's yes."""
import json

import httpx

from coinpile import web
from devtools.fake_llm import openai_server
from tests.test_coinpile import TODAY, signed_in, world


def connector():
    log = []

    def h(req: httpx.Request):
        log.append((req.method, req.url.path + (("?" + req.url.query.decode()) if req.url.query else "")))
        if req.url.path == "/login":
            return httpx.Response(200, json={"token": "sess_1"})
        p = req.url.path.removeprefix("/g/alice/")
        if p == "api/status":
            return httpx.Response(200, json={"plaid": {"configured": True, "items": [
                {"item_id": "i1", "institution": "Huntington", "status": "error", "status_detail": "ITEM_LOGIN_REQUIRED: the login details of this item have changed", "last_sync": 1.0, "accounts": []},
                {"item_id": "i2", "institution": "Discover", "status": None, "last_sync": 2.0, "accounts": []}]}})
        if p == "api/firefly/status":
            return httpx.Response(200, json={"configured": True, "sent": 10, "waiting": 0, "failed": 0, "last_error": None})
        if p == "api/plaid/log":
            return httpx.Response(200, json={"log": [{"at": 5.0, "item_id": "i2", "institution": "Discover", "what": "quick update", "ok": False, "detail": "INSTITUTION_NOT_AVAILABLE: try later"}]})
        if p == "api/firefly/accounts":
            return httpx.Response(200, json={"accounts": [
                {"id": "chk", "label": "Huntington - Checking", "kind": "cash", "mode": "link", "ff_id": 1, "balance": 100.0, "ff_balance": "110.00", "drift": True},
                {"id": "crd", "label": "Discover - Card", "kind": "credit", "mode": "create", "ff_id": 2, "balance": 40.0, "ff_balance": "-40.00", "drift": False},
                {"id": "sav", "label": "Huntington - Savings", "kind": "cash", "mode": "link", "ff_id": 3, "balance": 900.0, "ff_balance": "900.00", "drift": False, "held": 120, "ff_seen": 118, "looks_like": ["sav2"]}]})
        if p == "api/plaid/items/i2/pull":
            return httpx.Response(200, json={"ok": True, "added": 3, "modified": 1, "removed": 0, "firefly": {"ok": True, "sent": 3}})
        if p == "api/firefly/accounts/chk/rebalance":
            return httpx.Response(200, json={"changed": True, "difference": -10.0})
        return httpx.Response(404, json={"detail": "no"})
    return httpx.MockTransport(h), log


def rig(tmp_path, script):
    t, clog = connector()
    log = []
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), connector_transport=t,
                       llm_transport=openai_server(script, log), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True})
    c.put("/api/settings", json={"connector_url": "http://connector:8000", "connector_name": "alice", "connector_token": "a long enough password"})
    return c, log, clog


def test_the_agent_sees_what_is_wrong_at_the_connector(tmp_path):
    c, log, clog = rig(tmp_path, [[("bank_problems", {})]])
    cid = c.post("/api/chats").json()["chat"]["id"]
    c.post(f"/api/chats/{cid}/messages", json={"content": "why is my checking balance off?"})
    got = json.loads(next(m for m in log[1]["body"]["messages"] if m["role"] == "tool")["content"])
    hb = {b["bank"]: b for b in got["banks"]}
    assert hb["Huntington"]["needs_sign_in_again"] and "ITEM_LOGIN_REQUIRED" in hb["Huntington"]["problem"]
    assert hb["Discover"]["last_result"]["ok"] is False and "INSTITUTION_NOT_AVAILABLE" in hb["Discover"]["last_result"]["detail"]
    assert [d["account_id"] for d in got["balance_differs"]] == ["chk"]
    assert got["held_from_firefly"][0]["waiting"] == 120 and got["looks_duplicated"][0]["account_id"] == "sav"
    assert "admin" in got["what_you_can_do"]
    assert not any(m == "POST" for m, _ in clog if not _.endswith("/login"))                 # reading only


def test_pulling_a_bank_and_matching_a_balance_wait_for_yes(tmp_path):
    script = [[("update_bank", {"item_id": "i2"}), ("update_bank", {"item_id": "i2", "full": True}), ("match_bank_balance", {"account_id": "chk"}),
               ("match_bank_balance", {"account_id": "crd"}), ("update_bank", {"item_id": "nope"})]]
    c, log, clog = rig(tmp_path, script)
    cid = c.post("/api/chats").json()["chat"]["id"]
    view = c.post(f"/api/chats/{cid}/messages", json={"content": "fix my banks"}).json()
    acts = view["actions"]
    assert [a["tool"] for a in acts] == ["update_bank", "update_bank", "match_bank_balance"]           # the card does not differ, "nope" is no bank: not asked
    assert "Discover" in acts[0]["summary"] and "full" in acts[1]["summary"] and "110.00" in acts[2]["summary"] and "100.00" in acts[2]["summary"]
    assert not any(p.startswith("/g/alice/api/plaid/items") or "rebalance" in p for _, p in clog)    # nothing done yet
    r = c.post(f"/api/agent/actions/{acts[0]['id']}/yes").json()
    assert r["status"] == "done" and r["result"]["added"] == 3
    assert ("POST", "/g/alice/api/plaid/items/i2/pull") in clog and ("POST", "/g/alice/api/plaid/items/i2/pull?full=true") not in clog
    assert c.post(f"/api/agent/actions/{acts[1]['id']}/no").json()["status"] == "declined"
    assert ("POST", "/g/alice/api/plaid/items/i2/pull?full=true") not in clog
    r = c.post(f"/api/agent/actions/{acts[2]['id']}/yes").json()
    assert r["result"] == {"changed": True, "difference": -10.0} and ("POST", "/g/alice/api/firefly/accounts/chk/rebalance") in clog


def test_without_a_connector_the_tools_say_so(tmp_path):
    log = []
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(),
                       llm_transport=openai_server([[("bank_problems", {}), ("update_bank", {"item_id": "i1"})]], log), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True})
    cid = c.post("/api/chats").json()["chat"]["id"]
    view = c.post(f"/api/chats/{cid}/messages", json={"content": "pull my bank"}).json()
    tools = [json.loads(m["content"]) for m in log[1]["body"]["messages"] if m["role"] == "tool"]
    assert "Plaid connector" in tools[0]["error"] and "not asked" in tools[1]["error"] and view["actions"] == []
