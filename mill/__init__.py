"""Library to handle connection with mill."""
# Based on https://pastebin.com/53Nk0wJA and Postman capturing from the app
import asyncio
from base64 import b64encode
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import logging
import random
import string
import time

import aiohttp
import async_timeout
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

API_ENDPOINT_1 = "https://eurouter.ablecloud.cn:9005/zc-account/v1/"
API_ENDPOINT_2 = "https://eurouter.ablecloud.cn:9005/millService/v1/"
API_ENDPOINT_STATS = "https://api.millheat.com/statistics/"
DEFAULT_TIMEOUT = 10
MIN_TIME_BETWEEN_UPDATES = dt.timedelta(seconds=5)
MIN_TIME_BETWEEN_STATS_UPDATES = dt.timedelta(minutes=10)
REQUEST_TIMEOUT = "300"

_LOGGER = logging.getLogger(__name__)


class Mill:
    """Class to comunicate with the Mill api."""

    # pylint: disable=too-many-instance-attributes, too-many-public-methods

    def __init__(self, username, password, timeout=DEFAULT_TIMEOUT, websession=None):
        """Initialize the Mill connection."""
        if websession is None:

            async def _create_session():
                return aiohttp.ClientSession()

            loop = asyncio.get_event_loop()
            self.websession = loop.run_until_complete(_create_session())
        else:
            self.websession = websession
        self._timeout = timeout
        self._username = username
        self._password = password
        self._user_id = None
        self._token = None
        self.rooms = {}
        self.heaters = {}
        self.sensors = {}
        self._throttle_time = None
        self._throttle_all_time = None

        key = (
            b"vO\xe4O\xe0G\xeb|$\x9d\x8375\xd6\x1bl\xca\x96'\x8f"
            b"\x02\x06\xc8\n\xe5\x85/\x81\xd6\x0f\x93\xa0"
        )
        _iv = bytearray([10, 1, 11, 5, 4, 15, 7, 9, 23, 3, 1, 6, 8, 12, 13, 91])
        self._cipher = Cipher(
            algorithms.AES(key), modes.CBC(_iv), backend=default_backend()
        )

    async def connect(self, retry=2):
        """Connect to Mill."""
        # pylint: disable=too-many-return-statements
        url = API_ENDPOINT_1 + "login"
        headers = {
            "Content-Type": "application/x-zc-object",
            "Connection": "Keep-Alive",
            "X-Zc-Major-Domain": "seanywell",
            "X-Zc-Msg-Name": "millService",
            "X-Zc-Sub-Domain": "milltype",
            "X-Zc-Seq-Id": "1",
            "X-Zc-Version": "1",
        }
        payload = {"account": self._username, "password": self._password}
        try:
            with async_timeout.timeout(self._timeout):
                resp = await self.websession.post(
                    url, data=json.dumps(payload), headers=headers
                )
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if retry < 1:
                _LOGGER.error("Error connecting to Mill", exc_info=True)
                return False
            return await self.connect(retry - 1)

        result = await resp.text()
        if '"errorCode":3504' in result:
            _LOGGER.error("Wrong password")
            return False

        if '"errorCode":3501' in result:
            _LOGGER.error("Account does not exist")
            return False

        data = json.loads(result)
        token = data.get("token")
        if token is None:
            _LOGGER.error("No token")
            return False

        user_id = data.get("userId")
        if user_id is None:
            _LOGGER.error("No user id")
            return False

        self._token = token
        self._user_id = user_id
        return True

    async def close_connection(self):
        """Close the Mill connection."""
        await self.websession.close()

    async def request(self, command, payload, retry=3):
        """Request data."""
        # pylint: disable=too-many-return-statements

        if self._token is None:
            _LOGGER.error("No token")
            return None

        _LOGGER.debug(command, payload)

        nonce = "".join(
            random.choice(string.ascii_uppercase + string.digits) for _ in range(16)
        )
        url = API_ENDPOINT_2 + command
        timestamp = int(time.time())
        signature = hashlib.sha1(
            str(REQUEST_TIMEOUT + str(timestamp) + nonce + self._token).encode("utf-8")
        ).hexdigest()

        headers = {
            "Content-Type": "application/x-zc-object",
            "Connection": "Keep-Alive",
            "X-Zc-Major-Domain": "seanywell",
            "X-Zc-Msg-Name": "millService",
            "X-Zc-Sub-Domain": "milltype",
            "X-Zc-Seq-Id": "1",
            "X-Zc-Version": "1",
            "X-Zc-Timestamp": str(timestamp),
            "X-Zc-Timeout": REQUEST_TIMEOUT,
            "X-Zc-Nonce": nonce,
            "X-Zc-User-Id": str(self._user_id),
            "X-Zc-User-Signature": signature,
            "X-Zc-Content-Length": str(len(payload)),
        }
        try:
            with async_timeout.timeout(self._timeout):
                resp = await self.websession.post(
                    url, data=json.dumps(payload), headers=headers
                )
        except asyncio.TimeoutError:
            if retry < 1:
                _LOGGER.error("Timed out sending command to Mill: %s", command)
                return None
            return await self.request(command, payload, retry - 1)
        except aiohttp.ClientError:
            _LOGGER.error("Error sending command to Mill: %s", command, exc_info=True)
            return None

        result = await resp.text()

        _LOGGER.debug(result)

        if not result or result == '{"errorCode":0}':
            return None

        if "access token expire" in result or "invalid signature" in result:
            if retry < 1:
                return None
            if not await self.connect():
                return None
            return await self.request(command, payload, retry - 1)

        if '"error":"device offline"' in result:
            if retry < 1:
                _LOGGER.error("Failed to send request, %s", result)
                return None
            _LOGGER.debug("Failed to send request, %s. Retrying...", result)
            await asyncio.sleep(3)
            return await self.request(command, payload, retry - 1)

        if "errorCode" in result:
            _LOGGER.error("Failed to send request, %s", result)
            return None
        data = json.loads(result)
        return data

    async def request_stats(self, command, payload, retry=3):
        """Request data."""
        _LOGGER.debug(command, payload)
        url = API_ENDPOINT_STATS + command
        json_data = json.dumps(payload)
        token = str(int(time.time() * 1000)) + "_" + str(self._user_id)
        padder = padding.PKCS7(128).padder()
        data = padder.update(token.encode()) + padder.finalize()
        encryptor = self._cipher.encryptor()
        milltoken = b64encode(encryptor.update(data) + encryptor.finalize())

        headers = {
            "accept-encoding": "gzip",
            "accept-language": "en-US,en;q=0.8",
            "connection": "Keep-Alive",
            "content-length": str(len(json_data)),
            "content-type": "application/json",
            "milltoken": milltoken.decode(),
        }
        try:
            with async_timeout.timeout(self._timeout):
                resp = await self.websession.post(url, data=json_data, headers=headers)
        except asyncio.TimeoutError:
            if retry < 1:
                _LOGGER.error("Timed out sending stats command to Mill: %s", command)
                return None
            return await self.request_stats(command, payload, retry - 1)
        except aiohttp.ClientError:
            _LOGGER.error(
                "Error sending stats command to Mill: %s", command, exc_info=True
            )
            return None

        result = await resp.text()

        _LOGGER.debug(result)
        data = json.loads(result)
        return data

    async def get_home_list(self):
        """Request data."""
        resp = await self.request("selectHomeList", "{}")
        if resp is None:
            return []
        return resp.get("homeList", [])

    async def update_rooms(self):
        """Request data."""
        homes = await self.get_home_list()
        for home in homes:
            payload = {"homeId": home.get("homeId"), "timeZoneNum": "+01:00"}
            data = await self.request("selectRoombyHome", payload)
            rooms = data.get("roomInfo", [])
            for _room in rooms:
                _id = _room.get("roomId")
                room = self.rooms.get(_id, Room())
                room.room_id = _id
                room.comfort_temp = _room.get("comfortTemp")
                room.away_temp = _room.get("awayTemp")
                room.sleep_temp = _room.get("sleepTemp")
                room.name = _room.get("roomName")
                room.current_mode = _room.get("currentMode")
                room.heat_status = _room.get("heatStatus")
                room.home_name = data.get("homeName")
                room.avg_temp = _room.get("avgTemp")

                self.rooms[_id] = room
                payload = {"roomId": _room.get("roomId"), "timeZoneNum": "+01:00"}
                room_device = await self.request("selectDevicebyRoom", payload)
                # room_device = await self.request("selectDevicebyRoom2020", payload)

                room.always = room_device.get("always")
                room.backHour = room_device.get("backHour")
                room.backMinute = room_device.get("backMinute")

                if room_device is None:
                    continue
                heater_info = room_device.get("deviceInfo", [])
                for _heater in heater_info:
                    _id = _heater.get("deviceId")
                    heater = self.heaters.get(_id, Heater())
                    heater.device_id = _id
                    heater.independent_device = False
                    heater.can_change_temp = _heater.get("canChangeTemp")
                    heater.name = _heater.get("deviceName")
                    heater.room = room
                    self.heaters[_id] = heater
                    if heater.home_id is None:
                        heater.home_id = home.get("homeId")

    async def set_room_temperatures_by_name(
        self, room_name, sleep_temp=None, comfort_temp=None, away_temp=None
    ):
        """Set room temps by name."""
        if sleep_temp is None and comfort_temp is None and away_temp is None:
            return
        for room_id, _room in self.rooms.items():
            if _room.name.lower().strip() == room_name.lower().strip():
                await self.set_room_temperatures(
                    room_id, sleep_temp, comfort_temp, away_temp
                )
                return
        _LOGGER.error("Could not find a room with name %s", room_name)

    async def set_room_temperatures(
        self, room_id, sleep_temp=None, comfort_temp=None, away_temp=None
    ):
        """Set room temps."""
        if sleep_temp is None and comfort_temp is None and away_temp is None:
            return
        room = self.rooms.get(room_id)
        if room is None:
            _LOGGER.error("No such device")
            return
        room.sleep_temp = sleep_temp if sleep_temp else room.sleep_temp
        room.away_temp = away_temp if away_temp else room.away_temp
        room.comfort_temp = comfort_temp if comfort_temp else room.comfort_temp
        payload = {
            "roomId": room_id,
            "sleepTemp": room.sleep_temp,
            "comfortTemp": room.comfort_temp,
            "awayTemp": room.away_temp,
        }
        await self.request("changeRoomModeTempInfoAway", payload)
        self.rooms[room_id] = room

    async def set_room_mode(self, room_id, mode=None, hour=0, minute=0):
        """Set room program override."""

        if mode is None:
            _LOGGER.error(
                "Need to define mode 0 (normal), 1 (comfort), 2 (sleep), 3 (away)"
            )
            return

        room = self.rooms.get(room_id)

        if room is None:
            _LOGGER.error("No such device")
            return

        room.backHour = hour
        room.backMinute = minute
        room.always = 0

        if hour == 0 and minute == 0:
            room.always = 1

        payload = {
            "roomId": room_id,
            "hour": room.backHour,
            "minute": room.backMinute,
            "mode": mode,
            "always": room.always,
            "timeZoneNum": "+02:00",
        }

        await self.request("changeRoomMode", payload)
        self.rooms[room_id] = room

    async def fetch_heater_data(self):
        """Request data."""
        if not self.heaters:
            await self.update_rooms()

        await self.update_heaters()
        return self.heaters

    async def fetch_heater_and_sensor_data(self):
        """Request data."""
        if not self.heaters:
            await self.update_rooms()

        await self.update_heaters()
        return {**self.heaters, **self.sensors}

    async def update_heaters(self):
        """Request data."""
        tasks = []
        homes = await self.get_home_list()
        for home in homes:
            tasks.append(self._update_independent_devices(home))

        for heater in self.heaters.values():
            tasks.append(self._update_consumption(heater))
            if heater.independent_device:
                continue
            tasks.append(self._update_heater_data(heater))
        await asyncio.gather(*tasks)

    async def _update_independent_devices(self, home):
        payload = {"homeId": home.get("homeId")}
        data = await self.request("getIndependentDevices2020", payload)
        if data is None:
            return
        dev_data = data.get("deviceInfo", [])
        if not dev_data:
            return
        for dev in dev_data:
            _id = dev.get("deviceId")

            if dev["subDomainId"] in (6933,):
                self.sensors[_id] = Sensor.init_from_response(dev)
            else:
                heater = self.heaters.get(_id, Heater())
                heater.device_id = _id
                set_heater_values(dev, heater)
                self.heaters[_id] = heater
                if heater.home_id is None:
                    heater.home_id = home.get("homeId")

    async def _update_heater_data(self, heater):
        payload = {"deviceId": heater.device_id}
        _heater = await self.request("selectDevice", payload)
        if _heater is None:
            self.heaters[heater.device_id].available = False
            return
        set_heater_values(_heater, heater)
        self.heaters[heater.device_id] = heater

    async def _update_consumption(self, heater):
        now = dt.datetime.now()
        if heater.last_consumption_update and (
            now - heater.last_consumption_update
        ) < MIN_TIME_BETWEEN_STATS_UPDATES + dt.timedelta(random.randint(0, 60)):
            return

        payload = {
            "dateType": None,
            "dateStr": now.strftime("%Y-%m-%d"),
            "timeZone": "GMT+02:00",
            "haveSensor": 1,
            "deviceId": heater.device_id,
        }

        tasks = []
        for date_type in [0, 3]:
            payload["dateType"] = date_type
            tasks.append(
                self.request_stats(
                    "statisticDeviceForAndroid",
                    payload.copy(),
                )
            )

        (cons0, cons3) = await asyncio.gather(*tasks)

        if cons0 is not None and cons0.get("code") == 0:
            heater.day_consumption = float(cons0.get("valueTotal"))
            heater.last_consumption_update = now

        if cons3 is not None and cons3.get("code") == 0:
            heater.year_consumption = float(cons3.get("valueTotal"))

    async def heater_control(self, device_id, fan_status=None, power_status=None):
        """Set heater temps."""
        heater = self.heaters.get(device_id)
        if heater is None:
            _LOGGER.error("No such device")
            return
        if fan_status is None:
            fan_status = heater.fan_status
        if power_status is None:
            power_status = heater.power_status
        operation = 0 if fan_status == heater.fan_status else 4
        payload = {
            "subDomain": heater.sub_domain,
            "deviceId": device_id,
            "testStatus": 1,
            "operation": operation,
            "status": power_status,
            "windStatus": fan_status,
            "holdTemp": heater.set_temp,
            "tempType": 0,
            "powerLevel": 0,
        }
        await self.request("deviceControl", payload)

    async def set_heater_temp(self, device_id, set_temp):
        """Set heater temp."""
        payload = {
            "homeType": 0,
            "timeZoneNum": "+02:00",
            "deviceId": device_id,
            "value": int(set_temp),
            "key": "holidayTemp",
        }
        await self.request("changeDeviceInfo", payload)


