"""The Membase tool provider: checks the credentials when a workspace saves them."""

from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError
from membase.errors import MembaseError, PermissionDeniedError

from membase_dify.client import KEYS_PAGE, MembaseToolError, explain, open_client


class MembaseProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        """Lists the key's containers: the cheapest call every Membase key may make."""
        try:
            with open_client(credentials) as client:
                client.containers.list()
        except PermissionDeniedError as err:
            raise ToolProviderCredentialValidationError(
                f"Membase refused this API key (403: {err.message}). It is mistyped, expired, "
                f"or its owner revoked it. Check or create a key under {KEYS_PAGE}."
            ) from err
        except MembaseError as err:
            raise ToolProviderCredentialValidationError(
                f"Could not verify the Membase API key. {explain(err)}"
            ) from err
        except MembaseToolError as err:
            raise ToolProviderCredentialValidationError(str(err)) from err
