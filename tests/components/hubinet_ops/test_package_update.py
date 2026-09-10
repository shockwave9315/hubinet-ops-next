"""Home Assistant UX tests for reviewed package updates."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops.packages.models import (
    PackageMutationResult,
    PackageScanRecord,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
    PendingPackage,
)
from custom_components.hubinet_ops.packages.parser import (
    ParsedAptSimulation,
    ParsedAutoremoveSimulation,
)
from custom_components.hubinet_ops.packages.presentation import (
    notify_retained_snapshots,
    notify_review_plan,
    notify_update_complete,
)
from custom_components.hubinet_ops.packages.snapshots import (
    RetainedSnapshotSummary,
    SnapshotError,
    retained_snapshot_summary,
)
from homeassistant.components import persistent_notification as pn
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from . import setup_integration
from .test_packages import RESULT, GateTransport

SCAN = "button.ct_nginx_scan_pending_packages"
REVIEW = "button.ct_nginx_review_package_update"
APPROVE = "button.ct_nginx_approve_reviewed_plan"
UPDATE = "button.ct_nginx_update_packages"
SCAN_SENSOR = "sensor.ct_nginx_pending_package_updates"
UPDATE_SENSOR = "sensor.ct_nginx_package_update"


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    """Press one native Home Assistant button."""
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def _setup_scanned(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> GateTransport:
    """Set up package entities and complete one successful package scan."""
    transport = GateTransport()

    async def fake_scan(_self, node, vmid):
        return await transport.async_scan(node, vmid)

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=fake_scan,
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN)
        await transport.entered(200).wait()
        transport.release(200)
        await transport.completed(200).wait()
        await hass.async_block_till_done()
    return transport


async def test_review_approve_update_button_availability_and_exact_view_token(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The normal UX moves only through Scan → Review → Approve → Update."""
    await _setup_scanned(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    token = manager.record("pve1", 200).token

    assert hass.states.get(SCAN).state != STATE_UNAVAILABLE
    assert hass.states.get(REVIEW).state != STATE_UNAVAILABLE
    assert hass.states.get(APPROVE).state == STATE_UNAVAILABLE
    assert hass.states.get(UPDATE).state == STATE_UNAVAILABLE

    await _press(hass, REVIEW)
    assert manager.viewed_token("pve1", 200) == token
    assert hass.states.get(APPROVE).state != STATE_UNAVAILABLE
    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_package_review_pve1_200"
    ]
    assert "| openssl | amd64 | 1.0 | 1.1 | Debian-Security | yes |" in (
        notification["message"]
    )
    assert "Approve reviewed plan" in notification["message"]

    with patch.object(manager, "confirm_review", wraps=manager.confirm_review) as confirm:
        await _press(hass, APPROVE)
    confirm.assert_called_once_with("pve1", 200, token)
    assert manager.viewed_token("pve1", 200) is None
    assert manager.record("pve1", 200).reviewed is True
    assert hass.states.get(APPROVE).state == STATE_UNAVAILABLE
    assert hass.states.get(UPDATE).state != STATE_UNAVAILABLE


