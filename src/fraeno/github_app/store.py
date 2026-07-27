from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    event: str
    status: str
    attempts: int
    received_at: datetime
    updated_at: datetime
    expires_at: datetime
    error_kind: str = ""
    replay_actor: str = ""
    replay_reason: str = ""


@dataclass(frozen=True)
class RepositoryRecord:
    installation_id: int
    repository_id: int
    full_name: str
    default_branch: str
    status: str
    reason: str
    updated_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        installation_id: int,
        repository_id: int,
        full_name: str,
        default_branch: str,
        status: str,
        reason: str,
        retention_days: int = 30,
    ) -> RepositoryRecord:
        now = utc_now()
        return cls(
            installation_id=installation_id,
            repository_id=repository_id,
            full_name=full_name,
            default_branch=default_branch,
            status=status,
            reason=reason,
            updated_at=now,
            expires_at=now + timedelta(days=retention_days),
        )


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
    expires_at: datetime | None = None

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
        retention_days: int = 30,
    ) -> RunRecord:
        now = utc_now()
        return cls(
            workflow_run_id=workflow_run_id,
            check_run_id=check_run_id,
            installation_id=installation_id,
            repository_id=repository_id,
            repository=repository,
            pull_request_number=pull_request_number,
            head_sha=head_sha,
            details_url=details_url,
            created_at=now.isoformat(),
            base_sha=base_sha,
            change=change,
            head_repository=head_repository,
            expires_at=now + timedelta(days=retention_days),
        )


class EventStore(Protocol):
    async def claim_delivery(
        self,
        delivery_id: str,
        *,
        event: str = "",
        retry: bool = False,
    ) -> bool: ...

    async def get_delivery(self, delivery_id: str) -> DeliveryRecord | None: ...

    async def list_stale_deliveries(
        self, updated_before: datetime
    ) -> list[DeliveryRecord]: ...

    async def complete_delivery(self, delivery_id: str) -> None: ...

    async def fail_delivery(
        self, delivery_id: str, *, error_kind: str = "retryable"
    ) -> None: ...

    async def dead_letter_delivery(
        self, delivery_id: str, *, error_kind: str
    ) -> None: ...

    async def request_replay(
        self,
        delivery_id: str,
        *,
        actor: str,
        reason: str,
        audit_retention_days: int,
    ) -> bool: ...

    async def reject_replay(
        self, delivery_id: str, *, error_kind: str
    ) -> None: ...

    async def save_run(self, record: RunRecord) -> None: ...

    async def get_run(self, workflow_run_id: int) -> RunRecord | None: ...

    async def get_active_run(
        self, repository_id: int, pull_request_number: int
    ) -> RunRecord | None: ...

    async def list_stale_runs(self, created_before: datetime) -> list[RunRecord]: ...

    async def list_active_runs(
        self, repository_id: int | None = None
    ) -> list[RunRecord]: ...

    async def clear_active_run(self, record: RunRecord) -> None: ...

    async def upsert_repository(self, record: RepositoryRecord) -> None: ...

    async def get_repository(
        self, repository_id: int
    ) -> RepositoryRecord | None: ...

    async def list_repositories(
        self, installation_id: int
    ) -> list[RepositoryRecord]: ...


