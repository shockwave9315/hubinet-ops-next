"""Config flow for Proxmox VE integration."""

from collections.abc import Mapping
import logging
from typing import Any, override

from proxmoxer import AuthenticationError, ProxmoxAPI
from proxmoxer.core import ResourceException
import requests
from requests.exceptions import ConnectTimeout, SSLError
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .common import sanitize_config_entry
from .const import (
    AUTH_METHODS,
    AUTH_OTHER,
    AUTH_PVE,
    CONF_AUTH_METHOD,
    CONF_CONTAINERS,
    CONF_NODE,
    CONF_NODES,
    CONF_PACKAGE_NODE,
    CONF_REALM,
    CONF_SSH_HOST_KEY,
    CONF_SSH_PRIVATE_KEY,
    CONF_TOKEN_ID,
    CONF_TOKEN_SECRET,
    CONF_VMS,
    DEFAULT_PORT,
    DEFAULT_REALM,
    DEFAULT_TIMEOUT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    GUIDED_TOKEN_ID,
    GUIDED_USERNAME,
    INTEGRATION_VERSION,
    NODE_ONLINE,
)
from .enrollment import EnrollmentError, parse_enrollment
from .packages.transport import (
    AsyncSSHPackageTransport,
    PackageHelperProtocolError,
    PackageHelperUnavailableError,
    PackageTransportAuthenticationError,
    PackageTransportConnectionError,
    PackageTransportHostKeyError,
)

_LOGGER = logging.getLogger(__name__)
BASE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTH_METHOD, default=DEFAULT_REALM): SelectSelector(
            SelectSelectorConfig(
                options=AUTH_METHODS,
                translation_key=CONF_AUTH_METHOD,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
        ),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Required(CONF_TOKEN, default=False): cv.boolean,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): cv.boolean,
    }
)

PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD,
                autocomplete="current-password",
            )
        ),
    }
)
TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN_ID): cv.string,
        vol.Required(CONF_TOKEN_SECRET): cv.string,
    }
)
GUIDED_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): cv.boolean,
    }
)
CONF_ENROLLMENT = "enrollment"
ENROLLMENT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENROLLMENT): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)
BOOTSTRAP_COMMAND = (
    "curl -fsSL "
    "https://raw.githubusercontent.com/shockwave9315/hubinet-ops-next/"
    f"{INTEGRATION_VERSION}/deploy/bootstrap-proxmox.sh | bash"
)
RESET_BOOTSTRAP_COMMAND = f"{BOOTSTRAP_COMMAND} -s -- --reset"

_GUIDED_DATA_KEYS = (
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
    CONF_ENROLLMENT,
)


def _is_guided_entry(data: Mapping[str, Any]) -> bool:
    """Return whether an entry uses the fixed guided API identity."""
    return (
        data.get(CONF_USERNAME) == GUIDED_USERNAME
        and data.get(CONF_TOKEN_ID) == GUIDED_TOKEN_ID
        and data.get(CONF_TOKEN) is True
    )


