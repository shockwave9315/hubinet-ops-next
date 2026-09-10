"""Bounded Home Assistant notification presentation for package updates."""

from html import escape

from custom_components.hubinet_ops.const import (
    BOOTSTRAP_COMMAND,
    DOMAIN,
    EXPECTED_HELPER_VERSION,
)
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_cached_translations

from .models import (
    PackageScanRecord,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
    RemovablePackage,
)
from .snapshots import RetainedSnapshotSummary

_REASON_KEYS = {
    PackageUpdateOutcome.PLAN_FAILED: "package_update_reason_plan_failed",
    PackageUpdateOutcome.SNAPSHOT_FAILED: "package_update_reason_snapshot_failed",
    PackageUpdateOutcome.PACKAGE_MANAGER_BUSY: "package_update_reason_busy",
    PackageUpdateOutcome.MUTATION_FAILED: "package_update_reason_mutation_failed",
    PackageUpdateOutcome.MUTATION_TIMED_OUT: "package_update_reason_mutation_timed_out",
    PackageUpdateOutcome.MUTATION_UNCERTAIN: "package_update_reason_mutation_uncertain",
    PackageUpdateOutcome.GUEST_UNAVAILABLE: "package_update_reason_guest_unavailable",
    PackageUpdateOutcome.LIVENESS_FAILED: "package_update_reason_liveness_failed",
}


def escape_markdown_cell(value: object) -> str:
    """Escape one bounded foreign value for a Markdown table cell."""
    text = "".join(
        character if ord(character) >= 32 and character != "\x7f" else " "
        for character in str(value)
    )
    return escape(text, quote=True).replace("|", "&#124;")


def _translate(hass: HomeAssistant, key: str, **placeholders: str) -> str:
    """Return one cached current-language string with safe English fallback."""
    localize_key = f"component.{DOMAIN}.exceptions.{key}.message"
    languages = (hass.config.language, "en")
    for language in dict.fromkeys(languages):
        message = async_get_cached_translations(
            hass, language, "exceptions", DOMAIN
        ).get(localize_key)
        if message is None:
            continue
        message = message.rstrip(".")
        if not placeholders:
            return message
        try:
            return message.format(**placeholders)
        except (IndexError, KeyError, ValueError):
            continue
    return key


def _notification_id(kind: str, node: str, vmid: int) -> str:
    """Return a stable per-target notification ID."""
    return f"hubinet_ops_{kind}_{node}_{vmid}"


def _helper_issue_id(entry_id: str) -> str:
    """Return the stable stale-helper Repairs issue ID for one config entry."""
    return f"helper_outdated_{entry_id}"


