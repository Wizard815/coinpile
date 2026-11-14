"""A joint bank account that is in both people's Firefly III, tied together between their two Coinpiles."""
import time

from tests.test_coinpile import ALICE, BOB, app, signed_in, world


def wait(cond, n=60):
    for _ in range(n):
        if cond():
            return True
        time.sleep(0.05)
    return False


def two_fireflies():
    """The Plaid connector's "Joint with" sent the same Ally joint account (••5555) into both Firefly III."""
    ff = world()
    ff.acct("1", 21, "Ally - Joint Checking ••5555", balance="1000")
    ff.acct("2", 31, "Ally - Joint Checking ••5555", balance="1000")
    ff.acct("2", 32, "Bob's savings ••7777", role="savingAsset", balance="50")
    ff.tx("1", 50, "transfer", 300, "To joint", 1, 21, "2026-11-03")              # alice moves 300 in from her own checking
    ff.tx("2", 51, "transfer", 100, "To joint", 11, 31, "2026-11-04")             # bob moves 100 in from his
    ff.tx("1", 52, "withdrawal", 75, "ALLY BANK TRANSFER", 1, 98, "2026-11-05")   # as a bank feed brings it: out of her checking ...
    ff.tx("1", 53, "deposit", 75, "Transfer from Huntington", 99, 21, "2026-11-06")  # ... and into the joint account a day later
    ff.tx("1", 54, "deposit", 40, "Zelle from Bob", 99, 21, "2026-11-07")          # bob's money shows in alice's copy too, but is not hers
    return ff


def linked(tmp_path, ff):
    a = app(tmp_path, ff)
    alice, bob = signed_in(a, ALICE), signed_in(a, BOB)
    alice.post("/api/links", json={"peer": "bob"})
    bob.post("/api/links/alice/accept", json={})
    assert wait(lambda: alice.get("/api/links").json()["links"][0]["status"] == "active")
    return alice, bob


def test_a_joint_account_is_linked_counted_by_share_and_settled_up(tmp_path):
    ff = two_fireflies()
    alice, bob = linked(tmp_path, ff)
    nw_before = alice.get("/api/overview").json()["net_worth"]
    j = alice.post("/api/joint", json={"account_id": "ff:21", "peer": "bob", "my_share": 50}).json()
    assert j["state"] == "offered" and j["my_share"] == 50
    assert wait(lambda: bob.get("/api/joint").json())
    offer = bob.get("/api/joint").json()[0]
    assert offer["state"] == "pending" and offer["my_share"] == 50 and "Joint Checking" in offer["their_account"]
    assert offer["suggestions"][0]["id"] == "ff:31"                                                    # same last digits come first
    assert bob.post(f"/api/joint/{offer['id']}/accept", json={"account_id": "ff:31"}).json()["state"] == "active"
    assert wait(lambda: alice.get("/api/joint").json()[0]["state"] == "active")
    # each side counts half of it now (its Firefly III balance stays what the bank says)
    assert round(nw_before - alice.get("/api/overview").json()["net_worth"], 2) == 500
    assert next(a for a in alice.get("/api/accounts").json() if a["id"] == "ff:21")["joint_with"] == "bob"
    # both send what they moved in, read from their own Firefly III: alice 300 + 75, bob 100 -> 237.50 each would be even, so bob owes alice 137.50
    alice.post("/api/links/flush", json={}); bob.post("/api/links/flush", json={})
    assert wait(lambda: alice.get("/api/joint").json()[0]["this_month"]["they_put_in"] is not None)
    va, vb = alice.get("/api/joint").json()[0], bob.get("/api/joint").json()[0]
    month = va["this_month"]["month"]
    if month == "2026-11":                                                                              # the contributions are this month's
        assert va["this_month"]["you_put_in"] == 375 and va["this_month"]["they_put_in"] == 100 and va["this_month"]["settle"] == "bob owes you 137.50"
        assert vb["this_month"]["settle"] == "you owe alice 137.50"
    assert va["balance_check"]["in_step"] is True and vb["balance_check"]["yours"] == 1000
    # one Firefly III falls behind: both are told
    ff.data["2"]["accounts"][-2]["attributes"]["current_balance"] = "950"
    bob.post("/api/refresh"); bob.post("/api/links/flush", json={})
    assert wait(lambda: alice.get("/api/joint").json()[0]["balance_check"].get("in_step") is False)
    assert "different balances" in alice.get("/api/joint").json()[0]["balance_check"]["note"]
    # either can stop it: both go back to counting the whole account, nothing changes in Firefly III
    writes = len(getattr(ff, "writes", []))
    assert bob.delete(f"/api/joint/{vb['id']}").status_code == 200
    assert wait(lambda: not alice.get("/api/joint").json())
    assert next(a for a in alice.get("/api/accounts").json() if a["id"] == "ff:21")["joint_with"] is None
    assert len(getattr(ff, "writes", [])) == writes


