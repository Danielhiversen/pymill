"""Library to handle connection with mill."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import jwt

API_ENDPOINT = "https://api.millnorwaycloud.com/"
DEFAULT_TIMEOUT = 10
WINDOW_STATES = {0: "disabled", 3: "enabled_not_active", 2: "enabled_active"}

# HTTP status codes
HTTP_UNAUTHORIZED = 401
HTTP_TOO_MANY_REQUESTS = 429

# Time constants
EARLY_MORNING_HOUR = 2

_LOGGER = logging.getLogger(__name__)
LOCK = asyncio.Lock()


class TooManyRequestsError(Exception):
    """Too many requests."""


class Mill:
    """Class to communicate with the Mill api."""

    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        username: str,
        password: str,
        timeout: int = DEFAULT_TIMEOUT,
        websession: aiohttp.ClientSession | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Initialize the Mill connection."""
        self.devices: dict = {}

        if websession is None:

            async def _create_session() -> aiohttp.ClientSession:
                return aiohttp.ClientSession()

            loop = asyncio.get_event_loop()
            self.websession = loop.run_until_complete(_create_session())
        else:
            self.websession = websession

        self._ua: str | None = user_agent
        self._timeout = timeout
        self._username = username
        self._password = password
        self._user_id = None
        self._token = None
        self._token_expires = None
        self._refresh_token = None
        self._cached_data = {}
        self._cached_stats_data = {}

    async def connect(self, retry: int = 2) -> bool:
        """Connect to Mill."""
        # pylint: disable=too-many-return-statements
        payload = {"login": self._username, "password": self._password}
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self.websession.post(
                    API_ENDPOINT + "customer/auth/sign-in",
                    json=payload,
                    headers=({"User-Agent": self._ua} if self._ua else None),
                )
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if retry < 1:
                _LOGGER.exception("Error connecting to Mill")
                return False
            return await self.connect(retry - 1)
        result = await resp.text()

        if "Incorrect login or password" in result:
            _LOGGER.error("Incorrect login or password, %s", result)
            return False
        data = json.loads(result)
        if not self._update_tokens(data):
            return False

        if self._user_id is not None:
            return True
        async with asyncio.timeout(self._timeout):
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
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Authorization": "Bearer " + self._token}
        if self._ua:
            headers["User-Agent"] = self._ua
        return headers

    @property
    def user_agent(self) -> str:
        return self._ua

    async def close_connection(self) -> None:
        """Close the Mill connection."""
        await self.websession.close()

    async def refresh_token(self) -> bool:
        """Refresh the token."""
        _LOGGER.info("Refreshing token")
        async with LOCK:
            if dt.datetime.now(dt.timezone.utc) < self._token_expires:
                return True
            headers = {"Authorization": f"Bearer {self._refresh_token}"}
            if self._ua:
                headers["User-Agent"] = self._ua
            try:
                async with asyncio.timeout(self._timeout):
                    response = await self.websession.post(
                        API_ENDPOINT + "/customer/auth/refresh",
                        headers=headers,
                    )
            except (asyncio.TimeoutError, aiohttp.ClientError):
                _LOGGER.exception("Failed to refresh token")
                return False
            if response.status == HTTP_UNAUTHORIZED:
                return await self.connect()

            data = await response.json()

            if not self._update_tokens(data) and not await self.connect():
                _LOGGER.error("Failed to refresh token")
                return False

        return True

    async def request(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        retry: int = 3,
        patch: bool = False,
    ) -> dict[str, Any] | None:
        """Request data."""
        # pylint: disable=too-many-return-statements, too-many-branches
        if self._token is None or self._token_expires is None:
            _LOGGER.error("No token")
            return None

        _LOGGER.debug("Request %s %s", command, payload or "")

        if dt.datetime.now(dt.timezone.utc) >= self._token_expires:
            _LOGGER.debug("Token expired, refreshing")
            if not await self.refresh_token():
                _LOGGER.error("Failed to refresh token")
                return None
            _LOGGER.debug("Token refreshed")

        url = API_ENDPOINT + command

        try:
            async with asyncio.timeout(self._timeout):
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

                if resp.status == HTTP_UNAUTHORIZED:
                    _LOGGER.debug("Invalid auth token")
                    if await self.refresh_token():
                        return await self.request(command, payload, retry - 1, patch=patch)
                    _LOGGER.error("Invalid auth token")
                    return None
                if resp.status == HTTP_TOO_MANY_REQUESTS:
                    raise TooManyRequestsError(await resp.text())
                _LOGGER.debug("Status %s", resp.status)
                resp.raise_for_status()
        except asyncio.TimeoutError:
            if retry < 1:
                _LOGGER.error("Timed out sending command to Mill: %s", url)
                return None
            await asyncio.sleep(max(0.5, 2 ** (3 - retry) - 0.5))
            return await self.request(command, payload, retry - 1, patch=patch)
        except aiohttp.ClientError:
            _LOGGER.exception("Error sending command to Mill: %s", url)
            return None

        result = await resp.text()
        _LOGGER.debug("Result %s", result)
        return json.loads(result)

    async def cached_request(
        self,
        url: str,
        payload: dict[str, Any] | None = None,
        ttl: int = 20 * 60,
    ) -> dict[str, Any] | None:
        """Request data and cache."""
        res, timestamp, _payload = self._cached_data.get(url + str(payload), (None, None, None))
        if (
            url is not None
            and _payload == payload
            and timestamp is not None
            and dt.datetime.now(dt.timezone.utc) - timestamp < dt.timedelta(seconds=ttl)
        ):
            return res
        try:
            res = await self.request(url, payload)
            if res is not None and ttl > 0:
                self._cached_data[url + str(payload)] = (
                    res,
                    dt.datetime.now(dt.timezone.utc),
                    payload,
                )
        except TooManyRequestsError:
            if res is None:
                raise
            _LOGGER.warning("Too many requests, using cache %s", url)
        return res

    async def update_devices(self) -> list[dict[str, Any]] | None:
        """Request data."""
        resp = await self.cached_request("houses")
        if resp is None:
            return []
        homes = resp.get("ownHouses", [])
        tasks = [self._update_home(home) for home in homes]
        await asyncio.gather(*tasks)
        return None

    async def _update_home(self, home: dict[str, Any]) -> None:
        independent_devices_data = await self.cached_request(
            f"houses/{home.get('id')}/devices/independent",
            ttl=60,
        )
        tasks = []
        if independent_devices_data is not None:
            tasks.extend(self._update_device(device) for device in independent_devices_data.get("items", []))

        rooms_data = await self.cached_request(f"houses/{home.get('id')}/devices")
        if rooms_data is not None:
            for room in rooms_data:
                if not isinstance(room, dict):
                    _LOGGER.debug("Unexpected room data %s", room)
                    continue
                tasks.append(self._update_room(room))

        await asyncio.gather(*tasks)

    async def _update_room(self, room: dict[str, Any]) -> None:
        if (room_id := room.get("roomId")) is None:
            return
        room_data = await self.cached_request(f"rooms/{room_id}/devices", ttl=90)

        tasks = [self._update_device(device, room_data) for device in room.get("devices", [])]
        await asyncio.gather(*tasks)

    async def _update_device(self, device_data: dict[str, Any], room_data: dict[str, Any] | None = None) -> None:
        if device_data is None:
            _LOGGER.warning("No device data")
            return
        device_type = device_data.get("deviceType", {}).get("parentType", {}).get("name")
        _id = device_data.get("deviceId")

        if device_type in ("Heaters", "Sockets"):
            now = dt.datetime.now(dt.timezone.utc)
            if _id in self.devices and (now - self.devices[_id].last_fetched < dt.timedelta(seconds=15)):
                return
            device_stats = await self.fetch_yearly_stats(_id)
            if device_type == "Heaters":
                self.devices[_id] = Heater.init_from_response(device_data, room_data, device_stats)
            else:
                self.devices[_id] = Socket.init_from_response(device_data, room_data, device_stats)
        elif device_type in ("Sensors",):
            self.devices[_id] = Sensor.init_from_response(device_data)
        else:
            _LOGGER.error("Unsupported device, %s %s", device_type, device_data)
            return

    async def fetch_yearly_stats(self, device_id: str, ttl: int = 30 * 60) -> dict[str, float]:
        """Fetch yearly stats."""

        now = dt.datetime.now(dt.timezone.utc)

        cache = self._cached_stats_data.get(device_id)
        if cache and (
            (now - cache[1] > dt.timedelta(days=10))
            or (now.day == 1 and now.hour < EARLY_MORNING_HOUR and now - cache[1] > dt.timedelta(hours=2))
        ):
            self._cached_stats_data.pop(device_id)

        if device_id not in self._cached_stats_data:
            _energy_prev_month = 0
            for month in range(1, now.month):
                _energy_prev_month += sum(
                    item.get("value", 0)
                    for item in (await self.fetch_stats(device_id, now.year, month, 1, "daily", ttl=0))
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
                hourly_stats = await self.fetch_stats(device_id, _date.year, _date.month, _date.day, "hourly", ttl=ttl)
                for _item in hourly_stats.get("energyUsage", {}).get("items", []):
                    _energy_this_month += _item.get("value", 0)
                continue
            _energy_this_month += item.get("value", 0)

        return {"yearly_consumption": (_energy_this_month + _energy_prev_month)}

    async def fetch_historic_energy_usage(self, device_id: str, n_days: int = 4) -> dict[dt.datetime, float]:
        """Fetch historic energy usage."""
        now = dt.datetime.now(dt.timezone.utc)
        res = {}
        n_days = max(n_days, 1)

        for day in range(n_days + 1):
            date = now - dt.timedelta(days=n_days - day)
            hourly_stats = await self._fetch_stats_safe(device_id, date.year, date.month, date.day, "hourly")
            if hourly_stats is None:
                break
            for item in hourly_stats.get("energyUsage", {}).get("items", []):
                res[dt.datetime.fromisoformat(item["startPeriod"]).astimezone(dt.timezone.utc)] = (
                    item.get("value", 0) / 1000.0
                )
        return res

    async def _fetch_stats_safe(
        self,
        device_id: str,
        year: int,
        month: int,
        day: int,
        period: str,
    ) -> dict[str, Any] | None:
        """Safely fetch stats with error handling."""
        try:
            return await self.fetch_stats(device_id, year, month, day, period)
        except aiohttp.ClientResponseError:
            _LOGGER.warning(
                "Error when fetching stats for device_id=%s, year=%s, month=%s, day=%s, period=%s",
                device_id,
                year,
                month,
                day,
                period,
            )
            return None

    # pylint: disable=too-many-arguments
    async def fetch_stats(
        self,
        device_id: str,
        year: int,
        month: int,
        day: int,
        period: str,
        ttl: int = 60 * 60,
    ) -> dict[str, Any]:
        """Fetch stats."""
        try:
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
        except TooManyRequestsError:
            _LOGGER.warning(
                "Too many requests when fetching stats for device_id=%s, year=%s, month=%s, day=%s, period=%s",
                device_id,
                year,
                month,
                day,
                period,
            )
            return {}
        if device_stats is None:
            return {}
        return device_stats

    async def set_room_temperatures_by_name(
        self,
        room_name: str,
        sleep_temp: float | None = None,
        comfort_temp: float | None = None,
        away_temp: float | None = None,
    ) -> None:
        """Set room temps by name."""
        if sleep_temp is None and comfort_temp is None and away_temp is None:
            _LOGGER.error("Missing input data %s", room_name)
            return
        for heater in self.devices.values():
            if not isinstance(heater, Heater) or heater.room_name is None:
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
        room_id: str,
        sleep_temp: float | None = None,
        comfort_temp: float | None = None,
        away_temp: float | None = None,
    ) -> None:
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

    async def fetch_heater_data(self) -> dict[str, Heater | Socket]:
        """Request data."""
        await self.update_devices()
        return {key: val for key, val in self.devices.items() if isinstance(val, Heater | Socket)}

    async def fetch_heater_and_sensor_data(self) -> dict[str, MillDevice]:
        """Request data."""
        await self.update_devices()
        return self.devices

    async def heater_control(self, device_id: str, power_status: bool) -> None:
        """Set heater temps."""
        if device_id not in self.devices:
            _LOGGER.error("Device id %s not found", device_id)
            return
        payload = {
            "deviceType": self.devices[device_id].device_type,
            "enabled": power_status,
            "settings": {"operation_mode": "control_individually" if power_status > 0 else "off"},
        }
        if await self.request(f"devices/{device_id}/settings", payload, patch=True):
            self._cached_data = {}
            self.devices[device_id].power_status = power_status
            if not power_status:
                self.devices[device_id].is_heating = False
            else:
                self.devices[device_id].is_heating = (
                    self.devices[device_id].set_temp > self.devices[device_id].current_temp
                )
            self.devices[device_id].last_fetched = dt.datetime.now(dt.timezone.utc)

    async def max_heating_power(self, device_id: str, heating_power: float) -> None:
        """Max heating power."""
        payload = {
            "deviceType": self.devices[device_id].device_type,
            "enabled": True,
            "settings": {
                "operation_mode": "control_individually",
                "max_heater_power": heating_power,
            },
        }
        await self.request(f"devices/{device_id}/settings", payload, patch=True)

    async def set_heater_temp(self, device_id: str, set_temp: float) -> None:
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
            self.devices[device_id].is_heating = set_temp > self.devices[device_id].current_temp
            self.devices[device_id].last_fetched = dt.datetime.now(dt.timezone.utc)

    def _update_tokens(self, data: dict[str, Any]) -> bool:
        """Update access and refresh tokens from API response data."""
        if token := data.get("idToken"):
            self._token = token
            self._token_expires = self._get_token_expiration(token)
            _LOGGER.debug("Token expires at %s", self._token_expires)
        else:
            _LOGGER.error("No token")
            return False

        if refresh_token := data.get("refreshToken"):
            self._refresh_token = refresh_token
        else:
            _LOGGER.error("No refresh token")
            return False

        return True

    def _get_token_expiration(self, token: str) -> dt.datetime:
        """Extract expiration time from JWT token."""
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            exp_timestamp = payload.get("exp")
            if exp_timestamp:
                return dt.datetime.fromtimestamp(exp_timestamp, tz=dt.timezone.utc)
        except jwt.InvalidTokenError as e:
            _LOGGER.warning("Could not decode token expiration, using default: %s", e)
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)


