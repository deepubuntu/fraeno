from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone

from fraeno.github_app.client import GitHubApiError, GitHubClient
from fraeno.github_app.settings import AppSettings
from fraeno.github_app.store import (
    EntitlementRecord,
    EventStore,
    FirestoreEventStore,
    utc_now,
)


def _project_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--project",
        required=True,
        help="Exact GCP project containing Fraeno's Firestore metadata.",
    )


def _optional_timestamp(raw_value: str) -> datetime | None:
    value = raw_value.strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="fraeno-github-ops",
        description="Run guarded Fraeno GitHub App recovery operations.",
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    replay = subcommands.add_parser(
        "replay",
        help="Ask GitHub to redeliver a failed signed webhook.",
    )
    replay.add_argument("--delivery-guid", required=True)
    replay.add_argument("--github-delivery-id", required=True, type=int)
    _project_argument(replay)
    replay.add_argument("--reason", required=True)
    replay.add_argument(
        "--actor",
        default=os.environ.get("FRAENO_OPERATOR", ""),
        help="Operator identity. FRAENO_OPERATOR is used when omitted.",
    )
    replay.add_argument(
        "--confirm",
        required=True,
        help="Repeat the full delivery GUID to confirm the exact target.",
    )
    replay.add_argument(
        "--execute",
        action="store_true",
        help="Perform the redelivery. Without this flag the command is read-only.",
    )
    customers = subcommands.add_parser(
        "customers",
        help="List installation, activation, entitlement, and current usage records.",
    )
    _project_argument(customers)
    entitlement = subcommands.add_parser(
        "entitle",
        help="Create or change one installation entitlement.",
    )
    _project_argument(entitlement)
    entitlement.add_argument("--installation-id", required=True, type=int)
    entitlement.add_argument(
        "--status",
        required=True,
        choices=("trial", "active", "grace", "suspended", "expired"),
    )
    entitlement.add_argument("--plan", default="private_beta")
    entitlement.add_argument(
        "--source", choices=("manual", "stripe", "marketplace"), default="manual"
    )
    entitlement.add_argument(
        "--billing-status",
        choices=("unpaid", "paid", "comped"),
        default="unpaid",
    )
    entitlement.add_argument("--starts-at", type=_optional_timestamp, default=None)
    entitlement.add_argument("--ends-at", type=_optional_timestamp, default=None)
    entitlement.add_argument(
        "--grace-ends-at", type=_optional_timestamp, default=None
    )
    entitlement.add_argument(
        "--actor",
        default=os.environ.get("FRAENO_OPERATOR", ""),
        help="Operator identity. FRAENO_OPERATOR is used when omitted.",
    )
    entitlement.add_argument("--reason", required=True)
    entitlement.add_argument(
        "--confirm",
        required=True,
        help="Repeat the installation ID to confirm the exact account.",
    )
    entitlement.add_argument(
        "--execute",
        action="store_true",
        help="Write the entitlement. Without this flag the command is a dry run.",
    )
    return command


async def list_customers(store: EventStore) -> int:
    installations = await store.list_installations()
    entitlements = {
        item.installation_id: item for item in await store.list_entitlements()
    }
    current_period = utc_now().strftime("%Y-%m")
    usage = {
        item.installation_id: item for item in await store.list_usage(current_period)
    }
    output = []
    for installation in sorted(
        installations, key=lambda item: item.installed_at, reverse=True
    ):
        output.append(
            {
                "installation": asdict(installation),
                "entitlement": (
                    asdict(entitlements[installation.installation_id])
                    if installation.installation_id in entitlements
                    else None
                ),
                "current_usage": (
                    asdict(usage[installation.installation_id])
                    if installation.installation_id in usage
                    else None
                ),
            }
        )
    print(json.dumps(output, default=str, indent=2, sort_keys=True))
    return 0


