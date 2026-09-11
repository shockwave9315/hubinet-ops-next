"""Test the config flow for Proxmox VE."""

import base64
import json
from typing import Any
from unittest.mock import MagicMock, patch

import asyncssh
import pytest
import requests
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from proxmoxer import AuthenticationError
from proxmoxer.core import ResourceException
from requests.exceptions import ConnectTimeout, SSLError
from tests.common import MockConfigEntry

from custom_components.hubinet_ops import CONF_AUTH_METHOD, CONF_REALM
from custom_components.hubinet_ops.const import (
    CONF_NODE,
    CONF_NODES,
    CONF_PACKAGE_NODE,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PRIVATE_KEY,
    CONF_TOKEN_ID,
    CONF_TOKEN_SECRET,
    DEFAULT_TIMEOUT,
    DOMAIN,
    INTEGRATION_VERSION,
)
from custom_components.hubinet_ops.enrollment import (
    ENROLLMENT_PREFIX,
    MAX_ENROLLMENT_LENGTH,
)
from custom_components.hubinet_ops.packages.transport import (
    PackageHelperProbe,
    PackageHelperProtocolError,
    PackageHelperUnavailableError,
    PackageTransportAuthenticationError,
    PackageTransportConnectionError,
    PackageTransportHostKeyError,
)

from .conftest import (
    MOCK_TEST_CONFIG,
    MOCK_TEST_OTHER_CONFIG,
    MOCK_TEST_TOKEN_CONFIG,
    MOCK_TEST_TOKEN_OTHER_CONFIG,
)

# Regular PAM user authentication + password
MOCK_USER_STEP = {
    CONF_AUTH_METHOD: "pam",
    CONF_HOST: "127.0.0.1",
    CONF_USERNAME: "test_user",
    CONF_VERIFY_SSL: True,
    CONF_PORT: 8006,
    CONF_TOKEN: False,
}

MOCK_USER_AUTH_STEP_PASSWORD = {
    CONF_PASSWORD: "test_password",
}

# API token authentication
MOCK_USER_STEP_TOKEN = {
    **MOCK_USER_STEP,
    CONF_TOKEN: True,
}

MOCK_USER_AUTH_STEP_TOKEN = {
    CONF_TOKEN_ID: "test_token_id",
    CONF_TOKEN_SECRET: "test_token_secret",
}

MOCK_USER_AUTH_STEP_TOKEN_FULL_ID = {
    CONF_TOKEN_ID: "test_user@pam!test_token_id",
    CONF_TOKEN_SECRET: "test_token_secret",
}

# Other authentication method (e.g. LDAP) with realm
MOCK_USER_STEP_OTHER = {
    **MOCK_USER_STEP,
    CONF_AUTH_METHOD: "other",
}

MOCK_USER_AUTH_STEP_OTHER = {
    **MOCK_USER_AUTH_STEP_PASSWORD,
    CONF_REALM: "Test_Realm",
}

# Other authentication method with realm and token
MOCK_USER_STEP_OTHER_TOKEN = {
    **MOCK_USER_STEP_TOKEN,
    CONF_AUTH_METHOD: "other",
}

MOCK_USER_AUTH_STEP_OTHER_TOKEN = {
    **MOCK_USER_AUTH_STEP_TOKEN,
    CONF_REALM: "Test_Realm",
}

MOCK_USER_SETUP = {CONF_NODES: ["pve1"]}

MOCK_USER_FINAL = {
    **MOCK_USER_STEP,
    **MOCK_USER_SETUP,
}


