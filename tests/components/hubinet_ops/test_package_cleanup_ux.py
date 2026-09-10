"""Home Assistant UX tests for helper repair and explicit package cleanup."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

from tests.common import MockConfigEntry  # noqa: TID251

from custom_components.hubinet_ops import async_unload_entry
from custom_components.hubinet_ops.const import (
    BOOTSTRAP_COMMAND,
    CONF_PACKAGE_NODE,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PRIVATE_KEY,
    DOMAIN,
    EXPECTED_HELPER_VERSION,
)
from custom_components.hubinet_ops.packages.models import (
    PackageMutationResult,
    PackageUpdateError,
    PackageUpdateOutcome,
    PackageUpdateStatus,
    RemovablePackage,
)
from custom_components.hubinet_ops.packages.parser import (
    ParsedAptSimulation,
    ParsedAutoremoveSimulation,
)
from custom_components.hubinet_ops.packages.presentation import (
    dismiss_cleanup_candidates,
    escape_markdown_cell,
    notify_cleanup_observation,
    update_helper_issue,
)
from custom_components.hubinet_ops.packages.transport import PackageHelperProbe
from homeassistant.components import persistent_notification as pn
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import setup_integration
from .test_packages import RESULT

SCAN = "button.ct_nginx_scan_pending_packages"
REVIEW = "button.ct_nginx_review_package_update"
APPROVE = "button.ct_nginx_approve_reviewed_plan"
UPDATE = "button.ct_nginx_update_packages"
AUTOREMOVE = "button.ct_nginx_autoremove_unused_packages"
UNUSED = "sensor.ct_nginx_unused_packages"

CANDIDATES = tuple(
    RemovablePackage(name, "amd64", "1.0")
    for name in (
        "libatomic1",
        "libglib2.0-0t64",
        "libglib2.0-data",
        "libslirp0",
        "shared-mime-info",
        "slirp4netns",
        "xdg-user-dirs",
    )
)


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_scan_presents_exact_cleanup_plan_before_enabling_autoremove(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The exact seven rows are visible before evidence becomes actionable."""
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            AsyncMock(return_value=RESULT),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            AsyncMock(return_value=ParsedAutoremoveSimulation(CANDIDATES, 0)),
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        assert hass.states.get(UNUSED).state == STATE_UNKNOWN
        assert hass.states.get(AUTOREMOVE).state == STATE_UNAVAILABLE
        await _press(hass, SCAN)
        await hass.async_block_till_done()

    assert hass.states.get(UNUSED).state == "7"
    assert hass.states.get(AUTOREMOVE).state != STATE_UNAVAILABLE
    state = hass.states.get(UNUSED)
    assert "candidates" not in state.attributes
    notification = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_package_cleanup_candidates_pve1_200"
    ]["message"]
    for candidate in CANDIDATES:
        assert (
            f"| {candidate.name} | {candidate.architecture} | "
            f"{candidate.installed_version} |"
        ) in notification
    assert "Autoremove unused packages" in notification


async def test_autoremove_button_calls_explicit_manager_gate(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """The native button is the sole explicit approval for the displayed plan."""
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            AsyncMock(return_value=RESULT),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            AsyncMock(return_value=ParsedAutoremoveSimulation(CANDIDATES, 0)),
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN)
        await hass.async_block_till_done()
    manager = mock_config_entry.runtime_data.package_manager
    with patch.object(manager, "async_start_autoremove") as start:
        await _press(hass, AUTOREMOVE)
    start.assert_called_once_with(
        "pve1", 200, target_is_running=True, snapshot_permission=True
    )


async def test_zero_cleanup_dismisses_old_plan_and_disables_button(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Known zero is distinct from unknown but never enables mutation."""
    plans = [
        ParsedAutoremoveSimulation(CANDIDATES, 0),
        ParsedAutoremoveSimulation((), 0),
    ]
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            AsyncMock(return_value=RESULT),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            AsyncMock(side_effect=plans),
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN)
        await hass.async_block_till_done()
        assert "hubinet_ops_package_cleanup_candidates_pve1_200" in (
            pn._async_get_or_create_notifications(hass)  # noqa: SLF001
        )
        await _press(hass, SCAN)
        await hass.async_block_till_done()
    assert hass.states.get(UNUSED).state == "0"
    assert hass.states.get(AUTOREMOVE).state == STATE_UNAVAILABLE
    assert "hubinet_ops_package_cleanup_candidates_pve1_200" not in (
        pn._async_get_or_create_notifications(hass)  # noqa: SLF001
    )


async def test_cleanup_entities_follow_snapshot_and_stopped_guest_policy(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Autoremove requires snapshot permission and cleanup state hides when stopped."""
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            AsyncMock(return_value=RESULT),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            AsyncMock(return_value=ParsedAutoremoveSimulation(CANDIDATES, 0)),
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN)
        await hass.async_block_till_done()
    stopped = deepcopy(mock_proxmox_client._node_mock.lxc.get.return_value)  # noqa: SLF001
    for container in stopped:
        if container["vmid"] == "200":
            container["status"] = "stopped"
    mock_proxmox_client._node_mock.lxc.get.return_value = stopped  # noqa: SLF001
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(UNUSED).state == STATE_UNAVAILABLE
    assert hass.states.get(AUTOREMOVE).state == STATE_UNAVAILABLE
    # Accepted PR #11 architecture removes cleanup evidence on non-running
    # observation instead of allowing it to reappear after restart.
    assert (
        mock_config_entry.runtime_data.package_manager.cleanup_evidence(
            "pve1", 200
        )
        is None
    )