async def test_update_button_requires_native_snapshot_permission(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Only the mutating control requires native PVE VM.Snapshot permission."""
    permissions = deepcopy(
        mock_proxmox_client.access.permissions.get.return_value
    )
    for grants in permissions.values():
        grants.pop("VM.Snapshot", None)
    mock_proxmox_client.access.permissions.get.return_value = permissions

    await setup_integration(hass, mock_config_entry)

    assert hass.states.get(SCAN) is not None
    assert hass.states.get(REVIEW) is not None
    assert hass.states.get(APPROVE) is not None
    assert hass.states.get(UPDATE) is None
    assert hass.states.get("button.ct_nginx_autoremove_unused_packages") is None


async def test_new_scan_invalidates_viewed_token_and_stale_approval(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Approval never means approve whatever scan happens to be current now."""
    transport = await _setup_scanned(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    await _press(hass, REVIEW)
    old_token = manager.viewed_token("pve1", 200)
    assert old_token is not None
    review_notification_id = "hubinet_ops_package_review_pve1_200"
    assert review_notification_id in pn._async_get_or_create_notifications(hass)  # noqa: SLF001

    transport.rearm(200)

    async def fake_scan(_self, node, vmid):
        return await transport.async_scan(node, vmid)

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_scan",
        new=fake_scan,
    ):
        await _press(hass, SCAN)
        assert manager.viewed_token("pve1", 200) is None
        assert manager.confirm_viewed_review("pve1", 200) is False
        assert review_notification_id not in pn._async_get_or_create_notifications(  # noqa: SLF001
            hass
        )
        transport.release(200)
        await transport.completed(200).wait()
        await hass.async_block_till_done()
    assert manager.record("pve1", 200).token != old_token
    assert manager.confirm_review("pve1", 200, old_token) is False


async def test_update_press_invalidates_scan_immediately_then_notifies_plan_changed(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Accepted Update hides stale count before its background plan gate runs."""
    await _setup_scanned(hass, mock_config_entry)
    await _press(hass, REVIEW)
    await _press(hass, APPROVE)
    review_notification_id = "hubinet_ops_package_review_pve1_200"
    assert review_notification_id in pn._async_get_or_create_notifications(hass)  # noqa: SLF001

    entered = asyncio.Event()
    release = asyncio.Event()

    async def changed_plan(_self, _node, _vmid):
        entered.set()
        await release.wait()
        return ParsedAptSimulation(
            RESULT.packages
            + (PendingPackage("new", "amd64", "1.0", "1.1", None, None),),
            0,
        )

    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_plan",
        new=changed_plan,
    ):
        await _press(hass, UPDATE)
        await entered.wait()
        assert review_notification_id not in pn._async_get_or_create_notifications(  # noqa: SLF001
            hass
        )
        assert hass.states.get(SCAN_SENSOR).state == STATE_UNKNOWN
        assert hass.states.get(UPDATE_SENSOR).state == "running"
        for entity_id in (SCAN, REVIEW, APPROVE, UPDATE):
            assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
        release.set()
        await hass.async_block_till_done()

    assert hass.states.get(UPDATE_SENSOR).state == "failed"
    assert hass.states.get(UPDATE_SENSOR).attributes["outcome"] == "plan_changed"
    assert hass.states.get(SCAN).state != STATE_UNAVAILABLE
    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_package_update_pve1_200"
    ]
    assert "Nothing was changed and no snapshot was created" in notification["message"]
    assert "Scan, Review, and Approve again" in notification["message"]


async def test_real_update_survives_stopped_observation_after_plan_gate(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A stopped observation invalidates evidence without cancelling Update."""
    await _setup_scanned(hass, mock_config_entry)
    await _press(hass, REVIEW)
    await _press(hass, APPROVE)
    manager = mock_config_entry.runtime_data.package_manager
    assert manager.cleanup_evidence("pve1", 200) is not None

    plan_entered = asyncio.Event()
    plan_release = asyncio.Event()

    async def gated_plan(_self, _node, _vmid):
        plan_entered.set()
        await plan_release.wait()
        return ParsedAptSimulation(RESULT.packages, RESULT.not_upgraded_count)

    mutation = PackageMutationResult(
        before={
            ("openssl", "amd64"): "1.0",
            ("example", "amd64"): "1.0",
        },
        after={
            ("openssl", "amd64"): "1.1",
            ("example", "amd64"): "1.1",
        },
    )
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan",
            new=gated_plan,
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_update",
            new=AsyncMock(return_value=mutation),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_ping",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            new=AsyncMock(return_value=ParsedAutoremoveSimulation((), 0)),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_list_snapshots",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_delete_snapshot",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.generate_snapshot_name",
            return_value="hubinet-preupd-20260910120000-ab12cd",
        ),
    ):
        await _press(hass, UPDATE)
        await plan_entered.wait()
        update_task = manager._update_tasks[("pve1", 200)]  # noqa: SLF001
        assert manager.update_record("pve1", 200).status is PackageUpdateStatus.RUNNING

        stopped = deepcopy(mock_proxmox_client._node_mock.lxc.get.return_value)  # noqa: SLF001
        for container in stopped:
            if container["vmid"] == "200":
                container["status"] = "stopped"
        mock_proxmox_client._node_mock.lxc.get.return_value = stopped  # noqa: SLF001
        await mock_config_entry.runtime_data.async_refresh()
        await asyncio.sleep(0)

        assert manager.record("pve1", 200) == PackageScanRecord()
        assert manager.cleanup_evidence("pve1", 200) is None
        assert manager.viewed_token("pve1", 200) is None
        assert manager.update_record("pve1", 200).status is PackageUpdateStatus.RUNNING
        assert manager._update_tasks[("pve1", 200)] is update_task  # noqa: SLF001
        assert not update_task.done()
        assert not update_task.cancelled()

        plan_release.set()
        await update_task
        await hass.async_block_till_done()

    record = manager.update_record("pve1", 200)
    assert record.status is PackageUpdateStatus.SUCCESS
    assert record.outcome is PackageUpdateOutcome.SUCCESS
    assert record.changed_package_count == 2
    assert record.liveness is True


