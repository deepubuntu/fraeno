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
    base_sha: str = ""
    change: str = "Dependency change"
    head_repository: str = ""

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
        base_sha: str,
        change: str,
        head_repository: str,
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
            base_sha=base_sha,
            change=change,
            head_repository=head_repository,
        )


class EventStore(Protocol):
    async def claim_delivery(
        self, delivery_id: str, *, retry: bool = False
    ) -> bool: ...

    async def save_run(self, record: RunRecord) -> None: ...

    async def get_run(self, workflow_run_id: int) -> RunRecord | None: ...

    async def get_active_run(
        self, repository_id: int, pull_request_number: int
    ) -> RunRecord | None: ...

    async def clear_active_run(self, record: RunRecord) -> None: ...

    async def complete_delivery(self, delivery_id: str) -> None: ...

    async def fail_delivery(self, delivery_id: str) -> None: ...


class MemoryEventStore:
    def __init__(self) -> None:
        self._deliveries: dict[str, str] = {}
        self._runs: dict[int, RunRecord] = {}
        self._active_runs: dict[tuple[int, int], RunRecord] = {}
        self._lock = asyncio.Lock()

    async def claim_delivery(
        self, delivery_id: str, *, retry: bool = False
    ) -> bool:
        async with self._lock:
            status = self._deliveries.get(delivery_id)
            if status == "completed":
                return False
            if status == "processing" and not retry:
                return False
            self._deliveries[delivery_id] = "processing"
            return True

    async def save_run(self, record: RunRecord) -> None:
        async with self._lock:
            self._runs[record.workflow_run_id] = record
            self._active_runs[
                (record.repository_id, record.pull_request_number)
            ] = record

    async def get_run(self, workflow_run_id: int) -> RunRecord | None:
        async with self._lock:
            return self._runs.get(workflow_run_id)

    async def get_active_run(
        self, repository_id: int, pull_request_number: int
    ) -> RunRecord | None:
        async with self._lock:
            return self._active_runs.get((repository_id, pull_request_number))

    async def clear_active_run(self, record: RunRecord) -> None:
        async with self._lock:
            key = (record.repository_id, record.pull_request_number)
            active = self._active_runs.get(key)
            if active and active.workflow_run_id == record.workflow_run_id:
                self._active_runs.pop(key)

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

    async def claim_delivery(
        self, delivery_id: str, *, retry: bool = False
    ) -> bool:
        from google.api_core.exceptions import AlreadyExists

        reference = self._client.collection("github_deliveries").document(delivery_id)
        value = {
            "status": "processing",
            "received_at": datetime.now(timezone.utc),
        }
        try:
            await reference.create(value)
        except AlreadyExists:
            snapshot = await reference.get()
            existing = snapshot.to_dict() if snapshot.exists else None
            status = existing.get("status") if isinstance(existing, dict) else None
            if status == "completed":
                return False
            if status == "processing" and not retry:
                return False
            await reference.set(value)
        return True

    async def save_run(self, record: RunRecord) -> None:
        reference = self._client.collection("github_runs").document(
            str(record.workflow_run_id)
        )
        await reference.set(asdict(record))
        await self._active_run_reference(
            record.repository_id, record.pull_request_number
        ).set(asdict(record))

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

    async def get_active_run(
        self, repository_id: int, pull_request_number: int
    ) -> RunRecord | None:
        snapshot = await self._active_run_reference(
            repository_id, pull_request_number
        ).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if not isinstance(data, dict):
            return None
        return RunRecord(**data)

    async def clear_active_run(self, record: RunRecord) -> None:
        from google.cloud import firestore

        reference = self._active_run_reference(
            record.repository_id, record.pull_request_number
        )
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def clear_if_current(active_transaction: Any) -> None:
            snapshot = await reference.get(transaction=active_transaction)
            data = snapshot.to_dict() if snapshot.exists else None
            if (
                isinstance(data, dict)
                and data.get("workflow_run_id") == record.workflow_run_id
            ):
                active_transaction.delete(reference)

        await clear_if_current(transaction)

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

    def _active_run_reference(
        self, repository_id: int, pull_request_number: int
    ) -> Any:
        return self._client.collection("github_active_runs").document(
            f"{repository_id}-{pull_request_number}"
        )
