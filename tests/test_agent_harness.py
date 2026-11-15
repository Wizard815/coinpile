"""The agent's harness: figures must come from tools (or the user), arguments are checked, repeats are cut short, skills load."""
import json

import httpx

from coinpile import web
from coinpile.agent import calculate, check_args, figures, known_numbers, unverified
from tests.test_coinpile import TODAY, signed_in, world


def scripted(turns, log):
    """turns[n] for the n-th model call: a list of (tool, args) to call, or a string to answer with."""
    def h(req: httpx.Request):
        if not req.url.path.endswith("/chat/completions"):
            return httpx.Response(404)
        body = json.loads(req.content)
        log.append(body)
        n = sum(1 for m in body["messages"] if m["role"] == "assistant")
        step = turns[min(n, len(turns) - 1)]
        if isinstance(step, str):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": step}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{n}_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(step)]}}]})
    return httpx.MockTransport(h)


def ask(tmp_path, turns, question="how much will I have?"):
    log = []
    a = web.create_app(firefly_url="https://firefly.example.com", data_dir=str(tmp_path), firefly_transport=world().transport(), llm_transport=scripted(turns, log), today=lambda: TODAY)
    c = signed_in(a)
    c.post("/api/agent/providers", json={"name": "Local", "kind": "openai", "base_url": "http://localhost:11434/v1", "model": "llama3", "tools": True})
    return c.post("/api/agent/chat", json={"messages": [{"role": "user", "content": question}]}).json(), log


def test_a_figure_the_model_worked_out_itself_is_sent_back_to_the_calculator(tmp_path):
    out, log = ask(tmp_path, [
        [("list_accounts", {})],
        "Checking has $1,200.50; after rent you keep $777.77.",                         # 777.77 came from nowhere
        [("calculate", {"expression": "1200.50 - 422.73"})],
        "Checking has $1,200.50; after rent you keep $777.77."])
    assert [t["tool"] for t in out["trace"]] == ["list_accounts", "(figure check)", "calculate"]
    assert out["trace"][1]["args"]["not_from_a_tool"] == ["$777.77"]
    assert "[Coinpile check]" in log[2]["messages"][-1]["content"] and "$777.77" in log[2]["messages"][-1]["content"]
    assert out["checked"] == {"figures": 2, "unverified": []}


def test_a_figure_still_not_from_a_tool_is_flagged(tmp_path):
    out, _ = ask(tmp_path, [[("list_accounts", {})], "You will have $4,321.00 by spring.", "You will have $4,321.00 by spring."])
    assert out["checked"]["unverified"] == ["$4,321.00"] and out["reply"] == "You will have $4,321.00 by spring."


def test_the_users_own_numbers_and_dates_are_fine(tmp_path):
    out, _ = ask(tmp_path, ["If you spend $1,200 on 2026-11-15 that is your call."], question="can I spend $1,200 on the 15th?")
    assert out["checked"]["unverified"] == []


def test_bad_arguments_unknown_tools_and_repeats_are_answered_plainly(tmp_path):
    out, log = ask(tmp_path, [[("search_transactions", {"limit": "lots"}), ("no_such_tool", {}), ("list_accounts", {}), ("list_accounts", {}),
                               ("account_details", {}), ("calculate", {"expression": "2+2", "precision": 3})], "done"])
    res = [json.loads(m["content"]) for m in log[1]["messages"] if m["role"] == "tool"]
    assert res[0]["error"].startswith("bad arguments: limit must be a integer")
    assert "unknown tool" in res[1]["error"]
    assert isinstance(res[2], list) and res[3]["repeat"] is True
    assert res[4]["error"] == "bad arguments: missing: account_id. Fix them and call again."
    assert "unknown argument 'precision'" in res[5]["error"]


def test_skills_are_listed_and_load(tmp_path):
    out, log = ask(tmp_path, [[("use_skill", {"name": "debt_payoff"}), ("use_skill", {"name": "nope"})], "ok"])
    system = log[0]["messages"][0]["content"]
    assert "SKILLS" in system and "- monthly_review:" in system and "- debt_payoff:" in system
    res = [json.loads(m["content"]) for m in log[1]["messages"] if m["role"] == "tool"]
    assert "loan_payoff" in res[0]["steps"] and res[0]["skill"] == "debt_payoff"
    assert res[1]["error"].startswith("bad arguments: name must be one of")


def test_the_calculator_does_money():
    assert round(calculate("pmt(0.24/12, 36, 5000)"), 2) == 196.16
    assert round(calculate("fv(0.04/12, 12, 100)"), 2) == 1222.25
    assert round(calculate("nper(0.02, 200, 5000)"), 1) == 35.0
    assert round(calculate("compound(1000, 5, 2)"), 2) == 1104.94
    assert calculate("pct(150, 600)") == 25 and calculate("5% * 2,000") == 100 and calculate("round(sum([1.005, 2]), 2)") == 3.0
    for bad in ("__import__('os')", "open('x')", "2 ** 99999", "nper(0.02, 50, 5000)"):
        try:
            calculate(bad)
            assert False, bad
        except (ValueError, OverflowError):
            pass


def test_the_calculator_takes_several_items(tmp_path):
    out, log = ask(tmp_path, [[("calculate", {"items": [{"label": "monthly", "expression": "1200/12"}, {"label": "bad", "expression": "1/0"}]})], "ok"])
    r = json.loads([m for m in log[1]["messages"] if m["role"] == "tool"][0]["content"])["results"]
    assert r[0] == {"label": "monthly", "expression": "1200/12", "result": 100.0, "rounded": 100.0} and "error" in r[1]


def test_figure_helpers():
    assert [v for _, v in figures("Rent $1,500.00 is 32% of income; 3 bills; due 2026-12-01; in 2027; 12k saved")] == [1500.0, 32.0, 12000.0]
    k = known_numbers([{"rate": 0.0475, "bal": -2500}])
    assert unverified("4.75% on $2,500", k) == [] and unverified("4.8% on $2,500", k) == []
    assert check_args({"properties": {"x": {"type": "number"}}, "required": ["x"]}, {"x": "1,200.50"}) is None
