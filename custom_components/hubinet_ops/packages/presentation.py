"""Bounded Home Assistant notification presentation for package updates."""

from html import escape

from custom_components.hubinet_ops.const import DOMAIN
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_exception_message

from .models import (
    PackageScanRecord,
    PackageUpdateOutcome,
    PackageUpdateRecord,
    PackageUpdateStatus,
)

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


def _translate(key: str, **placeholders: str) -> str:
    """Return one already-loaded English integration exception string."""
    return async_get_exception_message(DOMAIN, key, placeholders or None)


def _notification_id(kind: str, node: str, vmid: int) -> str:
    """Return a stable per-target notification ID."""
    return f"hubinet_ops_{kind}_{node}_{vmid}"


def notify_review_plan(
    hass: HomeAssistant, node: str, vmid: int, record: PackageScanRecord
) -> None:
    """Show the full exact current plan and explicit approval instruction."""
    assert record.result is not None
    rows = [
        "| Package | Architecture | Installed | Candidate | Origin | Security |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for package in record.result.packages:
        security = (
            "yes"
            if package.security is True
            else "no"
            if package.security is False
            else "unknown"
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
                    package.origin if package.origin is not None else "unknown",
                    security,
                )
            )
            + " |"
        )
    plan = "\n".join(rows)
    persistent_notification.async_create(
        hass,
        _translate(
            "package_review_notification",
            node=escape_markdown_cell(node),
            vmid=str(vmid),
            plan=plan,
        ),
        _translate("package_review_notification_title"),
        _notification_id("package_review", node, vmid),
    )


def notify_retained_snapshots(
    hass: HomeAssistant, node: str, vmid: int, names: tuple[str, ...]
) -> None:
    """Warn about old prefix-matched snapshots without blocking the update."""
    rendered_names = ", ".join(escape_markdown_cell(name) for name in names)
    persistent_notification.async_create(
        hass,
        _translate(
            "package_retained_snapshot_warning",
            node=escape_markdown_cell(node),
            vmid=str(vmid),
            count=str(len(names)),
            names=rendered_names,
        ),
        _translate("package_retained_snapshot_warning_title"),
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
        retained = (
            _translate(
                "package_update_retained_snapshot_detail",
                snapshot=escape_markdown_cell(record.snapshot_name),
            )
            if record.snapshot_retained and record.snapshot_name is not None
            else _translate("package_update_no_retained_snapshot_detail")
        )
        placeholders = {
            **target,
            "reason": _translate(reason_key),
            "retained": retained,
        }
    persistent_notification.async_create(
        hass,
        _translate(key, **placeholders),
        _translate("package_update_notification_title"),
        _notification_id("package_update", node, vmid),
    )