async def _start_existing_credentials(hass: HomeAssistant) -> dict[str, Any]:
    """Start the preserved upstream-compatible path through the new menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["guided", "existing_credentials"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "existing_credentials"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "existing_credentials"
    return result


def _enrollment_payload() -> dict[str, object]:
    """Return one valid external enrollment payload."""
    private = asyncssh.generate_private_key("ssh-ed25519").export_private_key()
    host = (
        asyncssh.generate_private_key("ssh-ed25519")
        .export_public_key()
        .decode()
        .strip()
    )
    return {
        "v": 1,
        "t": "guided-token-secret",
        "k": base64.b64encode(private).decode(),
        "h": host,
    }


def _encode_enrollment_payload(payload: object) -> str:
    """Encode the temporary external transport shape."""
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=")
    return f"HUBINET1-{encoded.decode()}"


def _enrollment() -> tuple[str, str, str]:
    """Return one valid enrollment plus its decoded keys."""
    payload = _enrollment_payload()
    private = base64.b64decode(payload["k"])
    return _encode_enrollment_payload(payload), private.decode(), payload["h"]


async def _start_guided(hass: HomeAssistant) -> dict[str, Any]:
    """Advance the user menu through the endpoint form to enrollment."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "guided"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "guided"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: 8006, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "enrollment"
    assert INTEGRATION_VERSION in result["description_placeholders"][
        "bootstrap_command"
    ]
    assert "/main/" not in result["description_placeholders"]["bootstrap_command"]
    assert result["description_placeholders"]["reset_bootstrap_command"] == (
        f'{result["description_placeholders"]["bootstrap_command"]} -s -- --reset'
    )
    return result


async def _start_advanced_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, Any]:
    """Advance an existing entry through the reconfigure menu."""
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [
        "reconfigure_guided",
        "reconfigure_existing_credentials",
    ]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "reconfigure_existing_credentials"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_existing_credentials"
    return result


