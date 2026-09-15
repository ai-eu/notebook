import asyncio
import logging
from pathlib import Path

import httpx

from app.config import settings


class TelegramError(Exception):
    """The Bot API answered with an error or is unreachable."""


class TelegramNotConfigured(TelegramError):
    """TELEGRAM_ENABLED / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set."""


def is_configured() -> bool:
    return bool(settings.telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id)


def mask_token(token: str | None) -> str:
    if not token:
        return "-"
    if len(token) <= 12:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


def _token() -> str:
    if not settings.telegram_bot_token:
        raise TelegramNotConfigured("TELEGRAM_BOT_TOKEN is not set")
    return settings.telegram_bot_token


def channel_id() -> str:
    if not settings.telegram_chat_id:
        raise TelegramNotConfigured("TELEGRAM_CHAT_ID is not set (run `python -m app.cli tg-discover`)")
    return settings.telegram_chat_id


def require_configuration() -> None:
    if not settings.telegram_enabled:
        raise TelegramNotConfigured("TELEGRAM_ENABLED is false")
    _token()
    channel_id()


def _new_client(timeout: float | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout or settings.telegram_timeout)


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _backoff(attempt: int) -> float:
    return min(2 ** (attempt - 1), 8)


def _method_url(method: str) -> str:
    return f"{settings.telegram_api_base.rstrip('/')}/bot{_token()}/{method}"


def _rewind(files: dict | None) -> None:
    """A retried upload has to send the same file from the beginning again."""
    for value in (files or {}).values():
        handle = value[1] if isinstance(value, tuple) else value
        if hasattr(handle, "seek"):
            handle.seek(0)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        retry_after = response.json().get("parameters", {}).get("retry_after")
    except ValueError:
        return None
    if retry_after is None:
        return None
    return min(float(retry_after), 30.0)


def _payload(response: httpx.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        raise TelegramError(f"Bot API returned a non-JSON response: {response.text[:200]}")


async def _api_call(
    method: str,
    *,
    data: dict | None = None,
    files: dict | None = None,
    retries: int | None = None,
) -> list | dict:
    """POST a Bot API method, retrying network errors, 429 and 5xx."""
    url = _method_url(method)
    attempts = max(1, retries if retries is not None else settings.telegram_max_retries)
    for attempt in range(1, attempts + 1):
        _rewind(files)
        try:
            async with _new_client() as client:
                response = await client.post(url, data=data or {}, files=files)
        except httpx.HTTPError as exc:
            if attempt == attempts:
                raise TelegramError(f"{method} failed: {exc}") from exc
            await _pause(_backoff(attempt))
            continue

        if response.status_code == 200:
            payload = _payload(response)
            if payload.get("ok"):
                return payload.get("result") or {}
            raise TelegramError(f"{method} rejected: {payload.get('description')}")

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == attempts:
                raise TelegramError(f"{method} failed with HTTP {response.status_code}: {response.text[:200]}")
            delay = _retry_after(response)
            await _pause(delay if delay is not None else _backoff(attempt))
            continue

        raise TelegramError(f"{method} failed with HTTP {response.status_code}: {response.text[:200]}")

    raise TelegramError(f"{method} failed")


async def get_me() -> dict:
    result = await _api_call("getMe")
    return result if isinstance(result, dict) else {}


async def get_updates(timeout: int = 0) -> list[dict]:
    result = await _api_call("getUpdates", data={"timeout": timeout})
    return result if isinstance(result, list) else []


async def discover_chats() -> list[dict]:
    """Chats the bot has recently seen — used to find the id of the channel."""
    seen: dict[int, dict] = {}
    for update in await get_updates():
        for payload in update.values():
            if not isinstance(payload, dict):
                continue
            chat = payload.get("chat") or {}
            if "id" not in chat:
                continue
            seen[chat["id"]] = {
                "id": chat["id"],
                "type": chat.get("type"),
                "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
            }
    return list(seen.values())


async def send_document(
    path: Path,
    *,
    caption: str,
    mime_type: str = "application/octet-stream",
    chat_id: str | None = None,
) -> dict:
    """Upload a file into the channel. Returns the raw Message object."""
    target = chat_id or channel_id()
    with path.open("rb") as handle:
        files = {"document": (path.name, handle, mime_type)}
        data = {
            "chat_id": target,
            "caption": caption,
            # Every upload would otherwise buzz the phone of the channel owner.
            "disable_notification": "true",
        }
        result = await _api_call("sendDocument", data=data, files=files)
    return result if isinstance(result, dict) else {}


async def get_file(file_id: str) -> str:
    result = await _api_call("getFile", data={"file_id": file_id})
    file_path = result.get("file_path") if isinstance(result, dict) else None
    if not file_path:
        raise TelegramError("getFile returned no file_path")
    return file_path


async def download(file_path: str, dest: Path) -> Path:
    """Stream a file from Telegram to dest."""
    url = f"{settings.telegram_api_base.rstrip('/')}/file/bot{_token()}/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    attempts = max(1, settings.telegram_max_retries)
    for attempt in range(1, attempts + 1):
        try:
            async with _new_client(timeout=settings.telegram_download_timeout) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise TelegramError(f"download failed with HTTP {response.status_code}")
                    with dest.open("wb") as handle:
                        async for block in response.aiter_bytes():
                            handle.write(block)
            return dest
        except (httpx.HTTPError, TelegramError) as exc:
            if attempt == attempts:
                raise TelegramError(f"download of {file_path} failed: {exc}") from exc
            logging.warning("Telegram download attempt %s failed: %s", attempt, exc)
            await _pause(_backoff(attempt))
    return dest


async def copy_message(
    message_id: int,
    *,
    from_chat_id: str | None = None,
    to_chat_id: str | None = None,
) -> dict:
    """Re-post a stored message; used to mint a fresh file_id when the old one expires."""
    source = from_chat_id or channel_id()
    result = await _api_call(
        "copyMessage",
        data={
            "chat_id": to_chat_id or source,
            "from_chat_id": source,
            "message_id": str(message_id),
        },
    )
    return result if isinstance(result, dict) else {}


async def delete_message(message_id: int, *, chat_id: str | None = None, retries: int = 1) -> None:
    """Remove a message from the channel; used when a recording is deleted."""
    await _api_call(
        "deleteMessage",
        data={"chat_id": chat_id or channel_id(), "message_id": str(message_id)},
        retries=retries,
    )
