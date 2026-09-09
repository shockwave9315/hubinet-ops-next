"""Tests for stateless native PVE snapshot lifecycle glue."""

from datetime import UTC, datetime
import re
from unittest.mock import MagicMock

import pytest

from custom_components.hubinet_ops.packages.snapshots import (
    PVE_SNAPSHOT_NAME_MAX_LENGTH,
    SNAPSHOT_PREFIX,
    SnapshotError,
    SnapshotTaskState,
    async_create_snapshot,
    async_delete_snapshot,
    generate_snapshot_name,
    parse_task_status,
    retained_snapshot_names,
    snapshot_is_complete,
    validate_snapshot_name,
)

CURRENT = "hubinet-preupd-20260908120000-ab12cd"
OLD = "hubinet-preupd-20260907120000-cd34ef"
UPID = "UPID:pve1:00000001:00000002:00000003:vzsnapshot:200:user@pve:"


async def _executor(call):
    """Run a synchronous proxmoxer call like HA's executor wrapper."""
    return call()


async def _no_sleep(_delay: float) -> None:
    """Skip polling delay in unit tests."""


def _proxmox(
    *, statuses: list[object], listings: list[object], delete_upid: str = UPID
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build the dynamic proxmoxer endpoint shape used by the adapter."""
    proxmox = MagicMock()
    node = proxmox.nodes.return_value
    lxc = node.lxc.return_value
    node.tasks.return_value.status.get.side_effect = statuses
    lxc.snapshot.get.side_effect = listings
    lxc.snapshot.post.return_value = UPID
    lxc.snapshot.return_value.delete.return_value = delete_upid
    return proxmox, node, lxc


def test_generated_snapshot_name_matches_current_pve_schema() -> None:
    """Generated names honor pve-configid, reservations, and the 40-char max."""
    name = generate_snapshot_name(
        now=lambda: datetime(2026, 9, 8, 12, 34, 56, tzinfo=UTC),
        random_suffix=lambda _size: "ab12cd",
    )
    assert name == "hubinet-preupd-20260908123456-ab12cd"
    assert len(name) <= PVE_SNAPSHOT_NAME_MAX_LENGTH == 40
    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]+", name)
    assert validate_snapshot_name(name) == name
    for reserved in ("current", "vzdump"):
        with pytest.raises(ValueError):
            validate_snapshot_name(reserved)


def test_retained_discovery_is_prefix_only_bounded_and_ignores_current() -> None:
    """Old Hubinet names warn; unrelated snapshots and current are ignored."""
    rows = [
        {"name": CURRENT},
        {"name": OLD},
        {"name": "manual-safe"},
        {"name": "current"},
    ]
    assert retained_snapshot_names(rows) == (OLD, CURRENT)
    assert retained_snapshot_names(rows, limit=1) == (OLD,)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "running"}, SnapshotTaskState.RUNNING),
        ({"status": "stopped", "exitstatus": "OK"}, SnapshotTaskState.SUCCESS),
        (
            {"status": "stopped", "exitstatus": "WARNINGS: 1"},
            SnapshotTaskState.SUCCESS,
        ),
        ({"status": "stopped", "exitstatus": "ERROR"}, SnapshotTaskState.FAILED),
        ({"status": "stopped"}, SnapshotTaskState.FAILED),
        ({"status": "unknown", "exitstatus": "OK"}, SnapshotTaskState.FAILED),
    ],
)
def test_native_task_status_semantics(
    payload: object, expected: SnapshotTaskState
) -> None:
    """Only running or stopped plus PVE OK/WARNINGS evidence is recognized."""
    assert parse_task_status(payload) is expected


async def test_create_waits_for_task_then_requires_exact_complete_row() -> None:
    """A submitted UPID is not readiness; terminal status and listing both matter."""
    proxmox, node, lxc = _proxmox(
        statuses=[{"status": "running"}, {"status": "stopped", "exitstatus": "OK"}],
        listings=[[{"name": CURRENT, "snaptime": 1}]],
    )
    await async_create_snapshot(
        proxmox,
        "pve1",
        200,
        CURRENT,
        executor=_executor,
        sleep=_no_sleep,
        max_polls=2,
    )
    assert node.tasks.call_args_list[0].args == (UPID,)
    lxc.snapshot.post.assert_called_once_with(
        snapname=CURRENT,
        description="Temporary Hubinet-Ops pre-update safety snapshot",
    )


@pytest.mark.parametrize(
    ("statuses", "rows"),
    [
        ([{"status": "stopped", "exitstatus": "ERROR"}], [{"name": CURRENT}]),
        ([{"status": "stopped"}], [{"name": CURRENT}]),
        (
            [{"status": "stopped", "exitstatus": "OK"}],
            [{"name": CURRENT, "snapstate": "prepare"}],
        ),
        ([{"status": "running"}], [{"name": CURRENT}]),
    ],
)
async def test_create_never_accepts_failed_incomplete_or_still_running_task(
    statuses: list[object], rows: list[object]
) -> None:
    """Uncertain/failed native creation never authorizes package mutation."""
    proxmox, _node, _lxc = _proxmox(statuses=statuses, listings=[rows])
    with pytest.raises(SnapshotError) as caught:
        await async_create_snapshot(
            proxmox,
            "pve1",
            200,
            CURRENT,
            executor=_executor,
            sleep=_no_sleep,
            max_polls=1,
        )
    assert caught.value.may_exist is True


def test_current_pseudo_entry_is_never_a_complete_owned_snapshot() -> None:
    """PVE's current pseudo-row cannot satisfy exact Hubinet confirmation."""
    assert snapshot_is_complete([{"name": "current"}], CURRENT) is False


async def test_delete_targets_only_exact_current_name_and_confirms_absence() -> None:
    """Cleanup addresses this operation's exact name and preserves older snapshots."""
    proxmox, _node, lxc = _proxmox(
        statuses=[{"status": "stopped", "exitstatus": "OK"}],
        listings=[[{"name": OLD}, {"name": "manual-safe"}, {"name": "current"}]],
    )
    await async_delete_snapshot(
        proxmox,
        "pve1",
        200,
        CURRENT,
        executor=_executor,
        sleep=_no_sleep,
    )
    lxc.snapshot.assert_called_once_with(CURRENT)
    lxc.snapshot.return_value.delete.assert_called_once_with()
    assert not any(call.args == (OLD,) for call in lxc.snapshot.call_args_list)


@pytest.mark.parametrize(
    ("statuses", "rows"),
    [
        ([{"status": "stopped", "exitstatus": "ERROR"}], []),
        ([{"status": "stopped", "exitstatus": "OK"}], [{"name": CURRENT}]),
    ],
)
async def test_delete_failure_or_persistent_exact_name_means_retained(
    statuses: list[object], rows: list[object]
) -> None:
    """A failed task or failed exact-absence proof cannot claim cleanup."""
    proxmox, _node, _lxc = _proxmox(statuses=statuses, listings=[rows])
    with pytest.raises(SnapshotError) as caught:
        await async_delete_snapshot(
            proxmox,
            "pve1",
            200,
            CURRENT,
            executor=_executor,
            sleep=_no_sleep,
        )
    assert caught.value.may_exist is True


async def test_delete_refuses_non_hubinet_snapshot_without_api_call() -> None:
    """Even an exact supplied name cannot escape the narrow cleanup namespace."""
    proxmox, _node, lxc = _proxmox(statuses=[], listings=[])
    with pytest.raises(SnapshotError):
        await async_delete_snapshot(
            proxmox,
            "pve1",
            200,
            "manual-safe",
            executor=_executor,
            sleep=_no_sleep,
        )
    lxc.snapshot.assert_not_called()
