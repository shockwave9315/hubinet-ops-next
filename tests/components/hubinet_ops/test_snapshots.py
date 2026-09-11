"""Tests for the thin native snapshot Restore adapter."""

from unittest.mock import MagicMock, patch

from proxmoxer.core import ResourceException
import pytest

from custom_components.hubinet_ops.snapshots import (
    RestoreOutcome,
    SnapshotKind,
    async_list_snapshots,
    async_rollback_snapshot,
    async_validate_snapshot,
    eligible_snapshots,
)

UPID = "UPID:pve1:00000001:00000002:00000003:qmrollback:100:user@pam:"


async def _executor(call):
    """Run a blocking-shaped callable immediately in adapter tests."""
    return call()


def _proxmox(kind: SnapshotKind, rows: object = ()) -> tuple[MagicMock, MagicMock]:
    proxmox = MagicMock()
    guest = getattr(proxmox.nodes.return_value, kind.value).return_value
    guest.snapshot.get.return_value = rows
    return proxmox, guest


def test_snapshot_eligibility_filter_and_order() -> None:
    """Filter reserved, transient, and malformed rows without hiding safety snapshots."""
    rows = [
        {"name": "older", "snaptime": 10},
        {"name": "same_b", "snaptime": 20},
        {"name": "same-a", "snaptime": 20},
        {"name": "hubinet-preupd-20260911", "snaptime": 30},
        {"name": "current", "snaptime": 40},
        {"name": "vzdump", "snaptime": 40},
        {"name": "bad.name", "snaptime": 40},
        {"name": "éclair", "snaptime": 40},
        {"name": "active", "snaptime": 40, "snapstate": "prepare"},
        {"name": "A" * 41, "snaptime": 40},
        {"name": "A", "snaptime": "bad"},
        {"missing": "name"},
    ]

    assert [snapshot.name for snapshot in eligible_snapshots(rows)] == [
        "hubinet-preupd-20260911",
        "same-a",
        "same_b",
        "older",
        "A",
    ]


@pytest.mark.parametrize("kind", list(SnapshotKind))
async def test_list_qemu_and_lxc_snapshots(kind: SnapshotKind) -> None:
    """Use the matching native proxmoxer QEMU or LXC endpoint."""
    proxmox, guest = _proxmox(kind, [{"name": "snap-one", "snaptime": 1}])

    result = await async_list_snapshots(
        proxmox, "pve1", 100, kind, executor=_executor
    )

    assert [snapshot.name for snapshot in result] == ["snap-one"]
    guest.snapshot.get.assert_called_once_with()


async def test_exact_validation_always_uses_fresh_list() -> None:
    """Require exact current eligibility instead of trusting select presentation."""
    proxmox, guest = _proxmox(
        SnapshotKind.QEMU, [{"name": "wanted", "snaptime": 1}]
    )
    assert await async_validate_snapshot(
        proxmox, "pve1", 100, SnapshotKind.QEMU, "wanted", executor=_executor
    )
    guest.snapshot.get.return_value = [
        {"name": "wanted", "snaptime": 1, "snapstate": "delete"}
    ]
    assert not await async_validate_snapshot(
        proxmox, "pve1", 100, SnapshotKind.QEMU, "wanted", executor=_executor
    )
    assert guest.snapshot.get.call_count == 2


@pytest.mark.parametrize("kind", list(SnapshotKind))
async def test_rollback_sends_start_and_classifies_success(kind: SnapshotKind) -> None:
    """Submit start=1 for either family and accept native OK completion."""
    proxmox, guest = _proxmox(kind)
    guest.snapshot.return_value.rollback.post.return_value = UPID
    with patch(
        "custom_components.hubinet_ops.snapshots.Tasks.blocking_status",
        return_value={"status": "stopped", "exitstatus": "OK"},
    ):
        result = await async_rollback_snapshot(
            proxmox, "pve1", 100, kind, "wanted", executor=_executor
        )

    assert result.outcome is RestoreOutcome.SUCCESS
    assert result.upid == UPID
    guest.snapshot.assert_called_once_with("wanted")
    guest.snapshot.return_value.rollback.post.assert_called_once_with(start=1)


