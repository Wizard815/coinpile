"""Edge cases an audit of the agent found: a calculator that cannot stall the server, a web reader that stays on the public internet, other
people's numbers that do not leak round the edges, a figure check that is harder to fool, and questions that say what a Yes will do."""
import gzip
import json
import time
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from coinpile import agent, agent_actions, netguard, plan, websearch
from coinpile.agent import calculate, check_args, figures, is_local, known_numbers, known_percent, unverified
from coinpile.db import DB
from tests.test_agent_actions import agent_app, results, searx, world
from tests.test_coinpile import ALICE, BOB, TODAY, app, signed_in
from tests.test_joint_accounts import two_fireflies, wait


# ------------------------------------------------------------------ calculator
@pytest.mark.parametrize("expr", ["fv(2, 60000000, 1)", "pmt(2, 60000000, 1)", "compound(2, 100, 99999999, 12)", "(10**9)**1000", "10**400", "9**9**9", "(-8)**0.5", "1/0", "[1, 2]",
                                  "(" * 300 + "1" + ")" * 300])
def test_the_calculator_refuses_what_it_cannot_do_quickly(expr):
    t = time.time()
    with pytest.raises(ValueError):
        calculate(expr)
    assert time.time() - t < 0.5


def test_commas_are_thousands_separators_only_where_that_is_certain():
    assert calculate("1,200 * 12") == 14400 and calculate("$1,200.50 + 300") == 1500.5 and calculate("(1,200 + 300) * 2") == 3000
    assert calculate("sum([100, 200, 300])") == 600 and round(calculate("pmt(0.01, 36, 5000, 100)"), 2) == 168.39
    for ambiguous in ("sum([100,200,300])", "max(1,200)", "pmt(0.01,36,5000,100)", "sum([12.5,300])"):       # never a silently wrong answer
        with pytest.raises(ValueError, match="ambiguous"):
            calculate(ambiguous)
    assert calculate("10 % 3") == 1 and calculate("200 * 5%") == 10 and calculate("50%") == 0.5
    assert calculate("pmt(1e-17, 12, 1200)") == 100                                                       # a rate that is as good as zero


# ------------------------------------------------------------------ where a model runs
@pytest.mark.parametrize("url,local", [("http://localhost:11434/v1", True), ("http://ollama:11434", True), ("http://192.168.1.5:1234/v1", True), ("http://[::1]:8080", True),
                                       ("http://134744072/v1", False), ("http://0x08080808/v1", False), ("http://01002004010/v1", False), ("https://openrouter。ai/api/v1", False),
                                       ("https://openrouter．ai/api/v1", False), ("https://api.openai.com/v1", False), ("http://[::ffff:8.8.8.8]/", False),
                                       ("http://user@localhost.evil.com/", False), ("", False)])
def test_local_is_decided_on_where_the_address_leads(url, local):
    assert is_local(url) is local


def test_a_local_looking_name_that_resolves_to_a_public_address_is_not_local(monkeypatch):
    import socket
    agent._local_seen.clear()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 0))])
    assert is_local("http://ollama:11434") is False and is_local("http://nas.lan:11434") is False
    agent._local_seen.clear()


# ------------------------------------------------------------------ reading a web page
class Slow(httpx.SyncByteStream):
    def __init__(self, data, delay=0.0):
        self.data, self.delay = data, delay

    def __iter__(self):
        for i in range(0, len(self.data), 8192):
            time.sleep(self.delay)
            yield self.data[i:i + 8192]


def page(data, enc=None, delay=0.0):
    return httpx.MockTransport(lambda req: httpx.Response(200, stream=Slow(data, delay), headers={"content-type": "text/html", **({"content-encoding": enc} if enc else {})}))


