"""Tests for pure Debian/Ubuntu package evidence parsing."""

import pytest

from custom_components.hubinet_ops.packages.models import (
    PackageScanError,
    PackageScanFailure,
)
from custom_components.hubinet_ops.packages.parser import (
    PackageScanParseError,
    parse_apt_simulation,
    parse_autoremove_simulation,
    parse_installed_inventory,
    parse_native_architecture,
    parse_os_release,
)

ZERO_SIMULATION = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
TWO_UPDATES = """\
Inst openssl [3.0.11-1] (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Inst apt [2.6.1] (2.6.2 Debian:12/oldstable [amd64])
Conf openssl (3.0.11-1~deb12u3 Debian-Security:12/oldstable-security [amd64])
Conf apt (2.6.2 Debian:12/oldstable [amd64])
2 upgraded, 0 newly installed, 0 to remove and 41 not upgraded.
"""
TWO_INVENTORY = "openssl\tamd64\t3.0.11-1\tinstalled\napt\tamd64\t2.6.1\tinstalled\n"
AUTOREMOVE_INVENTORY = (
    "native\tamd64\t1.0\tinstalled\n"
    "foreign\ti386\t2.0\tinstalled\n"
    "common\tall\t3.0\tinstalled\n"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('ID=debian\nVERSION_ID="12"\n', ("debian", "12")),
        ('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n', ("ubuntu", "24.04")),
    ],
)
def test_parse_supported_os_release(text: str, expected: tuple[str, str]) -> None:
    """Debian and Ubuntu os-release evidence is accepted."""
    assert parse_os_release(text) == expected


def test_unsupported_guest_os_fails_closed() -> None:
    """An unsupported distribution has a bounded failure class."""
    with pytest.raises(PackageScanError) as caught:
        parse_os_release('ID=alpine\nVERSION_ID="3.20"\n')
    assert caught.value.failure is PackageScanFailure.UNSUPPORTED_OS


def test_exact_rows_security_tri_state_and_not_upgraded_count() -> None:
    """Exact rows stay separate from kept-back and tri-state origin evidence.

    "apt" has a real, non-security origin (Debian:12/oldstable); lacking a
    "-security" marker is not reliable non-security evidence, so it stays
    unknown (None) rather than False. Only "openssl"'s Debian-Security
    origin is reliable positive evidence.
    """
    parsed = parse_apt_simulation(
        TWO_UPDATES,
        native_architecture="amd64",
        installed_inventory=TWO_INVENTORY,
    )
    assert [(package.name, package.security) for package in parsed.packages] == [
        ("apt", None),
        ("openssl", True),
    ]
    assert parsed.not_upgraded_count == 41

    unknown = parse_apt_simulation(
        "Inst example [1.0] (2.0 [amd64])\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        native_architecture="amd64",
        installed_inventory="example\tamd64\t1.0\tinstalled\n",
    )
    assert unknown.packages[0].origin is None
    assert unknown.packages[0].security is None


