"""
Component to create an interface to the Loxone Miniserver.

For more details about this component, please refer to the documentation at
https://github.com/JoDehli/pyloxone-api
"""

import asyncio
import logging
import warnings

import aiohttp

from .const import TIMEOUT
from .exceptions import (LoxoneMaxNumOfConnectionsError,
                         LoxoneServiceUnAvailableError,
                         LoxoneUnauthorisedError,
                         LoxoneUnrecognizedCommandError)

_LOGGER = logging.getLogger(__name__)


class LoxoneAsyncHttpClient:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        scheme: str = "http",
        verify_ssl: bool = True,
        session: aiohttp.ClientSession = None,
        timeout: float = TIMEOUT,
    ):
        # Validate input parameters
        if not url:
            raise ValueError("URL cannot be empty")
        if not username:
            raise ValueError("Username cannot be empty")
        if not password:
            raise ValueError("Password cannot be empty")
        if scheme not in ("http", "https"):
            raise ValueError(f"Invalid scheme '{scheme}'. Must be 'http' or 'https'")
        if not isinstance(verify_ssl, bool):
            raise ValueError("verify_ssl must be a boolean")
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number")

        # super().__init__()
        if session is None:
            self.session = aiohttp.ClientSession()
            self._own_session = True
        # session.auth = aiohttp.BasicAuth(username, password)
        else:
            if session.closed:
                raise ValueError("Provided session is already closed")
            self.session = session
            self._own_session = False

        self.timeout = float(timeout)
        self.base_url = f"{scheme}://{url}"
        self.scheme = scheme
        self.verify_ssl = verify_ssl
        self.username = username
        self.password = password
        self._closed = False

    async def get(self, endpoint):
        if self._closed:
            raise RuntimeError("HTTP client has been closed")

        if not endpoint:
            raise ValueError("Endpoint cannot be empty")

        if not endpoint.startswith("/"):
            _LOGGER.warning(f"Endpoint '{endpoint}' should start with '/'")
            endpoint = f"/{endpoint}"

        url = f"{self.base_url}{endpoint}"
        response = None

        try:
            _LOGGER.debug("Making Miniserver HTTP request")
            request_kwargs = {
                "auth": aiohttp.BasicAuth(self.username, self.password),
                "timeout": aiohttp.ClientTimeout(total=self.timeout),
            }
            if self.scheme == "https":
                request_kwargs["ssl"] = self.verify_ssl

            response = await self.session.get(url, **request_kwargs)

            if response.status != 200:
                await self._handle_error(response)

            return response

        except asyncio.TimeoutError as err:
            raise TimeoutError(
                f"Miniserver request timed out after {self.timeout} seconds"
            ) from err

        except aiohttp.ClientSSLError as err:
            raise ConnectionError("Miniserver TLS connection failed") from err

        except aiohttp.ClientProxyConnectionError as err:
            raise ConnectionError("Miniserver proxy connection failed") from err

        except aiohttp.ClientConnectorError as err:
            raise ConnectionError("Cannot resolve or connect to Miniserver") from err

        except aiohttp.ServerDisconnectedError as err:
            raise ConnectionError("Miniserver disconnected unexpectedly") from err

        except aiohttp.ClientConnectionError as err:
            raise ConnectionError("Miniserver connection failed") from err

        except aiohttp.ClientPayloadError as err:
            raise ValueError("Invalid response payload from Miniserver") from err

        except aiohttp.ClientResponseError as err:
            raise RuntimeError("Invalid HTTP response from Miniserver") from err

        except aiohttp.ClientError as err:
            raise RuntimeError("Miniserver HTTP client error") from err

        except (
            LoxoneUnauthorisedError,
            LoxoneUnrecognizedCommandError,
            LoxoneServiceUnAvailableError,
            LoxoneMaxNumOfConnectionsError,
        ):
            # Re-raise Loxone-specific errors without wrapping
            raise

        except Exception as err:
            raise RuntimeError("Unexpected Miniserver HTTP error") from err

    async def close(self):
        if self._closed:
            _LOGGER.warning("HTTP client is already closed")
            return

        try:
            if self._own_session and not self.session.closed:
                await self.session.close()
            self._closed = True
            _LOGGER.debug("HTTP client closed successfully")
        except Exception as err:
            self._closed = True
            raise RuntimeError("Failed to close Miniserver HTTP session") from err

    @staticmethod
    async def _handle_error(response):
        try:
            # Consume the body so the connection can be reused, but never retain,
            # log, or surface untrusted response content.
            await asyncio.wait_for(response.content.read(), timeout=5.0)
        except Exception:
            pass

        # Handle specific HTTP status codes
        if response.status == 400:
            raise ValueError("Bad request to Loxone Miniserver")

        elif response.status == 401:
            raise LoxoneUnauthorisedError("Miniserver rejected the credentials")

        elif response.status == 403:
            raise PermissionError("Miniserver access forbidden")

        elif response.status == 404:
            raise LoxoneUnrecognizedCommandError("Miniserver command not found")

        elif response.status == 408:
            raise TimeoutError("Miniserver request timed out")

        elif response.status == 429:
            raise RuntimeError("Miniserver rate limit exceeded")

        elif response.status == 500:
            raise RuntimeError("Miniserver internal error")

        elif response.status == 502:
            raise ConnectionError("Miniserver gateway failed")

        elif response.status == 503:
            raise LoxoneServiceUnAvailableError(
                "The Miniserver is restarting and not ready for requests"
            )

        elif response.status == 504:
            raise TimeoutError("Miniserver gateway timed out")

        elif response.status == 901:
            raise LoxoneMaxNumOfConnectionsError(
                "Maximum number of Miniserver connections reached"
            )

        else:
            raise RuntimeError(f"Miniserver HTTP error {response.status}")

    # def __enter__(self):
    #     raise RuntimeError("Use 'async with' to create an AsyncHttpClient instance")
    #
    # def __exit__(self, exc_type, exc_value, traceback):
    #     pass
