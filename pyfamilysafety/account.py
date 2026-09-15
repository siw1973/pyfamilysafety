# pylint: disable=line-too-long
"""Family safety account handler."""

import asyncio
import logging
from datetime import datetime, date, time
from urllib.parse import quote_plus

from .api import FamilySafetyAPI
from .device import Device
from .application import Application
from .enum import OverrideTarget, OverrideType
from .exceptions import HttpException
from .helpers import localise_datetime, API_TIMEZONE

_LOGGER = logging.getLogger(__name__)

_STALE_ROSTER_MARKERS = (
    "unabletofindtargetresource",
    "rostererror",
    "unable to find the node",
)

# Tracks (user_id, endpoint) pairs that have already emitted a WARNING so that
# subsequent update cycles downgrade to DEBUG, avoiding log spam.
_ROSTER_WARNED: set[tuple[str, str]] = set()


def _empty_screentime_report() -> dict:
    """Return an empty screentime report matching the real API response shape."""
    return {
        "deviceUsageAggregates": {
            "deviceAggregates": [],
            "totalScreenTime": 0,
            "dailyAverage": 0,
        }
    }


class Account:
    """Represents a single family safety account."""

    def __init__(self, api) -> None:
        """Init an account."""
        self.user_id = None
        self.role = None
        self.profile_picture = None
        self.first_name = None
        self.surname = None
        self.devices: list[Device] = None
        self.applications: list[Application] = []
        self.today_screentime_usage: int = None
        self.average_screentime_usage: float = None
        self.screentime_usage: dict = None
        self.application_usage: dict = None
        self.blocked_platforms: list[OverrideTarget] = None
        self.experimental: bool = False
        self._api: FamilySafetyAPI = api
        self.account_balance: float = 0.0
        self.account_currency: str = ""
        self.roster_errors: dict = {}

    async def update(self) -> None:
        """Update all account details, tolerating per-endpoint roster failures.

        Each of the five sub-requests is wrapped individually so that a
        stale-roster HttpException on one endpoint (e.g. from an Entra ID /
        MDM-managed device) does not abort the entire account.  The account
        remains in the accounts list with whatever partial data was retrieved.
        Failed endpoints are recorded in self.roster_errors and the user_id is
        appended to api.unresolvable_devices.
        """
        self.roster_errors = {}

        async def _guarded(endpoint: str, coro) -> None:
            """Await *coro*, silently tolerating stale-roster HttpException."""
            try:
                await coro
            except HttpException as err:
                text = str(err)
                if not any(m in text.lower() for m in _STALE_ROSTER_MARKERS):
                    raise
                self.roster_errors[endpoint] = text[:200]
                key = (str(self.user_id), endpoint)
                _LOGGER.log(
                    logging.DEBUG if key in _ROSTER_WARNED else logging.WARNING,
                    "Roster resolution error for account %s (%s) — device may be "
                    "enrolled in Entra ID/MDM or decommissioned. Skipping this endpoint.",
                    self.user_id,
                    endpoint,
                )
                _ROSTER_WARNED.add(key)
                if self.user_id not in self._api.unresolvable_devices:
                    self._api.unresolvable_devices.append(self.user_id)

        await _guarded("screentime_usage", self.get_screentime_usage())
        if self.screentime_usage is None:
            self.screentime_usage = _empty_screentime_report()
        if self.application_usage is None:
            self.application_usage = {"appActivity": []}
        await asyncio.gather(
            _guarded("devices", self._get_devices()),
            _guarded("overrides", self._get_overrides()),
            _guarded("applications", self._get_applications()),
            _guarded("spending", self._get_account_balance()),
        )
        if self.devices is None:
            self.devices = []
        if self.blocked_platforms is None:
            self.blocked_platforms = []

    async def _get_devices(self) -> list[Device]:
        """Returns all devices on the account."""
        response = await self._api.send_request("get_user_devices", USER_ID=self.user_id)
        self.devices = Device.from_dict(response.get("json"), self.screentime_usage)
        return self.devices

    async def _get_overrides(self):
        """Collects overrides."""
        response = await self._api.send_request(
            endpoint="get_override_device_restrictions",
            USER_ID=self.user_id)
        self._update_device_blocked(response.get("json"))

    async def _get_applications(self) -> list[Application]:
        """Returns all applications on the account."""
        if self.application_usage is None:
            raise ValueError("Application usage not collected, call 'get_screentime_usage' first.")
        parsed_applications = Application.from_app_activity_report(
            self.application_usage,
            self._api,
            self.user_id)
        for app in parsed_applications:
            try:
                self.get_application(app.app_id).update(app)
            except IndexError:
                self.applications.append(app)
        return self.applications

    async def _get_account_balance(self):
        """Updates the account balance."""
        response = await self._api.send_request(
            endpoint="get_user_spending",
            USER_ID=self.user_id
        )
        response = response["json"]
        balances = response.get("balances", [])
        if len(balances) == 1:
            self.account_balance = balances[0]["balance"]
            self.account_currency = balances[0]["currency"]

    async def get_screentime_usage(self,
                                   start_time: datetime = None,
                                   end_time: datetime = None,
                                   device_count = 4,
                                   platform: str = "ALL") -> dict:
        """Returns screentime usage for the account."""
        default = False
        if start_time is None:
            default = True
            start_time = localise_datetime(datetime.combine(date.today(), time(0,0,0), tzinfo=API_TIMEZONE))
        if end_time is None:
            default = True
            end_time = localise_datetime(datetime.combine(date.today(), time(23,59,59), tzinfo=API_TIMEZONE))

        device_usage = await self._api.send_request(
                endpoint="get_user_device_screentime_usage",
                headers={
                    "Plat-Info": platform
                },
                USER_ID=self.user_id,
                BEGIN_TIME=quote_plus(start_time.strftime('%Y-%m-%dT%H:%M:%S%z')),
                END_TIME=quote_plus(end_time.strftime('%Y-%m-%dT%H:%M:%S%z')),
                DEVICE_COUNT=device_count
            )

        application_usage = await self._api.send_request(
                endpoint="get_user_app_screentime_usage",
                headers={
                    "Plat-Info": platform
                },
                USER_ID=self.user_id,
                BEGIN_TIME=quote_plus(start_time.strftime('%Y-%m-%dT%H:%M:%S%z')),
                END_TIME=quote_plus(end_time.strftime('%Y-%m-%dT%H:%M:%S%z'))
            )

        if default:
            self.screentime_usage = device_usage.get("json")
            self.today_screentime_usage = self.screentime_usage["deviceUsageAggregates"]["totalScreenTime"]
            self.average_screentime_usage = self.screentime_usage["deviceUsageAggregates"]["dailyAverage"]
            self.application_usage = application_usage.get("json")
            return self.screentime_usage
        else:
            # don't actually set a value
            return {
                "devices": device_usage.get("json"),
                "applications": application_usage.get("json")
            }

    def get_device(self, device_id) -> Device:
        """Returns a single device."""
        return [x for x in self.devices if x.device_id == device_id][0]

    def get_application(self, application_id) -> Application:
        """Returns a single application."""
        return [x for x in self.applications if x.app_id == application_id][0]

    async def override_device(self,
                              target: OverrideTarget,
                              override: OverrideType,
                              valid_until: datetime = None) -> bool:
        """Overrides a single device (block/unblock)"""
        if override == OverrideType.UNTIL and valid_until is None:
            raise ValueError("valid_until is required if using OverrideType.UNTIL")
        if override == OverrideType.CANCEL:
            valid_until = datetime.now()
        response = await self._api.send_request(
            endpoint="override_device_restriction",
            body={
                "overrideType": str(override),
                "target": str(target),
                "validUntil": valid_until.strftime("%Y-%m-%dT%H:%M:%SZ")
            },
            USER_ID=self.user_id
        )
        self._update_device_blocked(response.get("json"))

    def _update_device_blocked(self, raw_response: dict):
        """updates device(s) blocked status from a overrides response."""
        platforms = raw_response.get("lockablePlatforms")
        blocked_platforms = []
        for platform in platforms:
            # get if locked
            state = len(platform.get("overrides"))>0
            if state:
                blocked_platforms.append(OverrideTarget.from_pretty(platform.get("appliesTo")))

            for device in platform.get("devices"):
                try:
                    self.get_device(device.get("deviceId").replace("g:", "")).update_blocked_status(state)
                finally:
                    pass
        self.blocked_platforms = blocked_platforms

    @classmethod
    async def from_dict(cls, api: FamilySafetyAPI, raw_response: dict, experimental: bool) -> list['Account']:
        """Converts a roster request response to an array."""
        response = []
        if "members" in raw_response.keys():
            members = raw_response.get("members")
            for member in members:
                if member.get("isDigitalSafetyEnabled"):
                    self = cls(api)
                    self.user_id = member.get("id")
                    self.role = member.get("role")
                    self.profile_picture = member.get("profilePicUrl")
                    self.first_name = member.get("user").get("firstName")
                    self.surname = member.get("user").get("lastName")
                    self.experimental = experimental
                    await self.update()
                    response.append(self)
        return response