def _guided_config_entry() -> MockConfigEntry:
    """Return an existing entry created by guided enrollment."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    return MockConfigEntry(
        domain=DOMAIN,
        title="Guided Proxmox",
        entry_id="guided-entry",
        version=4,
        data={
            **MOCK_TEST_TOKEN_CONFIG,
            CONF_AUTH_METHOD: "pve",
            CONF_REALM: "pve",
            CONF_USERNAME: "hubinetnext@pve",
            CONF_TOKEN_ID: "ha",
            CONF_TOKEN_SECRET: "old-token-secret",
            CONF_SSH_PRIVATE_KEY: private_key.export_private_key().decode(),
            CONF_SSH_HOST_KEY: host_key.export_public_key().decode().strip(),
            CONF_PACKAGE_NODE: "pve1",
        },
    )


async def _start_guided_reconfigure(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    host: str = "127.0.0.1",
) -> dict[str, Any]:
    """Advance an existing entry to its guided enrollment form."""
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_guided"}
    )
    assert result["step_id"] == "reconfigure_guided"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: host, CONF_PORT: 8006, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_enrollment"
    assert result["description_placeholders"]["bootstrap_command"].endswith(
        "| bash -s -- --reset"
    )
    return result


async def test_guided_happy_path_creates_one_ready_entry(
    hass: HomeAssistant, mock_proxmox_client: MagicMock
) -> None:
    """API and authenticated helper checks complete before one entry is saved."""
    result = await _start_guided(hass)
    value, private_key, host_key = _enrollment()
    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe",
        return_value=PackageHelperProbe(node="pve1", helper_version=2),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": value}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "127.0.0.1"
    assert result["data"][CONF_USERNAME] == "hubinetnext@pve"
    assert result["data"][CONF_TOKEN_ID] == "ha"
    assert result["data"][CONF_TOKEN_SECRET] == "guided-token-secret"
    assert result["data"][CONF_SSH_PRIVATE_KEY] == private_key
    assert result["data"][CONF_SSH_HOST_KEY] == host_key
    assert result["data"][CONF_PACKAGE_NODE] == "pve1"
    assert "enrollment" not in result["data"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    mock_proxmox_client._mock_api_cf.assert_called_once_with(
        host="127.0.0.1",
        port=8006,
        user="hubinetnext@pve",
        verify_ssl=True,
        timeout=DEFAULT_TIMEOUT,
        token_name="ha",
        token_value="guided-token-secret",
    )
    # Guided validation probes before entry creation; loaded runtime then performs
    # its separate one-shot, non-blocking compatibility observation.
    assert probe.await_count == 2


async def test_guided_duplicate_stops_before_bootstrap(
    hass: HomeAssistant,
    mock_setup_entry: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Duplicate host detection occurs on the endpoint form."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "guided"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: 8006, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_guided_invalid_enrollment_creates_no_entry(
    hass: HomeAssistant, mock_proxmox_client: MagicMock
) -> None:
    """Malformed setup transport remains on the enrollment form."""
    result = await _start_guided(hass)
    commands = result["description_placeholders"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enrollment": "not-an-enrollment"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "enrollment"
    assert result["errors"] == {"base": "invalid_enrollment_prefix"}
    assert result["description_placeholders"] == commands
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("value_factory", "reason"),
    [
        (
            lambda: ENROLLMENT_PREFIX + "A" * MAX_ENROLLMENT_LENGTH,
            "enrollment_too_large",
        ),
        (lambda: ENROLLMENT_PREFIX + "%%%", "invalid_enrollment_base64"),
        (
            lambda: ENROLLMENT_PREFIX
            + base64.urlsafe_b64encode(b"{").decode().rstrip("="),
            "invalid_enrollment_json",
        ),
        (
            lambda: _encode_enrollment_payload({**_enrollment_payload(), "x": "no"}),
            "invalid_enrollment_schema",
        ),
        (
            lambda: _encode_enrollment_payload(
                {
                    key: item
                    for key, item in _enrollment_payload().items()
                    if key != "t"
                }
            ),
            "invalid_enrollment_schema",
        ),
        (
            lambda: _encode_enrollment_payload(
                {**_enrollment_payload(), "t": False}
            ),
            "invalid_enrollment_schema",
        ),
        (
            lambda: _encode_enrollment_payload(
                {
                    **_enrollment_payload(),
                    "k": base64.b64encode(b"not a key").decode(),
                }
            ),
            "invalid_enrollment_private_key",
        ),
        (
            lambda: _encode_enrollment_payload(
                {**_enrollment_payload(), "h": "not a host key"}
            ),
            "invalid_enrollment_host_key",
        ),
        (
            lambda: _encode_enrollment_payload(
                {
                    **_enrollment_payload(),
                    "h": "ssh-ed25519 AAAA\nssh-ed25519 AAAA",
                }
            ),
            "invalid_enrollment_host_key",
        ),
    ],
)
async def test_guided_enrollment_failures_are_actionable_and_create_no_entry(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    value_factory,
    reason: str,
) -> None:
    """Each bounded enrollment check remains on its actionable setup form."""
    result = await _start_guided(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enrollment": value_factory()}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_guided_api_auth_failure_prevents_ssh_and_entry(
    hass: HomeAssistant, mock_proxmox_client: MagicMock
) -> None:
    """The fixed PVE token is validated before the SSH probe."""
    result = await _start_guided(hass)
    value, _, _ = _enrollment()
    mock_proxmox_client._mock_api_cf.side_effect = AuthenticationError("bad")
    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe"
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": value}
        )
    assert result["errors"] == {"base": "invalid_auth"}
    probe.assert_not_called()
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_guided_api_connection_failure_prevents_ssh_and_entry(
    hass: HomeAssistant, mock_proxmox_client: MagicMock
) -> None:
    """An unreachable API is reported before the SSH probe."""
    result = await _start_guided(hass)
    value, _, _ = _enrollment()
    mock_proxmox_client._mock_api_cf.side_effect = requests.exceptions.ConnectionError(
        "refused"
    )
    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe"
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": value}
        )
    assert result["errors"] == {"base": "cannot_connect"}
    probe.assert_not_called()
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PackageTransportAuthenticationError(), "ssh_auth_failed"),
        (PackageTransportHostKeyError(), "ssh_host_key_mismatch"),
        (PackageTransportConnectionError(), "ssh_cannot_connect"),
        (PackageHelperUnavailableError(), "helper_missing_or_outdated"),
        (PackageHelperProtocolError(), "helper_protocol_mismatch"),
    ],
)
async def test_guided_ssh_and_helper_failures_create_no_entry(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    error: Exception,
    reason: str,
) -> None:
    """Each actionable SSH/helper failure stays in guided setup."""
    result = await _start_guided(hass)
    value, _, _ = _enrollment()
    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": value}
        )
    assert result["errors"] == {"base": reason}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_guided_package_node_must_exist_in_api_discovery(
    hass: HomeAssistant, mock_proxmox_client: MagicMock
) -> None:
    """The helper-authenticated local node must be visible upstream."""
    result = await _start_guided(hass)
    value, _, _ = _enrollment()
    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe",
        return_value=PackageHelperProbe(node="other-node", helper_version=2),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": value}
        )
    assert result["errors"] == {"base": "package_node_not_found"}
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("mock_user_step", "mock_user_auth_step", "mock_test_config"),
    [
        (MOCK_USER_STEP, MOCK_USER_AUTH_STEP_PASSWORD, MOCK_TEST_CONFIG),
        (MOCK_USER_STEP_TOKEN, MOCK_USER_AUTH_STEP_TOKEN, MOCK_TEST_TOKEN_CONFIG),
        (
            MOCK_USER_STEP_TOKEN,
            MOCK_USER_AUTH_STEP_TOKEN_FULL_ID,
            MOCK_TEST_TOKEN_CONFIG,
        ),
        (MOCK_USER_STEP_OTHER, MOCK_USER_AUTH_STEP_OTHER, MOCK_TEST_OTHER_CONFIG),
        (
            MOCK_USER_STEP_OTHER_TOKEN,
            MOCK_USER_AUTH_STEP_OTHER_TOKEN,
            MOCK_TEST_TOKEN_OTHER_CONFIG,
        ),
    ],
)
async def test_form(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_user_step: dict[str, Any],
    mock_user_auth_step: dict[str, Any],
    mock_test_config: dict[str, Any],
) -> None:
    """Test we get the form."""
    result = await _start_existing_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=mock_user_step
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_auth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=mock_user_auth_step
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "127.0.0.1"
    assert result["data"] == mock_test_config


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (
            AuthenticationError("Invalid credentials"),
            "invalid_auth",
        ),
        (
            SSLError("SSL handshake failed"),
            "ssl_error",
        ),
        (
            ConnectTimeout("Connection timed out"),
            "connect_timeout",
        ),
        (
            ResourceException("500", "status_message", "content"),
            "api_error_no_details",
        ),
        (
            requests.exceptions.ConnectionError("Connection error"),
            "cannot_connect",
        ),
    ],
)
async def test_form_exceptions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    exception: Exception,
    reason: str,
) -> None:
    """Test we handle all exceptions."""
    mock_proxmox_client._mock_api_cf.side_effect = exception
    result = await _start_existing_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_STEP,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_auth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_AUTH_STEP_PASSWORD,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    mock_proxmox_client._mock_api_cf.side_effect = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_AUTH_STEP_PASSWORD
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (
            AuthenticationError("Invalid credentials"),
            "invalid_auth",
        ),
        (
            SSLError("SSL handshake failed"),
            "ssl_error",
        ),
        (
            ConnectTimeout("Connection timed out"),
            "connect_timeout",
        ),
        (
            ResourceException("400", "status_message", "content"),
            "no_nodes_found",
        ),
        (
            requests.exceptions.ConnectionError("Connection error"),
            "cannot_connect",
        ),
    ],
)
async def test_form_node_exceptions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    exception: Exception,
    reason: str,
) -> None:
    """Test we handle all exceptions."""
    mock_proxmox_client.nodes.get.side_effect = exception
    result = await _start_existing_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_STEP,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_auth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_AUTH_STEP_PASSWORD,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    mock_proxmox_client.nodes.get.side_effect = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_AUTH_STEP_PASSWORD
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (
            ResourceException("404", "status_message", "content"),
            "no_vmlxc_found",
        ),
        (
            requests.exceptions.ConnectionError("Connection error"),
            "cannot_connect",
        ),
    ],
)
async def test_form_exceptions_qemu(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    exception: Exception,
    reason: str,
) -> None:
    """Test we handle all exceptions."""
    mock_proxmox_client.nodes.get.return_value = [{"node": "pve1", "status": "online"}]
    node_resource = mock_proxmox_client.nodes.return_value
    node_resource.qemu.get.side_effect = exception
    result = await _start_existing_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_STEP,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_auth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_AUTH_STEP_PASSWORD,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    node_resource.qemu.get.side_effect = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_AUTH_STEP_PASSWORD
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_form_no_nodes_exception(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
) -> None:
    """Test we handle no nodes found exception."""
    result = await _start_existing_credentials(hass)

    mock_proxmox_client.nodes.get.side_effect = ResourceException(
        "404", "status_message", "content"
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_STEP
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_auth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_AUTH_STEP_PASSWORD
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_nodes_found"}

    mock_proxmox_client.nodes.get.side_effect = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_AUTH_STEP_PASSWORD
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_form_no_nodes_empty_list(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
) -> None:
    """Test we handle no nodes found exception when empty list is returned."""
    result = await _start_existing_credentials(hass)

    mock_proxmox_client.nodes.get.return_value = []

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_STEP
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_auth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_AUTH_STEP_PASSWORD
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_nodes_found"}


@pytest.mark.usefixtures("mock_setup_entry")
async def test_duplicate_entry(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test we handle duplicate entries."""
    mock_config_entry.add_to_hass(hass)

    result = await _start_existing_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_STEP
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_auth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_AUTH_STEP_PASSWORD
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def sanitize_config_entry(data: dict[str, Any]) -> dict[str, Any]:
    """Sanitize config entry data by removing unused auth keys."""
    # Ignore unused keys (i.e. when switching from password to token or vice versa)
    # as we cannot unset them in the config entry, but the flow should still succeed
    unused_auth_keys = [CONF_TOKEN_ID, CONF_TOKEN_SECRET]
    if data[CONF_TOKEN]:
        unused_auth_keys = [CONF_PASSWORD]
    return {
        k: v for k, v in data.items() if v is not None and k not in unused_auth_keys
    }