def test_a_page_cannot_unfold_in_memory_or_hold_a_request(monkeypatch):
    bomb = gzip.compress(b"<p>" + b"A" * 80_000_000 + b"</p>")
    assert len(bomb) < 200_000
    t = time.time()
    with pytest.raises(websearch.SearchError, match="too large"):
        websearch.read_page("https://x.example/", page(bomb, "gzip"))
    assert time.time() - t < 2
    assert websearch.read_page("https://x.example/", page(gzip.compress(b"<title>T</title><p>CD 1.93%</p>"), "gzip"))["text"] == "CD 1.93%"
    with pytest.raises(websearch.SearchError, match="cannot be read safely"):
        websearch.read_page("https://x.example/", page(b"zz", "br"))
    monkeypatch.setattr(websearch, "TOTAL_SECONDS", 0.5)
    t = time.time()
    with pytest.raises(websearch.SearchError, match="too long"):
        websearch.read_page("https://x.example/", page(b"y" * 200_000, delay=0.2))
    assert time.time() - t < 3


@pytest.mark.parametrize("ip", ["192.168.1.1", "10.0.0.5", "172.17.0.1", "127.0.0.1", "169.254.169.254", "100.100.100.200", "fd00:ec2::254", "::1", "::ffff:10.0.0.1"])
def test_a_page_from_the_internet_may_only_lead_to_the_public_internet(ip):
    assert netguard.bad_ip(ip, public_only=True)
    with pytest.raises(netguard.Blocked):
        netguard.check(f"http://[{ip}]/" if ":" in ip else f"http://{ip}/router", resolve=False, public_only=True)


def test_typed_addresses_may_be_on_the_home_network_but_not_cloud_metadata():
    assert not netguard.bad_ip("192.168.1.5") and not netguard.bad_ip("8.8.8.8", public_only=True)
    assert all(netguard.bad_ip(ip) for ip in ("100.100.100.200", "192.0.0.192", "fd00:ec2::254", "169.254.169.254"))
    for name in ("http://router/", "http://nas.lan/", "http://localhost/"):
        with pytest.raises(netguard.Blocked):
            netguard.check(name, resolve=False, public_only=True)


def test_a_redirect_into_the_home_network_is_not_followed(monkeypatch):
    """Over real sockets: a public-looking name resolves to a server on this machine; the reader refuses it, as it would after a redirect."""
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ["127.0.0.1"])
    with pytest.raises(websearch.SearchError, match="Blocked|ConnectError"):
        websearch.read_page("http://news.example.com/article")


# ------------------------------------------------------------------ other people's numbers
def shared_goal_db():
    db = DB(":memory:")
    with db.tx() as c:
        c.execute("INSERT INTO accounts(id,source,institution,name,kind,balance,savings_pool) VALUES('s','manual','Bank','Savings','cash',5000,1)")
        gid = c.execute("INSERT INTO goals(name,target,status,priority,shared_id,shared_state,members,created) VALUES('Trip',3000,'active',2,'sh1','active',?,'2026-01-01')", (json.dumps(["bob"]),)).lastrowid
        c.execute("INSERT INTO goal_partners(goal_id,peer,amount) VALUES(?, 'bob', 37.5)", (gid,))
    return db


def test_what_is_still_needed_on_a_shared_goal_does_not_give_away_the_other_persons_amount():
    db = shared_goal_db()
    full, own = plan.goals_view(db, TODAY)["items"][0], plan.goals_view(db, TODAY, own_only=True)["items"][0]
    assert full["shortfall"] == 2962.5 and own["shortfall"] == 3000 and own["pct"] == 0 and own["team_allocated"] == 0
    from coinpile import external, ledger
    assert "2,962.50" not in json.dumps(ledger.alerts(db, TODAY)) and "not fully funded" in json.dumps(ledger.alerts(db, TODAY))
    assert external.data(db, "goals", TODAY, {})["goals"][0]["shortfall"] == 3000