@pytest.mark.parametrize("exitstatus", ["WARNINGS: 1", "WARNINGS: 23"])
async def test_known_warning_terminal_form_succeeds(exitstatus: str) -> None:
    """Follow PVE's successful-with-warning terminal semantics."""
    proxmox, guest = _proxmox(SnapshotKind.LXC)
    guest.snapshot.return_value.rollback.post.return_value = UPID
    with patch(
        "custom_components.hubinet_ops.snapshots.Tasks.blocking_status",
        return_value={"status": "stopped", "exitstatus": exitstatus},
    ):
        result = await async_rollback_snapshot(
            proxmox, "pve1", 100, SnapshotKind.LXC, "wanted", executor=_executor
        )
    assert result.outcome is RestoreOutcome.SUCCESS


async def test_known_terminal_error_is_failed() -> None:
    """FAILED means the native worker exited with an error."""
    proxmox, guest = _proxmox(SnapshotKind.QEMU)
    guest.snapshot.return_value.rollback.post.return_value = UPID
    with patch(
        "custom_components.hubinet_ops.snapshots.Tasks.blocking_status",
        return_value={"status": "stopped", "exitstatus": "disk error"},
    ):
        result = await async_rollback_snapshot(
            proxmox, "pve1", 100, SnapshotKind.QEMU, "wanted", executor=_executor
        )
    assert result.outcome is RestoreOutcome.FAILED
    assert result.upid == UPID


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (
            ResourceException(400, "bad request", "rejected"),
            RestoreOutcome.NOT_STARTED,
        ),
        (ResourceException(500, "server error", "unknown"), RestoreOutcome.UNCERTAIN),
        (ConnectionError("connection lost"), RestoreOutcome.UNCERTAIN),
    ],
)
async def test_post_failure_classification(
    error: Exception, outcome: RestoreOutcome
) -> None:
    """Only a definite pre-worker rejection is NOT_STARTED."""
    proxmox, guest = _proxmox(SnapshotKind.LXC)
    guest.snapshot.return_value.rollback.post.side_effect = error
    result = await async_rollback_snapshot(
        proxmox, "pve1", 100, SnapshotKind.LXC, "wanted", executor=_executor
    )
    assert result.outcome is outcome


async def test_malformed_upid_is_uncertain() -> None:
    """A successful response without an observable task stays fail-closed."""
    proxmox, guest = _proxmox(SnapshotKind.LXC)
    guest.snapshot.return_value.rollback.post.return_value = "not-a-UPID"
    result = await async_rollback_snapshot(
        proxmox, "pve1", 100, SnapshotKind.LXC, "wanted", executor=_executor
    )
    assert result.outcome is RestoreOutcome.UNCERTAIN
    assert result.upid is None


async def test_observation_retries_share_one_absolute_deadline() -> None:
    """Retries receive remaining time, never a fresh 600-second allowance."""
    proxmox, guest = _proxmox(SnapshotKind.QEMU)
    guest.snapshot.return_value.rollback.post.return_value = UPID
    now = [100.0]
    timeouts: list[float] = []

    def blocking_status(_proxmox, _upid, *, timeout, polling_interval=1):
        del polling_interval
        timeouts.append(timeout)
        if len(timeouts) < 3:
            raise ConnectionError("transient")
        return {"status": "stopped", "exitstatus": "OK"}

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with patch(
        "custom_components.hubinet_ops.snapshots.Tasks.blocking_status",
        side_effect=blocking_status,
    ):
        result = await async_rollback_snapshot(
            proxmox,
            "pve1",
            100,
            SnapshotKind.QEMU,
            "wanted",
            executor=_executor,
            monotonic=lambda: now[0],
            sleep=sleep,
        )

    assert result.outcome is RestoreOutcome.SUCCESS
    assert timeouts == [600.0, 595.0, 590.0]
    assert sum(timeouts) < 1800


async def test_observation_timeout_is_uncertain() -> None:
    """None at the absolute deadline cannot prove a terminal task."""
    proxmox, guest = _proxmox(SnapshotKind.QEMU)
    guest.snapshot.return_value.rollback.post.return_value = UPID
    with patch(
        "custom_components.hubinet_ops.snapshots.Tasks.blocking_status",
        return_value=None,
    ):
        result = await async_rollback_snapshot(
            proxmox, "pve1", 100, SnapshotKind.QEMU, "wanted", executor=_executor
        )
    assert result.outcome is RestoreOutcome.UNCERTAIN
    assert result.upid == UPID
