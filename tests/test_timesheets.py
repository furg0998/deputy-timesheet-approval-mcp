import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import mcp_server  # noqa: E402


def _ts(id_, **kw):
    base = {
        "Id": id_,
        "Employee": 20,
        "Creator": 19,
        "Date": "2026-11-24T00:00:00+11:00",
        "StartTime": 1795469400,
        "EndTime": 1795500000,
        "TotalTime": 8.5,
        "Cost": 646,
        "Roster": 4096,
        "PayRuleApproved": True,
        "TimeApproved": True,
        "Exported": False,
        "Mealbreak": "2026-11-24T00:30:00+11:00",
        "Slots": [_slot("M")],
        "Created": "2026-11-26T09:22:18+11:00",
        "Modified": "2026-11-26T09:22:18+11:00",
        "_DPMetaData": {"EmployeeInfo": {"DisplayName": "Helen Cahill"}},
    }
    base.update(kw)
    return base


def _slot(kind, minutes=30):
    return {
        "blnEmptySlot": False,
        "strType": "B",
        "intStart": 0,
        "intEnd": minutes * 60,
        "mixedActivity": {"strBreakType": kind},
    }


def test_breaks_from_slots_and_mealbreak_fallback():
    assert mcp_server._breaks(_ts(1, Slots=[_slot("R")])) == [
        {"type": "rest", "minutes": 30}
    ]
    assert mcp_server._breaks(_ts(1, Slots=[])) == [
        {"type": "meal", "minutes": 30}
    ]


def test_clean_timesheet_has_no_anomalies():
    assert mcp_server._timesheet_anomalies(_ts(1)) == []


def test_timesheet_anomalies():
    a = mcp_server._timesheet_anomalies(
        _ts(
            2972,
            TotalTime=11,
            Cost=0,
            PayRuleApproved=False,
            Creator=20,
            Roster=None,
            Mealbreak="2026-11-24T00:00:00+11:00",
            Slots=[_slot("R")],
        )
    )
    assert a == [
        "timesheet 2972 (2026-11-24): long shift: 11.0h",
        "timesheet 2972 (2026-11-24): 11.0h shift with no meal break "
        "(rest break only)",
        "timesheet 2972 (2026-11-24): $0 cost (pay rules not yet approved)",
        "timesheet 2972 (2026-11-24): created by the employee with no "
        "rostered shift",
    ]
    empty = mcp_server._timesheet_anomalies(
        _ts(3, TotalTime=0, EndTime=None, Slots=[])
    )
    assert "empty: no hours or missing start/end time" in empty[0]


def test_anomalies_run_without_history():
    out = mcp_server._anomalies(
        20, [_ts(1, Slots=[], Mealbreak=None)], {}, []
    )
    assert out == ["timesheet 1 (2026-11-24): 8.5h shift with no break recorded"]


def test_anomalies_compare_with_history():
    out = mcp_server._anomalies(
        20, [_ts(1, TotalTime=4)], {}, [{"total_time": 8}]
    )
    assert out == [
        "timesheet 1: 4.0h is >30% off this employee's usual 8.0h"
    ]


class FakeClient:
    def __init__(self, sheets):
        self.sheets = sheets
        self.calls = []

    async def query_timesheets(self, *args, **kw):
        self.calls.append(kw)
        return self.sheets

    async def query_employees(self, ids):
        self.calls.append({"employees": ids})
        return [{"Id": i, "DisplayName": "Helen Cahill"} for i in ids]

    async def get_timesheets(self, ids):
        return [t for t in self.sheets if t["Id"] in ids]


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient(
        [
            _ts(1),
            _ts(2, Exported=True, Modified="2026-11-25T10:00:00+11:00"),
            _ts(3, _DPMetaData={"EmployeeInfo": {"DisplayName": "Kate K"}}),
        ]
    )
    monkeypatch.setattr(mcp_server, "_client", lambda: fake)
    return fake


def _list(**kw):
    return asyncio.run(
        mcp_server.list_timesheets("2026-11-23", "2026-11-29", **kw)
    )


def _ids(result):
    return [t["id"] for t in result["timesheets"]]


def test_list_filters(client):
    assert _ids(_list(employee_name="helen")) == [1, 2]
    assert _ids(_list(exported=False)) == [1, 3]
    assert _ids(_list(modified_since="2026-11-26")) == [1, 3]
    assert _ids(_list(modified_since="2026-11-25T09:00:00+11:00")) == [1, 2, 3]
    assert client.calls[0]["discarded"] is False
    _list(include_discarded=True)
    assert client.calls[-1]["discarded"] is None


def test_name_filter_resolves_missing_names(client):
    client.sheets.append(_ts(4, Employee=21, _DPMetaData={}))
    assert _ids(_list(employee_name="helen")) == [1, 2, 4]
    assert client.calls[-1] == {"employees": [21]}


def test_list_limit_reports_truncation(client):
    result = _list(limit=2)
    assert result["count"] == 3
    assert result["truncated"] is True
    assert _ids(result) == [1, 2]


def test_get_timesheets_reports_missing(client):
    result = asyncio.run(mcp_server.get_timesheets([1, 3, 99, 1]))
    assert [t["id"] for t in result["timesheets"]] == [1, 3]
    assert result["not_found"] == [99]
