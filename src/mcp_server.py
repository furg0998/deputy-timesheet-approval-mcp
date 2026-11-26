import inspect
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

import auth
import config
import deputy
import shortcuts
import store

mcp = FastMCP(
    "deputy-timesheet-approval",
    instructions=(
        "Approve Deputy timesheets with memory. Use get_approval_queue to "
        "see pending timesheets enriched with per-employee history, flags "
        "and notes from previous approval sessions. Approve, discard or "
        "unapprove with the write tools - every action is recorded so "
        "future sessions can spot unusual patterns. WARNING: this server is "
        "connected to a live Deputy account. Tools marked TRANSACTIONAL "
        "change real timesheets and payroll; never call them to test or "
        "explore, and only call them after the user has explicitly asked "
        "for that specific action. " + shortcuts.INSTRUCTIONS
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


def _user() -> dict:
    user = auth.current_user.get()
    if not user:
        raise RuntimeError("Not authenticated")
    return user


def _client() -> deputy.DeputyClient:
    return deputy.DeputyClient(_user())


_TRANSACTIONAL = (
    "TRANSACTIONAL: changes live Deputy payroll data. Do not call to test "
    "or explore; only call after the user explicitly asks for this action."
)


def _transactional_tool():
    def register(fn):
        return mcp.tool(
            description=f"{_TRANSACTIONAL}\n\n{inspect.cleandoc(fn.__doc__)}",
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=True
            ),
        )(fn)

    return register


def _tz() -> ZoneInfo:
    return ZoneInfo(config.DEPUTY_TIMEZONE)


def _today() -> date:
    return datetime.now(_tz()).date()


def _to_unix(d: str, days: int = 0) -> int:
    """Unix time of local midnight at the start of date d (+ days)."""
    day = datetime.strptime(d, "%Y-%m-%d").date() + timedelta(days=days)
    return int(datetime.combine(day, datetime.min.time(), _tz()).timestamp())


def _emp_name(ts: dict) -> str | None:
    return ((ts.get("_DPMetaData") or {}).get("EmployeeInfo") or {}).get(
        "DisplayName"
    )


async def _record(user: dict, ts_id: int, action: str, timesheet: dict | None):
    emp_id = int((timesheet or {}).get("Employee") or 0)
    if emp_id:
        store.record_approval_event(
            user["user_id"], emp_id, ts_id, action, timesheet
        )


_QUERY_MAX = 500
_BREAK_TYPES = {"M": "meal", "R": "rest"}


def _breaks(ts: dict) -> list[dict]:
    """Breaks from the timesheet's Slots; falls back to the Mealbreak
    duration for records without slot detail."""
    out = []
    for slot in ts.get("Slots") or []:
        if slot.get("strType") != "B" or slot.get("blnEmptySlot"):
            continue
        kind = (slot.get("mixedActivity") or {}).get("strBreakType")
        secs = int(slot.get("intEnd") or 0) - int(slot.get("intStart") or 0)
        out.append(
            {
                "type": _BREAK_TYPES.get(kind, kind or "unknown"),
                "minutes": max(secs, 0) // 60,
            }
        )
    if not out and (mins := deputy._mealbreak_minutes(ts)):
        out.append({"type": "meal", "minutes": mins})
    return out


def _self_created(ts: dict) -> bool:
    creator, emp = ts.get("Creator"), ts.get("Employee")
    return bool(creator) and int(creator) == int(emp or 0)


def _summary(t: dict) -> dict:
    return {
        "id": t["Id"],
        "employee_id": t.get("Employee"),
        "employee_name": _emp_name(t),
        "date": t.get("Date"),
        "start": t.get("StartTimeLocalized"),
        "end": t.get("EndTimeLocalized"),
        "hours": t.get("TotalTime"),
        "cost": t.get("Cost"),
        "breaks": _breaks(t),
        "time_approved": t.get("TimeApproved"),
        "pay_approved": t.get("PayRuleApproved"),
        "exported": t.get("Exported"),
        "pay_staged": t.get("PayStaged"),
        "pay_cycle_id": t.get("PaycycleId"),
        "discarded": t.get("Discarded"),
        "disputed": t.get("Disputed"),
        "in_progress": t.get("IsInProgress"),
        "is_leave": t.get("IsLeave"),
        "roster_id": t.get("Roster"),
        "self_created": _self_created(t),
        "created": t.get("Created"),
        "modified": t.get("Modified"),
        "employee_comment": t.get("EmployeeComment"),
    }


def _timesheet_anomalies(ts: dict) -> list[str]:
    out: list[str] = []
    total = ts.get("TotalTime") or 0
    in_progress = ts.get("IsInProgress")
    leave = ts.get("IsLeave")
    if not in_progress and (
        not total or not ts.get("StartTime") or not ts.get("EndTime")
    ):
        out.append("empty: no hours or missing start/end time")
    if total > 10:
        out.append(f"long shift: {total:.1f}h")
    if ts.get("Disputed"):
        out.append("marked as disputed")
    breaks = _breaks(ts)
    if total > 5 and not leave and not any(
        b["type"] == "meal" for b in breaks
    ):
        if breaks:
            out.append(f"{total:.1f}h shift with no meal break (rest break only)")
        else:
            out.append(f"{total:.1f}h shift with no break recorded")
    if total and not ts.get("Cost") and not leave:
        out.append(
            "$0 cost (pay rules not yet approved)"
            if not ts.get("PayRuleApproved")
            else "$0 cost"
        )
    if _self_created(ts) and not ts.get("Roster") and not leave:
        out.append("created by the employee with no rostered shift")
    day = str(ts.get("Date") or "")[:10]
    return [f"timesheet {ts.get('Id')} ({day}): {a}" for a in out]


def _anomalies(emp_id: int, timesheets: list[dict], profile: dict,
               history: list[dict]) -> list[str]:
    out: list[str] = []
    for f in profile.get("flags", [])[-3:]:
        out.append(f"flagged: {f.get('reason')} ({f.get('type')})")
    for ts in timesheets:
        out.extend(_timesheet_anomalies(ts))
    past_hours = [
        float(h["total_time"]) for h in history if h.get("total_time")
    ]
    avg = sum(past_hours) / len(past_hours) if past_hours else 0
    if avg:
        for ts in timesheets:
            total = ts.get("TotalTime") or 0
            if total and abs(total - avg) / avg > 0.3:
                out.append(
                    f"timesheet {ts.get('Id')}: {total:.1f}h is >30% off "
                    f"this employee's usual {avg:.1f}h"
                )
    return list(dict.fromkeys(out))


def _parse_since(value: str) -> datetime:
    """YYYY-MM-DD (local midnight) or an ISO datetime."""
    if len(value) == 10:
        return datetime.fromtimestamp(_to_unix(value), _tz())
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=_tz())


