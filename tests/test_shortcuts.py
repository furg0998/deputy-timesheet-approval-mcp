import asyncio
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import shortcuts  # noqa: E402


@pytest.fixture
def env():
    mcp = FastMCP("test")
    calls = []
    user = {"id": "u1"}

    @mcp.tool()
    async def get_queue(date_from: str) -> dict:
        calls.append(("get_queue", date_from))
        return {"queue": [{"employee_id": 1, "hours": 7.5},
                          {"employee_id": 2, "hours": 11.0}]}

    @mcp.tool()
    async def flag(employee_ids: list[int], reason: str) -> dict:
        calls.append(("flag", employee_ids, reason))
        return {"flagged": employee_ids}

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def approve(ids: list[int]) -> dict:
        calls.append(("approve", ids))
        return {"approved": ids}

    sc = shortcuts.Shortcuts(
        mcp, shortcuts.MemoryStorage(), lambda: user["id"]
    ).register()
    return mcp, sc, calls, user


def run(coro):
    return asyncio.run(coro)


def test_save_and_run_chains_outputs(env):
    mcp, sc, calls, _ = env
    run(sc.save_shortcut(
        "weekly-flag", "Flag everyone in the queue",
        steps=[
            {"id": "q", "tool": "get_queue", "args": {"date_from": "{{params.start}}"}},
            {"tool": "flag", "args": {
                "employee_ids": "{{steps.q.queue.*.employee_id}}",
                "reason": "week of {{params.start}}",
            }},
        ],
        params={"start": "first day"},
    ))
    out = run(sc.run_shortcut("weekly-flag", {"start": "2026-11-16"}))
    assert out["ran"] and out["result"] == {"flagged": [1, 2]}
    assert calls == [("get_queue", "2026-11-16"),
                     ("flag", [1, 2], "week of 2026-11-16")]


def test_registered_as_mcp_tools(env):
    mcp, _, _, _ = env
    names = {t.name for t in run(mcp.list_tools())}
    assert {"save_shortcut", "run_shortcut", "list_shortcuts",
            "delete_shortcut", "save_rule", "list_rules",
            "delete_rule"} <= names
    run(mcp.call_tool("save_shortcut", {
        "name": "q", "description": "d",
        "steps": [{"tool": "get_queue", "args": {"date_from": "x"}}],
    }))
    listed = run(mcp._tool_manager.call_tool("list_shortcuts", {}))
    assert [s["name"] for s in listed] == ["q"]


def test_destructive_steps_need_confirm(env):
    _, sc, calls, _ = env
    saved = run(sc.save_shortcut(
        "approve-all", "d",
        steps=[{"id": "q", "tool": "get_queue", "args": {"date_from": "x"}},
               {"tool": "approve", "args": {"ids": "{{steps.q.queue.*.employee_id}}"}}],
    ))
    assert saved["shortcut"]["requires_confirm"]
    out = run(sc.run_shortcut("approve-all"))
    assert out == {**out, "ran": False, "requires_confirm": True,
                   "guarded_tools": ["approve"]}
    assert calls == []
    assert run(sc.run_shortcut("approve-all", confirm=True))["ran"]
    assert calls[-1] == ("approve", [1, 2])


@pytest.mark.parametrize("steps,params,err", [
    ([{"tool": "nope"}], {}, "unknown tool"),
    ([{"tool": "run_shortcut"}], {}, "cannot call"),
    ([{"tool": "get_queue", "args": {"date_from": "{{params.x}}"}}], {}, "declared"),
    ([{"tool": "get_queue", "args": {"date_from": "{{steps.0.a}}"}}], {}, "earlier step"),
    ([], {}, "at least one"),
    ([{"id": "1", "tool": "get_queue", "args": {"date_from": "x"}},
      {"tool": "get_queue", "args": {"date_from": "y"}}], {}, "not just a number"),
])
def test_validation(env, steps, params, err):
    _, sc, _, _ = env
    with pytest.raises(shortcuts.ShortcutError, match=err):
        run(sc.save_shortcut("bad", "d", steps=steps, params=params))


def test_failed_step_stops_run(env):
    _, sc, calls, _ = env
    run(sc.save_shortcut("s", "d", steps=[
        {"tool": "get_queue", "args": {"date_from": "x"}},
        {"tool": "flag", "args": {"employee_ids": "{{steps.0.missing}}", "reason": "r"}},
        {"tool": "get_queue", "args": {"date_from": "y"}},
    ]))
    out = run(sc.run_shortcut("s"))
    assert not out["ran"] and out["failed_step"] == 1
    assert len(calls) == 1


def test_missing_params_and_defaults(env):
    _, sc, _, _ = env
    run(sc.save_shortcut("s", "d", params={"start": ""}, defaults={"start": "2026-01-01"},
                         steps=[{"tool": "get_queue", "args": {"date_from": "{{params.start}}"}}]))
    assert run(sc.run_shortcut("s"))["ran"]
    run(sc.save_shortcut("t", "d", params={"start": ""},
                         steps=[{"tool": "get_queue", "args": {"date_from": "{{params.start}}"}}]))
    with pytest.raises(shortcuts.ShortcutError, match="missing parameters"):
        run(sc.run_shortcut("t"))


def test_per_user_isolation_and_rules(env):
    _, sc, _, user = env
    run(sc.save_shortcut("s", "d", steps=[{"tool": "get_queue", "args": {"date_from": "x"}}]))
    run(sc.save_rule("Always flag shifts over 10h", rule_id="long-shifts"))
    user["id"] = "u2"
    assert run(sc.list_shortcuts()) == []
    assert run(sc.list_rules()) == []
    with pytest.raises(shortcuts.ShortcutError):
        run(sc.run_shortcut("s"))
    user["id"] = "u1"
    assert [r["rule_id"] for r in run(sc.list_rules())] == ["long-shifts"]
    assert run(sc.delete_rule("long-shifts")) == {"deleted": True}
    assert run(sc.delete_shortcut("s")) == {"deleted": True}
    assert run(sc.list_shortcuts()) == []