class MemoryEventStore:
    def __init__(self, *, delivery_retention_days: int = 14) -> None:
        self.delivery_retention_days = delivery_retention_days
        self._deliveries: dict[str, DeliveryRecord] = {}
        self._runs: dict[int, RunRecord] = {}
        self._active_runs: dict[tuple[int, int], RunRecord] = {}
        self._repositories: dict[int, RepositoryRecord] = {}
        self._replay_audit: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def claim_delivery(
        self,
        delivery_id: str,
        *,
        event: str = "",
        retry: bool = False,
    ) -> bool:
        async with self._lock:
            existing = self._deliveries.get(delivery_id)
            if existing is not None:
                if existing.status == "completed":
                    return False
                if existing.status == "processing" and not retry:
                    return False
                if existing.status in {"failed", "dead_letter"} and not retry:
                    return False
            now = utc_now()
            self._deliveries[delivery_id] = DeliveryRecord(
                delivery_id=delivery_id,
                event=event or (existing.event if existing else ""),
                status="processing",
                attempts=(existing.attempts if existing else 0) + 1,
                received_at=existing.received_at if existing else now,
                updated_at=now,
                expires_at=now + timedelta(days=self.delivery_retention_days),
                replay_actor=existing.replay_actor if existing else "",
                replay_reason=existing.replay_reason if existing else "",
            )
            return True

    async def get_delivery(self, delivery_id: str) -> DeliveryRecord | None:
        async with self._lock:
            return self._deliveries.get(delivery_id)

    async def list_stale_deliveries(
        self, updated_before: datetime
    ) -> list[DeliveryRecord]:
        async with self._lock:
            return [
                record
                for record in self._deliveries.values()
                if record.status == "processing"
                and record.updated_at <= updated_before
            ]

    async def complete_delivery(self, delivery_id: str) -> None:
        await self._set_delivery_status(delivery_id, "completed")

    async def fail_delivery(
        self, delivery_id: str, *, error_kind: str = "retryable"
    ) -> None:
        await self._set_delivery_status(
            delivery_id, "failed", error_kind=error_kind
        )

    async def dead_letter_delivery(
        self, delivery_id: str, *, error_kind: str
    ) -> None:
        await self._set_delivery_status(
            delivery_id, "dead_letter", error_kind=error_kind
        )

    async def request_replay(
        self,
        delivery_id: str,
        *,
        actor: str,
        reason: str,
        audit_retention_days: int,
    ) -> bool:
        async with self._lock:
            record = self._deliveries.get(delivery_id)
            if record is None or record.status not in {"dead_letter", "failed"}:
                return False
            now = utc_now()
            self._deliveries[delivery_id] = replace(
                record,
                status="replay_requested",
                updated_at=now,
                replay_actor=actor,
                replay_reason=reason,
                error_kind="",
            )
            self._replay_audit.append(
                {
                    "delivery_id": delivery_id,
                    "actor": actor,
                    "reason": reason,
                    "status": "requested",
                    "requested_at": now,
                    "expires_at": now + timedelta(days=audit_retention_days),
                }
            )
            return True

    async def reject_replay(
        self, delivery_id: str, *, error_kind: str
    ) -> None:
        await self._set_delivery_status(
            delivery_id, "dead_letter", error_kind=error_kind
        )

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

    async def list_stale_runs(self, created_before: datetime) -> list[RunRecord]:
        async with self._lock:
            return [
                record
                for record in self._active_runs.values()
                if datetime.fromisoformat(record.created_at) <= created_before
            ]

    async def list_active_runs(
        self, repository_id: int | None = None
    ) -> list[RunRecord]:
        async with self._lock:
            return [
                record
                for record in self._active_runs.values()
                if repository_id is None or record.repository_id == repository_id
            ]

    async def clear_active_run(self, record: RunRecord) -> None:
        async with self._lock:
            key = (record.repository_id, record.pull_request_number)
            active = self._active_runs.get(key)
            if active and active.workflow_run_id == record.workflow_run_id:
                self._active_runs.pop(key)

    async def upsert_repository(self, record: RepositoryRecord) -> None:
        async with self._lock:
            self._repositories[record.repository_id] = record

    async def get_repository(
        self, repository_id: int
    ) -> RepositoryRecord | None:
        async with self._lock:
            return self._repositories.get(repository_id)

    async def list_repositories(
        self, installation_id: int
    ) -> list[RepositoryRecord]:
        async with self._lock:
            return [
                record
                for record in self._repositories.values()
                if record.installation_id == installation_id
            ]

    async def _set_delivery_status(
        self,
        delivery_id: str,
        status: str,
        *,
        error_kind: str = "",
    ) -> None:
        async with self._lock:
            record = self._deliveries.get(delivery_id)
            if record is None:
                return
            self._deliveries[delivery_id] = replace(
                record,
                status=status,
                updated_at=utc_now(),
                error_kind=error_kind,
            )


