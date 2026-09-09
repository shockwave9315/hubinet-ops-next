"""Tests for strict temporary enrollment parsing."""

import base64
import json

import asyncssh
import pytest

from custom_components.hubinet_ops.enrollment import (
    ENROLLMENT_PREFIX,
    MAX_ENROLLMENT_LENGTH,
    EnrollmentError,
    parse_enrollment,
)


def _keys() -> tuple[str, str]:
    private = asyncssh.generate_private_key("ssh-ed25519")
    host = asyncssh.generate_private_key("ssh-ed25519")
    return (
        base64.b64encode(private.export_private_key()).decode(),
        host.export_public_key().decode().strip(),
    )


def _encode(payload: object) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=")
    return ENROLLMENT_PREFIX + encoded.decode()


def _payload() -> dict[str, object]:
    private, host = _keys()
    return {"v": 1, "t": "token-secret", "k": private, "h": host}


def test_valid_enrollment_imports_ed25519_material() -> None:
    """The exact version-1 schema produces runtime-ready key text."""
    parsed = parse_enrollment(_encode(_payload()))
    assert parsed.token_secret == "token-secret"
    assert asyncssh.import_private_key(parsed.private_key.encode()).get_algorithm() == (
        "ssh-ed25519"
    )
    assert asyncssh.import_public_key(parsed.host_key.encode()).get_algorithm() == (
        "ssh-ed25519"
    )


@pytest.mark.parametrize(
    "value",
    [
        "WRONG-e30",
        ENROLLMENT_PREFIX + "%%%",
        _encode("not-an-object"),
        ENROLLMENT_PREFIX + base64.urlsafe_b64encode(b"{").decode().rstrip("="),
    ],
)
def test_invalid_outer_enrollment_is_rejected(value: str) -> None:
    """Prefix, base64url, JSON, and object shape all fail closed."""
    with pytest.raises(EnrollmentError):
        parse_enrollment(value)


def test_oversized_enrollment_is_rejected_before_decoding() -> None:
    """The total temporary value has a strict upper bound."""
    with pytest.raises(EnrollmentError, match="size limit"):
        parse_enrollment(ENROLLMENT_PREFIX + "A" * MAX_ENROLLMENT_LENGTH)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: {**value, "extra": "no"},
        lambda value: {key: item for key, item in value.items() if key != "t"},
        lambda value: {**value, "v": True},
        lambda value: {**value, "t": 42},
        lambda value: {**value, "k": "%%%"},
        lambda value: {**value, "k": base64.b64encode(b"not a key").decode()},
        lambda value: {**value, "h": "not a key"},
        lambda value: {**value, "h": f"{value['h']}\n{value['h']}"},
    ],
)
def test_invalid_schema_or_key_material_is_rejected(mutate) -> None:
    """Unknown/missing fields, bad types, and ambiguous keys fail closed."""
    with pytest.raises(EnrollmentError):
        parse_enrollment(_encode(mutate(_payload())))


def test_multiple_private_keys_are_rejected() -> None:
    """AsyncSSH's permissive trailing-data behavior cannot create ambiguity."""
    payload = _payload()
    private_key = asyncssh.generate_private_key("ssh-ed25519").export_private_key()
    payload["k"] = base64.b64encode(private_key + private_key).decode()
    with pytest.raises(EnrollmentError, match="exactly one OpenSSH key"):
        parse_enrollment(_encode(payload))


def test_duplicate_json_fields_are_rejected() -> None:
    """A JSON decoder cannot silently choose one of two secret fields."""
    raw = json.dumps(_payload(), separators=(",", ":"))
    raw = raw[:-1] + ',"t":"second-secret"}'
    encoded = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
    with pytest.raises(EnrollmentError, match="fields must be unique"):
        parse_enrollment(f"{ENROLLMENT_PREFIX}{encoded}")
