"""Device for MotionBlinds BLE."""
from __future__ import annotations

import logging
from asyncio import (
    FIRST_COMPLETED,
    Future,
    Task,
    TimerHandle,
    create_task,
    get_event_loop,
    sleep,
    wait,
)
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from time import time, time_ns

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

from .const import (
    EXCEPTION_NO_END_POSITIONS,
    EXCEPTION_NO_FAVORITE_POSITION,
    SETTING_DISCONNECT_TIME,
    SETTING_MAX_COMMAND_ATTEMPTS,
    SETTING_MAX_CONNECT_ATTEMPTS,
    SETTING_NOTIFICATION_DELAY,
    MotionCharacteristic,
    MotionCommandType,
    MotionConnectionType,
    MotionNotificationType,
    MotionRunningType,
    MotionSpeedLevel,
)
from .crypt import MotionCrypt

_LOGGER = logging.getLogger(__name__)


def requires_end_positions(func: Callable) -> Callable:
    async def wrapper(self: MotionDevice, *args, **kwargs):
        ignore_end_positions_not_set = kwargs.get("ignore_end_positions_not_set", False)

        if (
            self.end_position_info is not None
            and not self.end_position_info.up
            and not ignore_end_positions_not_set
        ):
            self.refresh_disconnect_timer()
            if callable(self.running_callback):
                self.running_callback(MotionRunningType.STILL)
            raise NoEndPositionsException(
                EXCEPTION_NO_END_POSITIONS.format(device_name=self.device_name)
            )
        return await func(self, *args, **kwargs)

    return wrapper


def requires_favorite_position(func: Callable) -> Callable:
    async def wrapper(self: MotionDevice, *args, **kwargs):
        if (
            self.end_position_info is not None
            and not self.end_position_info.up
            and not self.end_position_info.favorite
        ):
            self.refresh_disconnect_timer()
            if callable(self.running_callback):
                self.running_callback(MotionRunningType.STILL)
            raise NoFavoritePositionException(
                EXCEPTION_NO_FAVORITE_POSITION.format(device_name=self.device_name)
            )
        return await func(self, *args, **kwargs)

    return wrapper


def requires_connection(func: Callable) -> Callable:
    async def wrapper(self: MotionDevice, *args, **kwargs):
        if not await self.connect():
            return False
        return await func(self, *args, **kwargs)

    return wrapper


@dataclass
class MotionPositionInfo:
    up: bool
    down: bool
    favorite: bool

    def __init__(self, end_positions_byte: int, favorite_bytes: int) -> None:
        self.up = bool(end_positions_byte & 0x08)
        self.down = bool(end_positions_byte & 0x04)
        self.favorite = bool(favorite_bytes & 0x8000)

    def update_end_positions(self, end_positions_byte: int):
        self.up = bool(end_positions_byte & 0x08)
        self.down = bool(end_positions_byte & 0x04)


class ConnectionQueue:
    """Class used to ensure the first caller connects, but the last caller's command goes through after connection."""

    _ha_create_task: Callable[[Coroutine], Task] = None
    _connection_task: Task = None
    _last_caller_cancel: Future = None

    def set_ha_create_task(self, ha_create_task: Callable[[Coroutine], Task]) -> None:
        """Set the Home Assistant create_task function."""
        self._ha_create_task = ha_create_task

    def _create_connection_task(
        self, device: MotionDevice, use_notification_delay: bool = False
    ) -> None:
        """Create a connection task."""
        if self._ha_create_task:
            _LOGGER.info("HA connecting")
            self._connection_task = self._ha_create_task(
                target=device.establish_connection(
                    use_notification_delay=use_notification_delay
                )
            )
        else:
            _LOGGER.info("Normal connecting")
            self._connection_task = get_event_loop().create_task(
                device.establish_connection(
                    use_notification_delay=use_notification_delay
                )
            )

    async def wait_for_connection(
        self, device: MotionDevice, use_notification_delay: bool = False
    ) -> bool:
        """Wait for a connection, only return True to the last caller and if connected."""
        if self._connection_task is None:
            _LOGGER.info("First caller connecting")
            self._create_connection_task(
                device, use_notification_delay=use_notification_delay
            )
        else:
            _LOGGER.info("Already connecting, waiting for connection")

        # Cancel the previous caller
        if self._last_caller_cancel:
            self._last_caller_cancel.set_result(True)
        self._last_caller_cancel = Future()

        try:
            done, pending = await wait(
                [self._connection_task, self._last_caller_cancel],
                return_when=FIRST_COMPLETED,
            )
            if self._connection_task in done:
                result = (
                    self._connection_task.result()
                )  # Get the result of the completed connection task
                self._connection_task = None  # Reset the connection task
                _LOGGER.info("Last caller continuing")
                return result
            else:
                _LOGGER.info("Cancelled previous caller")
                return False

        except (BleakOutOfConnectionSlotsError, BleakNotFoundError) as e:
            device.set_connection(MotionConnectionType.DISCONNECTED)
            self._connection_task = None
            raise e

    def cancel(self) -> bool:
        """Cancel the connection task."""
        if self._connection_task is not None:
            self._connection_task.cancel()  # Indicate the connection has failed.
            self._connection_task = None
            return True
        return False


