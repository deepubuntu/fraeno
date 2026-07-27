from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence

from fraeno.github_app.client import GitHubApiError, GitHubClient
from fraeno.github_app.settings import AppSettings
from fraeno.github_app.store import EventStore, FirestoreEventStore


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
    replay.add_argument(
        "--project",
        required=True,
        help="Exact GCP project containing Fraeno's Firestore metadata.",
    )
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
    return command


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