class Room:
    """Representation of room."""

    # pylint: disable=too-few-public-methods

    name = None
    room_id = None
    comfort_temp = None
    away_temp = None
    sleep_temp = None
    is_offline = None
    heat_status = None
    home_name = None
    avg_temp = None  # current temperature in the room
    current_mode = None
    always = None
    backHour = None
    backMinute = None

    def __repr__(self):
        items = (f"{k}={v}" for k, v in self.__dict__.items())
        return f"{self.__class__.__name__}({', '.join(items)})"


class Heater:
    """Representation of heater."""

    # pylint: disable=too-few-public-methods
    name = None
    device_id = None
    home_id = None
    available = False

    current_temp = None
    set_temp = None
    fan_status = None
    power_status = None
    independent_device = True
    room = None
    open_window = None
    is_heating = None
    tibber_control = None
    sub_domain = 5332
    is_holiday = None
    can_change_temp = 1
    day_consumption = None
    year_consumption = None
    last_consumption_update = None

    @property
    def is_gen1(self):
        """Check if heater is gen 1."""
        return self.sub_domain in [
            863,
        ]

    @property
    def generation(self):
        """Check if heater is gen 1."""
        return 1 if self.is_gen1 else 2

    def __repr__(self):
        items = (f"{k}={v}" for k, v in self.__dict__.items())
        return f"{self.__class__.__name__}({', '.join(items)})"


