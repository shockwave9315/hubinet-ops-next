"""Stateless native Proxmox VE snapshot glue for package updates."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import re
import secrets
from typing import Any

SNAPSHOT_PREFIX = "hubinet-preupd-"
PVE_SNAPSHOT_NAME_MAX_LENGTH = 40
_SNAPSHOT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")
_TASK_WARNING_RE = re.compile(r"WARNINGS: \d+")
_TASK_POLL_INTERVAL_SECONDS = 2.0
_TASK_MAX_POLLS = 120

type Executor = Callable[[Callable[[], Any]], Awaitable[Any]]
type Sleeper = Callable[[float], Awaitable[None]]
type SubmissionCallback = Callable[[], None]


class SnapshotTaskState(StrEnum):
    """Normalized PVE task state."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(slots=True)
class SnapshotError(Exception):
    """A bounded native snapshot failure."""

    message: str
    may_exist: bool = False
    terminal_failure: bool = False

    def __str__(self) -> str:
        """Return the bounded error message."""
        return self.message


def validate_snapshot_name(name: str) -> str:
    """Validate the current PVE ``pve-snapshot-name`` schema and reservations."""
    if (
        not isinstance(name, str)
        or not 2 <= len(name) <= PVE_SNAPSHOT_NAME_MAX_LENGTH
        or not _SNAPSHOT_NAME_RE.fullmatch(name)
        or name in {"current", "vzdump"}
    ):
        raise ValueError("invalid PVE snapshot name")
    return name


def generate_snapshot_name(
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    random_suffix: Callable[[int], str] = secrets.token_hex,
) -> str:
    """Generate one recognizable, unique, safely bounded PVE snapshot name."""
    name = f"{SNAPSHOT_PREFIX}{now().strftime('%Y%m%d%H%M%S')}-{random_suffix(3)}"
    return validate_snapshot_name(name)


@dataclass(frozen=True, slots=True)
class RetainedSnapshotSummary:
    """True retained-snapshot count with a bounded displayed-name subset."""

    total_count: int
    names: tuple[str, ...]


