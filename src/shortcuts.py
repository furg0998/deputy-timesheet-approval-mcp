"""Per-user shortcuts and rules for any FastMCP server.

A shortcut is a saved sequence of steps that an AI can replay with a single
`run_shortcut` call. A rule is a standing instruction the user wants every
AI session to follow. Both are stored per user through a small storage
interface, so this module has no dependency on the host server and can be
reused by other MCP servers.

Each step either calls one of the server's tools:
  {"id": "queue", "tool": "get_approval_queue", "args": {...}}
  {"tool": "flag_employee", "foreach": "{{steps.long.*}}",
   "args": {"employee_id": "{{item.employee_id}}", ...}}   # once per item
or transforms earlier results in-process (no tool call):
  {"op": "filter", "input": ..., "where": [{"field", "op", "value"}],
   "sort_by": ..., "descending": bool, "limit": n}
  {"op": "pick", "input": ..., "fields": ["a", "b.c"] | {"new": "path"}}
  {"op": "join", "left": ..., "right": ..., "on": "key" |
   {"left": path, "right": path}, "as": "joined", "how": "left"|"inner",
   "many": bool}
  {"op": "value", "value": ...}   # build an object from earlier results

Any value may contain `{{...}}` references:
  {{params.<name>}}             a parameter passed to run_shortcut
  {{steps.<id|index>.<path>}}   output of an earlier step
  {{item.<path>}}               current item, inside a foreach step's args
Path segments are dict keys, list indexes, or `*` to map over a list. A value
that is exactly one reference keeps its type (list, int, ...); references
inside longer strings are interpolated as text.
"""
import operator
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
MAX_FOREACH = 100
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


def _resolve_ref(ref: str, scopes: dict) -> Any:
    head, *path = ref.split(".")
    if head == "params" and path:
        if path[0] not in scopes["params"]:
            raise ShortcutError(f"missing parameter '{path[0]}'")
        return _walk(scopes["params"][path[0]], path[1:])
    if head == "steps" and path:
        if path[0] not in scopes["steps"]:
            raise ShortcutError(f"unknown step '{path[0]}'")
        return _walk(scopes["steps"][path[0]], path[1:])
    if head == "item" and "item" in scopes:
        return _walk(scopes["item"], path)
    raise ShortcutError(f"invalid reference '{{{{{ref}}}}}'")


def render(value: Any, scopes: dict) -> Any:
    """Substitute {{...}} references. scopes holds params, steps and,
    inside a foreach, item."""
    if isinstance(value, str):
        whole = _REF_RE.fullmatch(value.strip())
        if whole:
            return _resolve_ref(whole.group(1), scopes)
        return _REF_RE.sub(
            lambda m: str(_resolve_ref(m.group(1), scopes)), value
        )
    if isinstance(value, list):
        return [render(v, scopes) for v in value]
    if isinstance(value, dict):
        return {k: render(v, scopes) for k, v in value.items()}
    return value


# --- in-process data steps ---


def _get(obj: Any, path: str) -> Any:
    if path in ("", None):
        return obj
    try:
        return _walk(obj, str(path).split("."))
    except (ShortcutError, IndexError):
        return None


def _safe(fn):
    def compare(a, b):
        try:
            return bool(fn(a, b))
        except TypeError:
            return False
    return compare


_COMPARE = {
    "==": _safe(operator.eq), "!=": _safe(operator.ne),
    ">": _safe(operator.gt), ">=": _safe(operator.ge),
    "<": _safe(operator.lt), "<=": _safe(operator.le),
    "in": _safe(lambda a, b: a in b),
    "not_in": _safe(lambda a, b: a not in b),
    "contains": _safe(lambda a, b: b in a),
    "exists": lambda a, b: (a is not None) == (True if b is None else bool(b)),
}

_OP_REQUIRED = {
    "filter": ("input",),
    "pick": ("input", "fields"),
    "join": ("left", "right", "on"),
    "value": ("value",),
}


def _as_list(value: Any, what: str) -> list:
    if not isinstance(value, list):
        raise ShortcutError(f"{what} must be a list, got {type(value).__name__}")
    return value


