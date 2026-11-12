"""Authentication.

Personal mode: one shared secret (``APP_TOKEN`` in .env). Clients send it as
``Authorization: Bearer <token>`` (REST) or ``?token=<token>`` (WebSocket).
If APP_TOKEN is empty the server is open — fine on 127.0.0.1, never expose that publicly.

Product mode (later): replace ``resolve_user`` with session cookies / magic-link JWTs;
everything downstream only needs the returned ``user_id``.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request, WebSocket, status

log = logging.getLogger(__name__)

OWNER_USER_ID = "owner"


class Authenticator:
    def __init__(self, app_token: str):
        self.app_token = app_token or ""
        if not self.app_token:
            log.warning("APP_TOKEN is empty — the server accepts any client (OK for 127.0.0.1 only)")

    def _ok(self, presented: str | None) -> bool:
        if not self.app_token:
            return True
        return bool(presented) and hmac.compare_digest(presented, self.app_token)

    def resolve_user(self, request: Request) -> str:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token")
        if not self._ok(token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing token")
        return OWNER_USER_ID

    def resolve_ws_user(self, ws: WebSocket) -> str | None:
        token = ws.query_params.get("token")
        if token is None:
            header = ws.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else None
        return OWNER_USER_ID if self._ok(token) else None
