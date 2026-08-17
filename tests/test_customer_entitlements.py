import argparse
from datetime import timedelta

import pytest
from test_github_handler import (
    FakeGitHubClient,
    pull_request_payload,
    workflow_completed,
)

from fraeno.github_app.handler import EventHandler
from fraeno.github_app.operations import list_customers, set_entitlement
from fraeno.github_app.store import (
    EntitlementRecord,
    InstallationRecord,
    MemoryEventStore,
    utc_now,
)


def entitlement(
    installation_id: int = 42,
    *,
    status: str = "active",
) -> EntitlementRecord:
    now = utc_now()
    return EntitlementRecord(
        installation_id=installation_id,
        status=status,
        plan="private_beta",
        source="manual",
        billing_status="comped",
        starts_at=now - timedelta(minutes=1),
        updated_at=now,
        updated_by="test@deepubuntu.com",
        note="Approved for the private beta.",
    )


def acme_payload() -> dict[str, object]:
    payload = pull_request_payload()
    payload["installation"] = {
        "id": 42,
        "account": {"id": 9001, "login": "acme", "type": "Organization"},
    }
    repository = payload["repository"]
    assert isinstance(repository, dict)
    repository["full_name"] = "acme/warehouse-robot"
    return payload


def test_entitlement_status_and_dates_control_access() -> None:
    now = utc_now()
    assert entitlement().permits(now)
    assert not entitlement(status="suspended").permits(now)
    expired = entitlement()
    expired = EntitlementRecord(
        **{
            **expired.__dict__,
            "ends_at": now - timedelta(seconds=1),
        }
    )
    assert not expired.permits(now)


@pytest.mark.anyio
async def test_entitled_installation_is_registered_activated_and_counted() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    await store.upsert_entitlement(entitlement())
    handler = EventHandler(client, store)  # type: ignore[arg-type]

    await handler.process("pull_request", "delivery-1", acme_payload())

    installation = await store.get_installation(42)
    assert installation is not None
    assert installation.account_id == 9001
    assert installation.account_login == "acme"
    assert installation.account_type == "Organization"
    assert installation.first_check_at is not None
    assert installation.activated_at == installation.first_check_at
    usage = await store.list_usage(utc_now().strftime("%Y-%m"))
    assert len(usage) == 1
    assert usage[0].checks_started == 1
    assert usage[0].checks_completed == 0

    await handler.process("workflow_run", "delivery-2", workflow_completed(300))

    usage = await store.list_usage(utc_now().strftime("%Y-%m"))
    assert usage[0].checks_started == 1
    assert usage[0].checks_completed == 1


@pytest.mark.anyio
async def test_suspended_entitlement_does_not_dispatch() -> None:
    client = FakeGitHubClient()
    store = MemoryEventStore()
    await store.upsert_entitlement(entitlement(status="suspended"))
    handler = EventHandler(client, store)  # type: ignore[arg-type]

    await handler.process("pull_request", "delivery-1", acme_payload())

    assert client.dispatches == []
    record = await store.get_repository(100)
    assert record is not None
    assert record.status == "not_approved"


@pytest.mark.anyio
async def test_installation_registry_is_permanent_across_status_changes() -> None:
    store = MemoryEventStore()
    installed = InstallationRecord.create(
        installation_id=42,
        account_id=9001,
        account_login="acme",
        account_type="Organization",
    )
    await store.upsert_installation(installed)

    removed = InstallationRecord(
        **{
            **installed.__dict__,
            "status": "removed",
            "updated_at": utc_now(),
            "connected_repositories": 0,
        }
    )
    await store.upsert_installation(removed)

    records = await store.list_installations()
    assert len(records) == 1
    assert records[0].installation_id == 42
    assert records[0].status == "removed"
    assert records[0].installed_at == installed.installed_at


@pytest.mark.anyio
async def test_guarded_manual_entitlement_requires_execute(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryEventStore()
    installation = InstallationRecord.create(
        installation_id=42,
        account_id=9001,
        account_login="acme",
        account_type="Organization",
    )
    await store.upsert_installation(installation)
    arguments = argparse.Namespace(
        installation_id=42,
        confirm="42",
        actor="operator@deepubuntu.com",
        reason="Approved for the private beta.",
        status="trial",
        plan="private_beta",
        source="manual",
        billing_status="comped",
        starts_at=None,
        ends_at=None,
        grace_ends_at=None,
        execute=False,
    )

    assert await set_entitlement(arguments, store) == 0
    assert await store.get_entitlement(42) is None
    assert "Dry run only" in capsys.readouterr().out

    arguments.execute = True
    assert await set_entitlement(arguments, store) == 0
    saved = await store.get_entitlement(42)
    assert saved is not None
    assert saved.status == "trial"
    assert saved.billing_status == "comped"


@pytest.mark.anyio
async def test_customer_listing_joins_current_entitlement_and_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryEventStore()
    installation = InstallationRecord.create(
        installation_id=42,
        account_id=9001,
        account_login="acme",
        account_type="Organization",
    )
    await store.upsert_installation(installation)
    await store.upsert_entitlement(entitlement())
    await store.record_check_started(42, "acme", utc_now())

    assert await list_customers(store) == 0
    output = capsys.readouterr().out
    assert '"account_login": "acme"' in output
    assert '"checks_started": 1' in output
    assert '"billing_status": "comped"' in output
