import asyncio
import logging

import httpx

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def get_client() -> httpx.AsyncClient:
    """One connection pool per event loop, shared by Groq calls.

    Tests replace ``httpx.AsyncClient`` with a fake; because the lookup happens
    here, the fake is honoured as usual.
    """
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if (
        _client is None
        or _client_loop is not loop
        or getattr(_client, "is_closed", False)
    ):
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        _client_loop = loop
    return _client


async def close_client() -> None:
    global _client, _client_loop
    if _client is not None and not getattr(_client, "is_closed", False):
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None
    _client_loop = None


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(float(value), 30.0)
    except ValueError:
        return None


def _rewind_files(files: dict | None) -> None:
    for value in (files or {}).values():
        handle = value[1] if isinstance(value, tuple) else value
        if hasattr(handle, "seek"):
            handle.seek(0)


async def post_with_retries(
    url: str,
    *,
    json: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    headers: dict[str, str],
    timeout: float,
    max_retries: int = 3,
) -> httpx.Response:
    """POST with retries on network errors, 429 and 5xx.

    The final response is returned even for 429/5xx so callers can report the
    upstream reason; network errors on the last attempt are raised.
    """
    client = get_client()
    response: httpx.Response | None = None
    for attempt in range(max(1, max_retries)):
        _rewind_files(files)
        try:
            response = await client.post(
                url,
                json=json,
                data=data,
                files=files,
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            if attempt == max_retries - 1:
                raise
            logging.warning("HTTP request failed (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(2 ** attempt)
            continue

        if response.status_code == 429:
            if attempt == max_retries - 1:
                return response
            wait = _retry_after(response) or (2 ** attempt)
            logging.warning("Rate limited (attempt %d); retry in %.1fs", attempt + 1, wait)
            await asyncio.sleep(wait)
            continue

        if response.status_code >= 500:
            if attempt == max_retries - 1:
                return response
            logging.warning("Upstream HTTP %s (attempt %d)", response.status_code, attempt + 1)
            await asyncio.sleep(2 ** attempt)
            continue

        return response

    if response is None:
        raise httpx.HTTPError("request failed")
    return response
