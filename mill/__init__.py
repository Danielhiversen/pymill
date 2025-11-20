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

# API Configuration
API_ENDPOINT = "https://api.millnorwaycloud.com/"
DEFAULT_TIMEOUT = 10

# Device States
WINDOW_STATES = {0: "disabled", 3: "enabled_not_active", 2: "enabled_active"}

# HTTP status codes
HTTP_UNAUTHORIZED = 401
HTTP_TOO_MANY_REQUESTS = 429

# Time constants
EARLY_MORNING_HOUR = 2
DEFAULT_CACHE_TTL = 20 * 60  # 20 minutes
STATS_CACHE_TTL = 30 * 60  # 30 minutes
DEVICE_UPDATE_INTERVAL = 15  # seconds
TOKEN_DEFAULT_LIFETIME = 10  # minutes

# Device types
DEVICE_TYPE_HEATERS = "Heaters"
DEVICE_TYPE_SOCKETS = "Sockets"
DEVICE_TYPE_SENSORS = "Sensors"

# Operation modes
MODE_INDIVIDUAL = "control_individually"
MODE_WEEKLY = "weekly_program"
MODE_OFF = "off"

# Lock states
LOCK_CHILD = "child_lock"
LOCK_NONE = "no_lock"
LOCK_COMMERCIAL = "commercial"

_LOGGER = logging.getLogger(__name__)
LOCK = asyncio.Lock()


class TooManyRequestsError(Exception):
    """Too many requests."""


@dataclass
class TokenManager:
    """Manages authentication tokens and their expiration."""

    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: dt.datetime | None = None

    def is_expired(self) -> bool:
        """Check if the access token is expired."""
        if not self.expires_at:
            return True
        return dt.datetime.now(dt.timezone.utc) >= self.expires_at

    def update(self, data: dict[str, Any]) -> bool:
        """Update tokens from API response."""
        access_token = data.get("idToken")
        if not access_token:
            _LOGGER.error("No token in response")
            return False

        refresh_token = data.get("refreshToken")
        if not refresh_token:
            _LOGGER.error("No refresh token in response")
            return False

        self.access_token = access_token
        self.refresh_token = refresh_token
        expires_at = None
        try:
            payload = jwt.decode(access_token, options={"verify_signature": False})
            exp_timestamp = payload.get("exp")
            if exp_timestamp:
                expires_at = dt.datetime.fromtimestamp(exp_timestamp, tz=dt.timezone.utc)
        except jwt.InvalidTokenError as e:
            _LOGGER.warning("Could not decode token expiration: %s", e)

        if not expires_at:
            expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=TOKEN_DEFAULT_LIFETIME)

        self.expires_at = expires_at
        _LOGGER.debug("Token expires at %s", self.expires_at)
        return True


@dataclass
class CacheEntry:
    """Represents a cached value with timestamp."""

    value: dict[str, Any]
    timestamp: dt.datetime
    payload: dict[str, Any] | None = None

    def is_valid(self, ttl: int, payload: dict[str, Any] | None = None) -> bool:
        """Check if cache entry is still valid."""
        if payload != self.payload:
            return False

        if ttl <= 0:
            return True

        age = dt.datetime.now(dt.timezone.utc) - self.timestamp
        return age < dt.timedelta(seconds=ttl)