def test_switching_a_chat_to_a_cloud_model_leaves_out_what_a_local_model_saw(tmp_path):
    from devtools.fake_llm import openai_server
    from coinpile import web
    log = []
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=two_fireflies().transport(),
                       llm_transport=openai_server([[("shared_with_me", {})]] * 2, log), today=lambda: TODAY)
    alice, bob = signed_in(a, ALICE), signed_in(a, BOB)
    alice.post("/api/links", json={"peer": "bob"}); bob.post("/api/links/alice/accept", json={})
    assert wait(lambda: alice.get("/api/links").json()["links"][0]["status"] == "active")
    bob.put("/api/links/alice/shares", json={"kind": "account", "ref": "ff:32", "on": True})
    alice.post("/api/links/flush", json={}); bob.post("/api/links/flush", json={})
    assert wait(lambda: alice.get("/api/sharing").json()[0]["from_them"])
    local = alice.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "m", "tools": True}).json()["id"]
    cloud = alice.post("/api/agent/providers", json={"name": "Cloud", "kind": "openai", "base_url": "https://api.example-cloud.com/v1", "model": "m", "tools": True, "allow_cloud": True}).json()["id"]
    cid = alice.post("/api/chats", json={}).json()["chat"]["id"]
    r = alice.post(f"/api/chats/{cid}/messages", json={"content": "what does bob share with me?", "provider_id": local}).json()
    assert "Bob's savings" in r["messages"][-1]["content"]
    log.clear()
    alice.post(f"/api/chats/{cid}/messages", json={"content": "and in short?", "provider_id": cloud})
    sent = json.dumps([e["body"] for e in log if "example-cloud" in e["url"]])
    assert sent != "[]" and "Bob's savings" not in sent and "left out here" in sent
    alice.post(f"/api/chats/{cid}/compact")                                         # a summary made by the cloud model gets no local answers either
    assert "Bob's savings" not in json.dumps([e["body"] for e in log if "example-cloud" in e["url"]])


# ------------------------------------------------------------------ the figure check
def test_made_up_figures_are_caught():
    src = [{"net_worth": 52340.17, "share": 0.013}]
    k, pc = known_numbers(*src), known_percent(*src)
    assert unverified("Net worth $52,340.17, that is 1.3%", k, pc) == []
    assert unverified("$5,234,017 or $523.40", k, pc) == ["$5,234,017", "$523.40"]                          # a known value times or divided by 100
    assert unverified("on the 17th you keep $2,026, about 17% more", k, pc, plain=[17.0, 11.0, 2026.0]) == ["$2,026", "17%"]   # today's date is not money
    assert unverified("due on 17", k, pc, plain=[17.0]) == []
    for text, flagged in (("USD98765.43", 98765.43), ("_98765.43_", 98765.43), ("2050 dollars", 2050), ("9 million dollars", 9e6), ("1e6 dollars", 1e6), ("$45/12", 45)):
        assert flagged in [v for _, v in figures(text)], text
    for fine in ("Max out your 401k first", "open a 529 plan", "Form 1040 and a 1099-INT", "your W-2", "paid 15.11.2026", "in 2027"):
        assert figures(fine) == [], fine
    assert figures("$1,099 and $401")[0][1] == 1099 and len(figures("$1,099 and $401")) == 2


def test_an_error_that_repeats_the_models_own_words_is_not_a_source(tmp_path):
    log = []
    c = agent_app(tmp_path, world(), [[("pay_out_31337", {})]], log)
    from devtools.fake_llm import openai_server                                        # noqa: F401
    out = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "how much?"}]}).json()
    assert out["trace"][0]["ok"] is False
    assert "31337" not in [str(int(v)) for v in known_numbers([])]
    assert agent.checked_reply("You get $31,337", [], [{"role": "user", "content": "how much?"}], "", TODAY) == ["$31,337"]


def test_bad_numbers_from_a_model_are_refused_before_any_tool_runs(tmp_path):
    num = {"properties": {"x": {"type": "number"}, "n": {"type": "integer"}}}
    a = {"x": "$5,000", "n": "12"}
    assert check_args(num, a) is None and a == {"x": 5000.0, "n": 12}                    # a number in words the tool can use
    for bad in ("nan", "inf", "-inf", float("inf"), float("nan"), 1e300, "banana"):
        assert check_args(num, {"x": bad}) is not None
    log = []
    c = agent_app(tmp_path, world(), [[("can_i_afford", {"amount": "inf"}), ("calculate", {"expression": "(10**9)**1000"}), ("loan_payoff", {"balance": 100000, "apr": 12, "payment": 1000.0001})]], log)
    r = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and [t["ok"] for t in r.json()["trace"][:3]] == [False, False, False]
    assert "100 years" in results(r.json(), log)["loan_payoff"]["error"]