def test_joint_needs_a_link_and_a_sensible_share(tmp_path):
    ff = two_fireflies()
    a = app(tmp_path, ff)
    alice = signed_in(a, ALICE)
    assert alice.post("/api/joint", json={"account_id": "ff:21", "peer": "bob"}).status_code == 422                 # not linked yet
    alice, bob = linked(tmp_path / "x", two_fireflies())
    assert alice.post("/api/joint", json={"account_id": "ff:21", "peer": "bob", "my_share": 100}).status_code == 422
    assert alice.post("/api/joint", json={"account_id": "ff:404", "peer": "bob"}).status_code == 404
    assert alice.post("/api/joint", json={"account_id": "ff:7", "peer": "bob"}).status_code == 200                  # a loan can be joint too


def test_the_agent_sees_linked_joint_accounts_in_settle_up(tmp_path):
    from coinpile import agent, links
    import glob, types
    from coinpile.db import DB
    from tests.test_coinpile import TODAY
    ff = two_fireflies()
    alice, bob = linked(tmp_path, ff)
    alice.post("/api/joint", json={"account_id": "ff:21", "peer": "bob"})
    assert wait(lambda: bob.get("/api/joint").json())
    bob.post(f"/api/joint/{bob.get('/api/joint').json()[0]['id']}/accept", json={"account_id": "ff:31"})
    assert wait(lambda: alice.get("/api/joint").json()[0]["state"] == "active")
    dbs = [DB(p) for p in glob.glob(str(tmp_path / "people" / "*.db"))]
    db = next(d for d in dbs if d.one("SELECT 1 FROM joint_accounts WHERE account_id='ff:21'"))
    tools = {n: f for n, _, _, f in agent.build_tools(types.SimpleNamespace(store=db, today=lambda: TODAY))}
    s = tools["shared_expenses"]({})
    assert s["linked_joint_accounts"][0]["peer"] == "bob" and s["linked_joint_accounts"][0]["my_share"] == 50


def test_joint_accounts_at_the_plaid_connector_are_asked_and_answered_from_coinpile(tmp_path):
    import json
    import httpx
    from coinpile import web
    from tests.test_coinpile import TODAY
    seen = []

    def h(req: httpx.Request):
        if req.url.path == "/login":
            return httpx.Response(200, json={"token": "sess_1"})
        p = req.url.path.removeprefix("/g/alice/")
        seen.append((req.method, p, json.loads(req.content) if req.content else None))
        if p == "api/joint" and req.method == "GET":
            return httpx.Response(200, json={"mine": [], "incoming": [{"sender": "bob", "account_id": "plaid-joint", "name": "Joint Checking", "institution": "Ally", "mask": "5555", "state": "asked"}],
                                             "people": ["bob"], "my_accounts": [{"id": "plaid-chk", "label": "Huntington - Checking ••4821"}]})
        return httpx.Response(200, json={"ok": True})
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), connector_transport=httpx.MockTransport(h), today=lambda: TODAY)
    c = signed_in(a, ALICE)
    c.put("/api/settings", json={"connector_url": "http://connector.local", "connector_name": "alice", "connector_token": "a long enough password"})
    j = c.get("/api/banks/joint").json()
    assert j["incoming"][0]["sender"] == "bob" and j["people"] == ["bob"]
    assert c.post("/api/banks/joint", json={"account_id": "plaid-chk", "peer": "Bob"}).status_code == 200
    assert ("POST", "api/joint/plaid-chk/ask", {"peer": "bob"}) in seen
    # accepting into an account alice already has in her Firefly III: Coinpile turns ff:3 into Firefly III's own id
    assert c.post("/api/banks/joint/bob/plaid-joint/accept", json={"mode": "link", "account_id": "ff:3"}).status_code == 200
    assert ("POST", "api/joint/from/bob/plaid-joint/accept", {"mode": "link", "ff_id": 3}) in seen
    assert c.post("/api/banks/joint/bob/plaid-joint/accept", json={"mode": "link", "account_id": "ff:404"}).status_code == 422
    assert c.post("/api/banks/joint/bob/plaid-joint/decline").status_code == 200 and c.delete("/api/banks/joint/plaid-chk").status_code == 200
    assert c.post("/api/banks/joint/bob/plaid-joint/maybe").status_code == 502                     # only accept, decline or leave reach the connector
    assert c.post("/api/banks/joint/bob/plaid joint/accept", json={"mode": "create"}).status_code in (404, 502)


