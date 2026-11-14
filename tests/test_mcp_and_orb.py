"""Firefly III detail (tags, budgets, payees) reaches the agent; tax and paycheck tools; Coinpile as an MCP server; what the Orb can see."""
import json

from fastapi.testclient import TestClient

from tests.test_coinpile import app, signed_in, world
from tests.test_pages_and_plans import rec


def tagged_world():
    ff = world()
    for i, (d, amt, desc, tags, budget, payee) in enumerate((("2025-03-02", 120, "Red Cross", ["tax-deductible", "donation"], None, "American Red Cross"),
                                                             ("2025-06-11", 85.5, "Dr Patel copay", ["tax-deductible"], "Health", "Patel Family Medicine"),
                                                             ("2025-09-30", 3.21, "Interest paid", [], None, "Axos"))):
        ff.tx("1", 80 + i, "deposit" if desc == "Interest paid" else "withdrawal", amt, desc, *(("92", "1") if desc == "Interest paid" else ("1", "96")), d)
        leg = ff.data["1"]["groups"][-1]["attributes"]["transactions"][0]
        leg.update({"tags": tags, "budget_name": budget, "destination_name": payee, "source_name": payee})
    return ff


def test_tags_budgets_and_payees_come_from_firefly_and_can_be_searched(tmp_path):
    c = signed_in(app(tmp_path, tagged_world()))
    c.post("/api/refresh?full=true")
    from coinpile import ledger
    from coinpile.db import DB
    import glob
    db = DB(glob.glob(str(tmp_path / "people" / "*.db"))[0])
    r = ledger.query_transactions(db, tag="Tax-Deductible", start="2025-01-01", end="2025-12-31")
    assert r["total"] == 2 and r["sum_out"] == -205.5
    assert ledger.query_transactions(db, budget="health")["items"][0]["payee"] == "Patel Family Medicine"
    assert ledger.query_transactions(db, q="patel")["total"] == 1                                       # found by the payee, not only the description


def test_tax_year_and_paycheck_plan(tmp_path):
    from coinpile import agent
    ff = tagged_world()
    ff.data["1"]["recurrences"] = [rec(2, "deposit", "ACME paycheck", 2500, "2026-10-01", ["2026-12-01"], unit="monthly", src="90", dst="1")]
    a = app(tmp_path, ff)
    c = signed_in(a)
    c.post("/api/refresh?full=true")
    import glob
    from coinpile.db import DB
    from tests.test_coinpile import TODAY
    db = DB(glob.glob(str(tmp_path / "people" / "*.db"))[0])
    tools = {n: f for n, _, _, f in agent.build_tools(__import__("types").SimpleNamespace(store=db, today=lambda: TODAY))}
    tx = tools["tax_year"]({"year": 2025})
    assert tx["tags"]["tax-deductible"] == {"count": 2, "total": -205.5} and tx["tags"]["donation"]["total"] == -120
    assert {x["name"] for x in tx["looks_tax_related"]} >= {"Red Cross", "Interest paid"}
    pc = tools["paycheck_plan"]({})
    assert pc["paycheck"]["name"] == "ACME paycheck" and pc["paycheck"]["date"] == "2026-12-01" and pc["covers_until"] == "2027-01-01"
    assert [b["name"] for b in pc["bills_due_before_next"]] == ["Rent"] and pc["bills_total"] == 1500
    assert pc["left_for_spending"] == round(2500 - 1500 - pc["piggy_total"], 2)


def rpc(c, tok, method, params=None, mid=1):
    r = c.post("/api/v1/mcp", headers={"Authorization": "Bearer " + tok}, json={"jsonrpc": "2.0", "id": mid, "method": method, **({"params": params} if params else {})})
    return r


