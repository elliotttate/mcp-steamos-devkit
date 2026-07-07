from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from .models import OperationRecord, SafetyLevel, to_jsonable
from .state import JsonStore


class OperationManager:
    def __init__(self, store: JsonStore):
        self.store = store

    @contextmanager
    def track(
        self,
        name: str,
        safety: SafetyLevel,
        device_id: str | None = None,
        summary: str | None = None,
    ) -> Iterator[OperationRecord]:
        record = self.start(name=name, safety=safety, device_id=device_id, summary=summary)
        try:
            yield record
        except Exception as exc:
            self.fail(record.id, str(exc))
            raise
        else:
            self.finish(record.id)

    def start(
        self,
        name: str,
        safety: SafetyLevel,
        device_id: str | None = None,
        summary: str | None = None,
    ) -> OperationRecord:
        now = self._now()
        record = OperationRecord(
            id=uuid.uuid4().hex,
            name=name,
            safety=safety,
            status="running",
            created_at=now,
            updated_at=now,
            device_id=device_id,
            summary=summary,
        )
        self._upsert(record)
        return record

    def finish(self, operation_id: str, result: dict[str, Any] | None = None) -> None:
        record = self.get(operation_id)
        if not record:
            return
        record["status"] = "succeeded"
        record["updated_at"] = self._now()
        if result is not None:
            record["result"] = result
        self.store.update_mapping("operations", operation_id, record)

    def fail(self, operation_id: str, error: str) -> None:
        record = self.get(operation_id)
        if not record:
            return
        record["status"] = "failed"
        record["updated_at"] = self._now()
        record["error"] = error
        self.store.update_mapping("operations", operation_id, record)

    def list(self) -> list[dict[str, Any]]:
        records = self.store.load("operations", {})
        return sorted(records.values(), key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, operation_id: str) -> dict[str, Any] | None:
        return self.store.load("operations", {}).get(operation_id)

    def _upsert(self, record: OperationRecord) -> None:
        self.store.update_mapping("operations", record.id, to_jsonable(record))

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

