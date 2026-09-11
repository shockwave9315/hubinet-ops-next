"""Thin stateless adapter for native Proxmox VE snapshot Restore operations."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
import time
from typing import Any

from proxmoxer.core import ResourceException
from proxmoxer.tools.tasks import Tasks

PVE_SNAPSHOT_NAME_MAX_LENGTH = 40
RESTORE_OBSERVATION_TIMEOUT = 600.0
_MAX_OBSERVATION_ATTEMPTS = 3
_OBSERVATION_RETRY_SECONDS = 5.0
_MAX_REASON_LENGTH = 500
_SNAPSHOT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}", re.ASCII)
_TASK_WARNING_RE = re.compile(r"WARNINGS: [0-9]+", re.ASCII)

type Executor = Callable[[Callable[[], Any]], Awaitable[Any]]


class SnapshotKind(StrEnum):
    """Native PVE guest API family."""

    QEMU = "qemu"
    LXC = "lxc"


class RestoreOutcome(StrEnum):
    """Bounded native Restore outcome."""

    NOT_STARTED = "not_started"
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class NativeSnapshot:
    """One eligible native snapshot choice."""

    name: str
    snaptime: int


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Bounded observation of one native Restore attempt."""

    outcome: RestoreOutcome
    upid: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class SnapshotListError(Exception):
    """Native snapshot listing could not be established."""

    message: str

    def __str__(self) -> str:
        """Return bounded prose."""
        return self.message


def snapshot_name_is_eligible(value: object) -> bool:
    """Return whether a value has PVE's current ASCII pve-configid shape."""
    return (
        isinstance(value, str)
        and value not in {"current", "vzdump"}
        and len(value) <= PVE_SNAPSHOT_NAME_MAX_LENGTH
        and _SNAPSHOT_NAME_RE.fullmatch(value) is not None
    )