async def test_update_sensor_terminal_failure_remains_readable_when_guest_stops(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """A stopped guest does not hide the latest retained-safety outcome sensor."""
    await _setup_scanned(hass, mock_config_entry)
    await _press(hass, REVIEW)
    await _press(hass, APPROVE)
    snapshot_name = "hubinet-preupd-20260908120000-ab12cd"
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan",
            new=AsyncMock(return_value=ParsedAptSimulation(RESULT.packages, 0)),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_update",
            new=AsyncMock(
                side_effect=PackageUpdateError(
                    PackageUpdateOutcome.MUTATION_FAILED,
                    "foreign helper details must not be rendered",
                )
            ),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_list_snapshots",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.generate_snapshot_name",
            return_value=snapshot_name,
        ),
    ):
        await _press(hass, UPDATE)
        await hass.async_block_till_done()
    assert hass.states.get(UPDATE_SENSOR).state == "failed"

    stopped = deepcopy(mock_proxmox_client._node_mock.lxc.get.return_value)  # noqa: SLF001
    for container in stopped:
        if container["vmid"] == "200":
            container["status"] = "stopped"
    mock_proxmox_client._node_mock.lxc.get.return_value = stopped  # noqa: SLF001
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(SCAN_SENSOR).state == STATE_UNAVAILABLE
    state = hass.states.get(UPDATE_SENSOR)
    assert state.state == "failed"
    assert state.attributes["outcome"] == "mutation_failed"
    assert state.attributes["retained_snapshot"] is True
    assert state.attributes["retained_snapshot_name"] == snapshot_name
    assert "packages" not in state.attributes


