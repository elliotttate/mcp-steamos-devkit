from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import SafetyLevel, to_jsonable
from .state import JsonStore


@dataclass
class Confirmation:
    token: str
    action: str
    digest: str
    summary: str
    safety: SafetyLevel
    expires_at: str


class ConfirmationManager:
    def __init__(self, store: JsonStore, ttl_seconds: int = 300):
        self.store = store
        self.ttl_seconds = ttl_seconds

    def require(
        self,
        action: str,
        params: dict[str, Any],
        safety: SafetyLevel,
        summary: str,
        token: str | None,
    ) -> dict[str, Any] | None:
        if safety not in {SafetyLevel.DESTRUCTIVE, SafetyLevel.ARBITRARY_EXECUTION}:
            return None
        if token and self.verify(token, action, params):
            return None
        confirmation = self.create(action, params, safety, summary)
        return {
            "requires_confirmation": True,
            "confirmation_token": confirmation.token,
            "expires_at": confirmation.expires_at,
            "safety": confirmation.safety.value,
            "summary": confirmation.summary,
        }

    def create(
        self,
        action: str,
        params: dict[str, Any],
        safety: SafetyLevel,
        summary: str,
    ) -> Confirmation:
        token = secrets.token_urlsafe(18)
        expires_at = (datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)).isoformat()
        confirmation = Confirmation(
            token=token,
            action=action,
            digest=self._digest(action, params),
            safety=safety,
            summary=summary,
            expires_at=expires_at,
        )
        confirmations = self._load()
        confirmations[token] = to_jsonable(confirmation)
        self.store.save("confirmations", confirmations)
        return confirmation

    def verify(self, token: str, action: str, params: dict[str, Any]) -> bool:
        confirmations = self._load()
        entry = confirmations.get(token)
        if not entry:
            return False
        expires_at = datetime.fromisoformat(entry["expires_at"])
        if expires_at < datetime.now(UTC):
            confirmations.pop(token, None)
            self.store.save("confirmations", confirmations)
            return False
        expected = self._digest(action, params)
        ok = entry["action"] == action and entry["digest"] == expected
        if ok:
            confirmations.pop(token, None)
            self.store.save("confirmations", confirmations)
        return ok

    def _load(self) -> dict[str, Any]:
        return self.store.load("confirmations", {})

    @staticmethod
    def _digest(action: str, params: dict[str, Any]) -> str:
        payload = json.dumps({"action": action, "params": to_jsonable(params)}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upload_safety(delete_extraneous: bool) -> SafetyLevel:
    return SafetyLevel.DESTRUCTIVE if delete_extraneous else SafetyLevel.WRITE