@pytest.mark.parametrize(
    ("mock_user_step", "mock_user_auth_step", "mock_test_config"),
    [
        (MOCK_USER_STEP, MOCK_USER_AUTH_STEP_PASSWORD, MOCK_TEST_CONFIG),
        (MOCK_USER_STEP_TOKEN, MOCK_USER_AUTH_STEP_TOKEN, MOCK_TEST_TOKEN_CONFIG),
        (MOCK_USER_STEP_OTHER, MOCK_USER_AUTH_STEP_OTHER, MOCK_TEST_OTHER_CONFIG),
        (
            MOCK_USER_STEP_OTHER_TOKEN,
            MOCK_USER_AUTH_STEP_OTHER_TOKEN,
            MOCK_TEST_TOKEN_OTHER_CONFIG,
        ),
        (
            MOCK_USER_STEP_TOKEN,
            MOCK_USER_AUTH_STEP_TOKEN_FULL_ID,
            MOCK_TEST_TOKEN_CONFIG,
        ),
    ],
)
@pytest.mark.usefixtures("mock_setup_entry")
async def test_full_flow_reconfigure(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    mock_user_step: dict[str, Any],
    mock_user_auth_step: dict[str, Any],
    mock_test_config: dict[str, Any],
) -> None:
    """Test the full flow of the config flow."""
    mock_config_entry.add_to_hass(hass)
    result = await _start_advanced_reconfigure(hass, mock_config_entry)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=mock_user_step,
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=mock_user_auth_step,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    sanitized = sanitize_config_entry(mock_config_entry.data)
    assert sanitized == mock_test_config


