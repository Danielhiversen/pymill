"""Library to handle connection with mill."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime as dt
import json
import logging

import aiohttp
import async_timeout

API_ENDPOINT = "https://api.millnorwaycloud.com/"
DEFAULT_TIMEOUT = 10
WINDOW_STATES = {0: "disabled", 3: "enabled_not_active", 2: "enabled_active"}

_LOGGER = logging.getLogger(__name__)


class TooManyRequests(Exception):
    """Too many requests."""


class Mill:
    """Class to communicate with the Mill api."""

    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        username,
        password,
        timeout=DEFAULT_TIMEOUT,
        websession=None,
    ) -> None:
        """Initialize the Mill connection."""
        self.devices: dict = {}
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
        self._cached_data = {}
        self._cached_stats_data = {}

    async def connect(self, retry=2):
        """Connect to Mill."""
        # pylint: disable=too-many-return-statements
        payload = {"login": self._username, "password": self._password}
        try:
            async with async_timeout.timeout(self._timeout):
                resp = await self.websession.post(
                    API_ENDPOINT + "customer/auth/sign-in",
                    json=payload,
                )
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if retry < 1:
                _LOGGER.error("Error connecting to Mill", exc_info=True)
                return False
            return await self.connect(retry - 1)
        result = await resp.text()

        if "Incorrect login or password" in result:
            _LOGGER.error("Incorrect login or password, %s", result)
            return False
        data = json.loads(result)
        if (token := data.get("idToken")) is None:
            _LOGGER.error("No token")
            return False
        self._token = token

        if self._user_id is not None:
            return True
        async with async_timeout.timeout(self._timeout):
            resp = await self.websession.get(
                API_ENDPOINT + "customer/details",
                headers=self._headers,
            )
        result = await resp.text()
        data = json.loads(result)
        if (user_id := data.get("id")) is None:
            _LOGGER.error("No user id")
            return False
        self._user_id = user_id
        return True

    @property
    def _headers(self):
        return {
            "Authorization": "Bearer " + self._token,
        }

    async def close_connection(self):
        """Close the Mill connection."""
        await self.websession.close()

    async def request(self, command, payload=None, retry=3, patch=False):
        """Request data."""
        # pylint: disable=too-many-return-statements, too-many-branches
        if self._token is None:
            _LOGGER.error("No token")
            return None

        _LOGGER.debug("Request %s %s", command, payload or "")
        url = API_ENDPOINT + command

        try:
            async with async_timeout.timeout(self._timeout):
                if payload:
                    if patch:
                        resp = await self.websession.patch(
                            url,
                            json=payload,
                            headers=self._headers,
                        )
                    else:
                        resp = await self.websession.post(
                            url,
                            json=payload,
                            headers=self._headers,
                        )
                else:
                    resp = await self.websession.get(url, headers=self._headers)

                if resp.status == 401:
                    _LOGGER.debug("Invalid auth token")
                    if await self.connect():
                        return await self.request(
                            command, payload, retry - 1, patch=patch
                        )
                    _LOGGER.error("Invalid auth token")
                    return None
                if resp.status == 429:
                    raise TooManyRequests(await resp.text())
                resp.raise_for_status()
        except asyncio.TimeoutError:
            if retry < 1:
                _LOGGER.error("Timed out sending command to Mill: %s", url)
                return None
            return await self.request(command, payload, retry - 1, patch=patch)
        except aiohttp.ClientError:
            _LOGGER.error("Error sending command to Mill: %s", url, exc_info=True)
            return None

        result = await resp.text()
        _LOGGER.debug("Result %s", result)
        return json.loads(result)

    async def cached_request(self, url, payload=None, ttl=20 * 60):
        """Request data and cache."""
        res, timestamp, _payload = self._cached_data.get(
            url + str(payload), (None, None, None)
        )
        if (
            url is not None
            and _payload == payload
            and timestamp is not None
            and dt.datetime.now() - timestamp < dt.timedelta(seconds=ttl)
        ):
            return res
        try:
            res = await self.request(url, payload)
            if res is not None and ttl > 0:
                self._cached_data[url + str(payload)] = (
                    res,
                    dt.datetime.now(),
                    payload,
                )
        except TooManyRequests:
            _LOGGER.warning("Too many requests, using cache %s", url)
            if res is None:
                raise
        return res

    async def update_devices(self):
        """Request data."""
        resp = await self.cached_request("houses")
        if resp is None:
            return []
        homes = resp.get("ownHouses", [])
        tasks = []
        for home in homes:
            tasks.append(self._update_home(home))
        await asyncio.gather(*tasks)

    async def _update_home(self, home):
        independent_devices_data = await self.cached_request(
            f"/houses/{home.get('id')}/devices/independent",
            ttl=60,
        )
        tasks = []
        if independent_devices_data is not None:
            for device in independent_devices_data.get("items", []):
                tasks.append(self._update_device(device))

        rooms_data = await self.cached_request(f"houses/{home.get('id')}/devices")
        if rooms_data is not None:
            for room in rooms_data:
                if not isinstance(room, dict):
                    _LOGGER.debug("Unexpected room data %s", room)
                    continue
                tasks.append(self._update_room(room))

        await asyncio.gather(*tasks)

    async def _update_room(self, room):
        if (room_id := room.get("roomId")) is None:
            return
        room_data = await self.cached_request(f"rooms/{room_id}/devices", ttl=90)

        tasks = []
        for device in room.get("devices", []):
            tasks.append(self._update_device(device, room_data))
        await asyncio.gather(*tasks)

    async def _update_device(self, device_data, room_data=None):
        if device_data is None:
            _LOGGER.warning("No device data")
            return
        device_type = (
            device_data.get("deviceType", {}).get("parentType", {}).get("name")
        )
        _id = device_data.get("deviceId")

        if device_type in ("Heaters", "Sockets"):
            now = dt.datetime.now()
            if _id in self.devices and (
                now - self.devices[_id].last_fetched < dt.timedelta(seconds=15)
            ):
                return
            device_stats = await self.fetch_yearly_stats(_id)
            if device_type == "Heaters":
                self.devices[_id] = Heater.init_from_response(
                    device_data, room_data, device_stats
                )
            else:
                self.devices[_id] = Socket.init_from_response(
                    device_data, room_data, device_stats
                )
        elif device_type in ("Sensors",):
            self.devices[_id] = Sensor.init_from_response(device_data)
        else:
            _LOGGER.error("Unsupported device, %s %s", device_type, device_data)
            return

    async def fetch_yearly_stats(self, device_id, ttl=30 * 60):
        """Fetch yearly stats."""

        now = dt.datetime.now()

        cache = self._cached_stats_data.get(device_id)
        if cache:
            if (now - cache[1] > dt.timedelta(days=10)) or (
                now.day == 1 and now.hour < 2 and now - cache[1] > dt.timedelta(hours=2)
            ):
                self._cached_stats_data.pop(device_id)

        if device_id not in self._cached_stats_data:
            _energy_prev_month = 0
            for month in range(1, now.month):
                _energy_prev_month += sum(
                    item.get("value", 0)
                    for item in (
                        await self.fetch_stats(
                            device_id, now.year, month, 1, "daily", ttl=0
                        )
                    )
                    .get("energyUsage", {})
                    .get("items", [])
                )
            self._cached_stats_data[device_id] = _energy_prev_month, now
        else:
            _energy_prev_month = cache[0]

        stats = await self.fetch_stats(
            device_id,
            now.year,
            now.month,
            1,
            "daily",
            ttl=12 * 60 * 60,
        )
        _energy_this_month = 0
        for item in stats.get("energyUsage", {}).get("items", []) or []:
            if item["lostStatisticData"]:
                _date = dt.datetime.fromisoformat(item["endPeriod"])
                hourly_stats = await self.fetch_stats(
                    device_id, _date.year, _date.month, _date.day, "hourly", ttl=ttl
                )
                for _item in hourly_stats.get("energyUsage", {}).get("items", []):
                    _energy_this_month += _item.get("value", 0)
                continue
            _energy_this_month += item.get("value", 0)

        return {"yearly_consumption": (_energy_this_month + _energy_prev_month)}

    # pylint: disable=too-many-arguments
    async def fetch_stats(self, device_id, year, month, day, period, ttl=60 * 60):
        """Fetch stats."""
        device_stats = await self.cached_request(
            f"devices/{device_id}/statistics",
            {
                "period": period,
                "year": year,
                "month": month,
                "day": day,
            },
            ttl=ttl,
        )
        if device_stats is None:
            return {}
        return device_stats

    async def set_room_temperatures_by_name(
        self, room_name, sleep_temp=None, comfort_temp=None, away_temp=None
    ):
        """Set room temps by name."""
        if sleep_temp is None and comfort_temp is None and away_temp is None:
            _LOGGER.error("Missing input data %s", room_name)
            return
        for heater in self.devices.values():
            if not isinstance(heater, Heater):
                continue
            if heater.room_name.lower().strip() == room_name.lower().strip():
                await self.set_room_temperatures(
                    heater.room_id,
                    sleep_temp,
                    comfort_temp,
                    away_temp,
                )
                return
        _LOGGER.error("Could not find a room with name %s", room_name)

    async def set_room_temperatures(
        self,
        room_id,
        sleep_temp=None,
        comfort_temp=None,
        away_temp=None,
    ):
        """Set room temps."""
        if sleep_temp is None and comfort_temp is None and away_temp is None:
            return
        payload = {}
        if sleep_temp:
            payload["roomSleepTemperature"] = sleep_temp
        if away_temp:
            payload["roomAwayTemperature"] = away_temp
        if comfort_temp:
            payload["roomComfortTemperature"] = comfort_temp

        self._cached_data = {}
        await self.request(f"rooms/{room_id}/temperature", payload)

    async def fetch_heater_data(self):
        """Request data."""
        await self.update_devices()
        return {
            key: val
            for key, val in self.devices.items()
            if isinstance(val, (Heater, Socket))
        }

    async def fetch_heater_and_sensor_data(self):
        """Request data."""
        await self.update_devices()
        return self.devices

    async def heater_control(self, device_id: str, power_status: bool):
        """Set heater temps."""
        if device_id not in self.devices:
            _LOGGER.error("Device id %s not found", device_id)
            return
        payload = {
            "deviceType": self.devices[device_id].device_type,
            "enabled": power_status,
            "settings": {
                "operation_mode": "control_individually" if power_status > 0 else "off"
            },
        }
        if await self.request(f"devices/{device_id}/settings", payload, patch=True):
            self._cached_data = {}
            self.devices[device_id].power_status = power_status
            if not power_status:
                self.devices[device_id].is_heating = False
            else:
                self.devices[device_id].is_heating = (
                    self.devices[device_id].set_temp
                    > self.devices[device_id].current_temp
                )
            self.devices[device_id].last_fetched = dt.datetime.now()

    async def set_heater_temp(self, device_id, set_temp):
        """Set heater temp."""
        payload = {
            "deviceType": self.devices[device_id].device_type,
            "enabled": True,
            "settings": {
                "operation_mode": "control_individually",
                "temperature_normal": set_temp,
            },
        }
        if await self.request(f"devices/{device_id}/settings", payload, patch=True):
            self._cached_data = {}
            self.devices[device_id].set_temp = set_temp
            self.devices[device_id].is_heating = (
                set_temp > self.devices[device_id].current_temp
            )
            self.devices[device_id].last_fetched = dt.datetime.now()


@dataclass(kw_only=True)
class MillDevice:
    """Mill Device."""

    # pylint: disable=too-many-instance-attributes

    name: str | None = None
    device_id: str | None = None
    available: bool | None = None
    model: str | None = None
    report_time: int | None = None
    data: dict | None = None
    room_data: dict | None = None
    stats: dict | None = None

    @classmethod
    def init_from_response(
        cls,
        device_data: dict,
        room_data: dict | None = None,
        device_stats: dict | None = None,
    ) -> MillDevice:
        """Class method."""
        device_type = device_data.get("deviceType")
        if device_type is None:
            model = None
        else:
            child_type = device_type.get("childType")
            if child_type is None:
                model = None
            else:
                model = child_type.get("name")
        last_metrics = device_data.get("lastMetrics")
        if last_metrics is None:
            report_time = None
        else:
            report_time = last_metrics.get("time")
        return cls(
            name=device_data.get("customName"),
            device_id=device_data.get("deviceId"),
            available=device_data.get("isConnected"),
            model=model,
            report_time=report_time,
            data=device_data,
            room_data=room_data,
            stats=device_stats,
        )

    @property
    def device_type(self) -> str:
        """Return device type."""
        return "unknown"

    @property
    def last_updated(self) -> dt.datetime:
        """Last updated."""
        if self.report_time is None:
            return dt.datetime.fromtimestamp(0).astimezone(dt.timezone.utc)
        return dt.datetime.fromtimestamp(self.report_time / 1000).astimezone(
            dt.timezone.utc
        )


@dataclass()
class Heater(MillDevice):
    """Representation of heater."""

    # pylint: disable=too-many-instance-attributes

    current_temp: float | None = None
    day_consumption: float | None = None
    home_id: str | None = None
    independent_device: bool | None = None
    is_heating: bool | None = None
    last_fetched: dt.datetime = dt.datetime.fromtimestamp(0)
    open_window: str | None = None
    power_status: bool | None = None
    room_avg_temp: float | None = None
    room_id: str | None = None
    room_name: str | None = None
    set_temp: float | None = None
    tibber_control: bool | None = None
    year_consumption: float | None = None

    def __post_init__(self) -> None:
        """Post init."""
        if self.data:
            last_metrics = self.data.get("lastMetrics", {})
            device_settings_desired = self.data.get("deviceSettings", {}).get(
                "desired", {}
            )
            if last_metrics is not None:
                self.current_temp = last_metrics.get("temperatureAmbient")
                self.is_heating = last_metrics.get("heaterFlag", 0) > 0
                self.power_status = last_metrics.get("powerStatus", 0) > 0
                self.set_temp = device_settings_desired.get(
                    "temperature_normal", last_metrics.get("temperature")
                )
                self.open_window = WINDOW_STATES.get(
                    last_metrics.get("openWindowsStatus")
                )
            else:
                _LOGGER.warning("No last metrics for device %s", self.device_id)
            self.day_consumption = self.data.get("energyUsageForCurrentDay", 0) / 1000.0

        if self.stats:
            self.year_consumption = self.stats.get("yearly_consumption", 0) / 1000.0
        if self.room_data:
            self.tibber_control = (
                self.room_data.get("controlSource", {}).get("tibber") == 1
            )
            self.home_id = self.room_data.get("houseId")
            self.room_id = self.room_data.get("id")
            self.room_name = self.room_data.get("name")
            self.room_avg_temp = self.room_data.get("averageTemperature")
            self.independent_device = False
        else:
            self.independent_device = True

    @property
    def device_type(self) -> str:
        """Return device type."""
        return "Heaters"


@dataclass()
class Socket(Heater):
    """Representation of socket."""

    @property
    def device_type(self) -> str:
        """Return device type."""
        return "Sockets"


@dataclass()
class Sensor(MillDevice):
    """Representation of sensor."""

    # pylint: disable=too-many-instance-attributes

    current_temp: float | None = None
    humidity: float | None = None
    tvoc: float | None = None
    eco2: float | None = None
    battery: float | None = None

    def __post_init__(self) -> None:
        """Post init."""
        if self.data:
            last_metrics = self.data.get("lastMetrics", {})
            self.current_temp = last_metrics.get("temperature")
            self.humidity = last_metrics.get("humidity")
            self.tvoc = last_metrics.get("tvoc")
            self.eco2 = last_metrics.get("eco2")
            self.battery = last_metrics.get("batteryPercentage")

    @property
    def device_type(self) -> str:
        """Return device type."""
        return "Sensors"