@mcp.tool()
async def get_approval_queue(
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """List employees with pending timesheets for a date range, enriched
    with stored history, flags and notes. Dates are YYYY-MM-DD; defaults to
    the previous 14 days ending today. Start here for weekly approvals."""
    user = _user()
    client = _client()
    end = _today()
    start = end - timedelta(days=14)
    df = date_from or start.isoformat()
    dt = date_to or end.isoformat()

    timesheets = await client.query_timesheets(
        _to_unix(df), _to_unix(dt, days=1), time_approved=False,
        discarded=False, max_results=_QUERY_MAX,
    )

    by_emp: dict[int, list[dict]] = {}
    for ts in timesheets:
        by_emp.setdefault(int(ts["Employee"]), []).append(ts)
    all_ids = sorted(by_emp)
    profiles = store.get_employee_profiles(user["user_id"], all_ids)
    names = {
        emp_id: next((n for n in map(_emp_name, sheets) if n), None)
        or profiles.get(emp_id, {}).get("employee_name")
        for emp_id, sheets in by_emp.items()
    }
    missing = [e for e, n in names.items() if not n]
    if missing:
        for emp in await client.query_employees(missing):
            names[int(emp["Id"])] = emp.get("DisplayName")

    queue = []
    for emp_id in all_ids:
        sheets = by_emp.get(emp_id, [])
        profile = profiles.get(emp_id, {})
        history = store.get_approval_history(
            user["user_id"], emp_id, limit=30
        )
        queue.append(
            {
                "employee_id": emp_id,
                "employee_name": names.get(emp_id),
                "watch": profile.get("watch", False),
                "flags": profile.get("flags", []),
                "notes": profile.get("notes", []),
                "prior_approvals": len(history),
                "anomalies": _anomalies(
                    emp_id, sheets, profile, history
                ),
                "pending_timesheets": [
                    {
                        k: v
                        for k, v in _summary(t).items()
                        if k not in ("employee_id", "employee_name")
                    }
                    for t in sheets
                ],
                "pending_count": len(sheets),
            }
        )
    return {
        "date_from": df,
        "date_to": dt,
        "employee_count": len(queue),
        "truncated": len(timesheets) >= _QUERY_MAX,
        "queue": queue,
    }


@mcp.tool()
async def list_timesheets(
    date_from: str,
    date_to: str,
    employee_id: int | None = None,
    employee_name: str | None = None,
    approved: bool | None = None,
    exported: bool | None = None,
    modified_since: str | None = None,
    include_discarded: bool = False,
    limit: int = 100,
) -> dict:
    """Query timesheets whose shift starts between date_from and date_to
    (YYYY-MM-DD, inclusive). Optional filters: employee_id, employee_name
    (case-insensitive partial match), approved (time approval), exported
    (sent to payroll), modified_since (YYYY-MM-DD or ISO datetime; matches
    records created or edited since then) and include_discarded (discarded
    timesheets are hidden by default). Returns at most `limit` rows;
    `truncated` is true when more matched, so narrow the filters."""
    client = _client()
    sheets = await client.query_timesheets(
        _to_unix(date_from),
        _to_unix(date_to, days=1),
        employee_id=employee_id,
        time_approved=approved,
        discarded=None if include_discarded else False,
        max_results=_QUERY_MAX,
    )
    capped = len(sheets) >= _QUERY_MAX
    if employee_name:
        names = {int(t["Employee"]): _emp_name(t) for t in sheets if _emp_name(t)}
        missing = {int(t["Employee"]) for t in sheets} - set(names)
        for emp in await client.query_employees(sorted(missing)):
            names[int(emp["Id"])] = emp.get("DisplayName")
        needle = employee_name.casefold()
        sheets = [
            t for t in sheets
            if needle
            in (_emp_name(t) or names.get(int(t["Employee"])) or "").casefold()
        ]
    if exported is not None:
        sheets = [t for t in sheets if bool(t.get("Exported")) == exported]
    if modified_since:
        since = _parse_since(modified_since)
        sheets = [
            t for t in sheets
            if (stamp := t.get("Modified") or t.get("Created"))
            and datetime.fromisoformat(stamp) >= since
        ]
    limit = max(1, min(limit, _QUERY_MAX))
    return {
        "count": len(sheets),
        "truncated": capped or len(sheets) > limit,
        "timesheets": [_summary(t) for t in sheets[:limit]],
    }


@mcp.tool()
async def get_timesheets(timesheet_ids: list[int]) -> dict:
    """Summaries (times, breaks, cost, approval/export state, created and
    modified times) for up to 100 timesheets in one Deputy call. Prefer this
    over calling get_timesheet_detail in a loop."""
    ids = list(dict.fromkeys(int(i) for i in timesheet_ids))
    if len(ids) > 100:
        raise ValueError("At most 100 timesheet_ids per call")
    sheets = await _client().get_timesheets(ids)
    found = {int(t["Id"]) for t in sheets}
    return {
        "count": len(sheets),
        "timesheets": [_summary(t) for t in sheets],
        "not_found": [i for i in ids if i not in found],
    }


@mcp.tool()
async def get_timesheet_detail(timesheet_id: int, full: bool = False) -> dict:
    """Detail for one timesheet: summary, anomalies, pay-range summary and
    any timesheet questions Deputy requires before approval. Pass full=true
    to include the raw Deputy record."""
    client = _client()
    ts = await client.get_timesheet(timesheet_id)
    if not ts:
        raise deputy.DeputyError(f"Timesheet {timesheet_id} not found")
    summary = await client.range_summary(timesheet_id)
    try:
        questions = await client.timesheet_questions(timesheet_id, ts)
    except deputy.DeputyError as e:
        questions = {"error": str(e)}
    return {
        "timesheet": ts if full else _summary(ts),
        "anomalies": _timesheet_anomalies(ts),
        "range_summary": summary,
        "questions": questions,
    }


@mcp.tool()
async def suggest_rounding(timesheet_id: int) -> dict:
    """Get Deputy's suggested rounding for a timesheet before approving.
    Read-only: does not change the timesheet."""
    return await _client().suggest_rounding(timesheet_id)


@_transactional_tool()
async def approve_timesheet(
    timesheet_id: int,
    set_properties: dict | None = None,
    set_answers: dict | None = None,
) -> dict:
    """Approve a timesheet for time AND pay. Optionally pass setProperties /
    setAnswers to override properties or answer required questions. The
    approval is recorded in local history for future anomaly detection."""
    user = _user()
    client = _client()
    ts = await client.get_timesheet(timesheet_id)
    result = await client.approve(
        timesheet_id,
        set_properties=set_properties,
        set_answers=set_answers,
        timesheet=ts,
    )
    await _record(user, timesheet_id, "approve", ts)
    return {"result": result, "recorded": True}


@_transactional_tool()
async def approve_time_only(timesheet_id: int) -> dict:
    """Approve only the time component of a timesheet (not pay)."""
    user = _user()
    client = _client()
    ts = await client.get_timesheet(timesheet_id)
    result = await client.approve_time(timesheet_id, ts)
    await _record(user, timesheet_id, "approve-time", ts)
    return {"result": result, "recorded": True}


@_transactional_tool()
async def batch_approve(timesheet_ids: list[int]) -> dict:
    """Approve multiple timesheets (time and pay) in one call. Use for the
    clean part of the weekly queue after anomalies are reviewed."""
    user = _user()
    client = _client()
    result = await client.batch("approve", timesheet_ids)
    for ts_id in timesheet_ids:
        ts = await client.get_timesheet(ts_id)
        await _record(user, ts_id, "batch-approve", ts)
    return {"result": result, "recorded": len(timesheet_ids)}


@_transactional_tool()
async def unapprove_timesheet(timesheet_id: int) -> dict:
    """Unapprove a timesheet (both time and pay components)."""
    user = _user()
    client = _client()
    ts = await client.get_timesheet(timesheet_id)
    result = await client.unapprove(timesheet_id)
    await _record(user, timesheet_id, "unapprove", ts)
    return {"result": result, "recorded": True}


@_transactional_tool()
async def unapprove_pay(timesheet_id: int) -> dict:
    """Unapprove only the pay component, leaving time approved."""
    user = _user()
    client = _client()
    ts = await client.get_timesheet(timesheet_id)
    result = await client.unapprove_pay(timesheet_id)
    await _record(user, timesheet_id, "unapprove-pay", ts)
    return {"result": result, "recorded": True}


@_transactional_tool()
async def discard_timesheet(timesheet_id: int) -> dict:
    """Discard a timesheet (e.g. a duplicate or erroneous record)."""
    user = _user()
    client = _client()
    ts = await client.get_timesheet(timesheet_id)
    result = await client.discard(timesheet_id)
    await _record(user, timesheet_id, "discard", ts)
    return {"result": result, "recorded": True}


@_transactional_tool()
async def reinstate_timesheet(timesheet_id: int) -> dict:
    """Reinstate a previously discarded timesheet."""
    user = _user()
    client = _client()
    ts = await client.get_timesheet(timesheet_id)
    result = await client.reinstate(timesheet_id)
    await _record(user, timesheet_id, "reinstate", ts)
    return {"result": result, "recorded": True}


@_transactional_tool()
async def add_timesheet_comment(timesheet_id: int, content: str) -> dict:
    """Add a comment to a timesheet - useful for recording why a timesheet
    was or was not approved."""
    return await _client().add_comment(timesheet_id, content)


@mcp.tool()
async def save_employee_note(employee_id: int, note: str) -> dict:
    """Save a persistent note about an employee (e.g. 'asked for extra
    weekend shifts in November'). Notes show up in future queues."""
    return store.save_employee_note(
        _user()["user_id"], employee_id, note
    )


@mcp.tool()
async def flag_employee(
    employee_id: int,
    reason: str,
    flag_type: str = "watch",
    employee_name: str | None = None,
) -> dict:
    """Flag an employee for attention (e.g. repeated unusual approvals).
    Flagged employees are highlighted in get_approval_queue. flag_type is a
    free-form label like 'watch', 'recurring-anomaly', 'reviewed'."""
    return store.set_employee_flag(
        _user()["user_id"], employee_id, reason, flag_type, employee_name
    )


@mcp.tool()
async def clear_employee_flags(employee_id: int) -> dict:
    """Clear all flags on an employee once concerns are resolved."""
    return store.clear_employee_flags(_user()["user_id"], employee_id)


@mcp.tool()
async def get_employee_profile(employee_id: int) -> dict:
    """Stored profile for an employee: notes, flags, watch status and their
    approval history recorded through this server."""
    user = _user()
    profile = store.get_employee_profile(user["user_id"], employee_id)
    return {
        **profile,
        "history": store.get_approval_history(
            user["user_id"], employee_id, limit=50
        ),
    }


@mcp.tool()
async def get_approval_history(
    employee_id: int | None = None, limit: int = 50
) -> list[dict]:
    """Approval actions previously recorded via this server, newest first.
    Pass employee_id to scope to one employee."""
    return store.get_approval_history(
        _user()["user_id"], employee_id, limit
    )


@mcp.tool()
async def check_connection() -> dict:
    """Verify Deputy connectivity and permissions for the current user."""
    client = _client()
    perms = await client.config_and_permissions()
    return {
        "endpoint": client.endpoint,
        "user": (_user().get("deputy_me") or {}),
        "permissions": perms,
    }


shortcuts.Shortcuts(
    mcp, store.ShortcutStorage(), lambda: _user()["user_id"]
).register()


class _IdempotentLifespan:
    """Mangum runs ASGI lifespan startup/shutdown on every invocation, but
    StreamableHTTPSessionManager.run() can only be entered once. Start the
    manager on the first startup and just ack the rest."""

    def __init__(self, app, session_manager):
        self.app = app
        self.manager = session_manager
        self._started = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            return await self.app(scope, receive, send)
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                if not self._started:
                    self._started = True
                    self._cm = self.manager.run()
                    await self._cm.__aenter__()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def build_app():
    mcp.custom_route("/.well-known/oauth-authorization-server", ["GET"])(
        auth.authorization_server_metadata
    )
    mcp.custom_route("/.well-known/openid-configuration", ["GET"])(
        auth.authorization_server_metadata
    )
    mcp.custom_route("/.well-known/oauth-protected-resource", ["GET"])(
        auth.protected_resource_metadata
    )
    mcp.custom_route("/register", ["POST"])(auth.register)
    mcp.custom_route("/authorize", ["GET"])(auth.authorize)
    mcp.custom_route("/oauth/callback", ["GET"])(auth.oauth_callback)
    mcp.custom_route("/token", ["POST"])(auth.token)
    mcp.custom_route("/", ["GET"])(
        lambda r: JSONResponse(
            {"service": "deputy-timesheet-approval-mcp", "mcp": "/mcp"}
        )
    )
    app = mcp.streamable_http_app()
    app.add_middleware(auth.BearerAuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return _IdempotentLifespan(app, mcp.session_manager)