async def test_guided_reconfigure_reenroll_updates_every_field_atomically(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
) -> None:
    """A validated enrollment replaces endpoint, auth, SSH, and node together."""
    entry = _guided_config_entry()
    object.__setattr__(entry, "data", {**entry.data, "enrollment": "stale-raw-blob"})
    entry.add_to_hass(hass)
    old_data = dict(entry.data)
    result = await _start_guided_reconfigure(
        hass, entry, host="192.0.2.55"
    )
    value, private_key, host_key = _enrollment()

    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe",
        return_value=PackageHelperProbe(node="pve1", helper_version=2),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": value}
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data != old_data
    assert {
        key: entry.data[key]
        for key in (
            CONF_AUTH_METHOD,
            CONF_REALM,
            CONF_USERNAME,
            CONF_HOST,
            CONF_PORT,
            CONF_VERIFY_SSL,
            CONF_TOKEN,
            CONF_TOKEN_ID,
            CONF_TOKEN_SECRET,
            CONF_SSH_PRIVATE_KEY,
            CONF_SSH_HOST_KEY,
            CONF_PACKAGE_NODE,
            CONF_NODES,
        )
    } == {
        CONF_AUTH_METHOD: "pve",
        CONF_REALM: "pve",
        CONF_USERNAME: "hubinetnext@pve",
        CONF_HOST: "192.0.2.55",
        CONF_PORT: 8006,
        CONF_VERIFY_SSL: True,
        CONF_TOKEN: True,
        CONF_TOKEN_ID: "ha",
        CONF_TOKEN_SECRET: "guided-token-secret",
        CONF_SSH_PRIVATE_KEY: private_key,
        CONF_SSH_HOST_KEY: host_key,
        CONF_PACKAGE_NODE: "pve1",
        CONF_NODES: MOCK_TEST_CONFIG[CONF_NODES],
    }
    assert CONF_PASSWORD not in entry.data
    assert "enrollment" not in entry.data
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(
    ("enrollment_value", "probe_error", "probe_node", "reason"),
    [
        ("not-an-enrollment", None, "pve1", "invalid_enrollment_prefix"),
        (None, PackageTransportAuthenticationError(), "pve1", "ssh_auth_failed"),
        (None, PackageTransportHostKeyError(), "pve1", "ssh_host_key_mismatch"),
        (None, None, "other-node", "package_node_not_found"),
    ],
)
async def test_guided_reenroll_failure_leaves_entry_exactly_unchanged(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
    enrollment_value: str | None,
    probe_error: Exception | None,
    probe_node: str,
    reason: str,
) -> None:
    """No guided field is partially written when any validation check fails."""
    entry = _guided_config_entry()
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await _start_guided_reconfigure(hass, entry)
    value = enrollment_value or _enrollment()[0]

    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe",
        side_effect=probe_error,
        return_value=PackageHelperProbe(node=probe_node, helper_version=2),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": value}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}
    assert entry.data == before
    assert len(mock_setup_entry.mock_calls) == 0