def test_what_others_share_reaches_only_a_local_agent(tmp_path):
    import json
    from devtools.fake_llm import openai_server
    from coinpile import web
    from tests.test_coinpile import TODAY
    ff = two_fireflies()
    log = []
    script = [[("shared_with_me", {}), ("shared_expenses", {})]] * 3
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), llm_transport=openai_server(script, log), today=lambda: TODAY)
    alice, bob = signed_in(a, ALICE), signed_in(a, BOB)
    alice.post("/api/links", json={"peer": "bob"}); bob.post("/api/links/alice/accept", json={})
    assert wait(lambda: alice.get("/api/links").json()["links"][0]["status"] == "active")
    bob.put("/api/links/alice/shares", json={"kind": "account", "ref": "ff:32", "on": True})          # bob shows alice his savings
    alice.post("/api/joint", json={"account_id": "ff:21", "peer": "bob"})
    assert wait(lambda: bob.get("/api/joint").json())
    bob.post(f"/api/joint/{bob.get('/api/joint').json()[0]['id']}/accept", json={"account_id": "ff:31"})
    alice.post("/api/links/flush", json={}); bob.post("/api/links/flush", json={})
    assert wait(lambda: alice.get("/api/sharing").json()[0]["from_them"] and alice.get("/api/joint").json()[0]["this_month"]["they_put_in"] is not None)

    def ask(base_url, allow_cloud=False):
        log.clear()
        pid = alice.post("/api/agent/providers", json={"name": base_url, "kind": "openai", "base_url": base_url, "model": "m", "tools": True, "allow_cloud": allow_cloud}).json()["id"]
        alice.post("/api/agent/chat", json={"provider_id": pid, "messages": [{"role": "user", "content": "what does bob share?"}]})
        return [json.loads(m["content"]) for m in log[1]["body"]["messages"] if m["role"] == "tool"]
    sw, se = ask("http://localhost:11434/v1")                                                            # a model on my own machine
    assert sw["people"][0]["they_show_you"][0]["balance"] == 50 and sw["people"][0]["they_show_you"][0]["name"].startswith("Bob's savings")
    assert se["linked_joint_accounts"][0]["this_month"]["they_put_in"] == 100
    sw, se = ask("https://api.example-cloud.com/v1", allow_cloud=True)                                   # a cloud model: only my own side
    assert sw["shared_with_me"].startswith("withheld") and "people" in sw and "Bob's savings" not in json.dumps(sw)
    assert "they_put_in" not in json.dumps(se) and se["linked_joint_accounts"][0]["this_month"]["you_put_in"] is not None
    tok = alice.post("/api/tokens", json={"name": "Orb", "scopes": ["mcp"]}).json()["token"]           # another app over MCP: the same
    from fastapi.testclient import TestClient
    r = TestClient(alice.app).post("/api/v1/mcp", headers={"Authorization": "Bearer " + tok}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "shared_with_me", "arguments": {}}}).json()
    assert "withheld" in r["result"]["content"][0]["text"] and "Bob's savings" not in r["result"]["content"][0]["text"]