class MotionDevice:
    """Class used to control a MotionBlinds device."""

    device_name: str = None
    device_address: str = None
    end_position_info: MotionPositionInfo = None
    _ble_device: BLEDevice = None
    _current_bleak_client: BleakClient = None
    _connection_type: MotionConnectionType = MotionConnectionType.DISCONNECTED
    _connection_queue: ConnectionQueue = None

    _disconnect_time: int = None
    _disconnect_timer: TimerHandle | Callable = None

    # Callbacks that are used to interface with HA
    _ha_call_later: Callable[[int, Coroutine], Callable] = None

    # Regular callbacks
    _position_callback: Callable[[int, int], None] = None
    running_callback: Callable[[bool], None] = None
    _connection_callback: Callable[[MotionConnectionType], None] = None
    _status_callback: Callable[[int, int, int, MotionSpeedLevel], None] = None

    def __init__(
        self, device_address: str, ble_device: BLEDevice = None, device_name: str = None
    ) -> None:
        self.device_address = device_address
        self.device_name = device_name if device_name is not None else device_address
        self._connection_queue = ConnectionQueue()
        if ble_device:
            self._ble_device = ble_device
        else:
            _LOGGER.info(
                "Could not find BLEDevice, creating new BLEDevice from address"
            )
            self._ble_device = BLEDevice(
                self.device_address, self.device_address, {}, rssi=0
            )

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Set the BLEDevice for this device."""
        self._ble_device = ble_device

    def set_ha_create_task(self, ha_create_task: Callable[[Coroutine], Task]) -> None:
        """Set the create_task function to use."""
        self._connection_queue.set_ha_create_task(ha_create_task)

    def set_ha_call_later(
        self, ha_call_later: Callable[[int, Coroutine], Callable]
    ) -> None:
        """Set the call_later function to use."""
        self._ha_call_later = ha_call_later

    def set_connection(self, connection_type: MotionConnectionType) -> None:
        """Set the connection to a particular connection type."""
        if self._connection_callback:
            self._connection_callback(connection_type)
        self._connection_type = connection_type

    def cancel_disconnect_timer(self) -> None:
        if self._disconnect_timer:
            # Cancel current timer
            if callable(self._disconnect_timer):
                self._disconnect_timer()
            else:
                self._disconnect_timer.cancel()

    def refresh_disconnect_timer(
        self, timeout: int = None, force: bool = False
    ) -> None:
        """Refresh the time before the device is disconnected."""
        timeout = SETTING_DISCONNECT_TIME if timeout is None else timeout
        # Don't refresh if the current disconnect timer has a larger timeout
        new_disconnect_time = time_ns() // 1e6 + timeout * 1e3
        if (
            not force
            and self._disconnect_timer is not None
            and self._disconnect_time > new_disconnect_time
        ):
            return

        self.cancel_disconnect_timer()

        _LOGGER.info(f"Refreshing disconnect timer to {timeout}s")

        async def _disconnect_later(t: datetime = None):
            _LOGGER.info(f"Disconnecting after {timeout}s")
            await self.disconnect()

        self._disconnect_time = new_disconnect_time
        if self._ha_call_later:
            self._disconnect_timer = self._ha_call_later(
                delay=timeout, action=_disconnect_later
            )
        else:
            self._disconnect_timer = get_event_loop().call_later(
                timeout, create_task, _disconnect_later()
            )

    def _notification_callback(
        self, char: BleakGATTCharacteristic, byte_array: bytearray
    ) -> None:
        """Callback called by Bleak when a notification is received."""
        decrypted_message: str = MotionCrypt.decrypt(byte_array.hex())
        decrypted_message_bytes: bytes = byte_array.fromhex(decrypted_message)
        _LOGGER.info("Received message: %s", decrypted_message)

        if (
            decrypted_message.startswith(MotionNotificationType.PERCENT.value)
            and self._position_callback is not None
        ):
            _LOGGER.info("Position notification")
            self.end_position_info.update_end_positions(decrypted_message_bytes[4])
            position_percentage: int = decrypted_message_bytes[6]
            angle: int = decrypted_message_bytes[7]
            angle_percentage = round(100 * angle / 180)
            self._position_callback(
                position_percentage, angle_percentage, self.end_position_info
            )
        elif (
            decrypted_message.startswith(MotionNotificationType.STATUS.value)
            and self._status_callback is not None
        ):
            _LOGGER.info("Updating status")
            position_percentage: int = decrypted_message_bytes[6]
            angle: int = decrypted_message_bytes[7]
            angle_percentage = round(100 * angle / 180)
            battery_percentage: int = decrypted_message_bytes[17]
            self.end_position_info: MotionPositionInfo = MotionPositionInfo(
                decrypted_message_bytes[4],
                int.from_bytes(
                    [decrypted_message_bytes[6], decrypted_message_bytes[7]]
                ),
            )
            try:
                speed_level: MotionSpeedLevel = MotionSpeedLevel(
                    decrypted_message_bytes[12]
                )
            except ValueError:
                speed_level = None
            self._status_callback(
                position_percentage,
                angle_percentage,
                battery_percentage,
                speed_level,
                self.end_position_info,
            )

    def _disconnect_callback(self, client: BleakClient) -> None:
        """Callback called by Bleak when a client disconnects."""
        _LOGGER.info("Device %s disconnected!", self.device_address)
        self.set_connection(MotionConnectionType.DISCONNECTED)
        self._current_bleak_client = None

    async def connect(self, use_notification_delay: bool = False) -> bool:
        """Connect to the device if not connected, return whether or not the motor is ready for a command."""
        if not self.is_connected():
            # Connect if not connected yet and not busy connecting
            return await self._connection_queue.wait_for_connection(
                self, use_notification_delay=use_notification_delay
            )
        else:
            self.refresh_disconnect_timer()
        return True

    async def disconnect(self) -> None:
        """Called by Home Assistant after X time."""
        self.set_connection(MotionConnectionType.DISCONNECTING)
        self.cancel_disconnect_timer()
        if self._connection_queue.cancel():
            _LOGGER.info("Cancelled connecting to %s", self.device_address)
        if self._current_bleak_client is not None:
            _LOGGER.info("Disconnecting %s", self.device_address)
            await self._current_bleak_client.disconnect()
            self._current_bleak_client = None
        self.set_connection(MotionConnectionType.DISCONNECTED)

    async def establish_connection(self, use_notification_delay: bool = False) -> bool:
        """Connect to the device, return whether or not the motor is ready for a command."""
        if self._connection_type is MotionConnectionType.CONNECTING:
            return False

        self.set_connection(MotionConnectionType.CONNECTING)
        _LOGGER.info("Connecting to %s", self.device_address)

        _LOGGER.info("Establishing connection")
        bleak_client = await establish_connection(
            BleakClient,
            self._ble_device,
            self.device_address,
            max_attempts=SETTING_MAX_CONNECT_ATTEMPTS,
        )

        _LOGGER.info("Connected to %s", self.device_address)
        self._current_bleak_client = bleak_client
        self.set_connection(MotionConnectionType.CONNECTED)

        await bleak_client.start_notify(
            str(MotionCharacteristic.NOTIFICATION.value),
            self._notification_callback,
        )

        # Used to initialize
        await self.set_key()

        if use_notification_delay:
            await sleep(SETTING_NOTIFICATION_DELAY)
        # Set the point (used after calibrating Curtain)
        # await self.point_set_query()
        await self.status_query()

        bleak_client.set_disconnected_callback(self._disconnect_callback)
        self.refresh_disconnect_timer()

        return True

    def is_connected(self) -> bool:
        """Return whether or not the device is connected."""
        return (
            self._current_bleak_client is not None
            and self._current_bleak_client.is_connected
        )

    async def _send_command(
        self, command_prefix: str, connection_command: bool = False
    ) -> bool:
        """Write a message to the command characteristic, return whether or not the command was successfully executed."""
        # Command must be generated just before sending due get_time timing
        command = MotionCrypt.encrypt(command_prefix + MotionCrypt.get_time())
        _LOGGER.info("Sending message: %s", MotionCrypt.decrypt(command))
        # response=False to solve Unlikely Error: [org.bluez.Error.Failed] Operation failed with ATT error: 0x0e (Unlikely Error)
        # response=True: 0.20s, response=False: 0.0005s
        number_of_tries = 0
        while number_of_tries < SETTING_MAX_COMMAND_ATTEMPTS:
            try:
                if self._current_bleak_client is not None:
                    a = time()
                    await self._current_bleak_client.write_gatt_char(
                        str(MotionCharacteristic.COMMAND.value),
                        bytes.fromhex(command),
                        response=True,
                    )
                    b = time()
                    _LOGGER.info("Received response in %ss", str(b - a))
                    return True
                else:
                    return False
            except BleakError as e:
                if number_of_tries == SETTING_MAX_COMMAND_ATTEMPTS:
                    await self.disconnect()
                    raise e
                else:
                    _LOGGER.warning(
                        "Error sending message (try %i): %s", number_of_tries, e
                    )
                    number_of_tries += 1

    @requires_connection
    async def user_query(self) -> bool:
        """Send user_query command."""
        command_prefix = str(MotionCommandType.USER_QUERY.value)
        return await self._send_command(command_prefix, connection_command=True)

    @requires_connection
    async def set_key(self) -> bool:
        """Send set_key command."""
        command_prefix = str(MotionCommandType.SET_KEY.value)
        return await self._send_command(command_prefix, connection_command=True)

    @requires_connection
    async def status_query(self) -> bool:
        """Send status_query command."""
        command_prefix = str(MotionCommandType.STATUS_QUERY.value)
        return await self._send_command(command_prefix, connection_command=True)

    @requires_connection
    async def point_set_query(self) -> bool:
        """Send point_set_query command."""
        command_prefix = str(MotionCommandType.POINT_SET_QUERY.value)
        return await self._send_command(command_prefix, connection_command=True)

    @requires_connection
    async def speed(self, speed_level: MotionSpeedLevel) -> bool:
        """Change the speed level of the device."""
        command_prefix = str(MotionCommandType.SPEED.value) + hex(
            int(speed_level.value)
        )[2:].zfill(2)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def percentage(
        self, percentage: int, ignore_end_positions_not_set: bool = False
    ) -> bool:
        """Moves the device to a specific percentage."""
        assert not percentage < 0 and not percentage > 100
        command_prefix = (
            str(MotionCommandType.PERCENT.value) + hex(percentage)[2:].zfill(2) + "00"
        )
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def open(self, ignore_end_positions_not_set: bool = False) -> bool:
        """Open the device."""
        command_prefix = str(MotionCommandType.OPEN.value)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def close(self, ignore_end_positions_not_set: bool = False) -> bool:
        """Close the device."""
        command_prefix = str(MotionCommandType.CLOSE.value)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def stop(self) -> bool:
        """Stop moving the device."""
        command_prefix = str(MotionCommandType.STOP.value)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_favorite_position
    async def favorite(self) -> bool:
        """Move the device to the favorite position."""
        command_prefix = str(MotionCommandType.FAVORITE.value)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def percentage_tilt(
        self, percentage: int, ignore_end_positions_not_set: bool = False
    ) -> bool:
        """Tilt the device to a specific position."""
        angle = round(180 * percentage / 100)
        command_prefix = (
            str(MotionCommandType.ANGLE.value) + "00" + hex(angle)[2:].zfill(2)
        )
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def open_tilt(self, ignore_end_positions_not_set: bool = False) -> bool:
        """Tilt the device open."""
        # Step or fully tilt?
        # command_prefix = str(MotionCommandType.OPEN_TILT.value)
        command_prefix = str(MotionCommandType.ANGLE.value) + "00" + hex(0)[2:].zfill(2)
        return await self._send_command(command_prefix)

    @requires_connection
    @requires_end_positions
    async def close_tilt(self, ignore_end_positions_not_set: bool = False) -> bool:
        """Tilt the device closed."""
        # Step or fully tilt?
        # command_prefix = str(MotionCommandType.CLOSE_TILT.value)
        command_prefix = (
            str(MotionCommandType.ANGLE.value) + "00" + hex(180)[2:].zfill(2)
        )
        return await self._send_command(command_prefix)

    def register_position_callback(self, callback: Callable[[int, int], None]) -> None:
        """Register the callback used to update the position."""
        self._position_callback = callback

    def register_running_callback(self, callback: Callable[[bool], None]) -> None:
        """Register the callback used to update the running type."""
        self.running_callback = callback

    def register_connection_callback(
        self, callback: Callable[[MotionConnectionType], None]
    ) -> None:
        """Register the callback used to update the connection status."""
        self._connection_callback = callback

    def register_status_callback(
        self, callback: Callable[[int, int, int, MotionSpeedLevel], None]
    ) -> None:
        """Register the callback used to update the motor status, e.g. position, tilt and battery percentage."""
        self._status_callback = callback


class NoEndPositionsException(Exception):
    """Exception to indicate the blind's endpositions must be set."""


class NoFavoritePositionException(Exception):
    """Exception to indicate the blind's favorite must be set."""
