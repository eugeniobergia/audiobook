import secrets
from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader

from audiobook.api.errors import AppError
from audiobook.config import Settings, get_settings

# auto_error=False: we decide what a missing key returns (our 401 and error shape),
# instead of FastAPI's default 403. Declaring the header also adds the "Authorize"
# button to /docs.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    api_key: Annotated[str | None, Security(api_key_header)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Reject the request with 401 unless it carries the configured API key."""
    # compare_digest takes the same time however much of the key matches, so
    # response timing can't be used to guess the key.
    if api_key is None or not secrets.compare_digest(
        api_key.encode(), settings.api_key.encode()
    ):
        raise AppError(401, "unauthorized", "Missing or invalid API key.")
