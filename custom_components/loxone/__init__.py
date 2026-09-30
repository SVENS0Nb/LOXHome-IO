"""
Component to create an interface to the Loxone Miniserver.

For more details about this component, please refer to the documentation at
https://github.com/JoDehli/PyLoxone
"""

import asyncio
import logging
import re
from functools import cached_property

import homeassistant.components.group as group
import voluptuous as vol
import websockets
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (CONF_HOST, CONF_PASSWORD, CONF_PORT,
                                 CONF_USERNAME, EVENT_HOMEASSISTANT_STARTED,
                                 EVENT_HOMEASSISTANT_STOP)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.setup import async_setup_component

from .const import (ATTR_AREA_CREATE, ATTR_CODE,
                    ATTR_CONFIG_ENTRY_ID, ATTR_DEVICE, ATTR_UUID, ATTR_VALUE,
                    CONF_ALLOW_INSECURE_HTTP, CONF_DOOR_PROFILES,
                    CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN, CONF_SCENE_GEN,
                    CONF_SCENE_GEN_DELAY, CONF_SELECTED_ENTITIES,
                    CONF_VERIFY_SSL,
                    DEFAULT_DELAY_SCENE, DEFAULT_PORT, DEFAULT_VERIFY_SSL,
                    DOMAIN, EVENT, LOXONE_PLATFORMS, cfmt)
from .coordinator import LoxoneCoordinator
from .catalog import door_identities, door_selection_key
from .helpers import get_all
from .miniserver import get_miniserver_from_hass
from .pyloxone_api.exceptions import (LoxoneConnectionClosedOk,
                                      LoxoneConnectionError,
                                      LoxoneOutOfServiceException,
                                      LoxoneServiceUnAvailableError,
                                      LoxoneTokenError,
                                      LoxoneUnauthorisedError)

REQUIREMENTS = ["websockets", "pycryptodome", "numpy"]

_LOGGER = logging.getLogger(__name__)

_DATA_SERVICES_REGISTERED = f"{DOMAIN}_services_registered"