class CacheManager:
    """Manages request caching with TTL."""

    def __init__(self) -> None:
        """Initialize cache manager."""
        self._cache: dict[str, CacheEntry] = {}

    def get(self, key: str, ttl: int, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Get cached value if valid."""
        entry = self._cache.get(key)
        if entry and entry.is_valid(ttl, payload):
            return entry.value
        return None

    def set(self, key: str, value: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        """Store value in cache."""
        self._cache[key] = CacheEntry(
            value=value,
            timestamp=dt.datetime.now(dt.timezone.utc),
            payload=payload,
        )

    def clear(self) -> None:
        """Clear all cached data."""
        self._cache.clear()

    def remove(self, key: str) -> None:
        """Remove specific cache entry."""
        self._cache.pop(key, None)


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
        self.websession = websession or aiohttp.ClientSession()

        self._ua = user_agent
        self._timeout = timeout
        self._username = username
        self._password = password
        self._user_id: str | None = None

        self._token_manager = TokenManager()
        self._cache = CacheManager()
        self._stats_cache: dict[str, tuple[float, dt.datetime]] = {}

    async def connect(self, retry: int = 2) -> bool:
        """Connect to Mill."""
        if not await self._authenticate(retry):
            return False

        if self._user_id is not None:
            return True

        return await self._fetch_user_id()

    async def _authenticate(self, retry: int) -> bool:
        """Authenticate with Mill API."""
        payload = {"login": self._username, "password": self._password}
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self.websession.post(
                    f"{API_ENDPOINT}customer/auth/sign-in",
                    json=payload,
                    headers=self._build_headers(),
                )
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if retry < 1:
                _LOGGER.exception("Error connecting to Mill")
                return False
            return await self._authenticate(retry - 1)

        result = await resp.text()

        if "Incorrect login or password" in result:
            _LOGGER.error("Incorrect login or password")
            return False

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            _LOGGER.error("Invalid JSON response during authentication")
            return False

        return self._token_manager.update(data)

    async def _fetch_user_id(self) -> bool:
        """Fetch and store user ID."""
        try:
            async with asyncio.timeout(self._timeout):
                resp = await self.websession.get(
                    f"{API_ENDPOINT}customer/details",
                    headers=self._build_headers(include_auth=True),
                )
            data = await resp.json()

            if user_id := data.get("id"):
                self._user_id = user_id
                return True

            _LOGGER.error("No user id in response")
            return False
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError):
            _LOGGER.exception("Error fetching user ID")
            return False

    def _build_headers(self, include_auth: bool = False) -> dict[str, str]:
        """Build request headers."""
        headers = {}
        if include_auth and self._token_manager.access_token:
            headers["Authorization"] = f"Bearer {self._token_manager.access_token}"
        if self._ua:
            headers["User-Agent"] = self._ua
        return headers

    @property
    def user_agent(self) -> str | None:
        """Get user agent string."""
        return self._ua

    async def close_connection(self) -> None:
        """Close the Mill connection."""
        await self.websession.close()

    async def refresh_token(self) -> bool:
        """Refresh the access token."""
        _LOGGER.info("Refreshing token")

        async with LOCK:
            if not self._token_manager.is_expired():
                return True

            if not self._token_manager.refresh_token:
                _LOGGER.error("No refresh token available")
                return await self.connect()

            headers = {
                "Authorization": f"Bearer {self._token_manager.refresh_token}",
            }
            if self._ua:
                headers["User-Agent"] = self._ua

            try:
                async with asyncio.timeout(self._timeout):
                    response = await self.websession.post(
                        f"{API_ENDPOINT}customer/auth/refresh",
                        headers=headers,
                    )
            except (asyncio.TimeoutError, aiohttp.ClientError):
                _LOGGER.exception("Failed to refresh token")
                return False

            if response.status == HTTP_UNAUTHORIZED:
                return await self.connect()

            try:
                data = await response.json()
            except json.JSONDecodeError:
                _LOGGER.error("Invalid JSON response during token refresh")
                return await self.connect()

            if not self._token_manager.update(data):
                return await self.connect()

        return True

    async def request(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        retry: int = 3,
        patch: bool = False,
    ) -> dict[str, Any] | None:
        """Execute API request with automatic token refresh and retry logic."""
        if not self._token_manager.access_token:
            _LOGGER.error("No access token available")
            return None

        _LOGGER.debug("Request %s %s", command, payload or "")

        if self._token_manager.is_expired():
            _LOGGER.debug("Token expired, refreshing")
            if not await self.refresh_token():
                _LOGGER.error("Failed to refresh token")
                return None

        url = f"{API_ENDPOINT}{command}"

        try:
            return await self._execute_request(url, payload, patch, retry, command)
        except asyncio.TimeoutError:
            if retry < 1:
                _LOGGER.error("Timed out sending command to Mill: %s", url)
                return None

            backoff_time = max(0.5, 2 ** (3 - retry) - 0.5)
            await asyncio.sleep(backoff_time)
            return await self.request(command, payload, retry - 1, patch=patch)
        except aiohttp.ClientError:
            _LOGGER.exception("Error sending command to Mill: %s", url)
            return None

    async def _execute_request(
        self,
        url: str,
        payload: dict[str, Any] | None,
        patch: bool,
        retry: int,
        command: str,
    ) -> dict[str, Any] | None:
        """Execute the actual HTTP request."""
        async with asyncio.timeout(self._timeout):
            headers = self._build_headers(include_auth=True)

            if not payload:
                resp = await self.websession.get(url, headers=headers)
            elif patch:
                resp = await self.websession.patch(url, json=payload, headers=headers)
            else:
                resp = await self.websession.post(url, json=payload, headers=headers)

            if resp.status == HTTP_UNAUTHORIZED:
                _LOGGER.debug("Invalid auth token, attempting refresh")
                if await self.refresh_token():
                    return await self.request(command, payload, retry - 1, patch=patch)

                _LOGGER.error("Invalid auth token, refresh failed")
                return None

            if resp.status == HTTP_TOO_MANY_REQUESTS:
                raise TooManyRequestsError(await resp.text())

            _LOGGER.debug("Status %s", resp.status)
            resp.raise_for_status()

            result = await resp.text()
            _LOGGER.debug("Result %s", result)
            return json.loads(result)

    async def cached_request(
        self,
        url: str,
        payload: dict[str, Any] | None = None,
        ttl: int = DEFAULT_CACHE_TTL,
    ) -> dict[str, Any] | None:
        """Request data with caching support."""
        cache_key = f"{url}:{json.dumps(payload, sort_keys=True) if payload else ''}"

        if cached := self._cache.get(cache_key, ttl, payload):
            return cached

        try:
            result = await self.request(url, payload)
            if result is not None and ttl > 0:
                self._cache.set(cache_key, result, payload)
            return result
        except TooManyRequestsError:
            if cached := self._cache.get(cache_key, ttl=0, payload=payload):
                _LOGGER.warning("Too many requests, using stale cache for %s", url)
                return cached
            raise

    async def update_devices(self) -> None:
        """Update all devices from all houses."""
        resp = await self.cached_request("houses")
        if not resp:
            return

        homes = resp.get("ownHouses", [])
        for home in homes:
            home_id = home.get("id")
            if not home_id:
                continue

            tasks = []

            independent_devices_data = await self.cached_request(
                f"houses/{home_id}/devices/independent",
                ttl=60,
            )
            if independent_devices_data:
                tasks.extend(self._update_device(device) for device in independent_devices_data.get("items", []))

            rooms_data = await self.cached_request(f"houses/{home_id}/devices")
            if rooms_data:
                for room in rooms_data:
                    if not isinstance(room, dict):
                        _LOGGER.debug("Unexpected room data: %s", room)
                        continue
                    room_id = room.get("roomId")
                    if not room_id:
                        continue
                    room_data = await self.cached_request(f"rooms/{room_id}/devices", ttl=90)
                    tasks.extend(self._update_device(device, room_data) for device in room.get("devices", []))

            if tasks:
                await asyncio.gather(*tasks)

    async def _update_device(self, device_data: dict[str, Any], room_data: dict[str, Any] | None = None) -> None:
        """Update a single device from API data."""
        if not device_data:
            _LOGGER.warning("No device data")
            return

        device_type = device_data.get("deviceType", {}).get("parentType", {}).get("name")
        device_id = device_data.get("deviceId")

        if not device_id:
            _LOGGER.warning("Device has no ID")
            return

        if device_type in (DEVICE_TYPE_HEATERS, DEVICE_TYPE_SOCKETS):
            if device_id in self.devices:
                time_since_update = dt.datetime.now(dt.timezone.utc) - self.devices[device_id].last_fetched
                if time_since_update < dt.timedelta(seconds=DEVICE_UPDATE_INTERVAL):
                    return

            device_stats = await self.fetch_yearly_stats(device_id)

            if device_type == DEVICE_TYPE_HEATERS:
                self.devices[device_id] = Heater.init_from_response(device_data, room_data, device_stats)
            else:
                self.devices[device_id] = Socket.init_from_response(device_data, room_data, device_stats)
        elif device_type == DEVICE_TYPE_SENSORS:
            self.devices[device_id] = Sensor.init_from_response(device_data)
        else:
            _LOGGER.error("Unsupported device type: %s", device_type)

    async def fetch_yearly_stats(self, device_id: str, ttl: int = STATS_CACHE_TTL) -> dict[str, float]:
        """Fetch yearly energy consumption statistics."""
        now = dt.datetime.now(dt.timezone.utc)

        # Remove stale cache entries
        if device_id in self._stats_cache:
            _, timestamp = self._stats_cache[device_id]
            is_old = now - timestamp > dt.timedelta(days=10)
            is_new_month = now.day == 1 and now.hour < EARLY_MORNING_HOUR and now - timestamp > dt.timedelta(hours=2)
            if is_old or is_new_month:
                self._stats_cache.pop(device_id)

        if device_id in self._stats_cache:
            prev_months_energy = self._stats_cache[device_id][0]
        else:
            prev_months_energy = 0.0
            for month in range(1, now.month):
                stats = await self.fetch_stats(device_id, now.year, month, 1, "daily", ttl=0)
                prev_months_energy += sum(
                    item.get("value", 0) for item in stats.get("energyUsage", {}).get("items", [])
                )

            self._stats_cache[device_id] = (prev_months_energy, now)

        stats = await self.fetch_stats(device_id, now.year, now.month, 1, "daily", ttl=12 * 60 * 60)

        current_month_energy = 0.0
        for item in stats.get("energyUsage", {}).get("items", []) or []:
            if item.get("lostStatisticData"):
                # Recover lost daily statistics using hourly data
                date = dt.datetime.fromisoformat(item["endPeriod"])
                hourly_stats = await self.fetch_stats(device_id, date.year, date.month, date.day, "hourly", ttl=ttl)
                current_month_energy += sum(
                    _item.get("value", 0) for _item in hourly_stats.get("energyUsage", {}).get("items", [])
                )
            else:
                current_month_energy += item.get("value", 0)

        return {"yearly_consumption": prev_months_energy + current_month_energy}

    async def fetch_historic_energy_usage(self, device_id: str, n_days: int = 4) -> dict[dt.datetime, float]:
        """Fetch historic hourly energy usage for the last n_days."""
        now = dt.datetime.now(dt.timezone.utc)
        n_days = max(n_days, 1)
        result = {}

        for day in range(n_days + 1):
            date = now - dt.timedelta(days=n_days - day)
            try:
                hourly_stats = await self.fetch_stats(device_id, date.year, date.month, date.day, "hourly")
            except aiohttp.ClientResponseError:
                _LOGGER.warning(
                    "Error when fetching stats for device_id=%s, year=%s, month=%s, day=%s, period=%s",
                    device_id,
                    date.year,
                    date.month,
                    date.day,
                    "hourly",
                )
                hourly_stats = None

            if hourly_stats is None:
                break

            for item in hourly_stats.get("energyUsage", {}).get("items", []):
                timestamp = dt.datetime.fromisoformat(item["startPeriod"]).astimezone(dt.timezone.utc)
                result[timestamp] = item.get("value", 0) / 1000.0

        return result

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
        """Set room temperature settings by room name."""
        if not any([sleep_temp, comfort_temp, away_temp]):
            _LOGGER.error("No temperature values provided for room %s", room_name)
            return

        normalized_room_name = room_name.lower().strip()

        for heater in self.devices.values():
            if not isinstance(heater, Heater) or not heater.room_name:
                continue

            if heater.room_name.lower().strip() == normalized_room_name:
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
        """Set room temperature settings."""
        if not any([sleep_temp, comfort_temp, away_temp]):
            return

        payload = {}
        if sleep_temp:
            payload["roomSleepTemperature"] = sleep_temp
        if away_temp:
            payload["roomAwayTemperature"] = away_temp
        if comfort_temp:
            payload["roomComfortTemperature"] = comfort_temp

        self._cache.clear()
        await self.request(f"rooms/{room_id}/temperature", payload)

    async def fetch_heater_data(self) -> dict[str, Heater | Socket]:
        """Fetch all heater and socket devices."""
        await self.update_devices()
        return {key: val for key, val in self.devices.items() if isinstance(val, Heater | Socket)}

    async def fetch_heater_and_sensor_data(self) -> dict[str, MillDevice]:
        """Fetch all devices including heaters, sockets, and sensors."""
        await self.update_devices()
        return self.devices

    async def heater_control(self, device_id: str, power_status: bool) -> None:
        """Control heater power status."""
        device = self.devices.get(device_id)
        if not device:
            _LOGGER.error("Device id %s not found", device_id)
            return

        if not isinstance(device, Heater):
            _LOGGER.error("Device %s is not a heater", device_id)
            return

        operation_mode = MODE_INDIVIDUAL if power_status else MODE_OFF
        payload: dict[str, Any] = {
            "deviceType": device.device_type,
            "enabled": power_status,
            "settings": {"operation_mode": operation_mode} if device.device_type == DEVICE_TYPE_HEATERS else {},
        }

        if await self.request(f"devices/{device_id}/settings", payload, patch=True):
            self._cache.clear()
            self.devices[device_id].power_status = power_status
            self.devices[device_id].is_heating = (
                power_status
                and device.set_temp is not None
                and device.current_temp is not None
                and device.set_temp > device.current_temp
            )
            self.devices[device_id].last_fetched = dt.datetime.now(dt.timezone.utc)

    async def max_heating_power(self, device_id: str, heating_power: float) -> None:
        """Set maximum heating power."""
        payload = {
            "deviceType": self.devices[device_id].device_type,
            "enabled": True,
            "settings": {
                "operation_mode": MODE_INDIVIDUAL,
                "max_heater_power": heating_power,
            },
        }
        await self.request(f"devices/{device_id}/settings", payload, patch=True)

    async def set_heater_temp(self, device_id: str, set_temp: float) -> None:
        """Set heater target temperature."""
        payload = {
            "deviceType": self.devices[device_id].device_type,
            "enabled": True,
            "settings": {
                "operation_mode": MODE_INDIVIDUAL,
                "temperature_normal": set_temp,
            },
        }

        if await self.request(f"devices/{device_id}/settings", payload, patch=True):
            self._cache.clear()
            device = self.devices[device_id]
            device.set_temp = set_temp
            device.is_heating = set_temp > device.current_temp
            device.last_fetched = dt.datetime.now(dt.timezone.utc)

    async def _patch_device_settings(self, device_id: str, settings: dict[str, Any]) -> bool:
        """PATCH /devices/{id}/settings for heaters and sockets."""
        device = self.devices.get(device_id)
        if not device or not isinstance(device, (Heater, Socket)):
            _LOGGER.error("Device id %s not found or unsupported", device_id)
            return False

        payload: dict[str, Any] = {
            "deviceType": device.device_type,
            "enabled": bool(device.power_status),
            "settings": settings,
        }

        resp = await self.request(f"devices/{device_id}/settings", payload, patch=True)
        if resp is None:
            _LOGGER.error("Failed to patch settings for %s", device_id)
            return False

        self._cache.clear()
        device.last_fetched = dt.datetime.now(dt.timezone.utc)
        return True

    async def set_individual_control(self, device_id: str, enabled: bool) -> bool:
        """Switch manual/individual control on or off."""
        mode = MODE_INDIVIDUAL if enabled else MODE_WEEKLY
        return await self._patch_device_settings(device_id, {"operation_mode": mode})

    async def set_child_lock(self, device_id: str, enabled: bool) -> bool:
        """Toggle child lock on or off."""
        status = LOCK_CHILD if enabled else LOCK_NONE
        return await self._patch_device_settings(device_id, {"lock_status": status})

    async def set_commercial_lock(self, device_id: str, enabled: bool) -> bool:
        """Switch commercial lock on or off."""
        _LOGGER.debug("Setting commercial lock to %s for %s", enabled, device_id)
        status = LOCK_COMMERCIAL if enabled else LOCK_NONE
        return await self._patch_device_settings(device_id, {"lock_status": status})

    async def set_open_window(self, device_id: str, enabled: bool) -> bool:
        """Toggle open-window detection on or off."""
        return await self._patch_device_settings(device_id, {"open_window": {"enabled": enabled}})

    async def set_regulator_type(self, device_id: str, regulator_type: str) -> bool:
        """Set the regulator type (e.g., "pid", "hysteresis_or_slow_pid")."""
        _LOGGER.debug("Setting regulator type to %s for %s", regulator_type, device_id)
        return await self._patch_device_settings(device_id, {"regulator_type": regulator_type})

    async def set_predictive_heating(self, device_id: str, enabled: bool) -> bool:
        """Enable or disable predictive heating."""
        _LOGGER.debug("Setting predictive heating to %s for %s", enabled, device_id)
        mode = "advanced" if enabled else "off"
        return await self._patch_device_settings(
            device_id, {"predictive_heating_type": mode}
        )

    async def set_night_saving(self, device_id: str, enabled: bool) -> bool:
        """Enable or disable night saving mode."""
        _LOGGER.debug("Setting night saving to %s for %s", enabled, device_id)
        return await self._patch_device_settings(
            device_id, {"night_saving_mode_active": enabled}
        )

    async def set_frost_protection(self, device_id: str, enabled: bool) -> bool:
        """Enable or disable frost protection mode."""
        _LOGGER.debug("Setting frost protection to %s for %s", enabled, device_id)
        return await self._patch_device_settings(
            device_id, {"frost_protection_active": enabled}
        )

    async def set_cooling_mode(self, device_id: str, enabled: bool) -> bool:
        """Enable or disable cooling mode for sockets."""
        _LOGGER.debug("Setting cooling mode to %s for %s", enabled, device_id)
        mode = "cooling" if enabled else None
        return await self._patch_device_settings(
            device_id, {"additional_socket_mode": mode}
        )

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
        """Initialize device from API response data."""
        # Extract device model from nested device type data
        device_type = device_data.get("deviceType")
        child_type = device_type.get("childType") if device_type else None
        model = child_type.get("name") if child_type else None

        # Extract report time from device metrics
        last_metrics = device_data.get("lastMetrics")
        report_time = last_metrics.get("time") if last_metrics else None

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
    regulator_type: str | None = None
    predictive_heating: bool | None = None
    commercial_lock: bool | None = None
    night_saving: bool | None = None
    frost_protection: bool | None = None

    def __post_init__(self) -> None:
        """Initialize heater from device data."""
        if self.data:
            self._populate_from_device_data()

        if self.stats:
            self.year_consumption = self.stats.get("yearly_consumption", 0) / 1000.0

        if self.room_data:
            self._populate_from_room_data()
        else:
            self.independent_device = True

    def _populate_from_device_data(self) -> None:
        """Populate heater attributes from device data."""
        device_settings = self.data.get("deviceSettings", {})
        device_settings_reported = device_settings.get("reported", {})
        device_settings_desired = device_settings.get("desired", {})
        self.regulator_type = device_settings_reported.get("regulator_type")
        predictive_mode = device_settings_reported.get("predictive_heating_type")
        self.predictive_heating = None if predictive_mode is None else predictive_mode == "advanced"
        night_saving_mode = device_settings_reported.get("night_saving_mode_active")
        self.night_saving = None if night_saving_mode is None else bool(night_saving_mode)
        lock_status = device_settings_reported.get("lock_status")
        if lock_status is not None:
            self.commercial_lock = lock_status == LOCK_COMMERCIAL
        frost_protection = device_settings_reported.get("frost_protection_active")
        self.frost_protection = None if frost_protection is None else bool(frost_protection)

        last_metrics = self.data.get("lastMetrics", {})

        if not last_metrics:
            _LOGGER.warning("No last metrics for device %s", self.device_id)
            return

        self.current_temp = last_metrics.get("temperatureAmbient")
        self.is_heating = last_metrics.get("heaterFlag", 0) > 0
        self.power_status = last_metrics.get("powerStatus", 0) > 0
        self.set_temp = device_settings_desired.get("temperature_normal", last_metrics.get("temperature"))
        self.open_window = WINDOW_STATES.get(last_metrics.get("openWindowsStatus"))
        self.control_signal = last_metrics.get("controlSignal")
        self.current_power = last_metrics.get("currentPower")
        self.total_consumption = last_metrics.get("energyUsage")
        self.floor_temperature = last_metrics.get("floorTemperature")
        self.day_consumption = self.data.get("energyUsageForCurrentDay", 0) / 1000.0

    def _populate_from_room_data(self) -> None:
        """Populate heater attributes from room data."""
        self.tibber_control = self.room_data.get("controlSource", {}).get("tibber") == 1
        self.home_id = self.room_data.get("houseId")
        self.room_id = self.room_data.get("id")
        self.room_name = self.room_data.get("name")
        self.room_avg_temp = self.room_data.get("averageTemperature")
        self.independent_device = False

    @property
    def device_type(self) -> str:
        """Return device type."""
        return DEVICE_TYPE_HEATERS


@dataclass()
class Socket(Heater):
    """Representation of socket with humidity sensor."""

    humidity: float | None = None

    def __post_init__(self) -> None:
        """Initialize socket from device data."""
        if self.data:
            last_metrics = self.data.get("lastMetrics", {})
            self.humidity = last_metrics.get("humidity")

            device_settings = self.data.get("deviceSettings", {})
            device_settings_reported = device_settings.get("reported", {})
            if device_settings_reported:
                self.cooling_mode = (
                    device_settings_reported.get("additional_socket_mode") == "cooling"
                )

        super().__post_init__()

    @property
    def device_type(self) -> str:
        """Return device type."""
        return DEVICE_TYPE_SOCKETS


@dataclass()
class Sensor(MillDevice):
    """Representation of air quality and environmental sensor."""

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
        """Initialize sensor from device data."""
        if not self.data:
            return

        self._populate_metrics()
        self._calculate_particles()
        self.filter_state = self.data.get("deviceSettings", {}).get("reported", {}).get("filter_state")

    def _populate_metrics(self) -> None:
        """Populate sensor metrics from last metrics data."""
        last_metrics = self.data.get("lastMetrics", {})

        self.current_temp = last_metrics.get("temperature")
        self.humidity = last_metrics.get("humidity")
        self.tvoc = last_metrics.get("tvoc")
        self.eco2 = last_metrics.get("eco2")
        self.battery = last_metrics.get("batteryPercentage")
        self.pm1 = last_metrics.get("massPm_10")
        self.pm25 = last_metrics.get("massPm_25")
        self.pm10 = last_metrics.get("massPm_100")

    def _calculate_particles(self) -> None:
        """Calculate average particle count from PM measurements."""
        if self.pm1 is not None and self.pm25 is not None and self.pm10 is not None:
            avg_particles = (float(self.pm1) + float(self.pm25) + float(self.pm10)) / 3
            self.particles = round(avg_particles, 2)

    @property
    def device_type(self) -> str:
        """Return device type."""
        return DEVICE_TYPE_SENSORS
