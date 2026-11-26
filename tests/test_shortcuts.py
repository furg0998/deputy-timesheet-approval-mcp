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
