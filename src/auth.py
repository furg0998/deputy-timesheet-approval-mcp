import base64
import hashlib
import json
import secrets
import time
from contextvars import ContextVar

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

import config
import deputy
import store

current_user: ContextVar[dict | None] = ContextVar("current_user", default=None)


def _json(data: dict, status: int = 200, headers: dict | None = None):
    return JSONResponse(data, status_code=status, headers=headers or {})


def _issue_access_token(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": config.ISSUER_URL,
            "sub": user_id,
            "iat": now,
            "exp": now + config.ACCESS_TOKEN_TTL,
            "scope": "mcp",
        },
        config.signing_key(),
        algorithm="HS256",
    )


def _issue_refresh_token(user_id: str) -> str:
    token = secrets.token_urlsafe(48)
    store.put_refresh_token(
        hashlib.sha256(token.encode()).hexdigest(), user_id
    )
    return token


def _token_response(user_id: str) -> JSONResponse:
    return _json(
        {
            "access_token": _issue_access_token(user_id),
            "token_type": "bearer",
            "expires_in": config.ACCESS_TOKEN_TTL,
            "refresh_token": _issue_refresh_token(user_id),
            "scope": "mcp",
        }
    )


# --- Discovery metadata ---


async def authorization_server_metadata(request: Request) -> JSONResponse:
    base = config.ISSUER_URL
    return _json(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": [
                "authorization_code",
                "refresh_token",
            ],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


async def protected_resource_metadata(request: Request) -> JSONResponse:
    base = config.ISSUER_URL
    return _json(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        }
    )


# --- Dynamic client registration (open, public clients only) ---


async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return _json(
        {
            "client_id": f"mcp-{secrets.token_urlsafe(16)}",
            "client_name": body.get("client_name", "mcp-client"),
            "redirect_uris": body.get("redirect_uris", []),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_id_issued_at": int(time.time()),
        },
        status=201,
    )


# --- Authorization: bounce to Deputy ---


async def authorize(request: Request):
    q = request.query_params
    redirect_uri = q.get("redirect_uri")
    state = q.get("state", "")
    code_challenge = q.get("code_challenge")
    if q.get("response_type") != "code" or not redirect_uri:
        return _json(
            {
                "error": "invalid_request",
                "error_description": "response_type=code and "
                "redirect_uri are required",
            },
            status=400,
        )
    if not code_challenge:
        return _json(
            {
                "error": "invalid_request",
                "error_description": "PKCE code_challenge is required",
            },
            status=400,
        )
    session_id = secrets.token_urlsafe(32)
    store.put_auth_session(
        session_id,
        {
            "redirect_uri": redirect_uri,
            "client_state": state,
            "code_challenge": code_challenge,
            "client_id": q.get("client_id", ""),
        },
    )
    creds = config.deputy_credentials()
    target = (
        f"{config.DEPUTY_AUTHORIZE_URL}"
        f"?client_id={creds['client_id']}"
        f"&redirect_uri={config.ISSUER_URL}/oauth/callback"
        f"&response_type=code"
        f"&scope={config.DEPUTY_SCOPE}"
        f"&state={session_id}"
    )
    return RedirectResponse(target)


async def oauth_callback(request: Request):
    q = request.query_params
    if q.get("error"):
        return PlainTextResponse(
            f"Deputy authorization failed: {q.get('error')}", status_code=400
        )
    session = store.pop_auth_session(q.get("state", ""))
    if not session:
        return PlainTextResponse(
            "Invalid or expired OAuth state. Please start login again.",
            status_code=400,
        )
    tokens = await deputy.exchange_code(
        q["code"], f"{config.ISSUER_URL}/oauth/callback"
    )
    endpoint = tokens["endpoint"]
    me = await deputy.whoami(endpoint, tokens["access_token"])
    deputy_uid = (
        me.get("Id") or me.get("UserId") or me.get("Employee") or me
    )
    user_id = hashlib.sha256(
        f"{endpoint}|{deputy_uid}".encode()
    ).hexdigest()[:32]
    store.put_user(
        user_id,
        {
            "endpoint": endpoint,
            "deputy_access_token": tokens["access_token"],
            "deputy_refresh_token": tokens["refresh_token"],
            "deputy_expires_at": int(time.time())
            + int(tokens.get("expires_in", 86400)),
            "deputy_me": me,
        },
    )
    code = secrets.token_urlsafe(32)
    store.put_auth_code(
        code,
        {
            "user_id": user_id,
            "code_challenge": session["code_challenge"],
            "redirect_uri": session["redirect_uri"],
            "client_id": session.get("client_id", ""),
        },
    )
    sep = "&" if "?" in session["redirect_uri"] else "?"
    target = f"{session['redirect_uri']}{sep}code={code}"
    if session.get("client_state"):
        target += f"&state={session['client_state']}"
    return RedirectResponse(target)


# --- Token endpoint ---


async def token(request: Request):
    form = await request.form()
    grant = form.get("grant_type")

    if grant == "authorization_code":
        record = store.pop_auth_code(form.get("code", ""))
        if not record:
            return _json(
                {"error": "invalid_grant",
                 "error_description": "unknown or expired code"},
                status=400,
            )
        verifier = (form.get("code_verifier") or "").encode()
        digest = hashlib.sha256(verifier).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if challenge != record["code_challenge"]:
            return _json(
                {"error": "invalid_grant",
                 "error_description": "PKCE verification failed"},
                status=400,
            )
        if form.get("redirect_uri") and form["redirect_uri"] != record[
            "redirect_uri"
        ]:
            return _json(
                {"error": "invalid_grant",
                 "error_description": "redirect_uri mismatch"},
                status=400,
            )
        return _token_response(record["user_id"])

    if grant == "refresh_token":
        token_hash = hashlib.sha256(
            (form.get("refresh_token") or "").encode()
        ).hexdigest()
        record = store.get_refresh_token(token_hash)
        if not record:
            return _json({"error": "invalid_grant"}, status=400)
        store.delete_refresh_token(token_hash)
        return _token_response(record["user_id"])

    return _json(
        {"error": "unsupported_grant_type"}, status=400
    )


# --- Bearer auth middleware for MCP endpoints ---


class BearerAuthMiddleware:
    def __init__(self, app, protected_prefixes=("/mcp",)):
        self.app = app
        self.protected = protected_prefixes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if scope.get("method") == "OPTIONS" or not any(
            path.startswith(p) for p in self.protected
        ):
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()
        www_auth = (
            f'Bearer resource_metadata="{config.ISSUER_URL}'
            f'/.well-known/oauth-protected-resource"'
        )
        if not auth.startswith("Bearer "):
            return await self._reject(send, www_auth, "missing bearer token")

        try:
            payload = jwt.decode(
                auth[7:], config.signing_key(), algorithms=["HS256"]
            )
        except jwt.PyJWTError:
            return await self._reject(send, www_auth, "invalid token")

        user = store.get_user(payload["sub"])
        if not user:
            return await self._reject(
                send, www_auth, "unknown user - re-authenticate"
            )
        current_user.set(user)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, www_auth: str, detail: str):
        body = json.dumps({"error": "invalid_token", "detail": detail})
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", www_auth.encode()),
                ],
            }
        )
        await send(
            {"type": "http.response.body", "body": body.encode()}
        )
