import time
from typing import Any

import httpx

import config
import store


class DeputyError(Exception):
    pass


async def _post_form(url: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, data=data)
    if resp.status_code != 200:
        raise DeputyError(f"Deputy auth error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


async def exchange_code(code: str, redirect_uri: str) -> dict:
    creds = config.deputy_credentials()
    return await _post_form(
        config.DEPUTY_TOKEN_URL,
        {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code": code,
            "scope": config.DEPUTY_SCOPE,
        },
    )


async def refresh_token(endpoint: str, refresh_tok: str) -> dict:
    creds = config.deputy_credentials()
    return await _post_form(
        f"https://{endpoint}/oauth/access_token",
        {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "redirect_uri": f"{config.ISSUER_URL}/oauth/callback",
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "scope": config.DEPUTY_SCOPE,
        },
    )


async def whoami(endpoint: str, access_token: str) -> dict:
    async with httpx.AsyncClient(
        base_url=f"https://{endpoint}", timeout=20
    ) as client:
        resp = await client.get(
            "/api/v1/me", headers=_auth_headers(access_token)
        )
    resp.raise_for_status()
    return resp.json()


def _auth_headers(access_token: str) -> dict:
    return {
        "Authorization": f"OAuth {access_token}",
        "OAuth": access_token,
        "Content-Type": "application/json",
    }


def _timesheet_properties(ts: dict) -> dict:
    """TSA v2 property fields from a v1 Timesheet record (int64 as str)."""
    props: dict[str, Any] = {}
    if ts.get("StartTime"):
        props["startTime"] = str(int(ts["StartTime"]))
    if ts.get("EndTime"):
        props["endTime"] = str(int(ts["EndTime"]))
    if ts.get("OperationalUnit"):
        props["operationalUnitId"] = int(ts["OperationalUnit"])
    return props


def _mealbreak_minutes(ts: dict) -> int:
    """Minutes of meal break from a v1 Mealbreak value ("...THH:MM:SS...")."""
    raw = str(ts.get("Mealbreak") or "")
    clock = raw.split("T", 1)[1] if "T" in raw else raw
    try:
        hours, minutes = clock.split(":")[:2]
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return 0


def _mealbreak_candidates(ts: dict) -> list[str]:
    # suggest-rounding requires a mealbreak string but its format is
    # undocumented, so try the plausible encodings in order.
    mins = _mealbreak_minutes(ts)
    candidates = [f"{mins // 60:02d}:{mins % 60:02d}", f"PT{mins}M", str(mins)]
    if ts.get("Mealbreak"):
        candidates.append(str(ts["Mealbreak"]))
    return list(dict.fromkeys(candidates))


class DeputyClient:
    """Per-user Deputy API client bound to their install endpoint."""

    def __init__(self, user: dict):
        self.user = user
        self.endpoint = user["endpoint"]

    async def _token(self) -> str:
        expires_at = int(self.user.get("deputy_expires_at", 0))
        if expires_at - 120 > time.time():
            return self.user["deputy_access_token"]
        refreshed = await refresh_token(
            self.endpoint, self.user["deputy_refresh_token"]
        )
        new_expiry = int(time.time()) + int(refreshed.get("expires_in", 86400))
        store.update_user_tokens(
            self.user["user_id"],
            refreshed["access_token"],
            refreshed["refresh_token"],
            new_expiry,
        )
        self.user["deputy_access_token"] = refreshed["access_token"]
        self.user["deputy_refresh_token"] = refreshed["refresh_token"]
        self.user["deputy_expires_at"] = new_expiry
        return refreshed["access_token"]

    async def _request(
        self, method: str, path: str, json_body: Any = None
    ) -> Any:
        token = await self._token()
        async with httpx.AsyncClient(
            base_url=f"https://{self.endpoint}", timeout=25
        ) as client:
            resp = await client.request(
                method, path, json=json_body, headers=_auth_headers(token)
            )
        if resp.status_code == 401:
            raise DeputyError(
                "Deputy token rejected (401). The user may need to "
                "re-authenticate with Deputy."
            )
        if resp.status_code >= 400:
            raise DeputyError(
                f"Deputy API {resp.status_code} {method} {path}: "
                f"{resp.text[:500]}"
            )
        if not resp.content:
            return {"success": True}
        return resp.json()

    # --- Resource API (v1) ---

    async def query_timesheets(
        self,
        date_from_unix: int | None = None,
        date_to_unix: int | None = None,
        employee_id: int | None = None,
        time_approved: bool | None = None,
        discarded: bool | None = None,
        max_results: int = 200,
    ) -> list[dict]:
        search: dict[str, Any] = {}
        n = 0

        def add(field, data, typ):
            nonlocal n
            n += 1
            search[f"s{n}"] = {"field": field, "data": data, "type": typ}

        if date_from_unix is not None:
            add("StartTime", date_from_unix, "ge")
        if date_to_unix is not None:
            add("StartTime", date_to_unix, "lt")
        if employee_id is not None:
            add("Employee", employee_id, "eq")
        if time_approved is not None:
            add("TimeApproved", 1 if time_approved else 0, "eq")
        if discarded is False:
            add("Discarded", 1, "ne")

        body = {"search": search, "max": max_results}
        result = await self._request(
            "POST", "/api/v1/resource/Timesheet/QUERY", body
        )
        if isinstance(result, list):
            return result
        return result.get("data", result) if isinstance(result, dict) else []

    async def get_timesheet(self, timesheet_id: int) -> dict | None:
        result = await self._request(
            "POST",
            "/api/v1/resource/Timesheet/QUERY",
            {"search": {"s1": {"field": "Id", "data": timesheet_id, "type": "eq"}}},
        )
        items = result if isinstance(result, list) else result.get("data", [])
        return items[0] if items else None

    async def query_employees(self, employee_ids: list[int]) -> list[dict]:
        if not employee_ids:
            return []
        result = await self._request(
            "POST",
            "/api/v1/resource/Employee/QUERY",
            {
                "search": {
                    "s1": {"field": "Id", "data": employee_ids, "type": "in"}
                }
            },
        )
        items = (
            result
            if isinstance(result, list)
            else result.get("data", result)
        )
        return items if isinstance(items, list) else []

    # --- TSA v2 API ---

    async def config_and_permissions(self) -> dict:
        return await self._request("GET", "/api/tsa/v2/config-and-permissions")

    async def employees_for_status_grouping(
        self,
        date_from: str,
        date_to: str,
        cursor: str | None = None,
        limit: int = 50,
        review_state: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "limit": limit,
        }
        if cursor:
            body["cursor"] = cursor
        if review_state:
            body["reviewState"] = review_state
        return await self._request(
            "POST", "/api/tsa/v2/employees-for-status-grouping", body
        )

    async def range_summary(self, timesheet_id: int) -> dict:
        return await self._request(
            "GET",
            f"/api/tsa/v2/timesheets/{timesheet_id}:applicable-range-summary",
        )

    async def timesheet_questions(
        self, timesheet_id: int, timesheet: dict | None = None
    ) -> dict:
        # Including employeeId makes Deputy treat this as an edit and
        # return 403 "cannot update employee".
        body = await self._current_properties_body(timesheet_id, timesheet)
        return await self._request(
            "POST",
            f"/api/tsa/v2/timesheets/{timesheet_id}:get-timesheet-questions",
            body,
        )

    async def suggest_rounding(
        self, timesheet_id: int, timesheet: dict | None = None
    ) -> dict:
        ts = timesheet or await self.get_timesheet(timesheet_id)
        body = (await self._current_properties_body(timesheet_id, ts))[
            "setProperties"
        ]
        error: DeputyError | None = None
        for mealbreak in _mealbreak_candidates(ts):
            try:
                return await self._request(
                    "POST",
                    f"/api/tsa/v2/timesheets/{timesheet_id}:suggest-rounding",
                    {**body, "mealbreak": mealbreak},
                )
            except DeputyError as e:
                if "mealbreak" not in str(e).lower():
                    raise
                error = e
        raise error

    async def approve(
        self,
        timesheet_id: int,
        set_properties: dict | None = None,
        set_answers: dict | None = None,
        set_pay_returns: dict | None = None,
        timesheet: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if set_properties:
            body["setProperties"] = set_properties
        if set_answers:
            body["setAnswers"] = set_answers
        if set_pay_returns:
            body["setPayReturns"] = set_pay_returns
        if not body:
            body = await self._current_properties_body(timesheet_id, timesheet)
        return await self._request(
            "POST", f"/api/tsa/v2/timesheets/{timesheet_id}:approve", body
        )

    async def approve_time(
        self, timesheet_id: int, timesheet: dict | None = None
    ) -> dict:
        return await self._request(
            "POST",
            f"/api/tsa/v2/timesheets/{timesheet_id}:approve-time",
            await self._current_properties_body(timesheet_id, timesheet),
        )

    async def _current_properties_body(
        self, timesheet_id: int, timesheet: dict | None
    ) -> dict:
        # Deputy rejects an empty JSON object with "invalid format", so echo
        # the timesheet's current properties (no effective override).
        ts = timesheet or await self.get_timesheet(timesheet_id)
        if not ts:
            raise DeputyError(f"Timesheet {timesheet_id} not found")
        return {"setProperties": _timesheet_properties(ts)}

    # These endpoints take no request body.

    async def unapprove(self, timesheet_id: int) -> dict:
        return await self._request(
            "POST", f"/api/tsa/v2/timesheets/{timesheet_id}:unapprove"
        )

    async def unapprove_pay(self, timesheet_id: int) -> dict:
        return await self._request(
            "POST", f"/api/tsa/v2/timesheets/{timesheet_id}:unapprove-pay"
        )

    async def reapprove(self, timesheet_id: int) -> dict:
        return await self._request(
            "POST", f"/api/tsa/v2/timesheets/{timesheet_id}:reapprove"
        )

    async def discard(self, timesheet_id: int) -> dict:
        return await self._request(
            "POST", f"/api/tsa/v2/timesheets/{timesheet_id}:discard"
        )

    async def reinstate(self, timesheet_id: int) -> dict:
        return await self._request(
            "POST", f"/api/tsa/v2/timesheets/{timesheet_id}:reinstate"
        )

    async def batch(self, action: str, timesheet_ids: list[int]) -> dict:
        return await self._request(
            "POST",
            f"/api/tsa/v2/timesheets:batch-{action}",
            {"query": {"timesheetIds": timesheet_ids}},
        )

    async def add_comment(
        self, timesheet_id: int, content: str, creator_id: int | None = None
    ) -> dict:
        body: dict[str, Any] = {"content": content}
        if creator_id:
            body["creator"] = creator_id
        return await self._request(
            "POST", f"/api/tsa/v2/{timesheet_id}/comments", body
        )