def eligible_snapshots(rows: object) -> tuple[NativeSnapshot, ...]:
    """Filter and deterministically order an unordered native PVE listing."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise SnapshotListError("PVE returned a malformed snapshot listing")

    snapshots: list[NativeSnapshot] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = row.get("name")
        if not snapshot_name_is_eligible(name) or row.get("snapstate"):
            continue
        raw_snaptime = row.get("snaptime", 0)
        snaptime = raw_snaptime if type(raw_snaptime) is int else 0
        snapshots.append(NativeSnapshot(name=name, snaptime=snaptime))
    return tuple(sorted(snapshots, key=lambda item: (-item.snaptime, item.name)))


def _snapshot_endpoint(proxmox: Any, node: str, vmid: int, kind: SnapshotKind) -> Any:
    """Return the proxmoxer resource for one native guest snapshot endpoint."""
    guest = (
        proxmox.nodes(node).qemu(vmid)
        if kind is SnapshotKind.QEMU
        else proxmox.nodes(node).lxc(vmid)
    )
    return guest.snapshot


async def async_list_snapshots(
    proxmox: Any,
    node: str,
    vmid: int,
    kind: SnapshotKind,
    *,
    executor: Executor,
) -> tuple[NativeSnapshot, ...]:
    """List eligible native snapshots without retaining inventory state."""
    try:
        rows = await executor(
            lambda: _snapshot_endpoint(proxmox, node, vmid, kind).get()
        )
    except Exception as err:
        raise SnapshotListError("could not list native PVE snapshots") from err
    return eligible_snapshots(rows)


async def async_validate_snapshot(
    proxmox: Any,
    node: str,
    vmid: int,
    kind: SnapshotKind,
    snapshot_name: str,
    *,
    executor: Executor,
) -> bool:
    """Freshly list and require one exact still-eligible native target."""
    if not snapshot_name_is_eligible(snapshot_name):
        return False
    snapshots = await async_list_snapshots(
        proxmox, node, vmid, kind, executor=executor
    )
    return any(snapshot.name == snapshot_name for snapshot in snapshots)


def _bounded_reason(value: object) -> str:
    """Return bounded single-line operator-safe diagnostic text."""
    return " ".join(str(value).split())[:_MAX_REASON_LENGTH]


def _valid_upid(value: object, node: str) -> str | None:
    """Return one structurally valid task ID for the expected node."""
    if not isinstance(value, str) or len(value) > 1024:
        return None
    try:
        decoded = Tasks.decode_upid(value)
    except (AssertionError, TypeError, ValueError):
        return None
    return value if decoded.get("node") == node else None


def _task_succeeded(payload: Mapping[str, Any]) -> bool:
    """Apply native PVE terminal success and successful-warning semantics."""
    exitstatus = payload.get("exitstatus")
    return exitstatus == "OK" or (
        isinstance(exitstatus, str) and _TASK_WARNING_RE.fullmatch(exitstatus) is not None
    )


def _observe_task(
    proxmox: Any,
    upid: str,
    *,
    timeout: float,
    attempts: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> RestoreResult:
    """Observe one UPID with retries sharing one absolute deadline."""
    deadline = monotonic() + timeout
    last_error: Exception | None = None
    for attempt in range(attempts):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            payload = Tasks.blocking_status(
                proxmox,
                upid,
                timeout=remaining,
            )
        except Exception as err:  # noqa: BLE001  # transport families vary
            last_error = err
            if attempt + 1 >= attempts:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(_OBSERVATION_RETRY_SECONDS, remaining))
            continue
        if payload is None:
            return RestoreResult(
                RestoreOutcome.UNCERTAIN,
                upid,
                "PVE task observation reached its deadline",
            )
        if not isinstance(payload, Mapping) or payload.get("status") != "stopped":
            return RestoreResult(
                RestoreOutcome.UNCERTAIN,
                upid,
                "PVE returned malformed task status",
            )
        if _task_succeeded(payload):
            return RestoreResult(RestoreOutcome.SUCCESS, upid)
        return RestoreResult(
            RestoreOutcome.FAILED,
            upid,
            _bounded_reason(payload.get("exitstatus", "PVE reported a task error")),
        )
    reason = (
        f"could not observe PVE task status: {_bounded_reason(last_error)}"
        if last_error is not None
        else "PVE task observation reached its deadline"
    )
    return RestoreResult(RestoreOutcome.UNCERTAIN, upid, reason)


async def async_rollback_snapshot(
    proxmox: Any,
    node: str,
    vmid: int,
    kind: SnapshotKind,
    snapshot_name: str,
    *,
    executor: Executor,
    observation_timeout: float = RESTORE_OBSERVATION_TIMEOUT,
    observation_attempts: int = _MAX_OBSERVATION_ATTEMPTS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RestoreResult:
    """Submit native rollback with start=1 and observe its returned UPID."""
    if not snapshot_name_is_eligible(snapshot_name):
        return RestoreResult(RestoreOutcome.NOT_STARTED, reason="invalid snapshot name")
    try:
        raw_upid = await executor(
            lambda: _snapshot_endpoint(proxmox, node, vmid, kind)
            (snapshot_name)
            .rollback.post(start=1)
        )
    except ResourceException as err:
        if 400 <= err.status_code < 500 and err.status_code != 408:
            return RestoreResult(
                RestoreOutcome.NOT_STARTED,
                reason=_bounded_reason(err),
            )
        return RestoreResult(RestoreOutcome.UNCERTAIN, reason=_bounded_reason(err))
    except Exception as err:  # noqa: BLE001  # any transport failure is uncertain
        return RestoreResult(RestoreOutcome.UNCERTAIN, reason=_bounded_reason(err))

    upid = _valid_upid(raw_upid, node)
    if upid is None:
        return RestoreResult(
            RestoreOutcome.UNCERTAIN,
            reason="PVE returned an invalid rollback task ID",
        )
    return await executor(
        lambda: _observe_task(
            proxmox,
            upid,
            timeout=observation_timeout,
            attempts=observation_attempts,
            monotonic=monotonic,
            sleep=sleep,
        )
    )