def _filter(spec: dict) -> list:
    items = _as_list(spec["input"], "filter input")
    where = spec.get("where") or []
    if isinstance(where, dict):
        where = [where]
    for cond in where:
        if cond.get("op", "==") not in _COMPARE:
            raise ShortcutError(
                f"unknown filter op '{cond.get('op')}'; use one of "
                + ", ".join(_COMPARE)
            )
    out = [
        item for item in items
        if all(
            _COMPARE[c.get("op", "==")](_get(item, c.get("field")), c.get("value"))
            for c in where
        )
    ]
    if spec.get("sort_by") is not None:
        key = spec["sort_by"]
        present = [i for i in out if _get(i, key) is not None]
        missing = [i for i in out if _get(i, key) is None]
        try:
            present.sort(key=lambda i: _get(i, key),
                         reverse=bool(spec.get("descending")))
        except TypeError as e:
            raise ShortcutError(f"cannot sort by '{key}': {e}") from e
        out = present + missing
    if spec.get("limit") is not None:
        out = out[: int(spec["limit"])]
    return out


def _pick(spec: dict) -> Any:
    fields = spec["fields"]
    if isinstance(fields, list):
        names = [f.split(".")[-1] for f in fields]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ShortcutError(
                f"pick fields share the name {', '.join(dupes)}; use the "
                "object form {new_name: path} to rename them"
            )
        fields = dict(zip(names, fields))
    if not isinstance(fields, dict):
        raise ShortcutError("pick fields must be a list or an object")

    def one(item):
        return {name: _get(item, path) for name, path in fields.items()}

    value = spec["input"]
    return [one(i) for i in value] if isinstance(value, list) else one(value)