class FirestoreEventStore:
    def __init__(
        self,
        client: Any | None = None,
        *,
        project: str | None = None,
        delivery_retention_days: int = 14,
    ) -> None:
        if client is None:
            from google.cloud import firestore

            client = firestore.AsyncClient(project=project)
        self._client = client
        self.delivery_retention_days = delivery_retention_days

    async def claim_delivery(
        self,
        delivery_id: str,
        *,
        event: str = "",
        retry: bool = False,
    ) -> bool:
        from google.api_core.exceptions import AlreadyExists

        reference = self._client.collection("github_deliveries").document(delivery_id)
        now = utc_now()
        value = {
            "delivery_id": delivery_id,
            "event": event,
            "status": "processing",
            "attempts": 1,
            "received_at": now,
            "updated_at": now,
            "expires_at": now + timedelta(days=self.delivery_retention_days),
            "error_kind": "",
            "replay_actor": "",
            "replay_reason": "",
        }
        try:
            await reference.create(value)
        except AlreadyExists:
            snapshot = await reference.get()
            existing = snapshot.to_dict() if snapshot.exists else None
            if not isinstance(existing, dict):
                return False
            status = str(existing.get("status", ""))
            if status == "completed":
                return False
            if status == "processing" and not retry:
                return False
            if status in {"failed", "dead_letter"} and not retry:
                return False
            value.update(
                {
                    "event": event or str(existing.get("event", "")),
                    "attempts": int(existing.get("attempts", 0)) + 1,
                    "received_at": existing.get("received_at", now),
                    "replay_actor": str(existing.get("replay_actor", "")),
                    "replay_reason": str(existing.get("replay_reason", "")),
                }
            )
            await reference.set(value)
        return True

    async def get_delivery(self, delivery_id: str) -> DeliveryRecord | None:
        snapshot = await self._client.collection("github_deliveries").document(
            delivery_id
        ).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        return (
            self._delivery_from_data(data, delivery_id=delivery_id)
            if isinstance(data, dict)
            else None
        )

    async def list_stale_deliveries(
        self, updated_before: datetime
    ) -> list[DeliveryRecord]:
        records: list[DeliveryRecord] = []
        async for snapshot in self._client.collection(
            "github_deliveries"
        ).stream():
            data = snapshot.to_dict()
            if not isinstance(data, dict) or data.get("status") != "processing":
                continue
            record = self._delivery_from_data(data, delivery_id=snapshot.id)
            if record.updated_at <= updated_before:
                records.append(record)
        return records

    async def complete_delivery(self, delivery_id: str) -> None:
        await self._update_delivery(delivery_id, status="completed")

    async def fail_delivery(
        self, delivery_id: str, *, error_kind: str = "retryable"
    ) -> None:
        await self._update_delivery(
            delivery_id, status="failed", error_kind=error_kind
        )

    async def dead_letter_delivery(
        self, delivery_id: str, *, error_kind: str
    ) -> None:
        await self._update_delivery(
            delivery_id, status="dead_letter", error_kind=error_kind
        )

    async def request_replay(
        self,
        delivery_id: str,
        *,
        actor: str,
        reason: str,
        audit_retention_days: int,
    ) -> bool:
        from google.cloud import firestore

        reference = self._client.collection("github_deliveries").document(delivery_id)
        now = utc_now()
        audit_id = f"{delivery_id}-{int(now.timestamp() * 1_000_000)}"
        audit_reference = self._client.collection("github_replay_audit").document(
            audit_id
        )
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def begin_replay(active_transaction: Any) -> bool:
            snapshot = await reference.get(transaction=active_transaction)
            data = snapshot.to_dict() if snapshot.exists else None
            if not isinstance(data, dict) or data.get("status") not in {
                "dead_letter",
                "failed",
            }:
                return False
            active_transaction.update(
                reference,
                {
                    "status": "replay_requested",
                    "updated_at": now,
                    "error_kind": "",
                    "replay_actor": actor,
                    "replay_reason": reason,
                },
            )
            active_transaction.set(
                audit_reference,
                {
                    "delivery_id": delivery_id,
                    "actor": actor,
                    "reason": reason,
                    "status": "requested",
                    "requested_at": now,
                    "expires_at": now + timedelta(days=audit_retention_days),
                },
            )
            return True

        return await begin_replay(transaction)

    async def reject_replay(
        self, delivery_id: str, *, error_kind: str
    ) -> None:
        await self._update_delivery(
            delivery_id, status="dead_letter", error_kind=error_kind
        )

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
        return self._run_from_snapshot(snapshot)

    async def get_active_run(
        self, repository_id: int, pull_request_number: int
    ) -> RunRecord | None:
        snapshot = await self._active_run_reference(
            repository_id, pull_request_number
        ).get()
        return self._run_from_snapshot(snapshot)

    async def list_stale_runs(self, created_before: datetime) -> list[RunRecord]:
        query = self._client.collection("github_active_runs").where(
            "created_at", "<=", created_before.isoformat()
        )
        records: list[RunRecord] = []
        async for snapshot in query.stream():
            record = self._run_from_snapshot(snapshot)
            if record is not None:
                records.append(record)
        return records

    async def list_active_runs(
        self, repository_id: int | None = None
    ) -> list[RunRecord]:
        query: Any = self._client.collection("github_active_runs")
        if repository_id is not None:
            query = query.where("repository_id", "==", repository_id)
        records: list[RunRecord] = []
        async for snapshot in query.stream():
            record = self._run_from_snapshot(snapshot)
            if record is not None:
                records.append(record)
        return records

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

    async def upsert_repository(self, record: RepositoryRecord) -> None:
        await self._client.collection("github_repositories").document(
            str(record.repository_id)
        ).set(asdict(record))

    async def get_repository(
        self, repository_id: int
    ) -> RepositoryRecord | None:
        snapshot = await self._client.collection("github_repositories").document(
            str(repository_id)
        ).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if not isinstance(data, dict):
            return None
        return RepositoryRecord(**data)

    async def list_repositories(
        self, installation_id: int
    ) -> list[RepositoryRecord]:
        query = self._client.collection("github_repositories").where(
            "installation_id", "==", installation_id
        )
        records: list[RepositoryRecord] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict()
            if isinstance(data, dict):
                records.append(RepositoryRecord(**data))
        return records

    async def _update_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        error_kind: str = "",
    ) -> None:
        reference = self._client.collection("github_deliveries").document(delivery_id)
        await reference.update(
            {
                "status": status,
                "error_kind": error_kind,
                "updated_at": utc_now(),
            }
        )

    def _active_run_reference(
        self, repository_id: int, pull_request_number: int
    ) -> Any:
        return self._client.collection("github_active_runs").document(
            f"{repository_id}-{pull_request_number}"
        )

    @staticmethod
    def _run_from_snapshot(snapshot: Any) -> RunRecord | None:
        if not snapshot.exists:
            return None
        data = snapshot.to_dict()
        if not isinstance(data, dict):
            return None
        return RunRecord(**data)

    @staticmethod
    def _delivery_from_data(
        data: dict[str, Any], *, delivery_id: str
    ) -> DeliveryRecord:
        now = utc_now()
        received_at = data.get("received_at", now)
        updated_at = data.get("updated_at", received_at)
        return DeliveryRecord(
            delivery_id=str(data.get("delivery_id") or delivery_id),
            event=str(data.get("event", "")),
            status=str(data["status"]),
            attempts=int(data.get("attempts", 0)),
            received_at=received_at,
            updated_at=updated_at,
            expires_at=data.get(
                "expires_at",
                now + timedelta(days=14),
            ),
            error_kind=str(data.get("error_kind", "")),
            replay_actor=str(data.get("replay_actor", "")),
            replay_reason=str(data.get("replay_reason", "")),
        )
