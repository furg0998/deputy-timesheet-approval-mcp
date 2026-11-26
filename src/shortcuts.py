"""Per-user shortcuts and rules for any FastMCP server.

A shortcut is a saved sequence of calls to the server's own tools that an AI
can replay with a single `run_shortcut` call. A rule is a standing
instruction the user wants every AI session to follow. Both are stored per
user through a small storage interface, so this module has no dependency on
the host server and can be reused by other MCP servers.

Step arguments may contain `{{...}}` references:
  {{params.<name>}}             a parameter passed to run_shortcut
  {{steps.<id|index>.<path>}}   output of an earlier step; path segments are
                                dict keys, list indexes, or `*` to map over
                                a list
A value that is exactly one reference keeps its type (list, int, ...);
references inside longer strings are interpolated as text.
"""

import re
import time
import uuid
from typing import Any, Callable, Protocol

from mcp.server.fastmcp import FastMCP

INSTRUCTIONS = (
    "This server supports per-user shortcuts and rules. At the start of a "
    "session call list_rules and follow them, and call list_shortcuts to see "
    "saved multi-step workflows. When the user repeats a multi-call task, "
    "offer to save it with save_shortcut, then use run_shortcut next time."
)

MAX_STEPS = 20
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REF_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_SELF_TOOLS = {
    "save_shortcut", "list_shortcuts", "run_shortcut", "delete_shortcut",
    "save_rule", "list_rules", "delete_rule",
}


class Storage(Protocol):
    def put(self, user_id: str, kind: str, key: str, item: dict) -> None: ...
    def get(self, user_id: str, kind: str, key: str) -> dict | None: ...
    def list(self, user_id: str, kind: str) -> list[dict]: ...
    def delete(self, user_id: str, kind: str, key: str) -> bool: ...


class MemoryStorage:
    """In-process storage, for tests and local development."""

    def __init__(self):
        self._data: dict[tuple[str, str], dict[str, dict]] = {}

    def put(self, user_id, kind, key, item):
        self._data.setdefault((user_id, kind), {})[key] = item

    def get(self, user_id, kind, key):
        return self._data.get((user_id, kind), {}).get(key)

    def list(self, user_id, kind):
        return list(self._data.get((user_id, kind), {}).values())

    def delete(self, user_id, kind, key):
        return self._data.get((user_id, kind), {}).pop(key, None) is not None


class ShortcutError(ValueError):
    pass


def _refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return _REF_RE.findall(value)
    if isinstance(value, list):
        return [r for v in value for r in _refs(v)]
    if isinstance(value, dict):
        return [r for v in value.values() for r in _refs(v)]
    return []


def _walk(value: Any, path: list[str]) -> Any:
    for i, seg in enumerate(path):
        if seg == "*":
            if not isinstance(value, list):
                raise ShortcutError("'*' used on a value that is not a list")
            return [_walk(v, path[i + 1:]) for v in value]
        if isinstance(value, dict):
            if seg not in value:
                raise ShortcutError(f"key '{seg}' not found")
            value = value[seg]
        elif isinstance(value, list) and seg.lstrip("-").isdigit():
            value = value[int(seg)]
        else:
            raise ShortcutError(f"cannot read '{seg}' from {type(value).__name__}")
    return value


def _resolve_ref(ref: str, params: dict, outputs: dict) -> Any:
    head, *path = ref.split(".")
    if head == "params" and path:
        if path[0] not in params:
            raise ShortcutError(f"missing parameter '{path[0]}'")
        return _walk(params[path[0]], path[1:])
    if head == "steps" and path:
        if path[0] not in outputs:
            raise ShortcutError(f"unknown step '{path[0]}'")
        return _walk(outputs[path[0]], path[1:])
    raise ShortcutError(f"invalid reference '{{{{{ref}}}}}'")


def render(value: Any, params: dict, outputs: dict) -> Any:
    """Substitute {{...}} references in step arguments."""
    if isinstance(value, str):
        whole = _REF_RE.fullmatch(value.strip())
        if whole:
            return _resolve_ref(whole.group(1), params, outputs)
        return _REF_RE.sub(
            lambda m: str(_resolve_ref(m.group(1), params, outputs)), value
        )
    if isinstance(value, list):
        return [render(v, params, outputs) for v in value]
    if isinstance(value, dict):
        return {k: render(v, params, outputs) for k, v in value.items()}
    return value