def test_filter_join_foreach_value(env):
    mcp, sc, calls, _ = env

    @mcp.tool()
    async def list_employees() -> list[dict]:
        return [{"Id": 1, "Name": "Ann"}, {"Id": 2, "Name": "Bob"}]

    run(sc.save_shortcut(
        "long-shift-report", "Join long shifts with names and flag them",
        params={"min_hours": "threshold"},
        steps=[
            {"id": "q", "tool": "get_queue", "args": {"date_from": "x"}},
            {"id": "long", "op": "filter", "input": "{{steps.q.queue}}",
             "where": [{"field": "hours", "op": ">", "value": "{{params.min_hours}}"}],
             "sort_by": "hours", "descending": True},
            {"id": "emps", "tool": "list_employees"},
            {"id": "named", "op": "join", "left": "{{steps.long}}",
             "right": "{{steps.emps}}", "on": {"left": "employee_id", "right": "Id"},
             "as": "employee", "how": "inner"},
            {"id": "rows", "op": "pick", "input": "{{steps.named}}",
             "fields": {"name": "employee.Name", "hours": "hours"}},
            {"id": "flags", "tool": "flag", "foreach": "{{steps.named}}",
             "args": {"employee_ids": ["{{item.employee_id}}"],
                      "reason": "{{item.employee.Name}} worked {{item.hours}}h"}},
            {"op": "value", "value": {"long_shifts": "{{steps.rows}}",
                                      "flagged": "{{steps.flags.*.flagged}}",
                                      "queue_size": "{{steps.q.queue.*.employee_id}}"}},
        ],
    ))
    out = run(sc.run_shortcut("long-shift-report", {"min_hours": 10}))
    assert out["ran"], out
    assert out["result"] == {"long_shifts": [{"name": "Bob", "hours": 11.0}],
                             "flagged": [[2]], "queue_size": [1, 2]}
    assert calls[-1] == ("flag", [2], "Bob worked 11.0h")


@pytest.mark.parametrize("step,err", [
    ({"op": "nope", "input": []}, "unknown op"),
    ({"op": "join", "left": []}, "needs right, on"),
    ({"tool": "get_queue", "op": "filter"}, "either a 'tool' or an 'op'"),
    ({"tool": "get_queue", "args": {"date_from": "{{item.x}}"}}, "foreach"),
])
def test_op_validation(env, step, err):
    _, sc, _, _ = env
    with pytest.raises(shortcuts.ShortcutError, match=err):
        run(sc.save_shortcut("bad", "d", steps=[step]))


def test_foreach_destructive_needs_confirm_and_limit(env):
    _, sc, calls, _ = env
    run(sc.save_shortcut("each", "d", steps=[
        {"id": "q", "tool": "get_queue", "args": {"date_from": "x"}},
        {"tool": "approve", "foreach": "{{steps.q.queue}}",
         "args": {"ids": ["{{item.employee_id}}"]}},
    ]))
    assert run(sc.run_shortcut("each"))["requires_confirm"]
    assert run(sc.run_shortcut("each", confirm=True))["result"] == [
        {"approved": [1]}, {"approved": [2]}]
    run(sc.save_shortcut("big", "d", params={"xs": ""}, steps=[
        {"tool": "get_queue", "foreach": "{{params.xs}}", "args": {"date_from": "x"}}]))
    out = run(sc.run_shortcut("big", {"xs": list(range(101))}))
    assert not out["ran"] and "limit" in out["steps"][0]["error"]


def test_review_fixes(env):
    mcp, sc, calls, _ = env
    saved = run(sc.save_shortcut("s", "d", steps=[
        {"tool": "get_queue", "args": {"date_from": "x"}},
        {"tool": "get_queue", "args": {"date_from": "y"}}]))
    run(sc.save_shortcut("s", "new description", steps=saved["shortcut"]["steps"]))

    with pytest.raises(shortcuts.ShortcutError, match="share the name"):
        shortcuts._pick({"input": {}, "fields": ["hours", "prior.hours"]})

    joined = shortcuts._join({"left": [{"k": 8}], "right": [{"k": 8.0, "v": 1}, {"k": "8", "v": 2}],
                              "on": "k", "how": "inner", "many": True})
    assert [r["v"] for r in joined[0]["joined"]] == [1, 2]

    @mcp.tool()
    async def maybe_fail(n: int) -> int:
        if n == 2:
            raise RuntimeError("boom")
        return n

    run(sc.save_shortcut("f", "d", params={"ns": ""}, steps=[
        {"tool": "maybe_fail", "foreach": "{{params.ns}}", "args": {"n": "{{item}}"}}]))
    out = run(sc.run_shortcut("f", {"ns": [0, 1, 2, 3]}))
    assert not out["ran"] and out["steps"][0]["completed_items"] == [0, 1]

    steps = [{"id": "a", "op": "value", "value": "x" * 1000}]
    prev = "a"
    for n in range(12):
        steps.append({"id": f"b{n}", "op": "value", "value": [f"{{{{steps.{prev}}}}}"] * 2})
        prev = f"b{n}"
    run(sc.save_shortcut("bomb", "d", steps=steps))
    out = run(sc.run_shortcut("bomb"))
    assert not out["ran"] and "limit" in out["steps"][-1]["error"]
