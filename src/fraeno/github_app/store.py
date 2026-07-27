from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass(frozen=True)
class RunRecord:
    workflow_run_id: int
    check_run_id: int
    installation_id: int
    repository_id: int
    repository: str
    pull_request_number: int
    head_sha: str
    details_url: str
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        workflow_run_id: int,
        check_run_id: int,
        installation_id: int,
        repository_id: int,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        details_url: str,
    ) -> RunRecord:
        return cls(
            workflow_run_id=workflow_run_id,
            check_run_id=check_run_id,
            installation_id=installation_id,
            repository_id=repository_id,
            repository=repository,
            pull_request_number=pull_request_number,
            head_sha=head_sha,
            details_url=details_url,
            created_at=datetime.now(timezone.utc).isoformat(),
        )


class EventStore(Protocol):
    async def claim_delivery(self, delivery_id: str) -> bool: ...

    async def save_run(self, record: RunRecord) -> None: ...

    async def get_run(self, workflow_run_id: int) -> RunRecord | None: ...

    async def complete_delivery(self, delivery_id: str) -> None: ...

    async def fail_delivery(self, delivery_id: str) -> None: ...


class MemoryEventStore:
    def __init__(self) -> None:
        self._deliveries: dict[str, str] = {}
        self._runs: dict[int, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def claim_delivery(self, delivery_id: str) -> bool:
        async with self._lock:
            if delivery_id in self._deliveries:
                return False
            self._deliveries[delivery_id] = "processing"
            return True

    async def save_run(self, record: RunRecord) -> None:
        async with self._lock:
            self._runs[record.workflow_run_id] = record

    async def get_run(self, workflow_run_id: int) -> RunRecord | None:
        async with self._lock:
            return self._runs.get(workflow_run_id)

    async def complete_delivery(self, delivery_id: str) -> None:
        async with self._lock:
            self._deliveries[delivery_id] = "completed"

    async def fail_delivery(self, delivery_id: str) -> None:
        async with self._lock:
            self._deliveries.pop(delivery_id, None)


class FirestoreEventStore:
    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.AsyncClient()
        self._client = client

    async def claim_delivery(self, delivery_id: str) -> bool:
        reference = self._client.collection("github_deliveries").document(delivery_id)
        snapshot = await reference.get()
        if snapshot.exists:
            existing = snapshot.to_dict()
            if isinstance(existing, dict) and existing.get("status") != "failed":
                return False
        await reference.set(
            {
                "status": "processing",
                "received_at": datetime.now(timezone.utc),
            }
        )
        return True

    async def save_run(self, record: RunRecord) -> None:
        reference = self._client.collection("github_runs").document(
            str(record.workflow_run_id)
        )
        await reference.set(asdict(record))

    async def get_run(self, workflow_run_id: int) -> RunRecord | None:
        reference = self._client.collection("github_runs").document(
            str(workflow_run_id)
        )
        snapshot = await reference.get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if not isinstance(data, dict):
            return None
        return RunRecord(**data)

    async def complete_delivery(self, delivery_id: str) -> None:
        reference = self._client.collection("github_deliveries").document(delivery_id)
        await reference.update(
            {"status": "completed", "completed_at": datetime.now(timezone.utc)}
        )

    async def fail_delivery(self, delivery_id: str) -> None:
        reference = self._client.collection("github_deliveries").document(delivery_id)
        await reference.update(
            {"status": "failed", "failed_at": datetime.now(timezone.utc)}
        )
