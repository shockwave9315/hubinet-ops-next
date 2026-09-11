"""Runtime translation coverage for the custom integration."""

import json
import logging
from pathlib import Path
import string
from unittest.mock import patch

import pytest

from custom_components.hubinet_ops import binary_sensor, button, sensor
from custom_components.hubinet_ops.const import DOMAIN
from custom_components.hubinet_ops.packages.models import (
    PackageScanRecord,
    PackageScanStatus,
    RemovablePackage,
)
from custom_components.hubinet_ops.packages.presentation import (
    notify_cleanup_observation,
    notify_review_plan,
)
from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations, recursive_flatten

from .test_packages import RESULT

INTEGRATION_DIR = Path(button.__file__).parent
STRINGS = INTEGRATION_DIR / "strings.json"
ENGLISH = INTEGRATION_DIR / "translations" / "en.json"
POLISH = INTEGRATION_DIR / "translations" / "pl.json"
USER_FACING_CATEGORIES = (
    "config",
    "entity",
    "exceptions",
    "issues",
    "selector",
    "services",
)
PACKAGE_PRESENTATION_KEYS = {
    "package_review_notification_title",
    "package_review_notification",
    "package_retained_snapshot_warning_title",
    "package_retained_snapshot_warning",
    "package_update_notification_title",
    "package_update_plan_changed_notification",
    "package_update_success_notification",
    "package_update_cleanup_failed_notification",
    "package_update_helper_outdated_notification",
    "package_update_failed_notification",
    "package_update_retained_snapshot_detail",
    "package_update_uncertain_snapshot_detail",
    "package_update_no_retained_snapshot_detail",
    "package_update_reason_plan_failed",
    "package_update_reason_snapshot_failed",
    "package_update_reason_busy",
    "package_update_reason_mutation_failed",
    "package_update_reason_mutation_timed_out",
    "package_update_reason_mutation_uncertain",
    "package_update_reason_guest_unavailable",
    "package_update_reason_liveness_failed",
    "package_cleanup_notification_title",
    "package_cleanup_candidates_notification",
    "package_cleanup_empty_notification",
    "package_cleanup_unknown_notification",
    "package_cleanup_result_notification_title",
    "package_cleanup_plan_changed_notification",
    "package_cleanup_success_notification",
    "package_cleanup_snapshot_failed_notification",
    "package_cleanup_helper_outdated_notification",
    "package_cleanup_failed_notification",
    "package_table_package",
    "package_table_architecture",
    "package_table_installed_version",
    "package_table_candidate_version",
    "package_table_origin",
    "package_table_security",
    "package_table_yes",
    "package_table_no",
    "package_table_unknown",
}


def _load(path: Path) -> dict:
    """Load one committed translation artifact."""
    return json.loads(path.read_text(encoding="utf-8"))


def _leaf_placeholders(value: str) -> set[str]:
    """Return the placeholders used by one user-facing string."""
    return {
        field
        for _literal, field, _format_spec, _conversion in string.Formatter().parse(
            value
        )
        if field is not None
    }


def _runtime_entity_translation_keys() -> dict[str, set[str]]:
    """Collect translation keys from the integration's actual descriptions."""
    descriptions = {
        "binary_sensor": (
            *binary_sensor.NODE_SENSORS,
            *binary_sensor.CONTAINER_SENSORS,
            *binary_sensor.VM_SENSORS,
            *binary_sensor.STORAGE_SENSORS,
        ),
        "button": (
            *button.NODE_BUTTONS,
            *button.VM_BUTTONS,
            *button.CONTAINER_BUTTONS,
            button.PACKAGE_SCAN_BUTTON,
            button.PACKAGE_REVIEW_BUTTON,
            button.PACKAGE_APPROVE_BUTTON,
            button.PACKAGE_UPDATE_BUTTON,
            button.PACKAGE_AUTOREMOVE_BUTTON,
        ),
        "sensor": (
            *sensor.NODE_SENSORS,
            *sensor.VM_SENSORS,
            *sensor.CONTAINER_SENSORS,
            *sensor.STORAGE_SENSORS,
            sensor.PACKAGE_SCAN_SENSOR,
            sensor.PACKAGE_UPDATE_SENSOR,
            sensor.UNUSED_PACKAGES_SENSOR,
        ),
    }
    return {
        platform: {
            description.translation_key
            for description in platform_descriptions
            if description.translation_key is not None
        }
        for platform, platform_descriptions in descriptions.items()
    }


def test_committed_english_is_canonical_and_contains_no_references() -> None:
    """The runtime English artifact exactly matches canonical authoring content."""
    assert _load(STRINGS) == _load(ENGLISH)
    for path in (STRINGS, ENGLISH, POLISH):
        assert "[%key:" not in path.read_text(encoding="utf-8")


def test_polish_keys_are_supported_and_placeholders_match_english() -> None:
    """Polish adds no private identifiers and safely formats English contracts."""
    english_source = _load(ENGLISH)
    polish_source = _load(POLISH)
    english = recursive_flatten("", english_source)
    polish = recursive_flatten("", polish_source)
    assert polish.keys() <= english.keys()
    for category in USER_FACING_CATEGORIES:
        assert recursive_flatten("", polish_source[category]).keys() == (
            recursive_flatten("", english_source[category]).keys()
        )
    for key, value in polish.items():
        assert _leaf_placeholders(value) == _leaf_placeholders(english[key]), key