async def test_cleanup_notification_escapes_exact_rows_and_dismisses(
    hass: HomeAssistant,
) -> None:
    """Foreign package fields cannot inject Markdown or stale actionability."""
    candidates = (RemovablePackage("evil|name\n<script>", "amd64", "1<2"),)
    assert escape_markdown_cell(candidates[0].name) == (
        "evil&#124;name &lt;script&gt;"
    )
    # Translation-backed notification rendering is covered after integration
    # setup above; this direct call additionally locks down stable dismissal.
    notify_cleanup_observation(hass, "pve1", 200, candidates, "scan")
    dismiss_cleanup_candidates(hass, "pve1", 200)
    assert "hubinet_ops_package_cleanup_candidates_pve1_200" not in (
        pn._async_get_or_create_notifications(hass)  # noqa: SLF001
    )


async def test_v3_probe_creates_actionable_repairs_issue_and_v4_clears_it(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Observed helper age drives one repair issue without disabling native PVE."""
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_probe",
        AsyncMock(return_value=PackageHelperProbe(node="pve1", helper_version=3)),
    ):
        await setup_integration(hass, mock_config_entry)
    issue_id = f"helper_outdated_{mock_config_entry.entry_id}"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {
        "installed": "3",
        "required": str(EXPECTED_HELPER_VERSION),
        "bootstrap_command": BOOTSTRAP_COMMAND,
    }
    assert "--reset" not in BOOTSTRAP_COMMAND
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.pve1_status") is not None

    update_helper_issue(hass, mock_config_entry.entry_id, EXPECTED_HELPER_VERSION)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_successful_unload_clears_stale_helper_issue(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The real successful config-entry unload removes its Repairs issue."""
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_probe",
        AsyncMock(return_value=PackageHelperProbe(node="pve1", helper_version=3)),
    ):
        await setup_integration(hass, mock_config_entry)
    issue_id = f"helper_outdated_{mock_config_entry.entry_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_reload_without_package_transport_clears_stale_helper_issue(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Reload clears old helper evidence when package transport disappears."""
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_probe",
        AsyncMock(return_value=PackageHelperProbe(node="pve1", helper_version=3)),
    ):
        await setup_integration(hass, mock_config_entry)
    issue_id = f"helper_outdated_{mock_config_entry.entry_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    data = dict(mock_config_entry.data)
    for key in (CONF_PACKAGE_NODE, CONF_SSH_PRIVATE_KEY, CONF_SSH_HOST_KEY):
        data.pop(key)
    hass.config_entries.async_update_entry(mock_config_entry, data=data)
    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    manager = mock_config_entry.runtime_data.package_manager
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert manager.configured is False
    assert manager.helper_version is None
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_failed_unload_keeps_stale_helper_issue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A failed platform unload cannot falsely clear an active Repair."""
    update_helper_issue(hass, mock_config_entry.entry_id, 3)
    issue_id = f"helper_outdated_{mock_config_entry.entry_id}"
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=False),
    ):
        assert await async_unload_entry(hass, mock_config_entry) is False
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None


async def test_unknown_probe_does_not_claim_outdated_helper(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Probe failure leaves version unknown and creates no installed-version claim."""
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_probe",
        AsyncMock(side_effect=RuntimeError("offline")),
    ):
        await setup_integration(hass, mock_config_entry)
    manager = mock_config_entry.runtime_data.package_manager
    assert manager.helper_version is None
    assert issue_registry.async_get_issue(
        DOMAIN, f"helper_outdated_{mock_config_entry.entry_id}"
    ) is None


