# deputy-timesheet-approval-mcp

A remote MCP server for approving Deputy timesheets, hosted serverless on
AWS (Lambda + API Gateway HTTP API + DynamoDB). Users log in with their
Deputy account via OAuth; the server stores per-user Deputy tokens plus a
local augmentation layer (employee notes, watch flags, approval history)
so the agent learns which approvals look unusual over time.

## How it works

- **OAuth proxy** - the server implements the MCP OAuth 2.1 flow and brokers
  to Deputy's OAuth (`once.deputy.com`). MCP clients register dynamically,
  get bounced to Deputy to log in, and receive server-issued tokens (PKCE).
  Deputy access/refresh tokens are stored per-user in DynamoDB along with
  their install endpoint (`{install}.{geo}.deputy.com`), so any Deputy
  tenant works.
- **Approval tools** - wraps the Deputy TSA v2 endpoints plus the v1
  Resource API for querying timesheets.
- **Memory** - every approve/unapprove/discard action is recorded in
  DynamoDB per employee, alongside notes and flags you set via tools.
  `get_approval_queue` joins pending timesheets with this history and
  surfaces anomalies (hours far off the employee's norm, long shifts,
  missing meal breaks, disputed sheets, flagged employees).

## Tools

> **Live payroll warning:** the server talks to a real Deputy account. The
> approve / unapprove / discard / reinstate / comment tools are
> *transactional* - they change live timesheets and pay. Do not call them to
> test or explore; ask the account owner first and only run them on a
> timesheet they name. Their tool descriptions start with `TRANSACTIONAL`
> and carry the MCP `destructiveHint` annotation so clients can prompt for
> confirmation. Everything else is read-only against Deputy (notes and flags
> only touch this server's DynamoDB table).

Dates are interpreted in the business's local time zone, set by the
`DEPUTY_TIMEZONE` Lambda environment variable (default `Australia/Sydney`).

| Tool | Purpose |
| --- | --- |
| `get_approval_queue` | Pending timesheets grouped by employee, with history/flags/anomalies |
| `list_timesheets` | Raw timesheet query by date range / employee / approval state |
| `get_timesheet_detail` | Timesheet + pay-range summary + required questions |
| `suggest_rounding` | Deputy's rounding suggestion |
| `approve_timesheet` | **Transactional.** Approve time + pay (optional overrides/question answers) |
| `approve_time_only` | **Transactional.** Approve time only |
| `batch_approve` | **Transactional.** Approve many at once |
| `unapprove_timesheet` / `unapprove_pay` | **Transactional.** Reverse approval |
| `discard_timesheet` / `reinstate_timesheet` | **Transactional.** Discard / restore |
| `add_timesheet_comment` | **Transactional.** Attach a comment |
| `save_employee_note` / `flag_employee` / `clear_employee_flags` | Augmentation layer |
| `get_employee_profile` / `get_approval_history` | Read the memory layer |
| `check_connection` | Verify Deputy auth + permissions |
| `save_shortcut` / `list_shortcuts` / `run_shortcut` / `delete_shortcut` | Per-user saved multi-step workflows |
| `save_rule` / `list_rules` / `delete_rule` | Per-user standing instructions for the AI |

### Shortcuts and rules

Each user can save **shortcuts**: a named list of calls to this server's
tools that the AI replays with one `run_shortcut` call. Step arguments can
reference run-time parameters (`{{params.start}}`) and earlier step outputs
(`{{steps.queue.queue.*.employee_id}}`; `*` maps over a list). **Rules** are
standing instructions (e.g. "always flag shifts over 10h") that the AI is told
to load with `list_rules` at the start of a session. Both are stored per user
in DynamoDB with no expiry.

Example `save_shortcut` call:

```json
{
  "name": "flag-long-shifts",
  "description": "Flag everyone in the queue for review",
  "params": {"start": "first day of the pay period, YYYY-MM-DD"},
  "steps": [
    {"id": "queue", "tool": "get_approval_queue", "args": {"date_from": "{{params.start}}"}},
    {"tool": "flag_employee", "args": {"employee_id": "{{steps.queue.queue.0.employee_id}}", "reason": "review"}}
  ]
}
```

If a shortcut contains a transactional tool, `run_shortcut` returns
`requires_confirm` without running anything until it is called with
`confirm=true`.

The feature lives in `src/shortcuts.py` and has no Deputy or AWS dependency.
Another FastMCP server can reuse it by passing its own storage backend and a
function that returns the current user id:

```python
shortcuts.Shortcuts(mcp, store.ShortcutStorage(), lambda: current_user_id()).register()
```

Tests: `pip install pytest && pytest tests`.

## Deploy

### One-time AWS setup (run locally after `aws login`)

```bash
./scripts/create-deploy-bucket.sh          # creates s3 artifact bucket
ARTIFACT_BUCKET=<bucket> ./scripts/setup-github-oidc.sh
```

The OIDC script auto-detects GitHub's *immutable OIDC subject* setting via
`gh api` (sub becomes `repo:owner@ID/name@ID:...`). If `gh` isn't
available and your repo has it enabled, pass the prefix manually:
`GITHUB_SUB_PREFIX=repo:owner@ID/name@ID`.

Then in GitHub repo settings:

- Variables: `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `ARTIFACT_BUCKET`
- Secrets: `DEPUTY_CLIENT_ID`, `DEPUTY_CLIENT_SECRET`

Also set your Deputy OAuth client's redirect URI to
`https://<api-id>.execute-api.ap-southeast-2.amazonaws.com/oauth/callback`
(you'll know the exact URL after the first deploy; until then the OAuth
login will fail at the callback step).

Every push to `main` then builds the zip, uploads it, and runs
`aws cloudformation deploy`.

### Manual deploy

```bash
DEPUTY_CLIENT_ID=.. DEPUTY_CLIENT_SECRET=.. ARTIFACT_BUCKET=.. \
  ./scripts/deploy-local.sh
```

## Local dev

```bash
pip install -r requirements.txt
TABLE_NAME=deputy-mcp DEPUTY_CLIENT_ID=.. DEPUTY_CLIENT_SECRET=.. \
  ISSUER_URL=http://localhost:8000 python src/app.py
```

Requires AWS creds (DynamoDB + secrets fall back to env vars for Deputy
credentials). MCP endpoint at `http://localhost:8000/mcp`.

## Connecting a client

Point any MCP client that supports remote servers + OAuth at
`https://<api-id>.execute-api.ap-southeast-2.amazonaws.com/mcp`. It will
discover auth via `/.well-known/oauth-protected-resource`, register
dynamically, and open a Deputy login.

## Weekly approval flow

1. `get_approval_queue` (defaults to last 14 days) - the agent shows each
   employee's pending timesheets plus anomalies from history.
2. Review flagged employees (`get_timesheet_detail`,
   `get_employee_profile`); `flag_employee` / `save_employee_note` to
   remember anything odd.
3. `batch_approve` the clean timesheets; `approve_timesheet` or
   `discard_timesheet` for the rest.
