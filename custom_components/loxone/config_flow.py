"""Config and options flows for the Loxone integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .catalog import (
    EntityCandidate,
    build_action_options,
    build_entity_catalog,
    build_feedback_options,
    entity_is_selected,
)
from .const import (
    CONF_ALLOW_INSECURE_HTTP,
    CONF_DOOR_PROFILES,
    CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN,
    CONF_SCENE_GEN,
    CONF_SCENE_GEN_DELAY,
    CONF_SELECTED_ENTITIES,
    CONF_VERIFY_SSL,
    DEFAULT_ALLOW_INSECURE_HTTP,
    DEFAULT_DELAY_SCENE,
    DEFAULT_IP,
    DEFAULT_PORT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .pyloxone_api.connection import LoxoneConnection
from .pyloxone_api.exceptions import (
    LoxoneException,
    LoxoneUnauthorisedError,
)


def _connection_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    """Build the connection form with optional suggested values."""
    values = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=values.get(CONF_USERNAME, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD, default=values.get(CONF_PASSWORD, "")): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            ),
            vol.Required(CONF_HOST, default=values.get(CONF_HOST, DEFAULT_IP)): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(CONF_PORT, default=values.get(CONF_PORT, DEFAULT_PORT)): NumberSelector(
                NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=1, max=65535)
            ),
            vol.Required(
                CONF_VERIFY_SSL,
                default=values.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ): BooleanSelector(),
            vol.Required(
                CONF_ALLOW_INSECURE_HTTP,
                default=values.get(
                    CONF_ALLOW_INSECURE_HTTP, DEFAULT_ALLOW_INSECURE_HTTP
                ),
            ): BooleanSelector(),
            vol.Required(CONF_SCENE_GEN, default=values.get(CONF_SCENE_GEN, False)): BooleanSelector(),
            vol.Optional(
                CONF_SCENE_GEN_DELAY,
                default=values.get(CONF_SCENE_GEN_DELAY, DEFAULT_DELAY_SCENE),
            ): NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, min=3)),
            vol.Required(
                CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN,
                default=values.get(CONF_LIGHTCONTROLLER_SUBCONTROLS_GEN, False),
            ): BooleanSelector(),
        }
    )


def _reauth_schema(username: str) -> vol.Schema:
    """Build a credential-only reauthentication form without exposing a password."""
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=username): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            ),
        }
    )


def _validate_latin1(user_input: Mapping[str, Any]) -> None:
    """Validate credentials supported by the Loxone hashing implementation."""
    for key in (CONF_USERNAME, CONF_PASSWORD):
        try:
            str(user_input.get(key, "")).encode("latin-1")
        except UnicodeEncodeError as err:
            raise vol.Invalid(f"{key}_not_latin1") from err


def _validate_transport(user_input: Mapping[str, Any]) -> None:
    """Require an explicit opt-in before sending credentials over HTTP."""
    host = str(user_input.get(CONF_HOST, ""))
    port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
    parsed = urlparse(host if "://" in host else f"//{host}")
    effective_port = parsed.port or port
    scheme = parsed.scheme.casefold() or (
        "https" if effective_port == 443 else "http"
    )
    if scheme not in {"http", "https"}:
        raise vol.Invalid("invalid_scheme")
    if scheme == "http" and not user_input.get(CONF_ALLOW_INSECURE_HTTP, False):
        raise vol.Invalid("insecure_http")


def _normalize_embedded_port(user_input: dict[str, Any]) -> None:
    """Keep the separate port option consistent with a full URL."""
    host = str(user_input.get(CONF_HOST, ""))
    parsed = urlparse(host if "://" in host else f"//{host}")
    if parsed.port is not None:
        user_input[CONF_PORT] = parsed.port


def _flow_error_from_invalid(err: vol.Invalid) -> str:
    """Map validation failures to a translated config-flow error."""
    if str(err) == "insecure_http":
        return "insecure_connection"
    if str(err) == "invalid_scheme":
        return "invalid_url"
    return "invalid_characters"


async def _async_read_structure(
    hass,
    connection_options: Mapping[str, Any],
    token: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Connect once, authenticate and return structure plus refreshed token."""
    connection = LoxoneConnection(
        host=str(connection_options[CONF_HOST]),
        port=int(connection_options[CONF_PORT]),
        username=str(connection_options[CONF_USERNAME]),
        password=str(connection_options[CONF_PASSWORD]),
        token=dict(token) if token else None,
        verify_ssl=bool(connection_options.get(CONF_VERIFY_SSL, True)),
        allow_insecure_http=bool(
            connection_options.get(
                CONF_ALLOW_INSECURE_HTTP, DEFAULT_ALLOW_INSECURE_HTTP
            )
        ),
        timeout=15,
    )
    try:
        connection.connection = await connection.open(async_get_clientsession(hass))
        return dict(connection.structure_file), connection.get_token_dict()
    finally:
        await connection.close()