async def test_v3_cleanup_is_unknown_and_later_v4_response_clears_issue(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Old supported Scan works while only the unsupported cleanup flow degrades."""
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_probe",
            AsyncMock(return_value=PackageHelperProbe(node="pve1", helper_version=3)),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            AsyncMock(return_value=RESULT),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            AsyncMock(
                side_effect=PackageUpdateError(
                    PackageUpdateOutcome.HELPER_OUTDATED, "outdated"
                )
            ),
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN)
        await hass.async_block_till_done()
    manager = mock_config_entry.runtime_data.package_manager
    issue_id = f"helper_outdated_{mock_config_entry.entry_id}"
    assert manager.record("pve1", 200).status == "success"
    assert hass.states.get("sensor.pve1_status") is not None
    assert hass.states.get(UNUSED).state == STATE_UNKNOWN
    assert hass.states.get(AUTOREMOVE).state == STATE_UNAVAILABLE
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    transport = manager._transport  # noqa: SLF001
    transport._observed_helper_version = 4  # noqa: SLF001
    with (
        patch.object(transport, "async_scan", AsyncMock(return_value=RESULT)),
        patch.object(
            transport,
            "async_plan_autoremove",
            AsyncMock(return_value=ParsedAutoremoveSimulation((), 0)),
        ),
    ):
        await _press(hass, SCAN)
        await hass.async_block_till_done()
    assert manager.helper_version == 4
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_v3_supported_update_remains_usable_while_cleanup_is_unavailable(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Expected helper v4 never gates the unchanged helper-v3 Update contract."""
    outdated = PackageUpdateError(PackageUpdateOutcome.HELPER_OUTDATED, "outdated")
    with (
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_probe",
            AsyncMock(return_value=PackageHelperProbe(node="pve1", helper_version=3)),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_scan",
            AsyncMock(return_value=RESULT),
        ),
        patch(
            "custom_components.hubinet_ops.packages.transport."
            "AsyncSSHPackageTransport.async_plan_autoremove",
            AsyncMock(side_effect=outdated),
        ),
    ):
        await setup_integration(hass, mock_config_entry)
        await _press(hass, SCAN)
        await hass.async_block_till_done()
        await _press(hass, REVIEW)
        await _press(hass, APPROVE)

        with (
            patch(
                "custom_components.hubinet_ops.packages.transport."
                "AsyncSSHPackageTransport.async_plan",
                AsyncMock(return_value=ParsedAptSimulation(RESULT.packages, 0)),
            ),
            patch(
                "custom_components.hubinet_ops.packages.transport."
                "AsyncSSHPackageTransport.async_update",
                AsyncMock(
                    return_value=PackageMutationResult(
                        before={
                            ("openssl", "amd64"): "1.0",
                            ("example", "amd64"): "1.0",
                        },
                        after={
                            ("openssl", "amd64"): "1.1",
                            ("example", "amd64"): "1.1",
                        },
                    )
                ),
            ),
            patch(
                "custom_components.hubinet_ops.packages.transport."
                "AsyncSSHPackageTransport.async_ping",
                AsyncMock(return_value=True),
            ),
            patch(
                "custom_components.hubinet_ops.packages.manager.async_list_snapshots",
                AsyncMock(return_value=[]),
            ),
            patch(
                "custom_components.hubinet_ops.packages.manager.async_create_snapshot",
                AsyncMock(),
            ),
            patch(
                "custom_components.hubinet_ops.packages.manager.async_delete_snapshot",
                AsyncMock(),
            ),
            patch(
                "custom_components.hubinet_ops.packages.manager.generate_snapshot_name",
                return_value="hubinet-preupd-20260910120000-ab12cd",
            ),
        ):
            await _press(hass, UPDATE)
            await hass.async_block_till_done()

    manager = mock_config_entry.runtime_data.package_manager
    issue_id = f"helper_outdated_{mock_config_entry.entry_id}"
    assert manager.helper_version == 3
    assert manager.update_record("pve1", 200).status is PackageUpdateStatus.SUCCESS
    assert manager.cleanup_evidence("pve1", 200) is None
    assert hass.states.get(UNUSED).state == STATE_UNKNOWN
    assert hass.states.get(AUTOREMOVE).state == STATE_UNAVAILABLE
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None


async def test_blocked_probe_cannot_delay_native_coordinator_setup(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    package_transport_material: None,
) -> None:
    """Native PVE initialization and first refresh finish while SSH probe is blocked."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_probe(_self) -> PackageHelperProbe:
        entered.set()
        await release.wait()
        return PackageHelperProbe(node="pve1", helper_version=4)

    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.hubinet_ops.packages.transport."
        "AsyncSSHPackageTransport.async_probe",
        new=blocked_probe,
    ):
        assert await asyncio.wait_for(
            hass.config_entries.async_setup(mock_config_entry.entry_id), timeout=1
        )
        await entered.wait()
        assert mock_config_entry.state is ConfigEntryState.LOADED
        assert mock_config_entry.runtime_data.data["pve1"].node["status"] == "online"
        release.set()
        await hass.async_block_till_done()
