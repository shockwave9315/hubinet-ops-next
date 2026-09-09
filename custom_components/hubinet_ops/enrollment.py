"""Strict parsing for the temporary guided-enrollment value."""

import base64
import binascii
from dataclasses import dataclass
import json
import re
from typing import Any

import asyncssh

ENROLLMENT_PREFIX = "HUBINET1-"
MAX_ENROLLMENT_LENGTH = 16 * 1024
_FIELDS = {"v", "t", "k", "h"}
_OPENSSH_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN OPENSSH PRIVATE KEY-----\n"
    rb"(?:[A-Za-z0-9+/]+={0,2}\n)+"
    rb"-----END OPENSSH PRIVATE KEY-----(?:\n)?"
)


class EnrollmentError(ValueError):
    """The enrollment value is malformed or contains invalid key material."""

    def __init__(self, code: str, message: str) -> None:
        """Initialize with a UI-safe failure code and no credential content."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Enrollment:
    """Validated credentials carried by a temporary enrollment value."""

    token_secret: str
    private_key: str
    host_key: str


def _decode_base64(
    value: str, *, urlsafe: bool, field: str, error_code: str
) -> bytes:
    """Decode one unpadded base64 value with strict alphabet validation."""
    if not value or any(char.isspace() for char in value):
        raise EnrollmentError(error_code, f"{field} is not valid base64")
    if urlsafe and "=" in value:
        raise EnrollmentError(error_code, f"{field} is not canonical base64url")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_" if urlsafe else None,
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as err:
        raise EnrollmentError(error_code, f"{field} is not valid base64") from err


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate field ambiguity."""
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise EnrollmentError(
                "invalid_enrollment_schema", "enrollment fields must be unique"
            )
        result[key] = item
    return result


def parse_enrollment(value: Any) -> Enrollment:
    """Parse and validate the bounded version-1 enrollment contract."""
    if not isinstance(value, str):
        raise EnrollmentError("invalid_enrollment_schema", "enrollment must be text")
    if len(value) > MAX_ENROLLMENT_LENGTH:
        raise EnrollmentError(
            "enrollment_too_large", "enrollment exceeds its size limit"
        )
    if not value.startswith(ENROLLMENT_PREFIX):
        raise EnrollmentError(
            "invalid_enrollment_prefix", "enrollment prefix or version is invalid"
        )

    raw = _decode_base64(
        value.removeprefix(ENROLLMENT_PREFIX),
        urlsafe=True,
        field="enrollment",
        error_code="invalid_enrollment_base64",
    )
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys
        )
    except EnrollmentError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise EnrollmentError(
            "invalid_enrollment_json", "enrollment JSON is malformed"
        ) from err
    if not isinstance(payload, dict):
        raise EnrollmentError(
            "invalid_enrollment_schema", "enrollment JSON must be an object"
        )
    if set(payload) != _FIELDS:
        raise EnrollmentError(
            "invalid_enrollment_schema",
            "enrollment fields do not match the version-1 schema",
        )
    if type(payload["v"]) is not int or payload["v"] != 1:
        raise EnrollmentError(
            "invalid_enrollment_prefix", "enrollment payload version is invalid"
        )
    if not all(isinstance(payload[field], str) for field in ("t", "k", "h")):
        raise EnrollmentError(
            "invalid_enrollment_schema",
            "enrollment credential fields must be text",
        )

    token_secret = payload["t"]
    if not 1 <= len(token_secret) <= 512 or any(
        char.isspace() for char in token_secret
    ):
        raise EnrollmentError(
            "invalid_enrollment_token", "API token secret is malformed"
        )

    private_key_bytes = _decode_base64(
        payload["k"],
        urlsafe=False,
        field="SSH private key",
        error_code="invalid_enrollment_private_key",
    )
    if len(private_key_bytes) > 8192:
        raise EnrollmentError(
            "invalid_enrollment_private_key", "SSH private key exceeds its size limit"
        )
    if _OPENSSH_PRIVATE_KEY_RE.fullmatch(private_key_bytes) is None:
        raise EnrollmentError(
            "invalid_enrollment_private_key",
            "SSH private key must contain exactly one OpenSSH key",
        )
    try:
        private_key = asyncssh.import_private_key(private_key_bytes)
    except (ValueError, asyncssh.KeyImportError) as err:
        raise EnrollmentError(
            "invalid_enrollment_private_key", "SSH private key is malformed"
        ) from err
    if private_key.get_algorithm() != "ssh-ed25519":
        raise EnrollmentError(
            "invalid_enrollment_private_key", "SSH private key is not Ed25519"
        )

    host_key_text = payload["h"]
    if "\n" in host_key_text or "\r" in host_key_text:
        raise EnrollmentError(
            "invalid_enrollment_host_key",
            "SSH host key must contain exactly one key",
        )
    host_parts = host_key_text.split()
    if (
        len(host_parts) != 2
        or host_parts[0] != "ssh-ed25519"
        or host_key_text != f"{host_parts[0]} {host_parts[1]}"
    ):
        raise EnrollmentError(
            "invalid_enrollment_host_key", "SSH host key is malformed"
        )
    try:
        host_key = asyncssh.import_public_key(host_key_text.encode("ascii"))
    except (UnicodeEncodeError, ValueError, asyncssh.KeyImportError) as err:
        raise EnrollmentError(
            "invalid_enrollment_host_key", "SSH host key is malformed"
        ) from err
    if host_key.get_algorithm() != "ssh-ed25519":
        raise EnrollmentError(
            "invalid_enrollment_host_key", "SSH host key is not Ed25519"
        )

    return Enrollment(
        token_secret=token_secret,
        private_key=private_key_bytes.decode("ascii"),
        host_key=host_key_text,
    )
