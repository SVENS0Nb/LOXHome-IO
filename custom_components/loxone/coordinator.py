import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (CONF_HOST, CONF_PASSWORD, CONF_PORT,
                                 CONF_USERNAME)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ALLOW_INSECURE_HTTP,
    CONF_VERIFY_SSL,
    DEFAULT_ALLOW_INSECURE_HTTP,
    DEFAULT_VERIFY_SSL,
)
from .miniserver import MiniServer
from .pyloxone_api.connection import LoxoneConnection, LoxoneException

_LOGGER = logging.getLogger(__name__)


class LoxoneCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Loxone Miniserver."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name="PyLoxone Coordinator",
            update_method=None,  # Not polling!
        )
        self.config_entry = config_entry
        self._username = config_entry.options[CONF_USERNAME]
        self._password = config_entry.options[CONF_PASSWORD]
        self._host = config_entry.options[CONF_HOST]
        self._port = config_entry.options[CONF_PORT]
        self._verify_ssl = config_entry.options.get(
            CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL
        )
        self._allow_insecure_http = config_entry.options.get(
            CONF_ALLOW_INSECURE_HTTP, DEFAULT_ALLOW_INSECURE_HTTP
        )

        self.api: LoxoneConnection | None = None
        self.miniserver: MiniServer | None = None
        self.listeners = []
        self._listening_task: asyncio.Task | None = None
        self._reload_task: asyncio.Task | None = None
        self._unloading = False
        self.action_uuids: set[str] = set()

    async def async_config_entry_first_refresh(self) -> None:
        _LOGGER.debug("async_config_entry_first_refresh")
        if self.api and self.api.connection:
            await self.api.close()
            self.api.connection = None

        if "token" in self.config_entry.data:
            self.api = LoxoneConnection(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                token=self.config_entry.data,
                verify_ssl=self._verify_ssl,
                allow_insecure_http=self._allow_insecure_http,
            )
        else:
            self.api = LoxoneConnection(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                verify_ssl=self._verify_ssl,
                allow_insecure_http=self._allow_insecure_http,
            )
        try:
            session = async_get_clientsession(self.hass)
            self.api.connection = await self.api.open(session)
        except LoxoneException as e:
            _LOGGER.error("Could not connect to Loxone Miniserver")
            raise e
        except Exception as e:
            _LOGGER.error("Could not connect to Loxone Miniserver")
            raise e

        self.miniserver = MiniServer(
            self.hass, self.api.structure_file, self.config_entry
        )
        self.action_uuids = self._collect_action_uuids(self.api.structure_file)

        return None

    async def _async_update_data(self) -> None:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        return None

    @staticmethod
    def _collect_action_uuids(value) -> set[str]:
        """Collect every command UUID from the nested structure file."""
        result: set[str] = set()
        if isinstance(value, dict):
            action_uuid = value.get("uuidAction")
            if isinstance(action_uuid, str) and action_uuid:
                result.add(action_uuid)
            for child in value.values():
                result.update(LoxoneCoordinator._collect_action_uuids(child))
        elif isinstance(value, list):
            for child in value:
                result.update(LoxoneCoordinator._collect_action_uuids(child))
        return result

    async def async_cleanup(self):
        """Clean up resources."""
        self._unloading = True
        if hasattr(self, "listeners"):
            # Clean up all event listeners
            for listener in self.listeners:
                if listener is not None:
                    listener()
            self.listeners = []

        if (
            self._reload_task
            and self._reload_task is not asyncio.current_task()
            and not self._reload_task.done()
        ):
            self._reload_task.cancel()
            try:
                await self._reload_task
            except asyncio.CancelledError:
                pass
        self._reload_task = None

        if self._listening_task and not self._listening_task.done():
            self._listening_task.cancel()
            try:
                await self._listening_task
            except asyncio.CancelledError:
                pass
        self._listening_task = None

        # Close API connection
        if self.api:
            await self.api.close()