class Shortcuts:
    """Registers shortcut and rule tools on a FastMCP server.

    get_user_id returns the id of the authenticated user for the current
    request. Tools annotated with destructiveHint (or listed in
    guarded_tools) only run inside a shortcut when run_shortcut is called
    with confirm=True.
    """

    def __init__(
        self,
        mcp: FastMCP,
        storage: Storage,
        get_user_id: Callable[[], str],
        guarded_tools: set[str] | None = None,
    ):
        self.mcp = mcp
        self.storage = storage
        self.get_user_id = get_user_id
        self.extra_guarded = set(guarded_tools or ())

    # --- helpers ---

    def _tool(self, name: str):
        return self.mcp._tool_manager.get_tool(name)

    def _is_guarded(self, name: str) -> bool:
        if name in self.extra_guarded:
            return True
        tool = self._tool(name)
        return bool(tool and tool.annotations and tool.annotations.destructiveHint)

    def validate(self, name: str, steps: list[dict], params: dict) -> list[dict]:
        if not _NAME_RE.match(name):
            raise ShortcutError(
                "name must be 1-64 chars of lowercase letters, digits, - or _"
            )
        if not steps:
            raise ShortcutError("a shortcut needs at least one step")
        if len(steps) > MAX_STEPS:
            raise ShortcutError(f"a shortcut can have at most {MAX_STEPS} steps")
        seen: set[str] = set()
        clean = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
                raise ShortcutError(f"step {i}: needs a 'tool' name")
            tool = step["tool"]
            if tool in _SELF_TOOLS:
                raise ShortcutError(f"step {i}: shortcuts cannot call {tool}")
            if not self._tool(tool):
                raise ShortcutError(f"step {i}: unknown tool '{tool}'")
            args = step.get("args") or {}
            if not isinstance(args, dict):
                raise ShortcutError(f"step {i}: 'args' must be an object")
            step_id = str(i)
            if step.get("id") is not None:
                step_id = str(step["id"])
                if not _NAME_RE.match(step_id) or step_id.isdigit():
                    raise ShortcutError(
                        f"step {i}: id must be lowercase letters, digits, - or "
                        "_ and not just a number"
                    )
            if step_id in seen:
                raise ShortcutError(f"step {i}: duplicate id '{step_id}'")
            for ref in _refs(args):
                head, *path = ref.split(".")
                if head == "params" and path and path[0] in params:
                    continue
                if head == "steps" and path and path[0] in seen:
                    continue
                raise ShortcutError(
                    f"step {i}: reference '{{{{{ref}}}}}' must name a declared "
                    "parameter or an earlier step"
                )
            seen.update({step_id, str(i)})
            clean.append({"id": step_id, "tool": tool, "args": args})
        return clean

    def _summary(self, item: dict) -> dict:
        tools = [s["tool"] for s in item["steps"]]
        return {
            **item,
            "requires_confirm": any(self._is_guarded(t) for t in tools),
        }

    # --- tool implementations ---

    async def save_shortcut(
        self,
        name: str,
        description: str,
        steps: list[dict],
        params: dict[str, str] | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> dict:
        params = params or {}
        clean = self.validate(name, steps, params)
        user_id = self.get_user_id()
        existing = self.storage.get(user_id, "shortcut", name)
        now = int(time.time())
        item = {
            "name": name,
            "description": description,
            "params": params,
            "defaults": defaults or {},
            "steps": clean,
            "created_at": (existing or {}).get("created_at", now),
            "updated_at": now,
        }
        self.storage.put(user_id, "shortcut", name, item)
        return {"saved": True, "updated": existing is not None,
                "shortcut": self._summary(item)}

    async def list_shortcuts(self) -> list[dict]:
        items = self.storage.list(self.get_user_id(), "shortcut")
        return [self._summary(i) for i in sorted(items, key=lambda i: i["name"])]

    async def delete_shortcut(self, name: str) -> dict:
        return {"deleted": self.storage.delete(self.get_user_id(), "shortcut", name)}

    async def run_shortcut(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        confirm: bool = False,
        include_all_results: bool = False,
    ) -> dict:
        item = self.storage.get(self.get_user_id(), "shortcut", name)
        if not item:
            raise ShortcutError(f"no shortcut named '{name}'")
        guarded = sorted({s["tool"] for s in item["steps"] if self._is_guarded(s["tool"])})
        if guarded and not confirm:
            return {
                "ran": False,
                "requires_confirm": True,
                "guarded_tools": guarded,
                "message": (
                    "This shortcut calls tools that change live data. Ask the "
                    "user to confirm, then call run_shortcut again with "
                    "confirm=true."
                ),
            }
        values = {**(item.get("defaults") or {}), **(params or {})}
        missing = [p for p in item.get("params", {}) if p not in values]
        if missing:
            raise ShortcutError(f"missing parameters: {', '.join(missing)}")

        context = self.mcp.get_context()
        outputs: dict[str, Any] = {}
        log = []
        for i, step in enumerate(item["steps"]):
            try:
                args = render(step["args"], values, outputs)
                result = await self.mcp._tool_manager.call_tool(
                    step["tool"], args, context=context
                )
            except Exception as e:
                log.append({"step": i, "tool": step["tool"], "error": str(e)})
                return {"ran": False, "failed_step": i, "steps": log,
                        "results": outputs if include_all_results else None}
            outputs[step["id"]] = outputs[str(i)] = result
            log.append({"step": i, "tool": step["tool"], "ok": True})
        out = {"ran": True, "steps": log, "result": result}
        if include_all_results:
            out["results"] = {s["id"]: outputs[s["id"]] for s in item["steps"]}
        return out

    async def save_rule(self, rule: str, rule_id: str | None = None) -> dict:
        rule_id = rule_id or uuid.uuid4().hex[:8]
        if not _NAME_RE.match(rule_id):
            raise ShortcutError(
                "rule_id must be 1-64 chars of lowercase letters, digits, - or _"
            )
        item = {"rule_id": rule_id, "rule": rule, "updated_at": int(time.time())}
        self.storage.put(self.get_user_id(), "rule", rule_id, item)
        return {"saved": True, **item}

    async def list_rules(self) -> list[dict]:
        items = self.storage.list(self.get_user_id(), "rule")
        return sorted(items, key=lambda i: i.get("updated_at", 0))

    async def delete_rule(self, rule_id: str) -> dict:
        return {"deleted": self.storage.delete(self.get_user_id(), "rule", rule_id)}

    # --- registration ---

    def register(self) -> "Shortcuts":
        add = self.mcp.add_tool
        add(self.save_shortcut, name="save_shortcut", description=(
            "Save (or overwrite) a named shortcut for the current user: a "
            "sequence of calls to this server's tools that can be replayed "
            "with run_shortcut. steps is a list of {\"tool\": name, \"args\": "
            "{...}, \"id\": optional label}. Args may reference "
            "{{params.<name>}} (declared in params as name -> description, "
            "optional defaults) or earlier outputs via "
            "{{steps.<id|index>.<path>}}; use '*' in a path to map over a "
            "list, e.g. {{steps.queue.queue.*.employee_id}}. Saving never "
            "runs the steps."
        ))
        add(self.list_shortcuts, name="list_shortcuts", description=(
            "List the current user's saved shortcuts with their steps, "
            "parameters, and whether they need confirm=true to run."
        ))
        add(self.run_shortcut, name="run_shortcut", description=(
            "Run a saved shortcut with the given params. Steps run in order "
            "and stop at the first error; returns the last step's result "
            "(set include_all_results for every step). If any step changes "
            "live data the call returns requires_confirm without running; "
            "only pass confirm=true after the user explicitly asks to run "
            "that shortcut."
        ))
        add(self.delete_shortcut, name="delete_shortcut",
            description="Delete one of the current user's shortcuts.")
        add(self.save_rule, name="save_rule", description=(
            "Save a standing instruction for the current user that every AI "
            "session should follow (e.g. 'always flag shifts over 10h'). "
            "Pass an existing rule_id to replace that rule."
        ))
        add(self.list_rules, name="list_rules", description=(
            "List the current user's saved rules. Call at the start of a "
            "session and follow them."
        ))
        add(self.delete_rule, name="delete_rule",
            description="Delete one of the current user's rules.")
        return self
