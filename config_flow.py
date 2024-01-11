"""Config flow for MotionBlinds BLE integration."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ADDRESS,
    CONF_BLIND_TYPE,
    CONF_LOCAL_NAME,
    CONF_MAC_CODE,
    DOMAIN,
    ERROR_ALREADY_CONFIGURED,
    ERROR_COULD_NOT_FIND_MOTOR,
    ERROR_INVALID_MAC_CODE,
    ERROR_NO_BLUETOOTH_ADAPTER,
    ERROR_NO_DEVICES_FOUND,
    MotionBlindType,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_MAC_CODE): str})


def is_valid_mac(data: str) -> bool:
    """Validate the provided MAC address."""

    mac_regex = r"^[0-9A-Fa-f]{4}$"
    return bool(re.match(mac_regex, data))


def get_mac_from_local_name(data: str) -> str:
    """Gets the MAC address from the bluetooth local name."""

    mac_regex = r"^MOTION_([0-9A-Fa-f]{4})$"
    match = re.search(mac_regex, data)
    return match.group(1) if match else None


class FlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MotionBlinds BLE."""

    VERSION = 1

    _discovery_info: BluetoothServiceInfoBleak | BLEDevice = None
    _mac_code: str = None
    _display_name: str = None
    _blind_type: MotionBlindType = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        _LOGGER.debug(
            "Discovered Motion bluetooth device: %s", discovery_info.as_dict()
        )
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._mac_code = get_mac_from_local_name(discovery_info.name)
        self._display_name = f"MotionBlind {self._mac_code}"
        self.context["local_name"] = discovery_info.name
        self.context["title_placeholders"] = {"name": self._display_name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a single device."""
        if user_input is not None:
            self._blind_type = user_input[CONF_BLIND_TYPE]
            return await self._async_create_entry_from_discovery(user_input)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BLIND_TYPE): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                blind_type.value for blind_type in MotionBlindType
                            ],
                            translation_key=CONF_BLIND_TYPE,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            description_placeholders={"display_name": self._display_name},
        )

    async def _async_create_entry_from_discovery(
        self, user_input: dict[str, Any]
    ) -> FlowResult:
        """Create an entry from a discovery."""

        return self.async_create_entry(
            title=self._display_name,
            data={
                CONF_ADDRESS: self._discovery_info.address,
                CONF_LOCAL_NAME: self._discovery_info.name,
                CONF_MAC_CODE: self._mac_code,
                CONF_BLIND_TYPE: self._blind_type,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            mac_code = user_input[CONF_MAC_CODE]
            # Discover with BLE
            try:
                await self.async_discover_motionblind(mac_code)
            except tuple(EXCEPTION_MAP.keys()) as e:
                errors = {
                    "base": EXCEPTION_MAP[type(e)]
                    if type(e) in EXCEPTION_MAP.keys()
                    else type(e)
                }
                return self.async_show_form(
                    step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
                )
            return await self.async_step_confirm()

        # Return and show error
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_discover_motionblind(self, mac_code: str) -> None:
        """Discover MotionBlinds initialized by the user."""
        if not is_valid_mac(mac_code):
            raise InvalidMACCode()

        count = bluetooth.async_scanner_count(self.hass, connectable=True)
        if count == 0:
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_configure(flow_id=self.flow_id)
            )
            _LOGGER.error("Could not find any bluetooth adapter")
            raise NoBluetoothAdapter()

        bleak_scanner = bluetooth.async_get_scanner(self.hass)
        devices = await bleak_scanner.discover()

        if len(devices) == 0:
            raise NoDevicesFound()

        motion_device: BLEDevice = next(
            (
                device
                for device in devices
                if device
                and device.name
                and f"MOTION_{mac_code.upper()}" in device.name
            ),
            None,
        )

        existing_entries = self._async_current_entries()

        if motion_device:
            unique_id = motion_device.address
            if any(entry.unique_id == unique_id for entry in existing_entries):
                _LOGGER.info("Already configured")
                raise AlreadyConfigured()
            await self.async_set_unique_id(unique_id, raise_on_progress=False)
            self._discovery_info = motion_device
            self._mac_code = mac_code.upper()
            self._display_name = f"MotionBlind {self._mac_code}"
        else:
            raise CouldNotFindMotor()


class CouldNotFindMotor(HomeAssistantError):
    """Error to indicate no motor with that MAC code could be found."""


class AlreadyConfigured(HomeAssistantError):
    """Error to indicate the device has already been configured."""


class InvalidMACCode(HomeAssistantError):
    """Error to indicate the MAC code is invalid."""


class NoBluetoothAdapter(HomeAssistantError):
    """Error to indicate no bluetooth adapter could be found."""


class NoDevicesFound(HomeAssistantError):
    """Error to indicate no bluetooth devices could be found."""


EXCEPTION_MAP = {
    NoBluetoothAdapter: ERROR_NO_BLUETOOTH_ADAPTER,
    NoDevicesFound: ERROR_NO_DEVICES_FOUND,
    CouldNotFindMotor: ERROR_COULD_NOT_FIND_MOTOR,
    AlreadyConfigured: ERROR_ALREADY_CONFIGURED,
    InvalidMACCode: ERROR_INVALID_MAC_CODE,
}