@pytest.mark.parametrize("answer", [httpx.Response(200, text="<html>hello</html>"), httpx.Response(200, json={"error": "overloaded"}), httpx.Response(200, json={"choices": []}),
                                    httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{"id": "1"}]}}]}), httpx.Response(200, json=[1, 2])])
def test_a_model_server_that_answers_nonsense_is_a_plain_error(tmp_path, answer):
    from coinpile import web
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), llm_transport=httpx.MockTransport(lambda req: answer), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True})
    r = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502 and "not a chat reply" in r.json()["detail"]


def test_parts_and_ready_made_arguments_from_a_model_are_understood(tmp_path):
    from coinpile import web
    calls = iter([httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{"id": "a", "function": {"name": "calculate", "arguments": {"expression": "2 + 2"}}}]}}]}),
                  httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "It is 4."}]}}]})])
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), llm_transport=httpx.MockTransport(lambda req: next(calls) if req.url.path.endswith("/chat/completions") else httpx.Response(404)), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True})
    assert c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "2+2?"}]}).json()["reply"] == "It is 4."


# ------------------------------------------------------------------ questions the person answers
def test_the_question_shows_what_a_yes_will_do(tmp_path):
    log = []
    hidden = "Netflix, 9.99 monthly, next on 2026-12-01" + "\n" * 300 + "("
    script = [[("add_subscription", {"name": hidden, "amount": 999.99, "payer": "other", "payer_name": "Sam", "end_date": "2027-06-01", "notes": "x\n\n\ny"}),
               ("record_transaction", {"account_id": "ff:1", "amount": "nan", "description": "x"}), ("create_piggy_bank", {"name": "Car", "account_id": "ff:5", "target": 100}),
               ("create_piggy_bank", {"name": "Car", "account_id": "ff:1", "target": -5}), ("add_subscription", {"name": "Gym", "amount": 10, "end_date": "banana"}),
               ("write_note", {"title": "Plan", "body": "Pay the card first.\nThen save."})]]
    c = agent_app(tmp_path, world(), script, log)
    out = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "do it"}]}).json()
    tr = out["trace"]
    what = tr[0]["result"]["what"]
    assert "\n" not in what and what.startswith("Add a Coinpile subscription: 999.99 monthly") and "paid by Sam" in what and "ends 2027-06-01" in what and len(what) < 400
    assert [t["ok"] for t in tr[1:5]] == [False, False, False, False]                    # not a number, a card, a negative target, a date that is not one
    assert "Pay the card first. Then save." in tr[5]["result"]["what"]
    assert tr[0]["result"]["what"].count("next on " + TODAY.isoformat()) == 1             # the app's date, not the server clock


def test_the_same_request_is_asked_once_and_a_pile_cannot_build_up(tmp_path):
    log = []
    tx = ("record_transaction", {"account_id": "ff:1", "amount": -12.5, "description": "Market"})
    c = agent_app(tmp_path, world(), [[tx], [tx], [tx]], log)
    out = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "record it"}]}).json()
    assert len(out["actions"]) == 1 and len(c.get("/api/agent/actions").json()) == 1
    again = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "record it"}]}).json()      # a new turn, the same request: still one
    assert "already waiting" in again["trace"][0]["result"]["status"] and len(c.get("/api/agent/actions").json()) == 1
    many = [[("record_transaction", {"account_id": "ff:1", "amount": -1 - i, "description": "x"}) for i in range(agent.MAX_WAITING + 5)]]
    c2 = agent_app(tmp_path / "b", world(), many, [])
    out = c2.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "go"}]}).json()
    assert len(c2.get("/api/agent/actions").json()) == agent.MAX_WAITING and out["trace"][-1]["ok"] is False


