"""Access tokens for the bot's Google service account (Calendar, Gemini on Agent Platform)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import aiohttp
from google.auth import crypt, jwt

TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleAuthError(Exception):
    """The service account key is missing, deleted or disabled."""


def token_claims(client_email: str, scope: str, now: int) -> dict:
    return {"iss": client_email, "scope": scope, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}


class ServiceAccountToken:
    """Signs a JWT with the key file and swaps it for a 1-hour access token, cached until near expiry."""

    def __init__(self, key_path: Path, scope: str):
        self.key_path = key_path
        self.scope = scope
        self._token: Optional[str] = None
        self._expires_at = 0.0

    def info(self) -> dict:
        return json.loads(self.key_path.read_text())

    def invalidate(self) -> None:
        self._token = None

    async def get(self, session: aiohttp.ClientSession) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        if not self.key_path.exists():
            raise GoogleAuthError("service account key file is missing")
        info = self.info()
        signer = crypt.RSASigner.from_service_account_info(info)
        assertion = jwt.encode(signer, token_claims(info["client_email"], self.scope, int(time.time()))).decode()
        data = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        async with session.post(TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            body = await resp.json(content_type=None)
        if "access_token" not in body:
            raise GoogleAuthError(body.get("error_description") or body.get("error") or str(body))
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        return self._token