def set_heater_values(heater_data, heater):
    """Set heater values from heater data"""
    try:
        heater.current_temp = float(heater_data.get("currentTemp"))
    except ValueError:
        heater.current_temp = None
    else:
        if heater.current_temp > 90.0:
            heater.current_temp = None
    heater.device_status = heater_data.get("deviceStatus")
    heater.available = heater.device_status == 0
    heater.name = heater_data.get("deviceName")
    heater.fan_status = heater_data.get("fanStatus")
    heater.is_holiday = heater_data.get("isHoliday")

    # Room assigned devices don't report canChangeTemp
    # in selectDevice response.
    if heater.room is None:
        heater.can_change_temp = heater_data.get("canChangeTemp")

    # Independent devices report their target temperature via
    # holidayTemp value. But isHoliday is still set to 0.
    # Room assigned devices may have set "Control Device individually"
    # which effectively set their isHoliday value to 1.
    # In this mode they behave similar to independent devices
    # reporting their target temperature also via holidayTemp.
    if heater.independent_device or heater.is_holiday == 1:
        heater.set_temp = heater_data.get("holidayTemp")
    elif heater.room is not None:
        if heater.room.current_mode == 1:
            heater.set_temp = heater.room.comfort_temp
        elif heater.room.current_mode == 2:
            heater.set_temp = heater.room.sleep_temp
        elif heater.room.current_mode == 3:
            heater.set_temp = heater.room.away_temp
    heater.power_status = heater_data.get("powerStatus")
    heater.tibber_control = heater_data.get("tibberControl")
    heater.open_window = heater_data.get("open_window", heater_data.get("open"))
    heater.is_heating = heater_data.get("heatStatus", heater_data.get("heaterFlag"))
    try:
        heater.sub_domain = int(
            float(
                heater_data.get(
                    "subDomain", heater_data.get("subDomainId", heater.sub_domain)
                )
            )
        )
    except ValueError:
        pass


@dataclass
class Sensor:
    """Representation of sensor."""

    # pylint: disable=too-many-instance-attributes

    name: str
    device_id: int
    available: bool
    current_temp: float
    humidity: float
    tvoc: float
    eco2: float
    battery: float

    @classmethod
    def init_from_response(cls, response):
        """Class method."""
        return cls(
            response.get("deviceName"),
            response.get("deviceId"),
            response.get("deviceStatus") == 0,
            response.get("currentTemp"),
            response.get("humidity"),
            response.get("tvoc"),
            response.get("eco2"),
            response.get("batteryPer"),
        )