@pytest.mark.parametrize(
    ("origin_field", "expected_security"),
    [
        ("Debian-Security:12/oldstable-security", True),
        ("Ubuntu:24.04/noble-security", True),
        ("Debian:12/stable", None),
        ("Ubuntu:24.04/noble-updates", None),
        ("Docker:1.0/docker", None),
        ("Proxmox:8/pve-no-subscription", None),
        ("ppa.launchpadcontent.net/user/ppa/ubuntu:24.04/noble", None),
        ("SomeUnknownVendor", None),
        (None, None),
    ],
)
def test_security_tri_state_only_positive_marker_evidence_is_true(
    origin_field: str | None, expected_security: bool | None
) -> None:
    """Only a reliable "-security" origin marker yields True.

    An origin that merely lacks that marker (a normal release, a third-party
    vendor, a PPA, or no origin at all) stays unknown, never False, per
    ARCHITECTURE.md's security tri-state rules.
    """
    relstr = f"{origin_field} [amd64]" if origin_field is not None else "[amd64]"
    simulation = (
        f"Inst example [1.0] (2.0 {relstr})\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    )
    parsed = parse_apt_simulation(
        simulation,
        native_architecture="amd64",
        installed_inventory="example\tamd64\t1.0\tinstalled\n",
    )
    assert parsed.packages[0].security is expected_security


def test_zero_updates_is_an_exact_empty_plan() -> None:
    """A successful zero plan is distinct from unknown scan evidence."""
    parsed = parse_apt_simulation(
        ZERO_SIMULATION,
        native_architecture="amd64\n",
        installed_inventory="",
    )
    assert parsed.packages == ()
    assert parsed.not_upgraded_count == 0


def test_multiarch_packages_remain_distinct() -> None:
    """Native and foreign package identities are not collapsed."""
    parsed = parse_apt_simulation(
        "Inst libc6 [2.31-1] (2.31-2 Debian:stable [amd64])\n"
        "Inst libc6:i386 [2.31-1] (2.31-2 Debian:stable [i386])\n"
        "2 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        native_architecture="amd64",
        installed_inventory=(
            "libc6\tamd64\t2.31-1\tinstalled\nlibc6\ti386\t2.31-1\tinstalled\n"
        ),
    )
    assert {(package.name, package.architecture) for package in parsed.packages} == {
        ("libc6", "amd64"),
        ("libc6", "i386"),
    }


@pytest.mark.parametrize(
    "simulation",
    [
        "Reading package lists...\n",
        "Inst apt broken\n"
        "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Inst apt [2.6.1] (2.6.2 Debian:12 [amd64])\n"
        "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Remv apt [2.6.1]\n"
        "0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
    ],
)
def test_malformed_apt_simulation_fails_closed(simulation: str) -> None:
    """Malformed or non-upgrade-only simulations remain malformed."""
    with pytest.raises(PackageScanParseError) as caught:
        parse_apt_simulation(
            simulation,
            native_architecture="amd64",
            installed_inventory="apt\tamd64\t2.6.1\tinstalled\n",
        )
    assert caught.value.failure is PackageScanFailure.MALFORMED_PLAN


@pytest.mark.parametrize(
    "status",
    [
        "half-installed",
        "unpacked",
        "half-configured",
        "triggers-awaited",
        "triggers-pending",
    ],
)
def test_unfinished_dpkg_inventory_has_semantic_failure(status: str) -> None:
    """Every known unfinished dpkg state fails with DPKG_UNFINISHED."""
    inventory_text = f"foo\tamd64\t1.0\t{status}\n"
    inventory = parse_installed_inventory(inventory_text)
    assert inventory.unfinished == (("foo", "amd64", status),)
    with pytest.raises(PackageScanParseError) as caught:
        parse_apt_simulation(
            ZERO_SIMULATION,
            native_architecture="amd64",
            installed_inventory=inventory_text,
        )
    assert caught.value.failure is PackageScanFailure.DPKG_UNFINISHED


def test_apt_unfinished_count_has_semantic_failure() -> None:
    """APT's unfinished package count is not treated as malformed syntax."""
    with pytest.raises(PackageScanParseError) as caught:
        parse_apt_simulation(
            f"{ZERO_SIMULATION}1 not fully installed or removed.\n",
            native_architecture="amd64",
            installed_inventory="",
        )
    assert caught.value.failure is PackageScanFailure.DPKG_UNFINISHED


def test_guest_changed_is_distinct_from_malformed_evidence() -> None:
    """Only valid cross-observation contradictions are classified as a change."""
    with pytest.raises(PackageScanParseError) as changed:
        parse_apt_simulation(
            "Inst apt [2.6.1] (2.6.2 Debian:12 [amd64])\n"
            "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
            native_architecture="amd64",
            installed_inventory="apt\tamd64\t2.6.0\tinstalled\n",
        )
    assert changed.value.failure is PackageScanFailure.GUEST_CHANGED_DURING_SCAN

    with pytest.raises(PackageScanParseError) as malformed:
        parse_apt_simulation(
            "Inst apt broken\n"
            "1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
            native_architecture="amd64",
            installed_inventory="apt\tamd64\t2.6.1\tinstalled\n",
        )
    assert malformed.value.failure is PackageScanFailure.MALFORMED_PLAN


@pytest.mark.parametrize("text", ["all\n", "", "amd64\ni386\n", "AMD64\n"])
def test_native_architecture_requires_one_real_dpkg_architecture(text: str) -> None:
    """Native architecture parsing rejects ambiguous and malformed evidence."""
    with pytest.raises(PackageScanParseError):
        parse_native_architecture(text)


@pytest.mark.parametrize(
    ("simulation", "expected"),
    [
        (
            "0 upgraded, 0 newly installed, 0 to remove and 19 not upgraded.\n",
            [],
        ),
        (
            "Remv native [1.0]\n"
            "0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
            [("native", "amd64", "1.0")],
        ),
        (
            "Remv foreign:i386 [2.0]\n"
            "Remv common [3.0]\n"
            "0 upgraded, 0 newly installed, 2 to remove and 7 not upgraded.\n",
            [("common", "all", "3.0"), ("foreign", "i386", "2.0")],
        ),
    ],
)
def test_parse_exact_autoremove_actions(
    simulation: str, expected: list[tuple[str, str, str]]
) -> None:
    """Only exact Remv identities plus the exact summary authorize cleanup."""
    parsed = parse_autoremove_simulation(
        simulation,
        native_architecture="amd64\n",
        installed_inventory=AUTOREMOVE_INVENTORY,
    )
    assert [
        (package.name, package.architecture, package.installed_version)
        for package in parsed.packages
    ] == expected


@pytest.mark.parametrize(
    "simulation",
    [
        "Purg native [1.0]\n"
        "0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
        "Inst native [1.0] (2.0 [amd64])\n"
        "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Conf native (1.0 [amd64])\n"
        "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Remv native\n"
        "0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
        "Remv missing [1.0]\n"
        "0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
        "Remv native [9.9]\n"
        "0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
        "Remv native [1.0]\nRemv native [1.0]\n"
        "0 upgraded, 0 newly installed, 2 to remove and 0 not upgraded.\n",
        "Remv foreign:BAD [2.0]\n"
        "0 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
        "Remv native [1.0]\n",
        "Remv native [1.0]\n"
        "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
        "Remv native [1.0]\n"
        "1 upgraded, 0 newly installed, 1 to remove and 0 not upgraded.\n",
        "Remv native [1.0]\n"
        "0 upgraded, 1 newly installed, 1 to remove and 0 not upgraded.\n",
    ],
)
def test_autoremove_parser_rejects_ambiguous_or_unsafe_evidence(
    simulation: str,
) -> None:
    """Forbidden actions, contradictions, and malformed plans fail closed."""
    with pytest.raises(PackageScanParseError):
        parse_autoremove_simulation(
            simulation,
            native_architecture="amd64",
            installed_inventory=AUTOREMOVE_INVENTORY,
        )


def test_autoremove_parser_rejects_unfinished_dpkg_evidence() -> None:
    """Unfinished dpkg state remains a semantic hard failure."""
    with pytest.raises(PackageScanParseError) as caught:
        parse_autoremove_simulation(
            "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n",
            native_architecture="amd64",
            installed_inventory="native\tamd64\t1.0\tunpacked\n",
        )
    assert caught.value.failure is PackageScanFailure.DPKG_UNFINISHED


def test_autoremove_parser_accepts_large_exact_plan() -> None:
    """Legitimate plans are not truncated before becoming actionable."""
    count = 1000
    inventory = "".join(
        f"pkg{index}\tamd64\t1.{index}\tinstalled\n" for index in range(count)
    )
    simulation = "".join(
        f"Remv pkg{index} [1.{index}]\n" for index in range(count)
    ) + f"0 upgraded, 0 newly installed, {count} to remove and 3 not upgraded.\n"
    assert len(
        parse_autoremove_simulation(
            simulation,
            native_architecture="amd64",
            installed_inventory=inventory,
        ).packages
    ) == count
