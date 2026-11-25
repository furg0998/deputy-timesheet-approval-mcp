import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

import config

_table = None


def _t():
    global _table
    if _table is None:
        _table = boto3.resource(
            "dynamodb", region_name=config.AWS_REGION
        ).Table(config.TABLE_NAME)
    return _table


def _put(item: dict) -> None:
    item.setdefault("ttl", int(time.time()) + 60 * 60 * 24 * 365)
    _t().put_item(Item=item)


def _get(pk: str, sk: str) -> dict | None:
    return _t().get_item(Key={"pk": pk, "sk": sk}).get("Item")


def _delete(pk: str, sk: str) -> None:
    _t().delete_item(Key={"pk": pk, "sk": sk})


_INTERNAL_KEYS = {"pk", "sk", "ttl"}


def _plain(value: Any) -> Any:
    """Convert DynamoDB Decimals to int/float, recursively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _public(item: dict) -> dict:
    """Strip storage keys and normalise numbers for tool output."""
    return {k: _plain(v) for k, v in item.items() if k not in _INTERNAL_KEYS}


def _query(pk: str, sk_prefix: str, limit: int = 50, descending: bool = True):
    return _t().query(
        KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with(sk_prefix),
        Limit=limit,
        ScanIndexForward=not descending,
    ).get("Items", [])


# --- Users (Deputy tokens + install info) ---


def put_user(user_id: str, data: dict) -> None:
    _put({"pk": f"USER#{user_id}", "sk": "PROFILE", "user_id": user_id, **data})


def get_user(user_id: str) -> dict | None:
    return _get(f"USER#{user_id}", "PROFILE")


def update_user_tokens(
    user_id: str, access_token: str, refresh_token: str, expires_at: int
) -> None:
    _t().update_item(
        Key={"pk": f"USER#{user_id}", "sk": "PROFILE"},
        UpdateExpression=(
            "SET deputy_access_token = :a, deputy_refresh_token = :r,"
            " deputy_expires_at = :e"
        ),
        ExpressionAttributeValues={
            ":a": access_token,
            ":r": refresh_token,
            ":e": expires_at,
        },
    )


# --- Employee profiles (augmentation layer) ---


def get_employee_profile(user_id: str, employee_id: int) -> dict:
    item = _get(f"USER#{user_id}", f"EMP#{employee_id}")
    if item:
        return _public(item)
    return {
        "employee_id": employee_id,
        "notes": [],
        "flags": [],
        "watch": False,
    }


def get_employee_profiles(user_id: str, employee_ids: list[int]) -> dict[int, dict]:
    if not employee_ids:
        return {}
    keys = [
        {"pk": f"USER#{user_id}", "sk": f"EMP#{eid}"} for eid in employee_ids
    ]
    resp = _t().meta.client.batch_get_item(
        RequestItems={config.TABLE_NAME: {"Keys": keys}}
    )
    out = {}
    for item in resp.get("Responses", {}).get(config.TABLE_NAME, []):
        out[int(item["employee_id"])] = _public(item)
    return out


def save_employee_note(user_id: str, employee_id: int, note: str) -> dict:
    profile = get_employee_profile(user_id, employee_id)
    notes = profile.get("notes", [])
    notes.append({"note": note, "at": int(time.time())})
    _put(
        {
            "pk": f"USER#{user_id}",
            "sk": f"EMP#{employee_id}",
            "employee_id": employee_id,
            "notes": notes,
            "flags": profile.get("flags", []),
            "watch": profile.get("watch", False),
            "employee_name": profile.get("employee_name"),
        }
    )
    profile["notes"] = notes
    return profile


def set_employee_flag(
    user_id: str,
    employee_id: int,
    reason: str,
    flag_type: str = "watch",
    employee_name: str | None = None,
) -> dict:
    profile = get_employee_profile(user_id, employee_id)
    flags = profile.get("flags", [])
    flags.append(
        {"type": flag_type, "reason": reason, "at": int(time.time())}
    )
    _put(
        {
            "pk": f"USER#{user_id}",
            "sk": f"EMP#{employee_id}",
            "employee_id": employee_id,
            "notes": profile.get("notes", []),
            "flags": flags,
            "watch": True,
            "employee_name": employee_name or profile.get("employee_name"),
        }
    )
    profile["flags"] = flags
    profile["watch"] = True
    return profile


def clear_employee_flags(user_id: str, employee_id: int) -> dict:
    profile = get_employee_profile(user_id, employee_id)
    _put(
        {
            "pk": f"USER#{user_id}",
            "sk": f"EMP#{employee_id}",
            "employee_id": employee_id,
            "notes": profile.get("notes", []),
            "flags": [],
            "watch": False,
            "employee_name": profile.get("employee_name"),
        }
    )
    profile["flags"] = []
    profile["watch"] = False
    return profile


# --- Approval history ---


def record_approval_event(
    user_id: str,
    employee_id: int,
    timesheet_id: int,
    action: str,
    timesheet: dict | None = None,
    extra: dict | None = None,
) -> None:
    ts = timesheet or {}
    _put(
        {
            "pk": f"USER#{user_id}",
            "sk": f"HIST#{employee_id}#{int(time.time())}#{timesheet_id}",
            "employee_id": employee_id,
            "timesheet_id": timesheet_id,
            "action": action,
            "at": int(time.time()),
            "date": ts.get("Date"),
            "start_time": ts.get("StartTime"),
            "end_time": ts.get("EndTime"),
            "total_time": ts.get("TotalTime"),
            "cost": ts.get("Cost"),
            "employee_name": (
                (ts.get("_DPMetaData") or {}).get("EmployeeInfo") or {}
            ).get("DisplayName"),
            **(extra or {}),
        }
    )


def get_approval_history(
    user_id: str, employee_id: int | None = None, limit: int = 50
) -> list[dict]:
    prefix = f"HIST#{employee_id}#" if employee_id is not None else "HIST#"
    return [_public(i) for i in _query(f"USER#{user_id}", prefix, limit=limit)]


# --- OAuth flow state (short-lived) ---


def put_auth_session(session_id: str, data: dict) -> None:
    _put(
        {
            "pk": f"AUTH#{session_id}",
            "sk": "SESSION",
            **data,
            "ttl": int(time.time()) + config.AUTH_SESSION_TTL,
        }
    )


def pop_auth_session(session_id: str) -> dict | None:
    item = _get(f"AUTH#{session_id}", "SESSION")
    if item:
        _delete(f"AUTH#{session_id}", "SESSION")
    return item


def put_auth_code(code: str, data: dict) -> None:
    _put(
        {
            "pk": f"CODE#{code}",
            "sk": "CODE",
            **data,
            "ttl": int(time.time()) + config.AUTH_CODE_TTL,
        }
    )


def pop_auth_code(code: str) -> dict | None:
    item = _get(f"CODE#{code}", "CODE")
    if item:
        _delete(f"CODE#{code}", "CODE")
    return item


def put_refresh_token(token_hash: str, user_id: str) -> None:
    _put(
        {
            "pk": f"RTOK#{token_hash}",
            "sk": "TOK",
            "user_id": user_id,
            "ttl": int(time.time()) + config.REFRESH_TOKEN_TTL,
        }
    )


def get_refresh_token(token_hash: str) -> dict | None:
    return _get(f"RTOK#{token_hash}", "TOK")


def delete_refresh_token(token_hash: str) -> None:
    _delete(f"RTOK#{token_hash}", "TOK")