def _key(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _join(spec: dict) -> list:
    left = _as_list(spec["left"], "join left")
    right = _as_list(spec["right"], "join right")
    on = spec["on"]
    lk, rk = (on.get("left"), on.get("right")) if isinstance(on, dict) else (on, on)
    name = spec.get("as") or "joined"
    how = spec.get("how") or "left"
    if how not in ("left", "inner"):
        raise ShortcutError("join how must be 'left' or 'inner'")
    out = []
    for item in left:
        key = _key(_get(item, lk))
        matches = [r for r in right if key is not None and _key(_get(r, rk)) == key]
        if how == "inner" and not matches:
            continue
        joined = matches if spec.get("many") else (matches[0] if matches else None)
        out.append({**item, name: joined} if isinstance(item, dict)
                   else {"value": item, name: joined})
    return out


_OPS = {
    "filter": _filter,
    "pick": _pick,
    "join": _join,
    "value": lambda spec: spec["value"],
}


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
            if not isinstance(step, dict) or ("tool" in step) == ("op" in step):
                raise ShortcutError(
                    f"step {i}: needs either a 'tool' or an 'op'"
                )
            step_id = str(i)
            if step.get("id") is not None:
                step_id = str(step["id"])
                if not _NAME_RE.match(step_id) or (
                    step_id.isdigit() and step_id != str(i)
                ):
                    raise ShortcutError(
                        f"step {i}: id must be lowercase letters, digits, - or "
                        "_ and not just a number"
                    )
            if step_id in seen:
                raise ShortcutError(f"step {i}: duplicate id '{step_id}'")

            if "tool" in step:
                tool = step["tool"]
                if not isinstance(tool, str) or not self._tool(tool):
                    raise ShortcutError(f"step {i}: unknown tool '{tool}'")
                if tool in _SELF_TOOLS:
                    raise ShortcutError(f"step {i}: shortcuts cannot call {tool}")
                args = step.get("args") or {}
                if not isinstance(args, dict):
                    raise ShortcutError(f"step {i}: 'args' must be an object")
                item = {"id": step_id, "tool": tool, "args": args}
                checks = [(args, "foreach" in step)]
                if "foreach" in step:
                    item["foreach"] = step["foreach"]
                    checks.append((step["foreach"], False))
            else:
                op = step["op"]
                if op not in _OPS:
                    raise ShortcutError(
                        f"step {i}: unknown op '{op}'; use one of "
                        + ", ".join(_OPS)
                    )
                missing = [k for k in _OP_REQUIRED[op] if k not in step]
                if missing:
                    raise ShortcutError(
                        f"step {i}: op '{op}' needs {', '.join(missing)}"
                    )
                item = {"id": step_id, **{k: v for k, v in step.items()
                                          if k != "id"}}
                checks = [({k: v for k, v in step.items()
                            if k not in ("id", "op")}, False)]

            for value, item_ok in checks:
                for ref in _refs(value):
                    head, *path = ref.split(".")
                    if head == "params" and path and path[0] in params:
                        continue
                    if head == "steps" and path and path[0] in seen:
                        continue
                    if head == "item" and item_ok:
                        continue
                    raise ShortcutError(
                        f"step {i}: reference '{{{{{ref}}}}}' must name a "
                        "declared parameter or an earlier step"
                        + ("" if item_ok else
                           " ({{item...}} only works in a foreach step's args)")
                    )
            seen.update({step_id, str(i)})
            clean.append(item)
        return clean

    def _summary(self, item: dict) -> dict:
        tools = [s["tool"] for s in item["steps"] if "tool" in s]
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
        guarded = sorted({
            s["tool"] for s in item["steps"]
            if "tool" in s and self._is_guarded(s["tool"])
        })
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
        scopes = {"params": values, "steps": outputs}
        log = []
        for i, step in enumerate(item["steps"]):
            name_ = step.get("tool") or f"op:{step['op']}"
            try:
                result = await self._run_step(step, scopes, context)
            except Exception as e:
                log.append({"step": i, "tool": name_, "error": str(e)})
                return {"ran": False, "failed_step": i, "steps": log,
                        "results": outputs if include_all_results else None}
            outputs[step["id"]] = outputs[str(i)] = result
            log.append({"step": i, "tool": name_, "ok": True})
        out = {"ran": True, "steps": log, "result": result}
        if include_all_results:
            out["results"] = {s["id"]: outputs[s["id"]] for s in item["steps"]}
        return out

    async def _run_step(self, step: dict, scopes: dict, context) -> Any:
        if "op" in step:
            spec = render(
                {k: v for k, v in step.items() if k not in ("id", "op")}, scopes
            )
            return _OPS[step["op"]](spec)
        call = self.mcp._tool_manager.call_tool
        if "foreach" not in step:
            return await call(step["tool"], render(step["args"], scopes),
                              context=context)
        items = _as_list(render(step["foreach"], scopes), "foreach")
        if len(items) > MAX_FOREACH:
            raise ShortcutError(
                f"foreach has {len(items)} items; the limit is {MAX_FOREACH}"
            )
        return [
            await call(step["tool"], render(step["args"], {**scopes, "item": it}),
                       context=context)
            for it in items
        ]

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
            "sequence of steps replayed later with run_shortcut. Saving never "
            "runs the steps.\n"
            "Tool step: {\"id\": label, \"tool\": name, \"args\": {...}}; add "
            "\"foreach\": <list ref> to call the tool once per item, using "
            "{{item.<path>}} in args.\n"
            "Data steps (no tool call): "
            "{\"op\": \"filter\", \"input\": ref, \"where\": [{\"field\": path, "
            "\"op\": ==|!=|>|>=|<|<=|in|not_in|contains|exists, \"value\": v}], "
            "\"sort_by\": path, \"descending\": bool, \"limit\": n}; "
            "{\"op\": \"pick\", \"input\": ref, \"fields\": [paths] or "
            "{new_name: path}}; "
            "{\"op\": \"join\", \"left\": ref, \"right\": ref, \"on\": key or "
            "{\"left\": path, \"right\": path}, \"as\": name, \"how\": "
            "left|inner, \"many\": bool} (adds the matching right item under "
            "'as'); {\"op\": \"value\", \"value\": any} (build an object from "
            "earlier results).\n"
            "References: {{params.<name>}} (declare in params as name -> "
            "description; optional defaults) and {{steps.<id|index>.<path>}}; "
            "path segments are keys, list indexes or '*' to map over a list, "
            "e.g. {{steps.queue.queue.*.employee_id}}."
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