@dataclass
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
            model = None if child_type is None else child_type.get("name")
        last_metrics = device_data.get("lastMetrics")
        report_time = None if last_metrics is None else last_metrics.get("time")
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
        return dt.datetime.fromtimestamp(self.report_time / 1000).astimezone(dt.timezone.utc)


@dataclass()
class Heater(MillDevice):
    """Representation of heater."""

    # pylint: disable=too-many-instance-attributes

    control_signal: float | None = None
    current_temp: float | None = None
    current_power: float | None = None
    day_consumption: float | None = None
    home_id: str | None = None
    independent_device: bool | None = None
    is_heating: bool | None = None
    last_fetched: dt.datetime = field(default_factory=lambda: dt.datetime.fromtimestamp(0, tz=dt.timezone.utc))
    open_window: str | None = None
    power_status: bool | None = None
    room_avg_temp: float | None = None
    room_id: str | None = None
    room_name: str | None = None
    set_temp: float | None = None
    tibber_control: bool | None = None
    total_consumption: float | None = None
    year_consumption: float | None = None
    floor_temperature: float | None = None

    def __post_init__(self) -> None:
        """Post init."""
        if self.data:
            last_metrics = self.data.get("lastMetrics", {})
            device_settings_desired = self.data.get("deviceSettings", {}).get("desired", {})
            if last_metrics is not None:
                self.current_temp = last_metrics.get("temperatureAmbient")
                self.is_heating = last_metrics.get("heaterFlag", 0) > 0
                self.power_status = last_metrics.get("powerStatus", 0) > 0
                self.set_temp = device_settings_desired.get("temperature_normal", last_metrics.get("temperature"))
                self.open_window = WINDOW_STATES.get(last_metrics.get("openWindowsStatus"))
                self.control_signal = last_metrics.get("controlSignal")
                self.current_power = last_metrics.get("currentPower")
                self.total_consumption = last_metrics.get("energyUsage")
                self.floor_temperature = last_metrics.get("floorTemperature")
            else:
                _LOGGER.warning("No last metrics for device %s", self.device_id)
            self.day_consumption = self.data.get("energyUsageForCurrentDay", 0) / 1000.0

        if self.stats:
            self.year_consumption = self.stats.get("yearly_consumption", 0) / 1000.0
        if self.room_data:
            self.tibber_control = self.room_data.get("controlSource", {}).get("tibber") == 1
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

    humidity: float | None = None

    def __post_init__(self) -> None:
        """Post init."""
        if self.data:
            last_metrics = self.data.get("lastMetrics", {})
            self.humidity = last_metrics.get("humidity")
        super().__post_init__()

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
    pm1: float | None = None
    pm25: float | None = None
    pm10: float | None = None
    particles: float | None = None
    filter_state: str | None = None

    def __post_init__(self) -> None:
        """Post init."""
        if self.data:
            last_metrics = self.data.get("lastMetrics", {})
            self.current_temp = last_metrics.get("temperature")
            self.humidity = last_metrics.get("humidity")
            self.tvoc = last_metrics.get("tvoc")
            self.eco2 = last_metrics.get("eco2")
            self.battery = last_metrics.get("batteryPercentage")
            self.pm1 = last_metrics.get("massPm_10")
            self.pm25 = last_metrics.get("massPm_25")
            self.pm10 = last_metrics.get("massPm_100")
            if self.pm1 is not None and self.pm25 is not None and self.pm10 is not None:
                self.particles = round((float(self.pm1) + float(self.pm25) + float(self.pm10)) / 3, 2)
            self.filter_state = self.data.get("deviceSettings", {}).get("reported", {}).get("filter_state")

    @property
    def device_type(self) -> str:
        """Return device type."""
        return "Sensors"
