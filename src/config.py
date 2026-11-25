import json
import os
from functools import lru_cache

import boto3

TABLE_NAME = os.environ.get("TABLE_NAME", "deputy-mcp")
DEPUTY_CREDENTIALS_SECRET_ARN = os.environ.get("DEPUTY_CREDENTIALS_SECRET_ARN", "")
SIGNING_SECRET_ARN = os.environ.get("SIGNING_SECRET_ARN", "")
ISSUER_URL = os.environ.get("ISSUER_URL", "http://localhost:8000")
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
DEPUTY_TIMEZONE = os.environ.get("DEPUTY_TIMEZONE", "Australia/Sydney")

DEPUTY_AUTHORIZE_URL = "https://once.deputy.com/my/oauth/login"
DEPUTY_TOKEN_URL = "https://once.deputy.com/my/oauth/access_token"
DEPUTY_SCOPE = "longlife_refresh_token"

ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 60 * 60 * 24 * 30
AUTH_CODE_TTL = 600
AUTH_SESSION_TTL = 600

@lru_cache(maxsize=1)
def _secrets():
    return boto3.client("secretsmanager", region_name=AWS_REGION)


@lru_cache(maxsize=8)
def _secret(arn: str) -> dict:
    resp = _secrets().get_secret_value(SecretId=arn)
    return json.loads(resp["SecretString"])


@lru_cache(maxsize=1)
def deputy_credentials() -> dict:
    if not DEPUTY_CREDENTIALS_SECRET_ARN:
        return {
            "client_id": os.environ["DEPUTY_CLIENT_ID"],
            "client_secret": os.environ["DEPUTY_CLIENT_SECRET"],
        }
    return _secret(DEPUTY_CREDENTIALS_SECRET_ARN)


@lru_cache(maxsize=1)
def signing_key() -> str:
    if not SIGNING_SECRET_ARN:
        return os.environ.get("LOCAL_SIGNING_KEY", "local-dev-signing-key")
    secret = _secret(SIGNING_SECRET_ARN)
    return secret.get("key") or next(iter(secret.values()))