_RAW_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE): cv.entity_id,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_UUID): cv.string,
        vol.Required(ATTR_VALUE): vol.Any(str, int, float),
    }
)
_SECURED_COMMAND_SCHEMA = _RAW_COMMAND_SCHEMA.extend(
    {vol.Required(ATTR_CODE): cv.string}
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(CONF_HOST): cv.string,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                vol.Optional(
                    CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL
                ): cv.boolean,
                vol.Optional(
                    CONF_ALLOW_INSECURE_HTTP, default=True
                ): cv.boolean,
                vol.Optional(CONF_SCENE_GEN, default=True): cv.boolean,
                vol.Optional(
                    CONF_SCENE_GEN_DELAY, default=DEFAULT_DELAY_SCENE
                ): cv.positive_int,
                vol.Required(CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN, default=False): bool,
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

_UNDEF: dict = {}

# TODO: get version and check for updates https://update.loxone.com/updatecheck.xml?serial=xxxxxxxxx


def _loaded_coordinators(hass):
    """Return all currently loaded Loxone coordinators."""
    return [
        coordinator
        for coordinator in hass.data.get(DOMAIN, {}).values()
        if isinstance(coordinator, LoxoneCoordinator)
        and not coordinator._unloading
    ]


def _resolve_raw_service_target(hass, call):
    """Resolve and validate the explicitly addressed Miniserver and UUID."""
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    device = call.data.get(ATTR_DEVICE)
    entity_uuid = call.data.get(ATTR_UUID)

    if device:
        registry_entry = er.async_get(hass).async_get(device)
        if registry_entry is None or registry_entry.platform != DOMAIN:
            raise HomeAssistantError("The selected entity is not a Loxone entity")
        entry_id = registry_entry.config_entry_id
        state = hass.states.get(device)
        entity_uuid = state.attributes.get("uuid") if state else None

    if not entry_id or not entity_uuid:
        raise HomeAssistantError(
            "Provide either a Loxone entity or both config_entry_id and uuid"
        )

    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(coordinator, LoxoneCoordinator) or coordinator._unloading:
        raise HomeAssistantError("The selected Loxone config entry is not loaded")
    if entity_uuid not in coordinator.action_uuids:
        raise HomeAssistantError(
            "The UUID is not a command target in the selected Miniserver structure"
        )
    return coordinator, entity_uuid


async def _async_register_runtime_handlers(hass) -> None:
    """Register global admin-protected services once."""
    if hass.data.get(_DATA_SERVICES_REGISTERED):
        return

    async def handle_websocket_command(call):
        coordinator, entity_uuid = _resolve_raw_service_target(hass, call)
        await coordinator.api.send_websocket_command(
            entity_uuid, call.data[ATTR_VALUE]
        )

    async def handle_secured_websocket_command(call):
        coordinator, entity_uuid = _resolve_raw_service_target(hass, call)
        await coordinator.api.send_secured__websocket_command(
            entity_uuid, call.data[ATTR_VALUE], call.data[ATTR_CODE]
        )

    async def handle_sync_areas(call):
        create_areas = bool(call.data.get(ATTR_AREA_CREATE, False))
        entity_registry = er.async_get(hass)
        area_registry = ar.async_get(hass)
        updates = []
        for entry in entity_registry.entities.values():
            if entry.platform != DOMAIN:
                continue
            state = hass.states.get(entry.entity_id)
            room = state.attributes.get("room") if state else None
            if not room:
                continue
            area = area_registry.async_get_area_by_name(room)
            if area is None and create_areas:
                area = area_registry.async_get_or_create(room)
            if area and entry.area_id is None:
                updates.append((entry.entity_id, area.id))
        for entity_id, area_id in updates:
            entity_registry.async_update_entity(entity_id, area_id=area_id)

    async def handle_reload(_call):
        await asyncio.gather(
            *(
                hass.config_entries.async_reload(entry.entry_id)
                for entry in hass.config_entries.async_entries(DOMAIN)
            )
        )

    async_register_admin_service(
        hass,
        DOMAIN,
        "event_websocket_command",
        handle_websocket_command,
        _RAW_COMMAND_SCHEMA,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        "event_secured_websocket_command",
        handle_secured_websocket_command,
        _SECURED_COMMAND_SCHEMA,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        "sync_areas",
        handle_sync_areas,
        vol.Schema({vol.Optional(ATTR_AREA_CREATE, default=False): cv.boolean}),
    )
    async_register_admin_service(
        hass, DOMAIN, "reload", handle_reload, vol.Schema({})
    )
    hass.data[_DATA_SERVICES_REGISTERED] = True


def _remove_runtime_handlers(hass) -> None:
    """Remove global handlers after the final config entry has unloaded."""
    if _loaded_coordinators(hass):
        return
    for service in (
        "event_websocket_command",
        "event_secured_websocket_command",
        "sync_areas",
        "reload",
    ):
        hass.services.async_remove(DOMAIN, service)
    hass.data.pop(_DATA_SERVICES_REGISTERED, None)


async def async_unload_entry(hass, config_entry):
    """Completely unloads the Loxone integration and closes all connections."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, LOXONE_PLATFORMS
    )
    if not unload_ok:
        return False

    coordinator = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if isinstance(coordinator, LoxoneCoordinator):
        token = coordinator.api.get_token_dict() if coordinator.api else {}
        if token.get("token"):
            hass.config_entries.async_update_entry(
                config_entry,
                data={**config_entry.data, **token},
            )
        try:
            await coordinator.async_cleanup()
        except Exception:
            _LOGGER.exception("Error while closing the Loxone connection")
        hass.data[DOMAIN].pop(config_entry.entry_id, None)
    _remove_runtime_handlers(hass)
    return True


async def async_setup(hass, config):
    """setup loxone"""
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "import"}, data=config[DOMAIN]
            )
        )
    return True


async def async_migrate_entry(hass, config_entry):
    """Migrate legacy entries while preserving their import-all behaviour."""
    options = dict(config_entry.options)
    version = config_entry.version
    if config_entry.version == 1:
        options[CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN] = True
        version = 2
        _LOGGER.info("Migration to version %s successful", 2)

    if version == 2:
        options[CONF_SCENE_GEN_DELAY] = DEFAULT_DELAY_SCENE
        version = 3
        _LOGGER.info("Migration to version %s successful", 3)
    if version == 3:
        # Do not add CONF_SELECTED_ENTITIES here: its absence is the explicit
        # compatibility signal for historic entries that imported everything.
        version = 4
        _LOGGER.info("Migration to version %s successful", 4)
    if version == 4:
        # Existing installations historically used HTTP on port 8080. Keep
        # them operational, but require an explicit opt-in for new entries.
        options.setdefault(CONF_ALLOW_INSECURE_HTTP, True)
        version = 5
        _LOGGER.warning(
            "Legacy Loxone entry allows insecure HTTP; switch to HTTPS when supported"
        )
    if version != config_entry.version or options != config_entry.options:
        hass.config_entries.async_update_entry(
            config_entry, options=options, version=version
        )
    return True


async def _async_migrate_door_identities(hass, config_entry, structure) -> None:
    """Replace index-based door selections, profiles and entity unique IDs."""
    replacements: dict[str, str] = {}
    unique_id_replacements: dict[str, str] = {}
    gateway_id = config_entry.unique_id or config_entry.entry_id
    for monitor in get_all(structure, "WindowMonitor"):
        monitor_uuid = str(monitor.get("uuidAction", ""))
        windows = monitor.get("details", {}).get("windows", ()) or ()
        for index, (_item, door_id) in enumerate(
            zip(windows, door_identities(windows), strict=False)
        ):
            replacements[door_selection_key(monitor_uuid, index)] = (
                door_selection_key(monitor_uuid, door_id)
            )
            new_unique_id = f"{gateway_id}-{monitor_uuid}-door-{door_id}"
            unique_id_replacements[f"{monitor_uuid}-door-{index}"] = new_unique_id
            unique_id_replacements[
                f"{monitor_uuid}-door-{door_id}"
            ] = new_unique_id

    options = dict(config_entry.options)
    changed = False
    selected = options.get(CONF_SELECTED_ENTITIES)
    if selected is not None:
        if not isinstance(selected, (list, tuple, set)):
            selected = ()
            options[CONF_SELECTED_ENTITIES] = []
            changed = True
        migrated_selection = list(
            dict.fromkeys(replacements.get(key, key) for key in selected)
        )
        if migrated_selection != list(selected):
            options[CONF_SELECTED_ENTITIES] = migrated_selection
            changed = True

    configured_profiles = options.get(CONF_DOOR_PROFILES, {}) or {}
    profiles = dict(configured_profiles) if isinstance(configured_profiles, dict) else {}
    if configured_profiles and not isinstance(configured_profiles, dict):
        changed = True
    for old_key, new_key in replacements.items():
        if old_key not in profiles:
            continue
        if new_key not in profiles:
            profiles[new_key] = profiles[old_key]
        profiles.pop(old_key)
        changed = True
    if changed:
        options[CONF_DOOR_PROFILES] = profiles
        hass.config_entries.async_update_entry(config_entry, options=options)

    registry = er.async_get(hass)
    for entry in list(registry.entities.values()):
        if (
            entry.platform != DOMAIN
            or entry.config_entry_id != config_entry.entry_id
            or entry.domain != "lock"
        ):
            continue
        new_unique_id = unique_id_replacements.get(entry.unique_id)
        if new_unique_id and new_unique_id != entry.unique_id:
            try:
                registry.async_update_entity(
                    entry.entity_id, new_unique_id=new_unique_id
                )
            except ValueError:
                _LOGGER.warning(
                    "Could not migrate duplicate lock entity %s", entry.entity_id
                )


async def async_set_options(hass, config_entry):
    options_in = {**config_entry.options}
    options = {
        CONF_HOST: options_in.pop(CONF_HOST, ""),
        CONF_PORT: options_in.pop(CONF_PORT, DEFAULT_PORT),
        CONF_USERNAME: options_in.pop(CONF_USERNAME, ""),
        CONF_PASSWORD: options_in.pop(CONF_PASSWORD, ""),
        CONF_VERIFY_SSL: options_in.pop(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        CONF_ALLOW_INSECURE_HTTP: options_in.pop(
            CONF_ALLOW_INSECURE_HTTP, True
        ),
        CONF_SCENE_GEN: options_in.pop(CONF_SCENE_GEN, ""),
        CONF_SCENE_GEN_DELAY: options_in.pop(CONF_SCENE_GEN_DELAY, DEFAULT_DELAY_SCENE),
        CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN: options_in.pop(
            CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN, ""
        ),
    }
    hass.config_entries.async_update_entry(
        config_entry, data=config_entry.data, options=options
    )


async def async_config_entry_updated(hass, entry) -> None:
    """Handle signals of config entry being updated.

    This is a static method because a class method (bound method), can not be used with weak references.
    Causes for this is either discovery updating host address or config entry options changing.
    """
    pass


async def create_group_for_loxone_entities(hass, entities, name, object_id):
    try:
        await group.Group.async_create_group(
            hass,
            name,
            created_by_service=False,
            entity_ids=entities,
            icon=None,
            mode=None,
            object_id=object_id,
            order=None,
        )
    except HomeAssistantError as err:
        await group.Group.async_create_group(
            hass,
            name,
            created_by_service=True,
            entity_ids=entities,
            icon=None,
            mode=None,
            object_id=object_id,
            order=None,
        )
        _LOGGER.error("Can't create group '%s' with error: %s", name, err)
    except Exception as e:
        _LOGGER.error(
            "Can't create group '%s'. Try to make at least one group manually. ("
            "https://www.home-assistant.io/integrations/group/)",
            e,
        )


async def async_setup_entry(hass, config_entry):
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    if not config_entry.options:
        await async_set_options(hass, config_entry)

    coordinator = LoxoneCoordinator(hass, config_entry)
    _LOGGER.info("Setting up LOXHome I/O")

    try:
        await coordinator.async_config_entry_first_refresh()
    except LoxoneServiceUnAvailableError as err:
        if coordinator.api:
            await coordinator.api.close()
        _LOGGER.debug("Loxone Miniserver unavailable; Home Assistant will retry")
        raise ConfigEntryNotReady from err
    except LoxoneUnauthorisedError as err:
        if coordinator.api:
            await coordinator.api.close()
        raise ConfigEntryAuthFailed(
            "The Loxone Miniserver rejected the configured credentials"
        ) from err
    except OSError as err:
        if coordinator.api:
            await coordinator.api.close()
        _LOGGER.debug("Miniserver network connection failed; Home Assistant will retry")
        raise ConfigEntryNotReady from err
    except (
        LoxoneConnectionError,
        LoxoneConnectionClosedOk,
        TimeoutError,
        ConnectionError,
    ) as err:
        if coordinator.api:
            await coordinator.api.close()
        _LOGGER.debug("Miniserver connection failed; Home Assistant will retry")
        raise ConfigEntryNotReady from err
    except Exception as err:
        if coordinator.api:
            await coordinator.api.close()
        _LOGGER.debug("Unexpected Miniserver connection failure; Home Assistant will retry")
        raise ConfigEntryNotReady from err

    _LOGGER.info("Successfully connected to Loxone Miniserver")

    await _async_migrate_door_identities(
        hass, config_entry, coordinator.api.structure_file
    )
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator
    await _async_register_runtime_handlers(hass)

    try:
        await hass.config_entries.async_forward_entry_setups(
            config_entry, LOXONE_PLATFORMS
        )
    except Exception:
        await coordinator.async_cleanup()
        hass.data[DOMAIN].pop(config_entry.entry_id, None)
        _remove_runtime_handlers(hass)
        raise

    async def _reload_after_delay(delay: float = 1.0) -> None:
        await asyncio.sleep(delay)
        if not coordinator._unloading:
            await hass.config_entries.async_reload(config_entry.entry_id)

    def schedule_reload() -> None:
        if coordinator._unloading:
            return
        if coordinator._reload_task and not coordinator._reload_task.done():
            return
        coordinator._reload_task = hass.async_create_task(_reload_after_delay())

    def handle_task_result(task: asyncio.Task) -> None:
        try:
            task.result()
        except LoxoneTokenError:
            _LOGGER.debug(
                "Token is not valid anymore. Delete token and try to reloading Loxone integration."
            )
            # First we delete the invalid token then try to reload
            hass.config_entries.async_update_entry(
                config_entry,
                data={
                    **config_entry.data,
                    "token": "",
                    "hash_alg": "",
                    "valid_until": "",
                },
            )
            # Loxone-Integration neu laden
            schedule_reload()
        except LoxoneOutOfServiceException:
            _LOGGER.debug(
                "Loxone LoxoneOutOfServiceException received. Try to reloading Loxone integration."
            )
            # Loxone-Integration neu laden
            schedule_reload()
        except LoxoneConnectionError:
            _LOGGER.debug(
                "Loxone LoxoneConnectionError received. Try to reloading Loxone integration."
            )
            # Loxone-Integration neu laden
            schedule_reload()
        except (
            LoxoneConnectionClosedOk,
            websockets.exceptions.ConnectionClosedOK,
        ):
            _LOGGER.debug(
                "Loxone LoxoneConnectionClosedOk received. Mostly a timeout Problem. Try to reloading Loxone integration."
            )
            # Loxone-Integration neu laden
            schedule_reload()
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Loxone listening task failed")
            schedule_reload()

    async def message_callback(message):
        """Fire message on HomeAssistant Bus."""
        if isinstance(message, dict):
            message = {**message, ATTR_CONFIG_ENTRY_ID: config_entry.entry_id}
        hass.bus.async_fire(EVENT, message)

    async def loxone_discovered(event):
        _LOGGER.info("Creating groups")
        miniserver = get_miniserver_from_hass(hass, config_entry)
        if miniserver.miniserver_type < 2 and "component" in event.data:
            if event.data["component"] == DOMAIN:
                try:
                    _LOGGER.info("loxone discovered")
                    await asyncio.sleep(0.1)
                    # await sync_areas_with_loxone()
                    entity_ids = hass.states.async_all()
                    sensors_analog = []
                    sensors_digital = []
                    switches = []
                    covers = []
                    lights = []
                    dimmers = []
                    climates = []
                    fans = []
                    accontrols = []
                    numbers = []
                    texts = []
                    buttons = []

                    for s in entity_ids:
                        s_dict = s.as_dict()
                        attr = s_dict["attributes"]
                        if "platform" in attr and attr["platform"] == DOMAIN:
                            device_type = attr.get("device_type", "")
                            if device_type in ["analog_sensor", "Meter"]:
                                sensors_analog.append(s_dict["entity_id"])
                            elif device_type == "digital_sensor":
                                sensors_digital.append(s_dict["entity_id"])
                            elif device_type in ["Jalousie", "Gate", "Window"]:
                                covers.append(s_dict["entity_id"])
                            elif device_type in ["Switch", "TimedSwitch"]:
                                switches.append(s_dict["entity_id"])
                            elif device_type == "Pushbutton":
                                buttons.append(s_dict["entity_id"])
                            elif device_type in ["LightControllerV2"]:
                                lights.append(s_dict["entity_id"])
                            elif device_type == "Dimmer":
                                dimmers.append(s_dict["entity_id"])
                            elif device_type == "IRoomControllerV2":
                                climates.append(s_dict["entity_id"])
                            elif device_type == "Ventilation":
                                fans.append(s_dict["entity_id"])
                            elif device_type == "AcControl":
                                accontrols.append(s_dict["entity_id"])
                            elif device_type == "Slider":
                                numbers.append(s_dict["entity_id"])
                            elif device_type == "TextInput":
                                texts.append(s_dict["entity_id"])

                    sensors_analog.sort()
                    sensors_digital.sort()
                    covers.sort()
                    switches.sort()
                    buttons.sort()
                    lights.sort()
                    climates.sort()
                    dimmers.sort()
                    fans.sort()
                    accontrols.sort()
                    numbers.sort()
                    texts.sort()
                    await async_setup_component(hass, "group", {})
                    await create_group_for_loxone_entities(
                        hass, sensors_analog, "Loxone Analog Sensors", "loxone_analog"
                    )
                    await create_group_for_loxone_entities(
                        hass,
                        sensors_digital,
                        "Loxone Digital Sensors",
                        "loxone_digital",
                    )
                    await create_group_for_loxone_entities(
                        hass, switches, "Loxone Switches", "loxone_switches"
                    )
                    await create_group_for_loxone_entities(
                        hass, buttons, "Loxone Buttons", "loxone_buttons"
                    )
                    await create_group_for_loxone_entities(
                        hass, covers, "Loxone Covers", "loxone_covers"
                    )
                    await create_group_for_loxone_entities(
                        hass, lights, "Loxone LightControllers", "loxone_lights"
                    )
                    await create_group_for_loxone_entities(
                        hass, lights, "Loxone Dimmer", "loxone_dimmers"
                    )
                    await create_group_for_loxone_entities(
                        hass, climates, "Loxone Room Controllers", "loxone_climates"
                    )
                    await create_group_for_loxone_entities(
                        hass,
                        fans,
                        "Loxone Ventilation Controllers",
                        "loxone_ventilations",
                    )
                    await create_group_for_loxone_entities(
                        hass,
                        accontrols,
                        "Loxone AC Controllers",
                        "loxone_accontrollers",
                    )
                    await create_group_for_loxone_entities(
                        hass, numbers, "Loxone Numbers", "loxone_numbers"
                    )
                    await create_group_for_loxone_entities(
                        hass, texts, "Loxone Texts", "loxone_texts"
                    )
                    await hass.async_block_till_done()
                    await create_group_for_loxone_entities(
                        hass,
                        [
                            "group.loxone_analog",
                            "group.loxone_digital",
                            "group.loxone_switches",
                            "group.loxone_buttons",
                            "group.loxone_covers",
                            "group.loxone_lights",
                            "group.loxone_ventilations",
                            "group.loxone_numbers",
                            "group.loxone_texts",
                        ],
                        "Loxone Group",
                        "loxone_group",
                    )
                except Exception as err:
                    _LOGGER.error(
                        "Can't create group '%s'. Try to make at least one group manually. ("
                        "https://www.home-assistant.io/integrations/group/)",
                        err,
                    )

    def start_event() -> None:
        # This connection lives until unload/stop. A tracked startup task would
        # keep async_block_till_done() waiting forever when HA boots with an entry.
        coordinator._listening_task = config_entry.async_create_background_task(
            hass,
            coordinator.api.start_listening(callback=message_callback),
            "loxone_websocket_listener",
        )
        coordinator._listening_task.add_done_callback(handle_task_result)

    async def stop_event(_):
        coordinator._unloading = True
        token = coordinator.api.get_token_dict()
        hass.config_entries.async_update_entry(
            config_entry,
            data={
                **config_entry.data,  # preserve existing data
                **token,
            },
        )
        await coordinator.api.close()
    coordinator.listeners = [
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_event),
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, loxone_discovered),
    ]

    start_event()

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True


class LoxoneEntity(Entity):
    """
    @DynamicAttrs
    """

    def __init__(self, **kwargs):
        self._config_entry_id = kwargs.pop("config_entry_id", None)
        self._gateway_id = kwargs.pop("gateway_id", None) or self._config_entry_id
        for key in kwargs:
            if not hasattr(self, key):
                if key == "name":
                    self._attr_name = kwargs[key]
                else:
                    setattr(self, key, kwargs[key])
            else:
                try:
                    setattr(self, key, kwargs[key])
                except AttributeError:
                    _LOGGER.error(f"Could set {key} for {self.name}")
                except Exception as err:
                    raise HomeAssistantError(
                        f"Could not set Loxone entity attribute {key!r}"
                    ) from err

        self.listener = None

        # Initialize base extra state attributes with common Loxone fields
        self._attr_extra_state_attributes = {
            "uuid": kwargs.get("uuidAction", ""),
            "platform": "loxone",
        }

        # Add optional common attributes from Loxone JSON if they exist
        if "room" in kwargs and kwargs["room"]:
            self._attr_extra_state_attributes["room"] = kwargs["room"]
        if "cat" in kwargs and kwargs["cat"]:
            self._attr_extra_state_attributes["category"] = kwargs["cat"]

    async def async_added_to_hass(self):
        """Subscribe to device events."""
        if self._config_entry_id is None:
            platform = getattr(self, "platform", None)
            config_entry = getattr(platform, "config_entry", None)
            self._config_entry_id = getattr(config_entry, "entry_id", None)
        async def scoped_event_handler(event):
            if not isinstance(event.data, dict):
                return
            event_entry_id = event.data.get(ATTR_CONFIG_ENTRY_ID)
            if event_entry_id is not None and event_entry_id != self._config_entry_id:
                return
            await self.event_handler(event)

        self.listener = self.hass.bus.async_listen(EVENT, scoped_event_handler)

    def _get_coordinator(self):
        """Return the coordinator that owns this entity."""
        if not self._config_entry_id:
            raise HomeAssistantError("Loxone entity is not associated with a config entry")
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry_id)
        if coordinator is None:
            raise HomeAssistantError("The Loxone config entry is not loaded")
        return coordinator

    async def async_send_command(self, uuid: str, value) -> None:
        """Send a command only through this entity's Miniserver connection."""
        await self._get_coordinator().api.send_websocket_command(uuid, value)

    async def async_send_secured_command(self, uuid: str, value, code: str) -> None:
        """Send a secured command only through this entity's Miniserver connection."""
        await self._get_coordinator().api.send_secured__websocket_command(
            uuid, value, code
        )

    async def async_will_remove_from_hass(self):
        """Disconnect callbacks."""
        if self.listener:
            self.listener()
        self.listener = None

    async def event_handler(self, e):
        pass

    @cached_property
    def name(self):
        return self._attr_name

    # @name.setter
    # def name(self, n):
    #     self._attr_name = n

    @staticmethod
    def _clean_unit(lox_format):
        search = re.search(cfmt, lox_format, flags=re.X)
        if search:
            unit = lox_format.replace(search.group(0).strip(), "").strip()
            if unit == "%%":
                unit = unit.replace("%%", "%")
            return unit
        else:
            return lox_format

    @staticmethod
    def _get_format(lox_format):
        search = re.search(cfmt, lox_format, flags=re.X)
        if search:
            return search.group(0).strip()
        return None

    @cached_property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self.uuidAction