def test_every_runtime_entity_translation_key_resolves_in_en_and_pl() -> None:
    """Actual entity descriptions have names in both shipped languages."""
    english = _load(ENGLISH)["entity"]
    polish = _load(POLISH)["entity"]
    for platform, translation_keys in _runtime_entity_translation_keys().items():
        assert translation_keys <= english[platform].keys()
        assert translation_keys <= polish[platform].keys()
    assert "resume" in english["button"]
    assert "resume_all" not in english["button"]


async def test_real_home_assistant_english_loading_resolves_runtime_content(
    hass: HomeAssistant,
) -> None:
    """Pinned Home Assistant loads concrete English without build-time literals."""
    loaded = {}
    for category in USER_FACING_CATEGORIES:
        loaded[category] = await async_get_translations(
            hass, "en", category, [DOMAIN]
        )
        assert loaded[category]
        assert all("[%key:" not in value for value in loaded[category].values())

    config = loaded["config"]
    assert config[f"component.{DOMAIN}.config.step.existing_credentials.data.host"] == (
        "Host"
    )
    assert config[
        f"component.{DOMAIN}.config.step.existing_credentials.data.username"
    ] == "Username"
    assert loaded["entity"][f"component.{DOMAIN}.entity.button.resume.name"] == (
        "Resume"
    )
    enrollment = config[
        f"component.{DOMAIN}.config.step.enrollment.description"
    ]
    assert "Use `--reset` only" in enrollment
    assert "API token" in enrollment
    assert "SSH enrollment key" in enrollment
    assert "Other Home Assistant instances" in enrollment
    assert "must be re-enrolled" in enrollment


async def test_real_home_assistant_polish_loading_covers_normal_ui(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pinned Home Assistant loads Polish for every supported UI category."""
    caplog.set_level(logging.ERROR, logger="homeassistant.helpers.translation")
    loaded = {}
    for category in USER_FACING_CATEGORIES:
        loaded[category] = await async_get_translations(
            hass, "pl", category, [DOMAIN]
        )
        assert loaded[category]
        expected = recursive_flatten(
            f"component.{DOMAIN}.{category}.", _load(POLISH)[category]
        )
        assert loaded[category].items() >= expected.items()

    prefix = f"component.{DOMAIN}"
    assert loaded["config"][
        f"{prefix}.config.step.existing_credentials.data.username"
    ] == "Nazwa użytkownika"
    assert loaded["entity"][f"{prefix}.entity.button.resume.name"] == "Wznów"
    enrollment = loaded["config"][f"{prefix}.config.step.enrollment.description"]
    assert "Użyj `--reset` tylko" in enrollment
    assert "token API Hubinet-Ops" in enrollment
    assert "klucz SSH używany do rejestracji" in enrollment
    assert "Inne instancje Home Assistanta" in enrollment
    assert "ponownej rejestracji" in enrollment
    exceptions = loaded["exceptions"]
    for key in PACKAGE_PRESENTATION_KEYS:
        assert f"{prefix}.exceptions.{key}.message" in exceptions
    assert exceptions[f"{prefix}.exceptions.package_table_package.message"] == (
        "Pakiet"
    )
    assert exceptions[f"{prefix}.exceptions.package_table_yes.message"] == "tak"
    assert exceptions[f"{prefix}.exceptions.package_table_unknown.message"] == (
        "nieznane"
    )
    assert not [
        record
        for record in caplog.records
        if "translation placeholder" in record.getMessage().lower()
    ]


async def test_polish_package_notification_localizes_copy_and_table(
    hass: HomeAssistant,
) -> None:
    """Persistent package UI uses the active HA language, including table labels."""
    await async_get_translations(hass, "pl", "exceptions", [DOMAIN])
    hass.config.language = "pl"

    notify_review_plan(
        hass,
        "pve1",
        200,
        PackageScanRecord(
            status=PackageScanStatus.SUCCESS,
            result=RESULT,
        ),
    )

    message = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_package_review_pve1_200"
    ]["message"]
    assert "Plan pakietów" in message
    assert "| Pakiet | Architektura | Zainstalowana wersja | Dostępna wersja" in (
        message
    )
    assert "| openssl | amd64 | 1.0 | 1.1 | Debian-Security | tak |" in message

    notify_cleanup_observation(
        hass,
        "pve1",
        200,
        (RemovablePackage("unused", "amd64", "1.0"),),
        "scan",
    )
    cleanup_message = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_package_cleanup_candidates_pve1_200"
    ]["message"]
    assert "| Pakiet | Architektura | Zainstalowana wersja |" in cleanup_message


def test_missing_localized_presentation_string_falls_back_to_english(
    hass: HomeAssistant,
) -> None:
    """A missing active-language key does not break valid notification rendering."""
    english = recursive_flatten(f"component.{DOMAIN}.", _load(ENGLISH))
    hass.config.language = "pl"

    def cached(
        _hass: HomeAssistant, language: str, _category: str, _domain: str
    ) -> dict[str, str]:
        return {} if language == "pl" else english

    with patch(
        "custom_components.hubinet_ops.packages.presentation."
        "async_get_cached_translations",
        side_effect=cached,
    ):
        notify_review_plan(
            hass,
            "pve1",
            200,
            PackageScanRecord(
                status=PackageScanStatus.SUCCESS,
                result=RESULT,
            ),
        )

    message = pn._async_get_or_create_notifications(hass)[  # noqa: SLF001
        "hubinet_ops_package_review_pve1_200"
    ]["message"]
    assert "Package plan" in message
    assert "| Package | Architecture | Installed version | Candidate version" in (
        message
    )
