"""Library to handle connection with mill."""
# Based on https://pastebin.com/53Nk0wJA and Postman capturing from the app
import asyncio
import datetime as dt
import hashlib
import json
import logging
import random
import string
import time

import aiohttp
import async_timeout

API_ENDPOINT_1 = "https://eurouter.ablecloud.cn:9005/zc-account/v1/"
API_ENDPOINT_2 = "https://eurouter.ablecloud.cn:9005/millService/v1/"
DEFAULT_TIMEOUT = 10
MIN_TIME_BETWEEN_UPDATES = dt.timedelta(seconds=2)
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
        self._throttle_time = None
        self._throttle_all_time = None
        self._cached_cons_data = {}
        self._lock_cons_data = asyncio.Lock()

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

    def sync_connect(self):
        """Close the Mill connection."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.connect())
        loop.run_until_complete(task)

    async def close_connection(self):
        """Close the Mill connection."""
        await self.websession.close()

    def sync_close_connection(self):
        """Close the Mill connection."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.close_connection())
        loop.run_until_complete(task)

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

    def sync_request(self, command, payload, retry=2):
        """Request data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.request(command, payload, retry))
        return loop.run_until_complete(task)

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

    def sync_update_rooms(self):
        """Request data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_rooms())
        return loop.run_until_complete(task)

    async def set_room_temperatures_by_name(
        self, room_name, sleep_temp=None, comfort_temp=None, away_temp=None
    ):
        """Set room temps by name."""
        if sleep_temp is None and comfort_temp is None and away_temp is None:
            return
        for room_id, _room in self.rooms.items():
            if _room.name == room_name:
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
            "homeType": 0,
        }
        await self.request("changeRoomModeTempInfo", payload)
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

    def sync_set_room_mode(self, room_id, mode=None, hour=0, minute=0):
        """Request data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.set_room_mode(room_id, mode, hour, minute))
        return loop.run_until_complete(task)

    async def update_heaters(self):
        """Request data."""
        tasks = []
        homes = await self.get_home_list()
        for home in homes:
            tasks.append(self._update_heater_home_data(home))

        for heater in self.heaters.values():
            tasks.append(self._update_consumption(heater))
            if heater.independent_device:
                continue
            tasks.append(self._update_heater_data(heater))
        await asyncio.gather(*tasks)

    async def _update_heater_home_data(self, home):
        payload = {"homeId": home.get("homeId")}
        data = await self.request("getIndependentDevices", payload)
        if data is None:
            return
        heater_data = data.get("deviceInfo", [])
        if not heater_data:
            return
        for _heater in heater_data:
            _id = _heater.get("deviceId")
            heater = self.heaters.get(_id, Heater())
            heater.device_id = _id
            set_heater_values(_heater, heater)
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
        async with self._lock_cons_data:
            cons0, cons3, timestamp = self._cached_cons_data.get(
                heater.home_id, (None, None, None)
            )
            if (
                cons0 is None
                or (dt.datetime.now() - timestamp).total_seconds() > 20 * 60
            ):
                task0 = self.request(
                    "statisticHome",
                    {
                        "homeId": heater.home_id,
                        "dateType": 0,
                        "timeZone": "GMT1",
                        "date": dt.datetime.now().strftime("%Y-%m-%d"),
                    },
                )
                task3 = self.request(
                    "statisticHome",
                    {
                        "homeId": heater.home_id,
                        "dateType": 3,
                        "timeZone": "GMT1",
                        "date": dt.datetime.now().strftime("%Y-%m-%d"),
                    },
                )
                (cons0, cons3) = await asyncio.gather(*[task0, task3])
                self._cached_cons_data[heater.home_id] = (
                    cons0,
                    cons3,
                    dt.datetime.now(),
                )

        if cons0 is not None:
            for device in cons0["deviceList"]:
                if device.get("deviceId") == heater.device_id:
                    heater.day_consumption = float(device.get("valueTotal"))
                    break
        if cons3 is not None:
            for device in cons3["deviceList"]:
                if device.get("deviceId") == heater.device_id:
                    heater.total_consumption = float(device.get("valueTotal"))
                    break

    def sync_update_heaters(self):
        """Request data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_heaters())
        loop.run_until_complete(task)

    async def throttle_update_heaters(self):
        """Throttle update device."""
        if (
            self._throttle_time is not None
            and dt.datetime.now() - self._throttle_time < MIN_TIME_BETWEEN_UPDATES
        ):
            return
        self._throttle_time = dt.datetime.now()
        await self.update_heaters()

    async def throttle_update_all_heaters(self):
        """Throttle update all devices and rooms."""
        if (
            self._throttle_all_time is not None
            and dt.datetime.now() - self._throttle_all_time < MIN_TIME_BETWEEN_UPDATES
        ):
            return
        self._throttle_all_time = dt.datetime.now()
        await self.find_all_heaters()

    async def update_device(self, device_id):
        """Update device."""
        await self.throttle_update_heaters()
        return self.heaters.get(device_id)

    async def update_room(self, room_id):
        """Update room."""
        await self.throttle_update_all_heaters()
        return self.rooms.get(room_id)

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

    def sync_heater_control(self, device_id, fan_status=None, power_status=None):
        """Set heater temps."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(
            self.heater_control(device_id, fan_status, power_status)
        )
        loop.run_until_complete(task)

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

    def sync_set_heater_temp(self, device_id, set_temp):
        """Set heater temps."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.set_heater_temp(device_id, set_temp))
        loop.run_until_complete(task)

    async def find_all_heaters(self):
        """Find all heaters."""
        await self.update_rooms()
        await self.update_heaters()


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
        items = ("%s=%r" % (k, v) for k, v in self.__dict__.items())
        return "%s(%s)" % (self.__class__.__name__, ", ".join(items))


class Heater:
    """Representation of heater."""

    # pylint: disable=too-few-public-methods
    name = None
    device_id = None
    home_id = None
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
    available = False
    is_holiday = None
    can_change_temp = 1
    day_consumption = None
    total_consumption = None

    @property
    def is_gen1(self):
        """Check if heater is gen 1."""
        return self.sub_domain in [
            863,
        ]

    def __repr__(self):
        items = ("%s=%r" % (k, v) for k, v in self.__dict__.items())
        return "%s(%s)" % (self.__class__.__name__, ", ".join(items))


def set_heater_values(heater_data, heater):
    """Set heater values from heater data"""
    heater.current_temp = heater_data.get("currentTemp")
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