def _get_nodes_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the user input and fetch data (sync, for executor)."""
    auth_kwargs = (
        {
            "token_name": data[CONF_TOKEN_ID],
            "token_value": data[CONF_TOKEN_SECRET],
        }
        if data.get(CONF_TOKEN)
        else {"password": data.get(CONF_PASSWORD)}
    )
    data = sanitize_config_entry(data)
    try:
        client = ProxmoxAPI(
            host=data[CONF_HOST],
            port=data[CONF_PORT],
            user=data[CONF_USERNAME],
            verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            timeout=DEFAULT_TIMEOUT,
            **auth_kwargs,
        )
    except AuthenticationError as err:
        raise ProxmoxAuthenticationError from err
    except SSLError as err:
        raise ProxmoxSSLError from err
    except ConnectTimeout as err:
        raise ProxmoxConnectTimeout from err
    except ResourceException as err:
        _LOGGER.debug("Error during Proxmox client initialisation", exc_info=True)
        raise ProxmoxInitFailed from err
    except requests.exceptions.ConnectionError as err:
        raise ProxmoxConnectionError from err

    try:
        nodes = client.nodes.get()
    except AuthenticationError as err:
        raise ProxmoxAuthenticationError from err
    except SSLError as err:
        raise ProxmoxSSLError from err
    except ConnectTimeout as err:
        raise ProxmoxConnectTimeout from err
    except ResourceException as err:
        _LOGGER.debug("Error fetching nodes", exc_info=True)
        raise ProxmoxNoNodesFound from err
    except requests.exceptions.ConnectionError as err:
        raise ProxmoxConnectionError from err

    if not nodes:
        raise ProxmoxNoNodesFound(
            translation_domain=DOMAIN, translation_key="no_nodes_found"
        )

    nodes_data: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("status") != NODE_ONLINE:
            _LOGGER.debug(
                "Node %s is offline, skipping VM/container fetch",
                node["node"],
            )
            continue
        try:
            vms = client.nodes(node["node"]).qemu.get()
            containers = client.nodes(node["node"]).lxc.get()
        except ResourceException as err:
            _LOGGER.debug(
                "Error fetching VMs/LXC for node %s", node["node"], exc_info=True
            )
            raise ProxmoxNoVMLXCFound from err
        except requests.exceptions.ConnectionError as err:
            raise ProxmoxConnectionError from err

        nodes_data.append(
            {
                CONF_NODE: node["node"],
                CONF_VMS: [int(vm["vmid"]) for vm in vms],
                CONF_CONTAINERS: [int(container["vmid"]) for container in containers],
            }
        )

    _LOGGER.debug("Nodes with data: %s", nodes_data)
    return nodes_data


class ProxmoxveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Proxmox VE."""

    VERSION = 4
    _data: dict[str, Any] = {}
    _entry: ConfigEntry

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        return self.async_show_menu(
            step_id="user", menu_options=["guided", "existing_credentials"]
        )

    async def async_step_existing_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Preserve the upstream-compatible manual credential path."""
        if user_input is not None:
            self._data = user_input
            return await self.async_step_user_auth()

        return self.async_show_form(
            step_id="existing_credentials",
            data_schema=BASE_SCHEMA,
        )

    async def async_step_guided(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect only the endpoint settings needed before bootstrap."""
        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            self._data = {
                **user_input,
                CONF_AUTH_METHOD: AUTH_PVE,
                CONF_REALM: AUTH_PVE,
                CONF_USERNAME: GUIDED_USERNAME,
                CONF_TOKEN: True,
                CONF_TOKEN_ID: GUIDED_TOKEN_ID,
            }
            return await self.async_step_enrollment()

        return self.async_show_form(step_id="guided", data_schema=GUIDED_SCHEMA)

    async def async_step_enrollment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate the temporary enrollment and create one ready entry."""
        final_data, errors = await self._async_validate_enrollment(user_input)
        if final_data is not None:
            return self.async_create_entry(
                title=final_data[CONF_HOST], data=final_data
            )

        return self.async_show_form(
            step_id="enrollment",
            data_schema=ENROLLMENT_SCHEMA,
            errors=errors,
            description_placeholders={"bootstrap_command": BOOTSTRAP_COMMAND},
        )

    async def async_step_user_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the auth step."""
        errors: dict[str, str] = {}
        proxmox_nodes: list[dict[str, Any]] = []

        if user_input is not None:
            self._data = sanitize_config_entry({**self._data, **user_input})
            self._async_abort_entries_match({CONF_HOST: self._data[CONF_HOST]})
            proxmox_nodes, errors = await self._validate_input(self._data)

            if not errors:
                return self.async_create_entry(
                    title=self._data[CONF_HOST],
                    data={**self._data, CONF_NODES: proxmox_nodes},
                )

        return self.async_show_form(
            step_id="user_auth",
            data_schema=self._get_auth_schema(self._data),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth when Proxmox VE authentication fails."""
        self._entry = self._get_reauth_entry()
        if _is_guided_entry(self._entry.data):
            self._data = dict(self._entry.data)
            return await self.async_step_reauth_enrollment()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_enrollment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace a guided entry's credentials from one new enrollment."""
        self._entry = self._get_reauth_entry()
        self._data = dict(self._entry.data)
        return await self._async_existing_enrollment_step(
            "reauth_enrollment", user_input
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth: ask for updated credentials and validate."""
        errors: dict[str, str] = {}
        self._entry = self._get_reauth_entry()
        if user_input is not None:
            merged_data = {**self._entry.data, **user_input}
            _, errors = await self._validate_input(merged_data)
            if not errors:
                return self.async_update_reload_and_abort(
                    self._entry,
                    data_updates=self._get_auth_updates(merged_data),
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._get_auth_schema(self._entry.data),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial reconfiguration step."""
        self._entry = self._get_reconfigure_entry()
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=[
                "reconfigure_guided",
                "reconfigure_existing_credentials",
            ],
        )

    async def async_step_reconfigure_guided(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the endpoint before rotating guided credentials."""
        self._entry = self._get_reconfigure_entry()
        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            self._data = {
                **user_input,
                CONF_AUTH_METHOD: AUTH_PVE,
                CONF_REALM: AUTH_PVE,
                CONF_USERNAME: GUIDED_USERNAME,
                CONF_TOKEN: True,
                CONF_TOKEN_ID: GUIDED_TOKEN_ID,
            }
            return await self.async_step_reconfigure_enrollment()

        return self.async_show_form(
            step_id="reconfigure_guided",
            data_schema=self.add_suggested_values_to_schema(
                data_schema=GUIDED_SCHEMA,
                suggested_values=self._entry.data,
            ),
        )

    async def async_step_reconfigure_enrollment(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Atomically apply a valid guided re-enrollment."""
        self._entry = self._get_reconfigure_entry()
        return await self._async_existing_enrollment_step(
            "reconfigure_enrollment", user_input
        )

    async def async_step_reconfigure_existing_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Retain the upstream-compatible credential reconfiguration path."""
        self._entry = self._get_reconfigure_entry()
        suggested_values = {
            CONF_AUTH_METHOD: self._entry.data.get(
                CONF_AUTH_METHOD, self._entry.data.get(CONF_REALM, DEFAULT_REALM)
            ),
            CONF_HOST: self._entry.data[CONF_HOST],
            CONF_USERNAME: self._entry.data[CONF_USERNAME].split("@")[0],
            CONF_PORT: self._entry.data[CONF_PORT],
            CONF_VERIFY_SSL: self._entry.data[CONF_VERIFY_SSL],
            CONF_TOKEN: self._entry.data.get(CONF_TOKEN, False),
            CONF_TOKEN_ID: self._entry.data.get(CONF_TOKEN_ID),
            CONF_REALM: self._entry.data[CONF_REALM],
        }
        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            self._data = sanitize_config_entry({**self._entry.data, **user_input})
            return await self.async_step_reconfigure_auth()

        return self.async_show_form(
            step_id="reconfigure_existing_credentials",
            data_schema=self.add_suggested_values_to_schema(
                data_schema=BASE_SCHEMA,
                suggested_values=self._data or suggested_values,
            ),
        )

    async def async_step_reconfigure_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of the integration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: self._data[CONF_HOST]})
            self._data = sanitize_config_entry({**self._data, **user_input})
            proxmox_nodes, errors = await self._validate_input(self._data)
            data_kwargs = {CONF_PASSWORD: self._data.get(CONF_PASSWORD)}
            if self._data[CONF_TOKEN]:
                data_kwargs = {
                    CONF_TOKEN_ID: self._data[CONF_TOKEN_ID],
                    CONF_TOKEN_SECRET: self._data[CONF_TOKEN_SECRET],
                }
            if not errors:
                updated_data = {
                    **self._entry.data,
                    CONF_AUTH_METHOD: self._data[CONF_AUTH_METHOD],
                    CONF_HOST: self._data[CONF_HOST],
                    CONF_USERNAME: self._data[CONF_USERNAME],
                    CONF_PORT: self._data[CONF_PORT],
                    CONF_VERIFY_SSL: self._data[CONF_VERIFY_SSL],
                    CONF_TOKEN: self._data[CONF_TOKEN],
                    CONF_REALM: self._data[CONF_REALM],
                    CONF_NODES: proxmox_nodes,
                    **data_kwargs,
                }
                if self._data[CONF_TOKEN]:
                    updated_data.pop(CONF_PASSWORD, None)
                else:
                    updated_data.pop(CONF_TOKEN_ID, None)
                    updated_data.pop(CONF_TOKEN_SECRET, None)
                if self._data[CONF_HOST] != self._entry.data[CONF_HOST]:
                    for key in (
                        CONF_SSH_PRIVATE_KEY,
                        CONF_SSH_HOST_KEY,
                        CONF_PACKAGE_NODE,
                    ):
                        updated_data.pop(key, None)
                return self.async_update_reload_and_abort(
                    self._entry,
                    data=updated_data,
                    title=self._data[CONF_HOST],
                )

        return self.async_show_form(
            step_id="reconfigure_auth",
            data_schema=self.add_suggested_values_to_schema(
                data_schema=self._get_auth_schema(self._data),
                suggested_values=sanitize_config_entry(self._data),
            ),
            errors=errors,
        )

    async def _async_existing_enrollment_step(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Validate and atomically apply enrollment to an existing entry."""
        final_data, errors = await self._async_validate_enrollment(user_input)
        if final_data is not None:
            updated_data = dict(self._entry.data)
            for key in (*_GUIDED_DATA_KEYS, CONF_PASSWORD):
                updated_data.pop(key, None)
            updated_data.update(final_data)
            return self.async_update_reload_and_abort(
                self._entry,
                data=updated_data,
                title=final_data[CONF_HOST],
            )

        return self.async_show_form(
            step_id=step_id,
            data_schema=ENROLLMENT_SCHEMA,
            errors=errors,
            description_placeholders={
                "bootstrap_command": RESET_BOOTSTRAP_COMMAND
            },
        )

    async def _async_validate_enrollment(
        self, user_input: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Validate enrollment, API, pinned SSH, helper, and node agreement."""
        errors: dict[str, str] = {}
        if user_input is None:
            return None, errors
        try:
            enrollment = parse_enrollment(user_input[CONF_ENROLLMENT])
        except EnrollmentError as err:
            errors["base"] = err.code
            return None, errors

        final_data = {
            **{
                key: value
                for key, value in self._data.items()
                if key not in (CONF_PASSWORD, CONF_ENROLLMENT)
            },
            CONF_TOKEN_SECRET: enrollment.token_secret,
            CONF_SSH_PRIVATE_KEY: enrollment.private_key,
            CONF_SSH_HOST_KEY: enrollment.host_key,
        }
        proxmox_nodes, errors = await self._validate_input(final_data)
        if errors:
            return None, errors
        try:
            transport = AsyncSSHPackageTransport(
                endpoint=final_data[CONF_HOST],
                private_key=enrollment.private_key,
                host_key=enrollment.host_key,
            )
            probe = await transport.async_probe()
        except ValueError:
            errors["base"] = "ssh_cannot_connect"
        except PackageTransportHostKeyError:
            errors["base"] = "ssh_host_key_mismatch"
        except PackageTransportAuthenticationError:
            errors["base"] = "ssh_auth_failed"
        except PackageTransportConnectionError:
            errors["base"] = "ssh_cannot_connect"
        except PackageHelperUnavailableError:
            errors["base"] = "helper_missing_or_outdated"
        except PackageHelperProtocolError:
            errors["base"] = "helper_protocol_mismatch"
        else:
            if probe.node not in {node[CONF_NODE] for node in proxmox_nodes}:
                errors["base"] = "package_node_not_found"
            else:
                return {
                    **final_data,
                    CONF_NODES: proxmox_nodes,
                    CONF_PACKAGE_NODE: probe.node,
                }, errors
        return None, errors

    async def _validate_input(
        self, user_input: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Validate the user input. Return nodes data and/or errors."""
        errors: dict[str, str] = {}
        proxmox_nodes: list[dict[str, Any]] = []
        err: ProxmoxError | None = None
        try:
            proxmox_nodes = await self.hass.async_add_executor_job(
                _get_nodes_data, user_input
            )
        except ProxmoxConnectTimeout as exc:
            errors["base"] = "connect_timeout"
            err = exc
        except ProxmoxAuthenticationError as exc:
            errors["base"] = "invalid_auth"
            err = exc
        except ProxmoxSSLError as exc:
            errors["base"] = "ssl_error"
            err = exc
        except ProxmoxInitFailed as exc:
            errors["base"] = "api_error_no_details"
            err = exc
        except ProxmoxNoNodesFound as exc:
            errors["base"] = "no_nodes_found"
            err = exc
        except ProxmoxNoVMLXCFound as exc:
            errors["base"] = "no_vmlxc_found"
            err = exc
        except ProxmoxConnectionError as exc:
            errors["base"] = "cannot_connect"
            err = exc

        if err is not None:
            _LOGGER.debug("Error: %s: %s", errors["base"], err)

        return proxmox_nodes, errors

    def _get_auth_schema(
        self,
        data: Mapping[str, Any],
    ) -> vol.Schema:
        """Return the auth schema based on the flow data."""
        schema = PASSWORD_SCHEMA
        if data.get(CONF_TOKEN):
            schema = TOKEN_SCHEMA
        if data.get(CONF_AUTH_METHOD) == AUTH_OTHER:
            schema = schema.extend({vol.Required(CONF_REALM): cv.string})
        return schema

    def _get_auth_updates(
        self,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the auth updates based on the flow data."""
        updates = {CONF_PASSWORD: data.get(CONF_PASSWORD)}
        if data.get(CONF_TOKEN):
            updates = {
                CONF_TOKEN_ID: data[CONF_TOKEN_ID],
                CONF_TOKEN_SECRET: data[CONF_TOKEN_SECRET],
            }
        if data.get(CONF_AUTH_METHOD) == AUTH_OTHER:
            updates[CONF_REALM] = data.get(CONF_REALM, DEFAULT_REALM)
        return updates


class ProxmoxError(HomeAssistantError):
    """Base class for Proxmox VE errors."""


class ProxmoxNoNodesFound(ProxmoxError):
    """Error to indicate no nodes found."""


class ProxmoxNoVMLXCFound(ProxmoxError):
    """Error to indicate no LXC or VM found."""


class ProxmoxInitFailed(ProxmoxError):
    """Error to indicate API initialisation failure."""


class ProxmoxConnectTimeout(ProxmoxError):
    """Error to indicate a connection timeout."""


class ProxmoxSSLError(ProxmoxError):
    """Error to indicate an SSL error."""


class ProxmoxAuthenticationError(ProxmoxError):
    """Error to indicate an authentication error."""


class ProxmoxConnectionError(ProxmoxError):
    """Error to indicate a connection error."""