def retained_snapshot_summary(
    rows: object, *, limit: int = 5
) -> RetainedSnapshotSummary:
    """Return the true retained count and a bounded warning-name subset."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise SnapshotError("PVE returned a malformed snapshot listing")
    if any(
        not isinstance(row, Mapping) or not isinstance(row.get("name"), str)
        for row in rows
    ):
        raise SnapshotError("PVE returned a malformed snapshot listing")
    names = sorted(
        {
            name
            for row in rows
            if isinstance(row, Mapping)
            and isinstance((name := row.get("name")), str)
            and name != "current"
            and name.startswith(SNAPSHOT_PREFIX)
        }
    )
    return RetainedSnapshotSummary(len(names), tuple(names[:limit]))


def snapshot_is_complete(rows: object, snapshot_name: str) -> bool:
    """Require one exact snapshot row with no transient ``snapstate``."""
    validate_snapshot_name(snapshot_name)
    if (
        not isinstance(rows, Sequence)
        or isinstance(rows, (str, bytes))
        or any(
            not isinstance(row, Mapping) or not isinstance(row.get("name"), str)
            for row in rows
        )
    ):
        return False
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("name") == snapshot_name
    ]
    return len(matches) == 1 and not matches[0].get("snapstate")


def snapshot_is_absent(rows: object, snapshot_name: str) -> bool:
    """Return true only when an exact validated name is absent from a valid listing."""
    validate_snapshot_name(snapshot_name)
    if (
        not isinstance(rows, Sequence)
        or isinstance(rows, (str, bytes))
        or any(
            not isinstance(row, Mapping) or not isinstance(row.get("name"), str)
            for row in rows
        )
    ):
        return False
    return not any(
        isinstance(row, Mapping) and row.get("name") == snapshot_name for row in rows
    )


def parse_task_status(payload: object) -> SnapshotTaskState:
    """Normalize PVE running/stopped task evidence, failing closed on ambiguity."""
    if not isinstance(payload, Mapping):
        return SnapshotTaskState.FAILED
    status = payload.get("status")
    if status == "running":
        return SnapshotTaskState.RUNNING
    if status != "stopped":
        return SnapshotTaskState.FAILED
    exitstatus = payload.get("exitstatus")
    if exitstatus == "OK" or (
        isinstance(exitstatus, str) and _TASK_WARNING_RE.fullmatch(exitstatus)
    ):
        return SnapshotTaskState.SUCCESS
    return SnapshotTaskState.FAILED


def _require_upid(value: object) -> str:
    """Require one bounded PVE task identifier returned by a mutating API call."""
    if (
        not isinstance(value, str)
        or not value.startswith("UPID:")
        or len(value) > 1024
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise SnapshotError("PVE snapshot operation returned an invalid task ID")
    return value


async def _async_wait_for_task(
    proxmox: Any,
    node: str,
    upid: str,
    *,
    executor: Executor,
    sleep: Sleeper,
    max_polls: int,
) -> None:
    """Poll one native PVE task to a terminal successful result."""
    for poll in range(max_polls):
        try:
            payload = await executor(
                lambda: proxmox.nodes(node).tasks(upid).status.get()
            )
        except Exception as err:
            raise SnapshotError("could not read PVE snapshot task status", True) from err
        state = parse_task_status(payload)
        if state is SnapshotTaskState.SUCCESS:
            return
        if state is SnapshotTaskState.FAILED:
            raise SnapshotError(
                "PVE snapshot task did not succeed",
                True,
                isinstance(payload, Mapping) and payload.get("status") == "stopped",
            )
        if poll + 1 < max_polls:
            await sleep(_TASK_POLL_INTERVAL_SECONDS)
    raise SnapshotError("PVE snapshot task did not finish before timeout", True)


async def async_list_snapshots(
    proxmox: Any, node: str, vmid: int, *, executor: Executor
) -> object:
    """List native LXC snapshots through proxmoxer."""
    try:
        return await executor(lambda: proxmox.nodes(node).lxc(vmid).snapshot.get())
    except Exception as err:
        raise SnapshotError("could not list native PVE snapshots") from err


async def async_create_snapshot(
    proxmox: Any,
    node: str,
    vmid: int,
    snapshot_name: str,
    *,
    executor: Executor,
    sleep: Sleeper = asyncio.sleep,
    max_polls: int = _TASK_MAX_POLLS,
    on_submit: SubmissionCallback = lambda: None,
) -> None:
    """Create, poll, and confirm one exact complete native LXC snapshot."""
    validate_snapshot_name(snapshot_name)
    def _submit() -> object:
        """Mark the precise point at which the native POST call is entered."""
        on_submit()
        return (
            proxmox.nodes(node)
            .lxc(vmid)
            .snapshot.post(
                snapname=snapshot_name,
                description="Temporary Hubinet-Ops pre-update safety snapshot",
            )
        )

    try:
        raw_upid = await executor(_submit)
    except Exception as err:
        raise SnapshotError("could not submit native PVE snapshot", True) from err
    try:
        upid = _require_upid(raw_upid)
    except SnapshotError as err:
        err.may_exist = True
        raise
    try:
        await _async_wait_for_task(
            proxmox,
            node,
            upid,
            executor=executor,
            sleep=sleep,
            max_polls=max_polls,
        )
    except SnapshotError as err:
        if err.terminal_failure:
            try:
                rows = await async_list_snapshots(
                    proxmox, node, vmid, executor=executor
                )
            except SnapshotError:
                pass
            else:
                if snapshot_is_absent(rows, snapshot_name):
                    err.may_exist = False
        raise
    try:
        rows = await async_list_snapshots(proxmox, node, vmid, executor=executor)
    except SnapshotError as err:
        err.may_exist = True
        raise
    if not snapshot_is_complete(rows, snapshot_name):
        raise SnapshotError("native PVE snapshot could not be confirmed complete", True)


async def async_delete_snapshot(
    proxmox: Any,
    node: str,
    vmid: int,
    snapshot_name: str,
    *,
    executor: Executor,
    sleep: Sleeper = asyncio.sleep,
    max_polls: int = _TASK_MAX_POLLS,
) -> None:
    """Delete only the exact supplied snapshot, then confirm exact absence."""
    validate_snapshot_name(snapshot_name)
    if not snapshot_name.startswith(SNAPSHOT_PREFIX):
        raise SnapshotError("refusing to delete a non-Hubinet snapshot", True)
    try:
        raw_upid = await executor(
            lambda: proxmox.nodes(node).lxc(vmid).snapshot(snapshot_name).delete()
        )
        upid = _require_upid(raw_upid)
    except Exception as err:
        raise SnapshotError("could not submit native PVE snapshot deletion", True) from err
    await _async_wait_for_task(
        proxmox,
        node,
        upid,
        executor=executor,
        sleep=sleep,
        max_polls=max_polls,
    )
    try:
        rows = await async_list_snapshots(proxmox, node, vmid, executor=executor)
    except SnapshotError as err:
        err.may_exist = True
        raise
    if not snapshot_is_absent(rows, snapshot_name):
        raise SnapshotError("native PVE snapshot deletion could not be confirmed", True)
