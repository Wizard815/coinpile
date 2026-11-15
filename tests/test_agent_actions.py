"""The agent reads freely, asks before every change, searches the web through the person's SearXNG, and does exact money maths."""
import json

import httpx

from coinpile import web
from devtools.fake_llm import openai_server
from tests.test_coinpile import TODAY, signed_in, world


def agent_app(tmp_path, ff, script, log, search=None):
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=ff.transport(), llm_transport=openai_server(script, log),
                       search_transport=search, today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True})
    return c


def results(out, log, turn=1):
    return {t["tool"]: json.loads(m["content"]) for t, m in zip(out["trace"], [m for m in log[turn]["body"]["messages"] if m["role"] == "tool"])}


def test_changes_are_asked_in_the_chat_and_done_only_on_yes(tmp_path):
    ff, log = world(), []
    script = [[("record_transaction", {"account_id": "ff:1", "amount": -12.5, "description": "Farmers market", "date": "2026-11-08", "category": "Groceries"}),
               ("set_budget", {"name": "Groceries", "amount": 400, "month": "2026-11"}),
               ("create_piggy_bank", {"name": "Tires", "account_id": "ff:3", "target": 800}),
               ("record_transaction", {"account_id": "ff:99", "amount": -1, "description": "x"})]]               # refused before asking
    c = agent_app(tmp_path, ff, script, log)
    cid = c.post("/api/chats").json()["chat"]["id"]
    view = c.post(f"/api/chats/{cid}/messages", json={"content": "log my market spend, budget 400 for food, and start a tires fund"}).json()
    acts = view["actions"]
    assert [a["status"] for a in acts] == ["asked", "asked", "asked"] and not getattr(ff, "writes", [])          # nothing happened yet
    assert acts[0]["summary"] == 'Record in Firefly III: paid 12.50 "Farmers market" on 2026-11-08, account Everyday Checking (Huntington), category Groceries'
    assert acts[0]["target"] == "firefly" and "Tires" in acts[2]["summary"]
    assert "not asked" in view["messages"][-1]["trace"][3]["preview"]
    # yes: Firefly III gets exactly that, even with every send switch off
    assert c.post(f"/api/agent/actions/{acts[0]['id']}/yes").json()["status"] == "done"
    leg = [w for w in ff.writes if w[1] == "/transactions"][0][2]["transactions"][0]
    assert leg["amount"] == "12.50" and leg["description"] == "Farmers market" and leg["source_id"] == "1" and leg["category_name"] == "Groceries"
    assert c.post(f"/api/agent/actions/{acts[0]['id']}/yes").status_code == 409                                 # pressing twice does nothing more
    assert c.post(f"/api/agent/actions/{acts[1]['id']}/yes").json()["result"]["made_budget"] is True
    assert ff.data["1"]["budget-limits"][0]["attributes"]["amount"] == "400.00"
    # no: nothing changes, and the agent is told
    assert c.post(f"/api/agent/actions/{acts[2]['id']}/no").json()["status"] == "declined"
    assert not any(p["attributes"]["name"] == "Tires" for p in ff.data["1"]["piggies"])
    msgs = c.get(f"/api/chats/{cid}").json()["messages"]
    assert msgs[-2]["content"].startswith("Yes to: In Firefly III, set the budget") and msgs[-1]["content"].startswith("No to: Make a piggy bank")
    assert len(c.get("/api/agent/actions?status=asked").json()) == 0


def test_a_failed_change_says_why(tmp_path):
    ff, log = world(), []
    ff.piggy_update_breaks = True
    c = agent_app(tmp_path, ff, [[("move_piggy_bank_money", {"piggy_id": "1", "amount": 50})]], log)
    cid = c.post("/api/chats").json()["chat"]["id"]
    a = c.post(f"/api/chats/{cid}/messages", json={"content": "put 50 in vacation"}).json()["actions"][0]
    assert "put 50.00 into the piggy bank Vacation" in a["summary"]
    r = c.post(f"/api/agent/actions/{a['id']}/yes").json()
    assert r["status"] == "failed" and "in Firefly III itself" in r["result"]["error"]


