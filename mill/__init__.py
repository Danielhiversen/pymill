"""Library to handle connection with mill."""
# Based on https://pastebin.com/53Nk0wJA and Postman capturing from the app
# All requests are send unencrypted from the app :(
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


API_ENDPOINT_1 = 'https://eurouter.ablecloud.cn:9005/zc-account/v1'
API_ENDPOINT_2 = 'http://eurouter.ablecloud.cn:5000/millService/v1'
DEFAULT_TIMEOUT = 10
MIN_TIME_BETWEEN_UPDATES = dt.timedelta(seconds=10)
REQUEST_TIMEOUT = '300'


_LOGGER = logging.getLogger(__name__)


class Mill:
    """Class to comunicate with the Mill api."""
    # pylint: disable=too-many-instance-attributes

    def __init__(self, username, password,
                 timeout=DEFAULT_TIMEOUT,
                 websession=None):
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

    async def connect(self):
        """Connect to Mill."""
        url = API_ENDPOINT_1 + '/login'
        headers = {
            "Content-Type": "application/x-zc-object",
            "Connection": "Keep-Alive",
            "X-Zc-Major-Domain": "seanywell",
            "X-Zc-Msg-Name": "millService",
            "X-Zc-Sub-Domain": "milltype",
            "X-Zc-Seq-Id": "1",
            "X-Zc-Version": "1",
        }
        payload = {"account": self._username,
                   "password": self._password}
        try:
            with async_timeout.timeout(self._timeout):
                resp = await self.websession.post(url,
                                                  data=json.dumps(payload),
                                                  headers=headers)
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error connecting to Mill: %s", err)
            return False

        result = await resp.text()
        if '"errorCode":3504' in result:
            _LOGGER.error('Wrong password')
            return False

        if '"errorCode":3501' in result:
            _LOGGER.error('Account does not exist')
            return False

        data = json.loads(result)
        token = data.get('token')
        if token is None:
            _LOGGER.error('No token')
            return False

        self._token = token
        self._user_id = data.get('userId')
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

    async def request(self, command, payload, retry=2):
        """Request data."""
        # pylint: disable=too-many-return-statements

        if self._token is None:
            _LOGGER.error("No token")
            return

        _LOGGER.debug(payload)

        nonce = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        url = API_ENDPOINT_2 + command
        timestamp = int(time.time())
        signature = hashlib.sha1(str(REQUEST_TIMEOUT
                                     + str(timestamp)
                                     + nonce
                                     + self._token).encode("utf-8")).hexdigest()

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
                resp = await self.websession.post(url,
                                                  data=json.dumps(payload),
                                                  headers=headers)
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Error sending command to Mill: %s, %s", command, err)
            return None

        result = await resp.text()

        _LOGGER.debug(result)

        if not result or result == '{"errorCode":0}':
            return None

        if 'access token expire' in result:
            if retry < 1:
                return None
            if not await self.connect():
                return None
            return await self.request(command, payload, retry-1)

        if '"error":"device offline"' in result:
            if retry < 1:
                _LOGGER.error("Failed to send request, %s", result)
                return None
            _LOGGER.error("Failed to send request, %s. Retrying...", result)
            return await self.request(command, payload, retry-1)

        if 'errorCode' in result:
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
        resp = await self.request("/selectHomeList", "{}")
        if resp is None:
            return None
        return resp.get('homeList')

    async def update_rooms(self):
        """Request data."""
        homes = await self.get_home_list()
        if homes is None:
            return None
        for home in homes:
            payload = {"homeId": home.get("homeId"), "timeZoneNum": "+01:00"}
            data = await self.request("/selectRoombyHome", payload)
            rooms = data.get('roomInfo', [])
            if not rooms:
                continue
            for _room in rooms:
                payload = {"roomId": _room.get("roomId"), "timeZoneNum": "+01:00"}
                data_heaters = await self.request("/selectDevicebyRoom", payload)
                if data_heaters is None:
                    continue
                heaters = data_heaters.get('deviceInfo', [])
                for _heater in heaters:
                    _id = _heater.get('deviceId')
                    heater = self.heaters.get(_id, Heater())
                    heater.device_id = _id
                    heater.independent_device = False
                    heater.name = _heater.get('deviceName')
                    self.heaters[_id] = heater

                _id = _room.get('roomId')
                room = self.rooms.get(_id, Room())
                room.room_id = _id
                room.comfort_temp = _room.get("comfortTemp")
                room.away_temp = _room.get("awayTemp")
                room.sleep_temp = _room.get("sleepTemp")
                room.name = _room.get("roomName")
                room.current_mode = _room.get("currentMode")
                room.heat_status = _room.get("heatStatus")

                self.rooms[_id] = room

    def sync_update_rooms(self):
        """Request data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_rooms())
        return loop.run_until_complete(task)

    async def set_room_temperatures(self, room_id, sleep_temp=None,
                                    comfort_temp=None, away_temp=None):
        """Set room temps."""
        room = self.rooms.get(room_id)
        if room is None:
            _LOGGER.error("No such device")
            return
        if sleep_temp is None:
            sleep_temp = room.sleep_temp
        if away_temp is None:
            away_temp = room.away_temp
        if comfort_temp is None:
            comfort_temp = room.comfort_temp
        payload = {"roomId": room_id,
                   "sleepTemp": sleep_temp,
                   "comfortTemp": comfort_temp,
                   "awayTemp": away_temp,
                   "homeType": 0}
        await self.request("/changeRoomModeTempInfo", payload)

    def sync_set_room_temperatures(self, room_id, sleep_temp=None,
                                   comfort_temp=None, away_temp=None):
        """Set heater temps."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.set_room_temperatures(room_id,
                                                           sleep_temp,
                                                           comfort_temp,
                                                           away_temp))
        return loop.run_until_complete(task)

    async def update_heaters(self):
        """Request data."""
        homes = await self.get_home_list()
        if homes is None:
            return None
        for home in homes:
            payload = {"homeId": home.get("homeId")}
            data = await self.request("/getIndependentDevices", payload)
            heaters = data.get('deviceInfo', [])
            if not heaters:
                continue
            for _heater in heaters:
                _id = _heater.get('deviceId')
                heater = self.heaters.get(_id, Heater())
                heater.device_id = _id
                heater.current_temp = _heater.get('currentTemp')
                heater.device_status = _heater.get('deviceStatus')
                heater.name = _heater.get('deviceName')
                heater.fan_status = _heater.get('fanStatus')
                heater.set_temp = _heater.get('holidayTemp')
                heater.power_status = _heater.get('powerStatus')

                self.heaters[_id] = heater

        for _id, heater in self.heaters.items():
            if heater.independent_device:
                continue
            payload = {"deviceId": _id}
            _heater = await self.request("/selectDevice", payload)
            if _heater is None:
                continue
            heater.current_temp = _heater.get('currentTemp')
            heater.device_status = _heater.get('deviceStatus')
            heater.name = _heater.get('deviceName')
            heater.fan_status = _heater.get('fanStatus')
            heater.set_temp = _heater.get('holidayTemp')
            heater.power_status = _heater.get('powerStatus')
            self.heaters[_id] = heater

    def sync_update_heaters(self):
        """Request data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_heaters())
        loop.run_until_complete(task)

    async def throttle_update_heaters(self):
        """Throttle update device."""
        if (self._throttle_time is not None
                and dt.datetime.now() - self._throttle_time < MIN_TIME_BETWEEN_UPDATES):
            return
        self._throttle_time = dt.datetime.now()
        await self.update_heaters()

    async def update_device(self, device_id):
        """Update device."""
        await self.throttle_update_heaters()
        return self.heaters.get(device_id)

    async def heater_control(self, device_id, fan_status=None,
                             power_status=None):
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
        payload = {"subDomain": 5332,
                   "deviceId": device_id,
                   "testStatus": 1,
                   "operation": operation,
                   "status": power_status,
                   "windStatus": fan_status,
                   "holdTemp": heater.set_temp,
                   "tempType": 0,
                   "powerLevel": 0}
        await self.request("/deviceControl", payload)

    def sync_heater_control(self, device_id, fan_status=None,
                            power_status=None):
        """Set heater temps."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.heater_control(device_id,
                                                    fan_status,
                                                    power_status))
        loop.run_until_complete(task)

    async def set_heater_temp(self, device_id, set_temp):
        """Set heater temp."""
        payload = {"homeType": 0,
                   "timeZoneNum": "+02:00",
                   "deviceId": device_id,
                   "value": int(set_temp),
                   "key": "holidayTemp"}
        await self.request("/changeDeviceInfo", payload)

    def sync_set_heater_temp(self, device_id, set_temp):
        """Set heater temps."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.set_heater_temp(device_id, set_temp))
        loop.run_until_complete(task)


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

    def __repr__(self):
        return 'Room(name={}, room_id={},' \
               ' comfort_temp={}, away_temp={})'.format(self.name,
                                                        self.room_id,
                                                        self.comfort_temp,
                                                        self.away_temp
                                                        )


class Heater:
    """Representation of heater."""
    # pylint: disable=too-few-public-methods
    name = None
    device_id = None
    current_temp = None
    set_temp = None
    fan_status = None
    power_status = None
    independent_device = True

    def __repr__(self):
        return 'Heater(name={}, device_id={},' \
               ' current_temp={}, set_temp={})'.format(self.name,
                                                       self.device_id,
                                                       self.current_temp,
                                                       self.set_temp
                                                       )