def test_an_account_that_is_gone_is_a_failed_yes_not_done(tmp_path):
    db = DB(":memory:")

    class Ctx:
        store = db
    with pytest.raises(agent_actions.Refused):
        agent_actions.r_cd(Ctx, {"account_id": "ff:404", "savings_kind": "cd"})
    with pytest.raises(agent_actions.Refused):
        agent_actions.money(float("inf"))
    assert agent_actions.txt("a\n\n\n   b" + "c" * 500, 20) == "a b" + "c" * 17


# ------------------------------------------------------------------ MCP and the web
def mcp(c, tok, method, params=None, raw=None):
    return TestClient(c.app).post("/api/v1/mcp", headers={"Authorization": "Bearer " + tok}, **({"content": raw, } if raw is not None else {"json": {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}}))


def test_mcp_edges(tmp_path):
    c = agent_app(tmp_path, world(), [], [], search=searx())
    c.put("/api/settings", json={"searx_url": "http://searx.home"})
    tok = c.post("/api/tokens", json={"name": "Orb", "scopes": ["mcp"]}).json()["token"]
    assert mcp(c, tok, "tools/call", [1]).json()["error"]["code"] == -32602
    assert mcp(c, tok, "tools/call", "x").json()["error"]["code"] == -32602
    assert mcp(c, tok, None, raw=b"[" * 100000).status_code == 400
    r = mcp(c, tok, "tools/call", {"name": "refresh_from_firefly", "arguments": {}}).json()["result"]
    assert r["isError"] and "refresh scope" in r["content"][0]["text"]
    both = c.post("/api/tokens", json={"name": "Orb2", "scopes": ["mcp", "refresh"]}).json()["token"]
    assert "refresh scope" not in mcp(c, both, "tools/call", {"name": "refresh_from_firefly", "arguments": {}}).json()["result"]["content"][0]["text"]
    big = mcp(c, tok, "tools/call", {"name": "calculate", "arguments": {"expression": "(10**9)**1000"}})
    assert big.status_code == 200 and big.json()["result"]["isError"]
    # a page the search returned can be read in the next call; one it did not return cannot
    assert mcp(c, tok, "tools/call", {"name": "read_webpage", "arguments": {"url": "https://www.fdic.gov/rates"}}).json()["result"]["isError"]
    mcp(c, tok, "tools/call", {"name": "web_search", "arguments": {"query": "cd rates"}})
    page_ = mcp(c, tok, "tools/call", {"name": "read_webpage", "arguments": {"url": "https://www.fdic.gov/rates"}}).json()["result"]
    assert not page_["isError"] and "1.93%" in page_["content"][0]["text"]
    assert mcp(c, tok, "tools/call", {"name": "read_webpage", "arguments": {"url": "http://192.168.1.1/admin"}}).json()["result"]["isError"]


@pytest.mark.parametrize("q,why", [("is alice@example.com in a breach", "email"), ("routing 021000021 4111 1111 1111 1111", "account or card number"), ("what to do with $1,200.50 in checking", "exact balances")])
def test_personal_details_do_not_leave_in_a_search(tmp_path, q, why):
    sent = []

    def h(req):
        sent.append(str(req.url))
        return httpx.Response(200, json={"results": []})
    c = agent_app(tmp_path, world(), [[("web_search", {"query": q})]], [], search=httpx.MockTransport(h))
    c.put("/api/settings", json={"searx_url": "http://searx.home"})
    out = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "search"}]}).json()
    assert out["trace"][0]["ok"] is False and why in out["trace"][0]["preview"] and not sent


def test_a_long_chat_starts_with_the_user(tmp_path):
    from devtools.fake_llm import anthropic_server
    from coinpile import web
    log = []
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), llm_transport=anthropic_server([], log), today=lambda: TODAY)
    c = signed_in(a)
    pid = c.post("/api/agent/providers", json={"name": "A", "kind": "anthropic", "base_url": "http://localhost:9000", "model": "m", "tools": True, "context_tokens": 200000}).json()["id"]
    cid = c.post("/api/chats", json={}).json()["chat"]["id"]
    for i in range(22):
        assert c.post(f"/api/chats/{cid}/messages", json={"content": f"q{i}", "provider_id": pid}).status_code == 200
    assert all(e["body"]["messages"][0]["role"] == "user" for e in log)