async def test_update_sensor_exposes_uncertain_snapshot_without_retained_name(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The sensor exposes possible snapshot existence without claiming retention."""
    await _setup_scanned(hass, mock_config_entry)
    await _press(hass, REVIEW)
    await _press(hass, APPROVE)
    snapshot_name = "hubinet-preupd-20260908120000-ab12cd"
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan",
            new=AsyncMock(return_value=ParsedAptSimulation(RESULT.packages, 0)),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_list_snapshots",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
            new=AsyncMock(side_effect=SnapshotError("create uncertain", True)),
        ),
        patch(
            "custom_components.hubinet_ops.packages.manager.generate_snapshot_name",
            return_value=snapshot_name,
        ),
    ):
        await _press(hass, UPDATE)
        await hass.async_block_till_done()

    attributes = hass.states.get(UPDATE_SENSOR).attributes
    assert attributes["retained_snapshot"] is False
    assert attributes["snapshot_uncertain"] is True
    assert attributes["uncertain_snapshot_name"] == snapshot_name
    assert "retained_snapshot_name" not in attributes


async def test_notification_rendering_escapes_foreign_markdown_and_control_text(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Package and snapshot strings cannot inject table columns or HTML controls."""
    await setup_integration(hass, mock_config_entry)
    malicious = replace(
        RESULT,
        packages=(
            PendingPackage(
                "evil|name\n<script>",
                "amd64",
                "1<2",
                "2&3",
                "origin|x\rnext",
                None,
            ),
        ),
    )
    notify_review_plan(
        hass,
        "pve1",
        200,
        PackageScanRecord(result=malicious),
    )
    message = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_package_review_pve1_200"
    ]["message"]
    assert "evil&#124;name &lt;script&gt;" in message
    assert "origin&#124;x next" in message
    assert "<script>" not in message

    notify_retained_snapshots(
        hass,
        "pve1",
        200,
        RetainedSnapshotSummary(1, ("safe|name\nnext",)),
    )
    warning = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_retained_snapshots_pve1_200"
    ]["message"]
    assert "safe&#124;name next" in warning


async def test_retained_snapshot_warning_reports_total_and_bounded_subset(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The real adapter summary never presents a truncated count as the total."""
    await setup_integration(hass, mock_config_entry)
    rows = [
        {"name": f"hubinet-preupd-2026090{day}120000-ab12c{day}"}
        for day in range(1, 8)
    ]
    summary = retained_snapshot_summary(rows)
    notify_retained_snapshots(hass, "pve1", 200, summary)
    warning = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_retained_snapshots_pve1_200"
    ]["message"]
    assert summary.total_count == 7
    assert len(summary.names) == 5
    assert "Found 7 retained" in warning
    assert "Showing 5" in warning
    assert "2 more are not shown" in warning


async def test_terminal_notifications_distinguish_success_cleanup_and_helper(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Result prose remains bounded and truthful for the key operator outcomes."""
    await setup_integration(hass, mock_config_entry)
    records = (
        PackageUpdateRecord(
            status=PackageUpdateStatus.SUCCESS,
            outcome=PackageUpdateOutcome.SUCCESS,
            changed_package_count=3,
            liveness=True,
        ),
        PackageUpdateRecord(
            status=PackageUpdateStatus.SUCCESS,
            outcome=PackageUpdateOutcome.SUCCESS,
            changed_package_count=3,
            liveness=True,
            snapshot_retained=True,
            snapshot_cleanup_failed=True,
            snapshot_name="hubinet-preupd-20260908120000-ab12cd",
        ),
        PackageUpdateRecord(
            status=PackageUpdateStatus.FAILED,
            outcome=PackageUpdateOutcome.HELPER_OUTDATED,
        ),
    )
    expected = (
        "temporary snapshot was removed",
        "snapshot cleanup failed",
        "Re-run the current Hubinet-Ops bootstrap",
    )
    for record, phrase in zip(records, expected, strict=True):
        notify_update_complete(hass, "pve1", 200, record)
        message = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
            "hubinet_ops_package_update_pve1_200"
        ]["message"]
        assert phrase in message


async def test_uncertain_snapshot_notification_never_claims_confirmed_retention(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """An unconfirmed create is described only as a snapshot that may exist."""
    await setup_integration(hass, mock_config_entry)
    snapshot_name = "hubinet-preupd-20260908120000-ab12cd"
    notify_update_complete(
        hass,
        "pve1",
        200,
        PackageUpdateRecord(
            status=PackageUpdateStatus.FAILED,
            outcome=PackageUpdateOutcome.SNAPSHOT_FAILED,
            snapshot_uncertain=True,
            snapshot_name=snapshot_name,
        ),
    )
    message = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_package_update_pve1_200"
    ]["message"]
    assert f"snapshot named {snapshot_name} may exist" in message
    assert "snapshot was retained" not in message