def notify_review_plan(
    hass: HomeAssistant, node: str, vmid: int, record: PackageScanRecord
) -> None:
    """Show the full exact current plan and explicit approval instruction."""
    assert record.result is not None
    rows = [
        "| "
        + " | ".join(
            _translate(hass, key)
            for key in (
                "package_table_package",
                "package_table_architecture",
                "package_table_installed_version",
                "package_table_candidate_version",
                "package_table_origin",
                "package_table_security",
            )
        )
        + " |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for package in record.result.packages:
        security = (
            _translate(hass, "package_table_yes")
            if package.security is True
            else _translate(hass, "package_table_no")
            if package.security is False
            else _translate(hass, "package_table_unknown")
        )
        rows.append(
            "| "
            + " | ".join(
                escape_markdown_cell(value)
                for value in (
                    package.name,
                    package.architecture,
                    package.installed_version,
                    package.candidate_version,
                    package.origin
                    if package.origin is not None
                    else _translate(hass, "package_table_unknown"),
                    security,
                )
            )
            + " |"
        )
    plan = "\n".join(rows)
    persistent_notification.async_create(
        hass,
        _translate(
            hass,
            "package_review_notification",
            node=escape_markdown_cell(node),
            vmid=str(vmid),
            plan=plan,
        ),
        _translate(hass, "package_review_notification_title"),
        _notification_id("package_review", node, vmid),
    )


def notify_retained_snapshots(
    hass: HomeAssistant,
    node: str,
    vmid: int,
    summary: RetainedSnapshotSummary,
) -> None:
    """Warn about old prefix-matched snapshots without blocking the update."""
    rendered_names = ", ".join(
        escape_markdown_cell(name) for name in summary.names
    )
    persistent_notification.async_create(
        hass,
        _translate(
            hass,
            "package_retained_snapshot_warning",
            node=escape_markdown_cell(node),
            vmid=str(vmid),
            count=str(summary.total_count),
            shown=str(len(summary.names)),
            names=rendered_names,
            remaining=str(summary.total_count - len(summary.names)),
        ),
        _translate(hass, "package_retained_snapshot_warning_title"),
        _notification_id("retained_snapshots", node, vmid),
    )


def notify_update_complete(
    hass: HomeAssistant, node: str, vmid: int, record: PackageUpdateRecord
) -> None:
    """Report a terminal update outcome without rendering foreign helper prose."""
    target = {"node": escape_markdown_cell(node), "vmid": str(vmid)}
    if record.status is PackageUpdateStatus.SUCCESS:
        changed = str(record.changed_package_count or 0)
        if record.snapshot_cleanup_failed and record.snapshot_name is not None:
            key = "package_update_cleanup_failed_notification"
            placeholders = {
                **target,
                "changed": changed,
                "snapshot": escape_markdown_cell(record.snapshot_name),
            }
        else:
            key = "package_update_success_notification"
            placeholders = {**target, "changed": changed}
    elif record.outcome is PackageUpdateOutcome.PLAN_CHANGED:
        key = "package_update_plan_changed_notification"
        placeholders = target
    elif record.outcome is PackageUpdateOutcome.HELPER_OUTDATED:
        key = "package_update_helper_outdated_notification"
        placeholders = target
    else:
        key = "package_update_failed_notification"
        reason_key = _REASON_KEYS.get(
            record.outcome, "package_update_reason_mutation_uncertain"
        )
        if record.snapshot_uncertain and record.snapshot_name is not None:
            retained = _translate(
                hass,
                "package_update_uncertain_snapshot_detail",
                snapshot=escape_markdown_cell(record.snapshot_name),
            )
        elif record.snapshot_retained and record.snapshot_name is not None:
            retained = _translate(
                hass,
                "package_update_retained_snapshot_detail",
                snapshot=escape_markdown_cell(record.snapshot_name),
            )
        else:
            retained = _translate(
                hass, "package_update_no_retained_snapshot_detail"
            )
        placeholders = {
            **target,
            "reason": _translate(hass, reason_key),
            "retained": retained,
        }
    persistent_notification.async_create(
        hass,
        _translate(hass, key, **placeholders),
        _translate(hass, "package_update_notification_title"),
        _notification_id("package_update", node, vmid),
    )


def dismiss_cleanup_candidates(hass: HomeAssistant, node: str, vmid: int) -> None:
    """Dismiss any exact cleanup plan which is no longer actionable."""
    persistent_notification.async_dismiss(
        hass, _notification_id("package_cleanup_candidates", node, vmid)
    )


def dismiss_review_plan(hass: HomeAssistant, node: str, vmid: int) -> None:
    """Dismiss any exact review plan which is no longer actionable."""
    persistent_notification.async_dismiss(
        hass, _notification_id("package_review", node, vmid)
    )


def notify_cleanup_observation(
    hass: HomeAssistant,
    node: str,
    vmid: int,
    candidates: tuple[RemovablePackage, ...] | None,
    source: str,
) -> None:
    """Present exact cleanup state independently from package-update truth."""
    notification_id = _notification_id("package_cleanup_candidates", node, vmid)
    if candidates is None:
        if source == "scan":
            persistent_notification.async_dismiss(hass, notification_id)
            return
        key = "package_cleanup_unknown_notification"
        placeholders = {"node": escape_markdown_cell(node), "vmid": str(vmid)}
    elif not candidates:
        if source == "scan":
            persistent_notification.async_dismiss(hass, notification_id)
            return
        key = "package_cleanup_empty_notification"
        placeholders = {"node": escape_markdown_cell(node), "vmid": str(vmid)}
    else:
        rows = [
            "| "
            + " | ".join(
                _translate(hass, key)
                for key in (
                    "package_table_package",
                    "package_table_architecture",
                    "package_table_installed_version",
                )
            )
            + " |",
            "| --- | --- | --- |",
        ]
        rows.extend(
            "| "
            + " | ".join(
                escape_markdown_cell(value)
                for value in (
                    package.name,
                    package.architecture,
                    package.installed_version,
                )
            )
            + " |"
            for package in candidates
        )
        key = "package_cleanup_candidates_notification"
        placeholders = {
            "node": escape_markdown_cell(node),
            "vmid": str(vmid),
            "count": str(len(candidates)),
            "plan": "\n".join(rows),
        }
    persistent_notification.async_create(
        hass,
        _translate(hass, key, **placeholders),
        _translate(hass, "package_cleanup_notification_title"),
        notification_id,
    )


def notify_cleanup_complete(
    hass: HomeAssistant, node: str, vmid: int, record: PackageUpdateRecord
) -> None:
    """Report the bounded mutation outcome separately from cleanup observation."""
    target = {"node": escape_markdown_cell(node), "vmid": str(vmid)}
    if record.status is PackageUpdateStatus.SUCCESS:
        if record.snapshot_cleanup_failed and record.snapshot_name is not None:
            key = "package_cleanup_snapshot_failed_notification"
            placeholders = {
                **target,
                "changed": str(record.changed_package_count or 0),
                "snapshot": escape_markdown_cell(record.snapshot_name),
            }
        else:
            key = "package_cleanup_success_notification"
            placeholders = {
                **target,
                "changed": str(record.changed_package_count or 0),
            }
    elif record.outcome is PackageUpdateOutcome.PLAN_CHANGED:
        key = "package_cleanup_plan_changed_notification"
        placeholders = target
    elif record.outcome is PackageUpdateOutcome.HELPER_OUTDATED:
        key = "package_cleanup_helper_outdated_notification"
        placeholders = target
    else:
        key = "package_cleanup_failed_notification"
        reason_key = _REASON_KEYS.get(
            record.outcome, "package_update_reason_mutation_uncertain"
        )
        if record.snapshot_uncertain and record.snapshot_name is not None:
            retained = _translate(
                hass,
                "package_update_uncertain_snapshot_detail",
                snapshot=escape_markdown_cell(record.snapshot_name),
            )
        elif record.snapshot_retained and record.snapshot_name is not None:
            retained = _translate(
                hass,
                "package_update_retained_snapshot_detail",
                snapshot=escape_markdown_cell(record.snapshot_name),
            )
        else:
            retained = _translate(
                hass, "package_update_no_retained_snapshot_detail"
            )
        placeholders = {
            **target,
            "reason": _translate(hass, reason_key),
            "retained": retained,
        }
    persistent_notification.async_create(
        hass,
        _translate(hass, key, **placeholders),
        _translate(hass, "package_cleanup_result_notification_title"),
        _notification_id("package_cleanup_result", node, vmid),
    )


def update_helper_issue(
    hass: HomeAssistant, entry_id: str, installed_version: int
) -> None:
    """Create or clear the one operator-facing stale-helper Repairs issue."""
    issue_id = _helper_issue_id(entry_id)
    if installed_version >= EXPECTED_HELPER_VERSION:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key="helper_outdated",
        translation_placeholders={
            "installed": str(installed_version),
            "required": str(EXPECTED_HELPER_VERSION),
            "bootstrap_command": BOOTSTRAP_COMMAND,
        },
    )


def clear_helper_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Clear the stale-helper Repairs issue for one unloaded config entry."""
    ir.async_delete_issue(hass, DOMAIN, _helper_issue_id(entry_id))