class _EntitySelectionMixin:
    """Shared entity and door-profile steps for config and options flows."""

    _structure: dict[str, Any]
    _candidates: list[EntityCandidate]
    _selected_entities: list[str]
    _door_profiles: dict[str, dict[str, str | None]]
    _door_keys: list[str]
    _door_position: int

    def _prepare_catalog(self) -> None:
        self._candidates = build_entity_catalog(self._structure)

    def _entity_schema(self, selected: list[str]) -> vol.Schema:
        options = [{"value": candidate.key, "label": candidate.label} for candidate in self._candidates]
        return vol.Schema(
            {
                vol.Required(CONF_SELECTED_ENTITIES, default=selected): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )

    async def _async_entity_step(
        self,
        user_input: dict[str, Any] | None,
        selected: list[str],
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="entities",
                data_schema=self._entity_schema(selected),
                description_placeholders={"count": str(len(self._candidates))},
            )

        self._selected_entities = list(user_input.get(CONF_SELECTED_ENTITIES, ()))
        self._door_keys = [
            candidate.key
            for candidate in self._candidates
            if candidate.platform == "lock" and candidate.key in self._selected_entities
        ]
        self._door_position = 0
        if self._door_keys:
            return await self.async_step_door_profile()
        return await self._async_finish_selection()

    async def async_step_door_profile(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure optional physical commands for every selected door."""
        door_key = self._door_keys[self._door_position]
        door = next(candidate for candidate in self._candidates if candidate.key == door_key)
        action_options = build_action_options(self._structure)
        existing = self._door_profiles.get(door_key, {})

        if user_input is not None:
            self._door_profiles[door_key] = {
                "lock_action": user_input.get("lock_action"),
                "unlock_action": user_input.get("unlock_action"),
                "open_action": user_input.get("open_action"),
                "assume_closed_after_open": user_input.get("assume_closed_after_open") is True,
                "admin_one_tap_use_login_password": user_input.get("admin_one_tap_use_login_password") is True,
            }
            if door_key.startswith("access_lock:"):
                self._door_profiles[door_key].update({
                    "locked_state": user_input.get("locked_state"),
                    "invert_locked_state": user_input.get("invert_locked_state") is True,
                })
            self._door_position += 1
            if self._door_position >= len(self._door_keys):
                return await self._async_finish_selection()
            return await self.async_step_door_profile()

        schema: dict[Any, Any] = {}
        if action_options:
            action_selector = SelectSelector(
                SelectSelectorConfig(
                    options=action_options,
                    multiple=False,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
            schema = {
                vol.Optional(
                    "lock_action",
                    description={"suggested_value": existing.get("lock_action")},
                ): action_selector,
                vol.Optional(
                    "unlock_action",
                    description={"suggested_value": existing.get("unlock_action")},
                ): action_selector,
                vol.Optional(
                    "open_action",
                    description={"suggested_value": existing.get("open_action")},
                ): action_selector,
            }
        schema[vol.Optional(
            "assume_closed_after_open", default=existing.get("assume_closed_after_open", False)
        )] = BooleanSelector()
        schema[vol.Optional(
            "admin_one_tap_use_login_password",
            default=existing.get("admin_one_tap_use_login_password", False),
        )] = BooleanSelector()
        if door_key.startswith("access_lock:"):
            feedback_options = build_feedback_options(self._structure)
            if feedback_options:
                schema[vol.Optional(
                    "locked_state", description={"suggested_value": existing.get("locked_state")}
                )] = SelectSelector(SelectSelectorConfig(
                    options=feedback_options, multiple=False, mode=SelectSelectorMode.DROPDOWN
                ))
                schema[vol.Optional(
                    "invert_locked_state", default=existing.get("invert_locked_state", False)
                )] = BooleanSelector()
        return self.async_show_form(
            step_id="door_profile",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "door": door.name,
                "current": str(self._door_position + 1),
                "total": str(len(self._door_keys)),
            },
        )

    async def _async_finish_selection(self) -> ConfigFlowResult:
        raise NotImplementedError


class LoxoneFlowHandler(_EntitySelectionMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial Loxone setup."""

    VERSION = 5

    def __init__(self) -> None:
        self._connection_options: dict[str, Any] = {}
        self._token: dict[str, Any] = {}
        self._structure = {}
        self._candidates = []
        self._selected_entities = []
        self._door_profiles = {}
        self._door_keys = []
        self._door_position = 0

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connect to the Miniserver and load its entity catalog."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _validate_latin1(user_input)
                user_input[CONF_PORT] = int(user_input[CONF_PORT])
                _normalize_embedded_port(user_input)
                _validate_transport(user_input)
                user_input[CONF_SCENE_GEN_DELAY] = int(user_input.get(CONF_SCENE_GEN_DELAY, DEFAULT_DELAY_SCENE))
                self._structure, self._token = await _async_read_structure(self.hass, user_input)
            except vol.Invalid as err:
                errors["base"] = _flow_error_from_invalid(err)
            except LoxoneUnauthorisedError:
                errors["base"] = "invalid_auth"
            except (LoxoneException, OSError, TimeoutError, ValueError, RuntimeError):
                errors["base"] = "cannot_connect"
            else:
                serial = str(self._structure.get("msInfo", {}).get("serialNr", ""))
                if serial:
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_configured()
                self._connection_options = dict(user_input)
                self._prepare_catalog()
                return await self.async_step_entities()

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(user_input),
            errors=errors,
        )

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select the exact entities to create."""
        return await self._async_entity_step(user_input, [])

    async def _async_finish_selection(self) -> ConfigFlowResult:
        title = str(self._structure.get("msInfo", {}).get("msName") or "Loxone")
        return self.async_create_entry(
            title=title,
            data=self._token,
            options={
                **self._connection_options,
                CONF_SELECTED_ENTITIES: self._selected_entities,
                CONF_DOOR_PROFILES: self._door_profiles,
            },
        )

    async def async_step_import(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Import legacy YAML configuration without changing its import-all behaviour."""
        user_input.setdefault(CONF_ALLOW_INSECURE_HTTP, True)
        return self.async_create_entry(title="Loxone", data={}, options=user_input)

    async def async_step_reauth(
        self, _entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after Home Assistant detects rejected credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement credentials and reload the existing entry."""
        reauth_entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            connection_options = {**reauth_entry.options, **user_input}
            try:
                _validate_latin1(connection_options)
                _validate_transport(connection_options)
                structure, token = await _async_read_structure(
                    self.hass, connection_options
                )
            except vol.Invalid as err:
                errors["base"] = _flow_error_from_invalid(err)
            except LoxoneUnauthorisedError:
                errors["base"] = "invalid_auth"
            except (LoxoneException, OSError, TimeoutError, ValueError, RuntimeError):
                errors["base"] = "cannot_connect"
            else:
                serial = str(structure.get("msInfo", {}).get("serialNr", ""))
                if not serial:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_mismatch(reason="wrong_miniserver")
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data=token,
                        options=connection_options,
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(
                str(reauth_entry.options.get(CONF_USERNAME, ""))
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the flow used to change connection and entity selection."""
        return LoxoneOptionsFlow()


class LoxoneOptionsFlow(_EntitySelectionMixin, config_entries.OptionsFlowWithReload):
    """Change connection details or selected entities."""

    def __init__(self) -> None:
        self._structure = {}
        self._candidates = []
        self._selected_entities = []
        self._door_profiles = {}
        self._door_keys = []
        self._door_position = 0
        self._pending_options: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the configuration menu."""
        return self.async_show_menu(step_id="init", menu_options=["entities", "connection"])

    async def _ensure_structure(self) -> None:
        if self._structure:
            return
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator and coordinator.miniserver:
            self._structure = dict(coordinator.miniserver.lox_config.json)
        else:
            self._structure, _ = await _async_read_structure(
                self.hass, self.config_entry.options, self.config_entry.data
            )
        self._prepare_catalog()

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add or remove selected Loxone entities."""
        try:
            await self._ensure_structure()
        except (LoxoneException, OSError, TimeoutError, ValueError, RuntimeError):
            return self.async_abort(reason="cannot_connect")
        self._pending_options = dict(self.config_entry.options)
        configured_profiles = self.config_entry.options.get(CONF_DOOR_PROFILES, {})
        self._door_profiles = (
            dict(configured_profiles) if isinstance(configured_profiles, dict) else {}
        )
        selected = list(
            self.config_entry.options.get(
                CONF_SELECTED_ENTITIES,
                [
                    candidate.key
                    for candidate in self._candidates
                    if entity_is_selected(self.config_entry, candidate.key)
                ],
            )
        )
        return await self._async_entity_step(user_input, selected)

    async def async_step_connection(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Validate and update Miniserver connection settings."""
        errors: dict[str, str] = {}
        defaults = self.config_entry.options
        if user_input is not None:
            try:
                _validate_latin1(user_input)
                user_input[CONF_PORT] = int(user_input[CONF_PORT])
                _normalize_embedded_port(user_input)
                _validate_transport(user_input)
                user_input[CONF_SCENE_GEN_DELAY] = int(user_input.get(CONF_SCENE_GEN_DELAY, DEFAULT_DELAY_SCENE))
                structure, token = await _async_read_structure(self.hass, user_input)
            except vol.Invalid as err:
                errors["base"] = _flow_error_from_invalid(err)
            except LoxoneUnauthorisedError:
                errors["base"] = "invalid_auth"
            except (LoxoneException, OSError, TimeoutError, ValueError, RuntimeError):
                errors["base"] = "cannot_connect"
            else:
                serial = str(structure.get("msInfo", {}).get("serialNr", ""))
                if self.config_entry.unique_id and serial != self.config_entry.unique_id:
                    errors["base"] = "wrong_miniserver"
                else:
                    self.hass.config_entries.async_update_entry(self.config_entry, data=token)
                    return self.async_create_entry(data={**self.config_entry.options, **user_input})
        return self.async_show_form(
            step_id="connection",
            data_schema=_connection_schema(user_input or defaults),
            errors=errors,
        )

    async def _async_finish_selection(self) -> ConfigFlowResult:
        active_profiles = {key: value for key, value in self._door_profiles.items() if key in self._selected_entities}
        return self.async_create_entry(
            data={
                **self._pending_options,
                CONF_SELECTED_ENTITIES: self._selected_entities,
                CONF_DOOR_PROFILES: active_profiles,
            }
        )