async def set_entitlement(arguments: argparse.Namespace, store: EventStore) -> int:
    installation_id = int(arguments.installation_id)
    if str(arguments.confirm).strip() != str(installation_id):
        print("Confirmation does not match the installation ID.")
        return 2
    actor = str(arguments.actor).strip()
    reason = str(arguments.reason).strip()
    if not actor:
        print("An operator identity is required through --actor or FRAENO_OPERATOR.")
        return 2
    if len(reason) < 10:
        print("Give a concrete entitlement reason with at least 10 characters.")
        return 2
    if await store.get_installation(installation_id) is None:
        print("Fraeno has no installation record for that ID.")
        return 2
    now = utc_now()
    record = EntitlementRecord(
        installation_id=installation_id,
        status=str(arguments.status),
        plan=str(arguments.plan),
        source=str(arguments.source),
        billing_status=str(arguments.billing_status),
        starts_at=arguments.starts_at or now,
        updated_at=now,
        updated_by=actor,
        note=reason,
        ends_at=arguments.ends_at,
        grace_ends_at=arguments.grace_ends_at,
    )
    if not arguments.execute:
        print(json.dumps(asdict(record), default=str, indent=2, sort_keys=True))
        print("Dry run only. Add --execute to write this entitlement.")
        return 0
    await store.upsert_entitlement(record)
    print(f"Updated entitlement for installation {installation_id}.")
    return 0


async def replay_delivery(
    arguments: argparse.Namespace,
    *,
    settings: AppSettings,
    client: GitHubClient,
    store: EventStore,
) -> int:
    guid = str(arguments.delivery_guid).strip()
    actor = str(arguments.actor).strip()
    reason = str(arguments.reason).strip()
    if arguments.confirm != guid:
        print("Confirmation does not match the delivery GUID.")
        return 2
    if not actor:
        print("An operator identity is required through --actor or FRAENO_OPERATOR.")
        return 2
    if len(reason) < 10:
        print("Give a concrete replay reason with at least 10 characters.")
        return 2

    record = await store.get_delivery(guid)
    if record is None:
        print("Fraeno has no metadata record for that delivery.")
        return 2
    if record.status not in {"failed", "dead_letter"}:
        print(f"Delivery is not replayable because its status is {record.status}.")
        return 2

    github_delivery = await client.app_delivery(arguments.github_delivery_id)
    if github_delivery.get("guid") != guid:
        print("GitHub's delivery GUID does not match the confirmed Fraeno record.")
        return 2
    if not arguments.execute:
        print(
            "Replay is eligible. Add --execute to ask GitHub to send the original "
            "signed webhook again."
        )
        return 0

    requested = await store.request_replay(
        guid,
        actor=actor,
        reason=reason,
        audit_retention_days=settings.replay_audit_retention_days,
    )
    if not requested:
        print("Delivery state changed before the replay could start.")
        return 2
    try:
        await client.redeliver_app_delivery(arguments.github_delivery_id)
    except GitHubApiError as error:
        if not error.retryable:
            await store.reject_replay(
                guid, error_kind="github_redelivery_rejected"
            )
        raise
    print("GitHub accepted the redelivery request.")
    return 0


async def run(
    arguments: argparse.Namespace,
    *,
    settings: AppSettings | None = None,
    client: GitHubClient | None = None,
    store: EventStore | None = None,
) -> int:
    if arguments.command in {"customers", "entitle"}:
        active_store = store or FirestoreEventStore(project=arguments.project)
        if arguments.command == "customers":
            return await list_customers(active_store)
        return await set_entitlement(arguments, active_store)
    active_settings = settings or AppSettings.from_environment()
    active_client = client or GitHubClient(active_settings)
    active_store = store or FirestoreEventStore(
        project=arguments.project,
        delivery_retention_days=active_settings.delivery_retention_days
    )
    close_client = client is None
    try:
        if arguments.command == "replay":
            return await replay_delivery(
                arguments,
                settings=active_settings,
                client=active_client,
                store=active_store,
            )
        raise RuntimeError("unknown operator command")
    finally:
        if close_client:
            await active_client.close()


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(parser().parse_args(argv)))