async def test_guided_reenroll_api_failure_leaves_entry_unchanged(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
) -> None:
    """API validation failure happens before SSH and before atomic replacement."""
    entry = _guided_config_entry()
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await _start_guided_reconfigure(hass, entry)
    mock_proxmox_client._mock_api_cf.side_effect = AuthenticationError("bad token")

    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe"
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": _enrollment()[0]}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data == before
    probe.assert_not_called()
    assert len(mock_setup_entry.mock_calls) == 0


async def test_guided_reauth_uses_enrollment_and_atomically_reloads(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
) -> None:
    """Guided API reauth never asks for a separately exposed token secret."""
    entry = _guided_config_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_enrollment"
    assert set(result["data_schema"].schema) == {"enrollment"}
    assert "--reset" in result["description_placeholders"]["bootstrap_command"]

    value, private_key, host_key = _enrollment()
    with patch(
        "custom_components.hubinet_ops.config_flow."
        "AsyncSSHPackageTransport.async_probe",
        return_value=PackageHelperProbe(node="pve1", helper_version=2),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enrollment": value}
        )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN_SECRET] == "guided-token-secret"
    assert entry.data[CONF_SSH_PRIVATE_KEY] == private_key
    assert entry.data[CONF_SSH_HOST_KEY] == host_key
    assert "enrollment" not in entry.data
    assert len(mock_setup_entry.mock_calls) == 1