def searx():
    pages = {"https://www.fdic.gov/rates": "<html><head><title>National rates</title><script>evil()</script></head><body><p>National average 12-month CD: 1.93%</p></body></html>"}

    def h(req: httpx.Request):
        if req.url.host == "searx.home" and req.url.path == "/search":
            assert req.url.params["format"] == "json"
            return httpx.Response(200, json={"results": [{"title": "National rates and rate caps", "url": "https://www.fdic.gov/rates", "content": "12-month CD 1.93%", "publishedDate": "2026-11-01T00:00:00"},
                                                         {"title": "dup", "url": "https://www.fdic.gov/rates"}, {"title": "bad", "url": "javascript:alert(1)"}]})
        if str(req.url) in pages:
            return httpx.Response(200, text=pages[str(req.url)], headers={"content-type": "text/html"})
        return httpx.Response(404)
    return httpx.MockTransport(h)


def test_the_agent_searches_through_searxng_and_reads_only_what_it_found(tmp_path):
    log = []
    script = [[("web_search", {"query": "average 12 month CD rate", "time_range": "month"}), ("read_webpage", {"url": "https://www.fdic.gov/rates"}),
               ("read_webpage", {"url": "http://192.168.1.1/admin"})]]
    c = agent_app(tmp_path, world(), script, log, search=searx())
    c.put("/api/settings", json={"searx_url": "http://searx.home"})
    out = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "are CD rates good now?"}]}).json()
    got = results(out, log)
    assert [r["url"] for r in got["web_search"]["results"]] == ["https://www.fdic.gov/rates"] and got["web_search"]["results"][0]["published"] == "2026-11-01"
    page = json.loads([m for m in log[1]["body"]["messages"] if m["role"] == "tool"][1]["content"])
    assert page["title"] == "National rates" and "1.93%" in page["text"] and "evil" not in page["text"]
    # read_webpage only follows what the search returned (no probing the home network from a page's text)
    assert out["trace"][2]["ok"] is False
    assert c.post("/api/agent/search-test", json={"q": "cd rates"}).json()["results"][0]["site"] == "www.fdic.gov"


def test_without_searxng_the_agent_is_told_how_to_set_it_up(tmp_path, monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    log = []
    c = agent_app(tmp_path, world(), [[("web_search", {"query": "x"})]], log)
    out = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "q"}]}).json()
    assert "SearXNG" in results(out, log)["web_search"]["error"]


def test_money_maths_is_exact(tmp_path):
    log = []
    script = [[("loan_payoff", {"balance": 5000, "apr": 24, "payment": 200, "extra_payment": 100}), ("loan_payoff", {"balance": 5000, "apr": 24, "payment": 50}),
               ("savings_plan", {"target": 2000, "have": 400, "by_date": "2027-06-10"}), ("savings_plan", {"target": 1000, "monthly": 100}),
               ("cashflow", {"months": 3})]]
    c = agent_app(tmp_path, world(), script, log)
    out = c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "maths"}]}).json()
    msgs = [json.loads(m["content"]) for m in log[1]["body"]["messages"] if m["role"] == "tool"]
    pay, never, plan_by, plan_monthly, flow = msgs
    assert pay["months"] == 36 and pay["with_extra"]["months"] == 21 and pay["with_extra"]["interest_saved"] > 600
    assert "never goes down" in never["error"]
    assert plan_by["months"] == 7 and plan_by["monthly_needed"] == 228.57
    assert plan_monthly["months"] == 10
    oct_ = next(m for m in flow["months"] if m["month"] == "2026-10")
    assert oct_["in"] == 2500 and oct_["out"] == round(82.4 + 49.99 + 15.99, 2)                                            # the transfer to savings is left out
    assert [t["tool"] for t in out["trace"]] == [s[0] for s in script[0]]


def test_the_agent_is_told_how_to_help_and_what_it_may_change(tmp_path):
    log = []
    c = agent_app(tmp_path, world(), [[]], log)
    c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    system = log[0]["body"]["messages"][0]["content"]
    assert "ASKS THE USER" in system and "Never put personal details in a search" in system and "Firefly III is the main record" in system
    names = {t["function"]["name"]: t["function"]["description"] for t in log[0]["body"]["tools"]}
    for n in ("record_transaction", "set_transaction_category", "create_piggy_bank", "update_piggy_bank", "move_piggy_bank_money", "set_budget", "add_subscription", "write_note", "mark_account_as_savings"):
        assert names[n].startswith("ASKS THE USER FIRST")
    assert "web_search" in names and "loan_payoff" in names
