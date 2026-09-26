"""Native snapshot Delete endpoint and bounded outcome tests."""

from unittest.mock import AsyncMock, MagicMock, patch

from proxmoxer.core import ProxmoxResource, ResourceException
import pytest

from custom_components.hubinet_ops.snapshots import (
    RestoreOutcome,
    RestoreResult,
    SnapshotKind,
    async_delete_snapshot,
    async_observe_task,
)

UPID = "UPID:pve1:00000001:00000002:00000003:qmdelsnapshot:100:user@pam:"


async def _executor(call):
    """Execute a blocking-shaped call synchronously in adapter tests."""
    return call()


@pytest.mark.parametrize("kind", list(SnapshotKind))
async def test_delete_native_resource_path(kind: SnapshotKind) -> None:
    """QEMU and LXC route the exact node, VMID, and selected snapshot name."""
    proxmox = ProxmoxResource(base_url="https://pve1:8006/api2/json")
    captured = []

    def request(resource, method, **kwargs):
        captured.append((resource._store["base_url"], method, kwargs))  # noqa: SLF001
        return UPID

    with (
        patch.object(ProxmoxResource, "_request", autospec=True, side_effect=request),
        patch(
            "custom_components.hubinet_ops.snapshots.async_observe_task",
            AsyncMock(return_value=RestoreResult(RestoreOutcome.SUCCESS, UPID)),
        ) as observe,
    ):
        result = await async_delete_snapshot(
            proxmox, "pve1", 100, kind, "before_update", executor=_executor
        )
    assert captured == [
        (
            f"https://pve1:8006/api2/json/nodes/pve1/{kind.value}/100/snapshot/before_update",
            "DELETE",
            {"params": {}},
        )
    ]

    assert result.outcome is RestoreOutcome.SUCCESS
    observe.assert_awaited_once_with(proxmox, "pve1", UPID, executor=_executor)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ({"status": "stopped", "exitstatus": "OK"}, RestoreOutcome.SUCCESS),
        ({"status": "stopped", "exitstatus": "WARNINGS: 2"}, RestoreOutcome.SUCCESS),
        ({"status": "stopped", "exitstatus": "disk locked"}, RestoreOutcome.FAILED),
        (None, RestoreOutcome.UNCERTAIN),
        ({"status": "invalid"}, RestoreOutcome.UNCERTAIN),
    ],
)
async def test_delete_task_result(status: object, outcome: RestoreOutcome) -> None:
    """Deletion follows the existing native task observation semantics."""
    proxmox = MagicMock()
    proxmox.nodes.return_value.lxc.return_value.snapshot.return_value.delete.return_value = UPID
    with patch(
        "custom_components.hubinet_ops.snapshots.Tasks.blocking_status",
        return_value=status,
    ) as observe:
        result = await async_delete_snapshot(
            proxmox, "pve1", 100, SnapshotKind.LXC, "manual", executor=_executor
        )
    assert result.outcome is outcome
    assert result.upid == UPID
    observe.assert_called_once_with(proxmox, UPID, timeout=pytest.approx(600, abs=1))


@pytest.mark.parametrize("name", ["current", "vzdump", "../snap", "a", "A" * 41, None])
async def test_delete_invalid_name_not_started(name: object) -> None:
    """Invalid names cannot reach PVE submission."""
    proxmox = MagicMock()
    result = await async_delete_snapshot(
        proxmox, "pve1", 100, SnapshotKind.QEMU, name, executor=_executor
    )
    assert result.outcome is RestoreOutcome.NOT_STARTED
    proxmox.nodes.assert_not_called()


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ResourceException(400, "bad request", "rejected"), RestoreOutcome.NOT_STARTED),
        (ResourceException(403, "forbidden", "rejected"), RestoreOutcome.NOT_STARTED),
        (ResourceException(404, "not found", "gone"), RestoreOutcome.NOT_STARTED),
        (ResourceException(409, "conflict", "locked"), RestoreOutcome.NOT_STARTED),
        (ResourceException(408, "timeout", "unknown"), RestoreOutcome.UNCERTAIN),
        (ResourceException(500, "server error", "unknown"), RestoreOutcome.UNCERTAIN),
        (ConnectionError("lost connection"), RestoreOutcome.UNCERTAIN),
    ],
)
async def test_delete_submission_failure(
    error: Exception, outcome: RestoreOutcome
) -> None:
    """A definite rejection differs from potentially accepted submission."""
    proxmox = MagicMock()
    delete = proxmox.nodes.return_value.qemu.return_value.snapshot.return_value.delete
    delete.side_effect = error
    with patch("custom_components.hubinet_ops.snapshots.async_observe_task") as observe:
        result = await async_delete_snapshot(
            proxmox, "pve1", 100, SnapshotKind.QEMU, "manual", executor=_executor
        )
    assert result.outcome is outcome
    delete.assert_called_once_with()
    observe.assert_not_called()


@pytest.mark.parametrize("upid", ["invalid", UPID.replace("pve1", "pve2"), None])
async def test_delete_malformed_upid_uncertain(upid: object) -> None:
    """An accepted response without observable native identity stays uncertain."""
    proxmox = MagicMock()
    proxmox.nodes.return_value.qemu.return_value.snapshot.return_value.delete.return_value = upid
    with patch(
        "custom_components.hubinet_ops.snapshots.Tasks.blocking_status"
    ) as observe:
        result = await async_delete_snapshot(
            proxmox, "pve1", 100, SnapshotKind.QEMU, "manual", executor=_executor
        )
    assert result.outcome is RestoreOutcome.UNCERTAIN
    observe.assert_not_called()


async def test_delete_observation_failure_uncertain() -> None:
    """Exhausted visibility never fabricates deletion success."""
    proxmox = MagicMock()
    proxmox.nodes.return_value.qemu.return_value.snapshot.return_value.delete.return_value = UPID

    async def observe(*args, **kwargs):
        return await async_observe_task(*args, **kwargs, sleep=lambda _seconds: None)

    with (
        patch(
            "custom_components.hubinet_ops.snapshots.Tasks.blocking_status",
            side_effect=ConnectionError("status unavailable"),
        ) as status,
        patch(
            "custom_components.hubinet_ops.snapshots.async_observe_task",
            side_effect=observe,
        ),
    ):
        result = await async_delete_snapshot(
            proxmox, "pve1", 100, SnapshotKind.QEMU, "manual", executor=_executor
        )
    assert result.outcome is RestoreOutcome.UNCERTAIN
    assert result.upid == UPID
    assert status.call_count == 3


async def test_delete_submission_reason_is_bounded() -> None:
    """Unbounded PVE output is reduced to the existing bounded detail surface."""
    proxmox = MagicMock()
    proxmox.nodes.return_value.qemu.return_value.snapshot.return_value.delete.side_effect = ResourceException(
        500, "error", "X" * 10000
    )
    result = await async_delete_snapshot(
        proxmox, "pve1", 100, SnapshotKind.QEMU, "manual", executor=_executor
    )
    assert result.outcome is RestoreOutcome.UNCERTAIN
    assert len(result.reason) == 500