async def test_advanced_entry_with_legacy_trust_keeps_upstream_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Migrated SSH trust does not misclassify an advanced API identity."""
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    object.__setattr__(
        mock_config_entry,
        "data",
        {
            **mock_config_entry.data,
            CONF_SSH_PRIVATE_KEY: private_key.export_private_key().decode(),
            CONF_SSH_HOST_KEY: host_key.export_public_key().decode().strip(),
            CONF_PACKAGE_NODE: "pve1",
        },
    )
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert {str(key) for key in result["data_schema"].schema} == {CONF_PASSWORD}


async def test_advanced_reconfigure_host_change_clears_stale_package_trust(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
) -> None:
    """Advanced endpoint changes cannot retain trust bound to the old host."""
    entry = _guided_config_entry()
    entry.add_to_hass(hass)
    result = await _start_advanced_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **MOCK_USER_STEP,
            CONF_HOST: "192.0.2.88",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], MOCK_USER_AUTH_STEP_PASSWORD
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.0.2.88"
    assert CONF_SSH_PRIVATE_KEY not in entry.data
    assert CONF_SSH_HOST_KEY not in entry.data
    assert CONF_PACKAGE_NODE not in entry.data
    assert len(mock_setup_entry.mock_calls) == 1


async def test_full_flow_reconfigure_match_entries(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the full flow of the config flow, this time matching existing entries."""
    mock_config_entry.add_to_hass(hass)

    # Adding a second entry with a different host, since configuring
    # the same host should work
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Second ProxmoxVE",
        data={
            **MOCK_TEST_CONFIG,
            CONF_HOST: "192.168.1.1",
        },
    )
    second_entry.add_to_hass(hass)

    result = await _start_advanced_reconfigure(hass, mock_config_entry)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            **MOCK_USER_STEP,
            CONF_HOST: "192.168.1.1",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    sanitized = sanitize_config_entry(mock_config_entry.data)
    assert sanitized == MOCK_TEST_CONFIG
    assert len(mock_setup_entry.mock_calls) == 0


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (
            AuthenticationError("Invalid credentials"),
            "invalid_auth",
        ),
        (
            SSLError("SSL handshake failed"),
            "ssl_error",
        ),
        (
            ConnectTimeout("Connection timed out"),
            "connect_timeout",
        ),
        (
            ResourceException("404", "status_message", "content"),
            "no_nodes_found",
        ),
        (
            requests.exceptions.ConnectionError("Connection error"),
            "cannot_connect",
        ),
    ],
)
@pytest.mark.usefixtures("mock_setup_entry")
async def test_full_flow_reconfigure_exceptions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    exception: Exception,
    reason: str,
) -> None:
    """Test the full flow of the config flow."""
    mock_config_entry.add_to_hass(hass)
    result = await _start_advanced_reconfigure(hass, mock_config_entry)

    mock_proxmox_client.nodes.get.side_effect = exception
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_STEP,
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_AUTH_STEP_PASSWORD,
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    mock_proxmox_client.nodes.get.side_effect = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_AUTH_STEP_PASSWORD,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    sanitized = sanitize_config_entry(mock_config_entry.data)
    assert sanitized == MOCK_TEST_CONFIG


async def test_full_flow_reauth(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Test the full flow of the config flow."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    # There is no user input
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_PASSWORD: "new_password"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new_password"
    assert len(mock_setup_entry.mock_calls) == 1


async def test_full_flow_reauth_token_other(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
    mock_config_entry_token_other: MockConfigEntry,
) -> None:
    """Test the full flow of the config flow."""
    mock_config_entry_token_other.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await mock_config_entry_token_other.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    # There is no user input
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_REALM: "Test_Realm",
            CONF_TOKEN_ID: "test_token_id",
            CONF_TOKEN_SECRET: "new_token_secret",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry_token_other.data[CONF_TOKEN_SECRET] == "new_token_secret"
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(
    ("exception", "reason"),
    [
        (
            AuthenticationError("Invalid credentials"),
            "invalid_auth",
        ),
        (
            SSLError("SSL handshake failed"),
            "ssl_error",
        ),
        (
            ConnectTimeout("Connection timed out"),
            "connect_timeout",
        ),
        (
            ResourceException("404", "status_message", "content"),
            "no_nodes_found",
        ),
        (
            requests.exceptions.ConnectionError("Connection error"),
            "cannot_connect",
        ),
    ],
)
async def test_full_flow_reauth_exceptions(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
    mock_setup_entry: MagicMock,
    mock_config_entry: MockConfigEntry,
    exception: Exception,
    reason: str,
) -> None:
    """Test we handle all exceptions in the reauth flow."""
    mock_config_entry.add_to_hass(hass)

    mock_proxmox_client.nodes.get.side_effect = exception

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_PASSWORD: "new_password"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    # Now test that we can recover from the error
    mock_proxmox_client.nodes.get.side_effect = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_PASSWORD: "new_password"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == "new_password"
    assert len(mock_setup_entry.mock_calls) == 1


async def test_form_offline_node_skipped(
    hass: HomeAssistant,
    mock_proxmox_client: MagicMock,
) -> None:
    """Test that offline nodes are skipped during config flow."""
    mock_proxmox_client.nodes.get.return_value = mock_proxmox_client._all_nodes

    result = await _start_existing_credentials(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_STEP
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_auth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_AUTH_STEP_PASSWORD
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    nodes_in_result = [node[CONF_NODE] for node in result["data"][CONF_NODES]]
    assert "pve3" not in nodes_in_result
    assert "pve1" in nodes_in_result
    assert "pve2" in nodes_in_result
