from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class SandboxError(ValueError):
    pass


@contextmanager
def disposable_workspace(
    source: Path,
    *,
    command_uid: int,
    command_gid: int,
) -> Iterator[Path]:
    source = source.resolve()
    if not source.is_dir():
        raise SandboxError(f"workspace source does not exist: {source}")
    if command_uid == 0 or command_gid == 0:
        raise SandboxError("sandbox commands cannot run as root")
    if os.geteuid() != 0 and (
        command_uid != os.geteuid() or command_gid != os.getegid()
    ):
        raise SandboxError("changing sandbox identity requires root")

    with tempfile.TemporaryDirectory(prefix="fraeno-workspace-") as temporary:
        temporary_path = Path(temporary)
        temporary_path.chmod(0o711)
        workspace = temporary_path / "workspace"
        shutil.copytree(
            source,
            workspace,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git"),
        )
        (workspace / ".fraeno-home").mkdir(mode=0o700)
        if os.geteuid() == 0:
            _change_owner(workspace, command_uid, command_gid)
        yield workspace


def require_protected_output(
    output: Path,
    *,
    command_uid: int,
    command_gid: int,
) -> None:
    output = output.resolve()
    parent = output.parent
    if not parent.is_dir():
        raise SandboxError(f"output directory does not exist: {parent}")
    if _identity_can_write(parent.stat(), command_uid, command_gid):
        raise SandboxError(
            "sandbox identity can write the protected evidence directory"
        )
    if output.exists() and _identity_can_write(
        output.stat(), command_uid, command_gid
    ):
        raise SandboxError("sandbox identity can write the protected evidence file")


def _identity_can_write(
    details: os.stat_result,
    uid: int,
    gid: int,
) -> bool:
    mode = details.st_mode
    if details.st_uid == uid:
        return bool(mode & stat.S_IWUSR)
    if details.st_gid == gid:
        return bool(mode & stat.S_IWGRP)
    return bool(mode & stat.S_IWOTH)


def _change_owner(root: Path, uid: int, gid: int) -> None:
    os.chown(root, uid, gid, follow_symlinks=False)
    for directory, names, files in os.walk(root):
        current = Path(directory)
        for name in [*names, *files]:
            os.chown(current / name, uid, gid, follow_symlinks=False)