def test_coinpile_is_an_mcp_server_for_other_assistants(tmp_path):
    ff = world()
    c = signed_in(app(tmp_path, ff))
    tok = c.post("/api/tokens", json={"name": "Pondering Orb", "scopes": ["mcp"]}).json()["token"]
    other = c.post("/api/tokens", json={"name": "HA", "scopes": ["summary"]}).json()["token"]
    anon = TestClient(c.app)
    assert rpc(anon, "cp_nobody_x", "initialize").status_code == 401 and rpc(anon, other, "initialize").status_code == 403
    init = rpc(anon, tok, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert init["serverInfo"]["name"] == "coinpile" and "calculate" in init["instructions"]
    assert anon.post("/api/v1/mcp", headers={"Authorization": "Bearer " + tok}, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    tools = {t["name"]: t for t in rpc(anon, tok, "tools/list").json()["result"]["tools"]}
    assert tools["list_accounts"]["annotations"]["readOnlyHint"] is True and tools["record_transaction"]["annotations"]["readOnlyHint"] is False
    assert "use_skill" not in tools and "paycheck_plan" in tools and "tax_year" in tools
    out = rpc(anon, tok, "tools/call", {"name": "calculate", "arguments": {"expression": "pmt(0.24/12, 36, 5000)"}}).json()["result"]
    assert round(json.loads(out["content"][0]["text"])["result"], 2) == 196.16 and out["isError"] is False
    assert rpc(anon, tok, "tools/call", {"name": "account_details", "arguments": {}}).json()["result"]["isError"] is True
    asked = json.loads(rpc(anon, tok, "tools/call", {"name": "record_transaction", "arguments": {"account_id": "ff:1", "amount": -4, "description": "Coffee"}}).json()["result"]["content"][0]["text"])
    assert asked["status"].startswith("waiting") and "Agent page" in asked["note"] and not getattr(ff, "writes", [])
    waiting = c.get("/api/agent/actions?status=asked").json()
    assert [w["chat_id"] for w in waiting] == [None] and c.post(f"/api/agent/actions/{waiting[0]['id']}/yes").json()["status"] == "done"
    prompts = {p["name"] for p in rpc(anon, tok, "prompts/list").json()["result"]["prompts"]}
    assert {"tax_prep", "paycheck_splitter", "monthly_review"} <= prompts
    msg = rpc(anon, tok, "prompts/get", {"name": "tax_prep"}).json()["result"]["messages"][0]["content"]["text"]
    assert "tax_year" in msg and "not tax advice" in msg
    assert rpc(anon, tok, "resources/list").json()["error"]["code"] == -32601


def test_the_orb_sees_piggy_bank_goals_budgets_and_the_next_paycheck(tmp_path):
    ff = world()
    ff.data["1"]["budgets"] = [{"type": "budgets", "id": "1", "attributes": {"name": "Groceries", "active": True, "spent": [{"sum": "-82.40"}]}}]
    ff.data["1"]["budget-limits"] = [{"type": "budget_limits", "id": "9", "attributes": {"budget_id": "1", "amount": "400"}}]
    ff.data["1"]["recurrences"] = [rec(2, "deposit", "ACME paycheck", 2500, "2026-10-01", ["2026-12-01"], unit="monthly", src="90", dst="1")]
    c = signed_in(app(tmp_path, ff))
    orb = c.post("/api/tokens", json={"name": "Orb", "scopes": ["orb"]}).json()["token"]
    assert c.put("/api/settings", json={"share": ["goals", "budgets", "paycheck"]}).status_code == 200
    me = TestClient(c.app).get("/api/v1/summary", headers={"Authorization": "Bearer " + orb}).json()["goblins"]
    s = next(iter(me.values()))
    assert s["goals"][0] == {"name": "Vacation", "target": 2000, "allocated": 400, "pct": 0.2, "by": "2027-06-01"}
    assert s["budgets"]["items"] == [{"name": "Groceries", "limit": 400, "spent": 82.4, "left": 317.6}]
    assert s["paycheck"]["date"] == "2026-12-01" and s["paycheck"]["bills"] == 1500


def test_week_changes_shared_expenses_and_the_new_skills(tmp_path):
    from coinpile import agent, agent_skills
    import glob
    from coinpile.db import DB
    from tests.test_coinpile import TODAY
    ff = world()
    for i, (d, amt, desc, tags) in enumerate((("2026-11-08", 60, "Costco run", ["shared"]), ("2026-11-09", 20, "Movie night", ["shared", "fun"]), ("2026-11-02", 30, "Gas", []))):
        ff.tx("1", 90 + i, "withdrawal", amt, desc, 1, 97, d)
        ff.data["1"]["groups"][-1]["attributes"]["transactions"][0]["tags"] = tags
    c = signed_in(app(tmp_path, ff))
    c.post("/api/refresh?full=true")
    db = DB(glob.glob(str(tmp_path / "people" / "*.db"))[0])
    tools = {n: f for n, _, _, f in agent.build_tools(__import__("types").SimpleNamespace(store=db, today=lambda: TODAY))}
    w = tools["week_changes"]({})
    assert w["from"] == "2026-11-04" and w["money_out"] == 80 and w["previous_week"]["money_out"] == 30 and w["out_change"] == 50
    assert [b["name"] for b in w["biggest"]] == ["Costco run", "Movie night"]
    s = tools["shared_expenses"]({"start": "2026-11-01", "end": "2026-11-10"})
    assert s["paid_by_me_for_both"] == 80 and s["they_owe_me"] == 40 and [i["name"] for i in s["items"]] == ["Movie night", "Costco run"]
    assert tools["shared_expenses"]({"start": "2026-11-01", "end": "2026-11-10", "my_share_pct": 60})["they_owe_me"] == 32
    for k in ("year_in_review", "insurance_and_renewals", "joint_settle_up", "weekly_brief", "tax_prep", "paycheck_splitter"):
        assert k in agent_skills.SKILLS and agent_skills.load(k)["steps"]
    assert "week_changes" in agent_skills.load("weekly_brief")["steps"] and "shared_expenses" in agent_skills.load("joint_settle_up")["steps"]
    # the Orb can be given the weekly brief
    orb = c.post("/api/tokens", json={"name": "Orb", "scopes": ["orb"]}).json()["token"]
    c.put("/api/settings", json={"share": ["week"]})
    wk = next(iter(TestClient(c.app).get("/api/v1/summary", headers={"Authorization": "Bearer " + orb}).json()["goblins"].values()))["week"]
    assert wk["money_out"] == 80 and wk["biggest"][0] == {"name": "Costco run", "amount": -60.0} and "items" not in wk
    # the Agent page can show what it can do
    tl = {t["name"]: t for t in c.get("/api/agent/tools").json()}
    assert tl["record_transaction"]["asks_first"] and not tl["record_transaction"]["description"].startswith("ASKS")
    assert not tl["week_changes"]["asks_first"] and tl["week_changes"]["over_mcp"] and not tl["use_skill"]["over_mcp"]
    assert {s["name"] for s in c.get("/api/agent/skills").json()} >= {"weekly_brief", "joint_settle_up"}
