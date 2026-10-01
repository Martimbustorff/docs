"""Alerts, trade approvals and operator commands over the raw Telegram Bot API.

The bot token never reaches a log line. Requests are built against a placeholder path
(`/botREDACTED/<method>`) and `_TokenTransport` swaps the real token in on a copy of the request
just before it goes on the wire. httpx logs `request.url` at INFO and attaches the request to its
exceptions; both only ever see the placeholder, whatever the logging configuration.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from bot.config import ConfigError, Settings
from bot.timeutil import utcnow

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_UNITS = 4000  # Telegram's limit is 4096 UTF-16 code units; keep a margin
MAX_PARTS = 10  # a runaway message is cut off rather than flooding the chat
MAX_ATTEMPTS = 3
BACKOFF_S = 0.5  # 5xx and network errors wait 0.5s, then 1s
MAX_RETRY_WAIT_S = 30.0  # a 429 asking for a longer wait is given up on
REQUEST_TIMEOUT_S = 10.0
OFFSET_KEY = "telegram_offset"
# Telegram discards unconfirmed updates after 24h, and after a week with no updates it restarts
# update ids at a random value that can be below our offset (so getUpdates would silently drop
# new commands). Past this age a stored offset can no longer prevent a replay, only hide
# commands, so it is dropped.
OFFSET_MAX_AGE = timedelta(days=3)

_TOKEN_PLACEHOLDER = "REDACTED"
_ALLOWED_UPDATES = ["message", "callback_query"]
_CALLBACK_RE = re.compile(r"(approve|reject):(\d{1,18})", re.ASCII)
_NO_BUTTONS: dict[str, Any] = {"inline_keyboard": []}

CommandKind = Literal["approve", "reject", "kill", "status", "pnl", "help", "unknown"]
_COMMANDS: dict[str, CommandKind] = {
    "kill": "kill",
    "status": "status",
    "pnl": "pnl",
    "help": "help",
    "start": "help",  # Telegram sends /start when a chat first opens the bot
}


@dataclass(frozen=True)
class Command:
    """An instruction from the authorised chat.

    `text` is the stripped message text for slash commands and free text, and the callback data
    (`approve:<id>` / `reject:<id>`) for button presses.
    """

    kind: CommandKind
    approval_id: int | None
    chat_id: str
    text: str
    message_id: int | None = None  # the approval message a button belongs to

    @property
    def argument(self) -> str:
        """Whatever follows the command word, e.g. the reason in "/kill spreads look wrong"."""
        parts = self.text.split(maxsplit=1)
        return parts[1].strip() if len(parts) == 2 else ""


@runtime_checkable
class Notifier(Protocol):
    def send(self, text: str) -> None:
        """Deliver an alert. Never raises."""
        ...

    def request_approval(self, approval_id: int, text: str) -> int | None:
        """Send `text` with Approve/Reject buttons; return the message id, or None on failure."""
        ...

    def poll(self) -> list[Command]:
        """Commands and approval decisions received since the previous poll."""
        ...


class KeyValueStore(Protocol):
    """The slice of `bot.store.Store` the notifier needs to survive restarts."""

    def kv_get(self, key: str) -> str | None: ...

    def kv_set(self, key: str, value: str) -> None: ...


def parse_command(text: str) -> CommandKind:
    """Map "/kill reason", "/status@MyBot" and so on to a command kind; anything else is "unknown"."""
    words = text.split(maxsplit=1)
    if not words or not words[0].startswith("/"):
        return "unknown"
    name = words[0][1:].split("@", 1)[0].lower()
    return _COMMANDS.get(name, "unknown")


def chunk_text(text: str, limit: int = MAX_MESSAGE_UNITS) -> list[str]:
    """Split `text` into parts of at most `limit` UTF-16 code units (Telegram's unit, where an
    emoji counts 2), breaking after a newline where possible. Blank parts are dropped."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in _pieces(text, limit):
        units = _utf16_len(piece)
        if current and size + units > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(piece)
        size += units
    if current:
        chunks.append("".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def _pieces(text: str, limit: int) -> Iterator[str]:
    """The lines of `text` with their line breaks, hard-splitting any line longer than `limit`."""
    for line in text.splitlines(keepends=True):
        if _utf16_len(line) <= limit:
            yield line
            continue
        part: list[str] = []
        size = 0
        for char in line:
            units = _utf16_len(char)
            if part and size + units > limit:
                yield "".join(part)
                part, size = [], 0
            part.append(char)
            size += units
        if part:
            yield "".join(part)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _chat_id(message: Any) -> str | None:
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    return str(chat_id) if _is_int(chat_id) else None


def _https_proxy_for(url: str) -> str | None:
    """The HTTPS proxy from the environment (HTTPS_PROXY / NO_PROXY) for `url`, if any."""
    if urllib.request.proxy_bypass(httpx.URL(url).host):
        return None
    return urllib.request.getproxies().get("https")


class _TokenTransport(httpx.BaseTransport):
    """Puts the real token into a copy of each request's path, so the request httpx logs and
    attaches to exceptions keeps the placeholder."""

    def __init__(self, inner: httpx.BaseTransport, token: SecretStr) -> None:
        self._inner = inner
        self._token = token

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace(
            f"/bot{_TOKEN_PLACEHOLDER}/", f"/bot{self._token.get_secret_value()}/", 1
        )
        real = httpx.Request(
            request.method,
            request.url.copy_with(path=path),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return self._inner.handle_request(real)

    def close(self) -> None:
        self._inner.close()


@dataclass(frozen=True)
class _Outcome:
    result: Any = None
    error: str | None = None  # None means success
    retryable: bool = False
    retry_after_s: float | None = None  # from a 429; otherwise exponential backoff applies


class TelegramNotifier:
    """Plain-text Telegram notifier (no parse_mode, so no text can be misread as markup).

    Only `chat_id` is obeyed: updates from any other chat, including button presses, are
    ignored and logged once per chat. The getUpdates offset is persisted in `store` (kv key
    `telegram_offset`) so a restart does not replay commands.
    """

    def __init__(
        self,
        token: SecretStr,
        chat_id: str,
        store: KeyValueStore | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        poll_timeout_s: int = 0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not token.get_secret_value().strip():
            raise ValueError("the Telegram bot token is empty")
        if not chat_id.strip():
            raise ValueError("the Telegram chat id is empty")
        if poll_timeout_s < 0:
            raise ValueError("poll_timeout_s must be >= 0")
        self._token = token
        self._chat_id = chat_id.strip()
        self._store = store
        self._poll_timeout_s = poll_timeout_s
        self._sleep = sleep
        self._clock = clock
        # httpx ignores proxy environment variables once a transport is supplied, so the
        # default transport reads them itself.
        inner = transport or httpx.HTTPTransport(proxy=_https_proxy_for(API_BASE))
        self._client = httpx.Client(
            base_url=API_BASE,
            transport=_TokenTransport(inner, token),
            timeout=REQUEST_TIMEOUT_S,
        )
        self._warned_chats: set[str] = set()
        self._offset, self._offset_ts = self._load_offset()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        store: KeyValueStore | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> TelegramNotifier:
        if settings.telegram_bot_token is None or not settings.telegram_chat_id:
            raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set")
        return cls(settings.telegram_bot_token, settings.telegram_chat_id, store, transport=transport)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ Notifier

    def send(self, text: str) -> None:
        try:
            parts = chunk_text(text)
            if len(parts) > MAX_PARTS:
                dropped = len(parts) - MAX_PARTS
                logger.warning("Telegram message too long; dropping its last %d parts", dropped)
                parts = parts[:MAX_PARTS] + [f"[truncated: {dropped} more parts not sent]"]
            for number, part in enumerate(parts, start=1):
                if self._send_message(part) is None:
                    if len(parts) > 1:
                        logger.warning("Telegram part %d/%d failed; not sending the rest", number, len(parts))
                    return
        except Exception as exc:  # send() must never raise
            logger.error("Telegram send failed: %s", self._describe(exc))

    def request_approval(self, approval_id: int, text: str) -> int | None:
        keyboard = {
            "inline_keyboard": [
                [{"text": "Approve", "callback_data": f"approve:{approval_id}"}],
                [{"text": "Reject", "callback_data": f"reject:{approval_id}"}],
            ]
        }
        try:
            *head, last = chunk_text(text) or [f"Approval #{approval_id}"]
            for part in head:
                if self._send_message(part) is None:
                    return None
            return self._send_message(last, reply_markup=keyboard)
        except Exception as exc:
            logger.error("Telegram approval request failed: %s", self._describe(exc))
            return None

    def poll(self) -> list[Command]:
        payload: dict[str, Any] = {"timeout": self._poll_timeout_s, "allowed_updates": _ALLOWED_UPDATES}
        offset = self._current_offset()
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout_s=REQUEST_TIMEOUT_S + self._poll_timeout_s)
        if result is None:
            return []
        if not isinstance(result, list):
            logger.warning("Telegram getUpdates returned %s, not a list", type(result).__name__)
            return []
        updates = [u for u in result if isinstance(u, dict) and _is_int(u.get("update_id"))]
        if len(updates) < len(result):
            logger.warning("ignoring %d Telegram updates without an update_id", len(result) - len(updates))
        if updates:
            # Advance before handling, so an update that cannot be handled is never refetched.
            self._advance_offset(max(u["update_id"] for u in updates) + 1)
        commands = [self._handle_update(update) for update in updates]
        return [command for command in commands if command is not None]

    # ------------------------------------------------------------------ updates

    def _handle_update(self, update: dict[str, Any]) -> Command | None:
        try:
            if isinstance(update.get("callback_query"), dict):
                return self._handle_callback(update["callback_query"])
            if isinstance(update.get("message"), dict):
                return self._handle_message(update["message"])
        except Exception:
            logger.exception("skipping Telegram update %s", update["update_id"])
        return None

    def _handle_message(self, message: dict[str, Any]) -> Command | None:
        chat_id = _chat_id(message)
        if chat_id != self._chat_id:
            self._ignore_chat(chat_id)
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            logger.debug("ignoring a Telegram message without text")
            return None
        text = text.strip()
        return Command(kind=parse_command(text), approval_id=None, chat_id=chat_id, text=text)

    def _handle_callback(self, query: dict[str, Any]) -> Command | None:
        message = query.get("message")
        chat_id = _chat_id(message)
        if not isinstance(message, dict) or chat_id != self._chat_id:
            self._ignore_chat(chat_id)
            return None
        data = query.get("data")
        match = _CALLBACK_RE.fullmatch(data) if isinstance(data, str) else None
        if match is None:
            self._answer_callback(query.get("id"), "Unknown button")
            return None
        kind: CommandKind = "approve" if match.group(1) == "approve" else "reject"
        # The bot still checks expiry, price drift and risk; it confirms the outcome in a new message.
        label = "Approve received" if kind == "approve" else "Reject received"
        self._answer_callback(query.get("id"), label)
        self._show_decision(message, label)
        message_id = message.get("message_id")
        return Command(
            kind=kind,
            approval_id=int(match.group(2)),
            chat_id=chat_id,
            text=match.group(0),
            message_id=message_id if _is_int(message_id) else None,
        )

    def _answer_callback(self, query_id: Any, text: str) -> None:
        if isinstance(query_id, str) and query_id:
            self._call("answerCallbackQuery", {"callback_query_id": query_id, "text": text})

    def _show_decision(self, message: dict[str, Any], label: str) -> None:
        """Append the decision to the approval message and remove its buttons."""
        message_id = message.get("message_id")
        if not _is_int(message_id):
            return
        target = {"chat_id": self._chat_id, "message_id": message_id, "reply_markup": _NO_BUTTONS}
        text = message.get("text")
        if isinstance(text, str) and text:
            self._call("editMessageText", {**target, "text": f"{text}\n\n{label}"})
        else:  # an old or inaccessible message comes without its text
            self._call("editMessageReplyMarkup", target)

    def _ignore_chat(self, chat_id: str | None) -> None:
        key = chat_id or "unknown"
        if key not in self._warned_chats:
            self._warned_chats.add(key)
            logger.warning("ignoring Telegram updates from unauthorised chat %s", key)

    # ------------------------------------------------------------------ offset

    def _load_offset(self) -> tuple[int | None, datetime | None]:
        if self._store is None:
            return None, None
        try:
            raw = self._store.kv_get(OFFSET_KEY)
        except Exception:
            logger.exception("could not read the Telegram update offset")
            return None, None
        if raw is None:
            return None, None
        try:
            data = json.loads(raw)
            offset, ts = data["offset"], datetime.fromisoformat(data["ts"])
        except (TypeError, ValueError, KeyError):
            logger.warning("ignoring an unreadable Telegram update offset in the store")
            return None, None
        if not _is_int(offset) or ts.tzinfo is None:
            logger.warning("ignoring an invalid Telegram update offset in the store")
            return None, None
        return offset, ts

    def _current_offset(self) -> int | None:
        if self._offset_ts is not None and self._clock() - self._offset_ts > OFFSET_MAX_AGE:
            logger.info("Telegram update offset is older than %s; dropping it", OFFSET_MAX_AGE)
            self._offset, self._offset_ts = None, None
        return self._offset

    def _advance_offset(self, offset: int) -> None:
        self._offset, self._offset_ts = offset, self._clock()
        if self._store is None:
            return
        try:
            self._store.kv_set(OFFSET_KEY, json.dumps({"offset": offset, "ts": self._offset_ts.isoformat()}))
        except Exception:
            logger.exception("could not persist the Telegram update offset")

    # ------------------------------------------------------------------ HTTP

    def _send_message(self, text: str, reply_markup: dict[str, Any] | None = None) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return message_id if _is_int(message_id) else None

    def _call(self, method: str, payload: dict[str, Any], timeout_s: float = REQUEST_TIMEOUT_S) -> Any:
        """POST one Bot API method and return its `result`, or None once retries are exhausted.

        Retries 429 (after `retry_after`), 5xx and network errors, up to MAX_ATTEMPTS in total.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            outcome = self._attempt(method, payload, timeout_s)
            if outcome.error is None:
                return outcome.result
            if not outcome.retryable or attempt == MAX_ATTEMPTS:
                break
            delay = outcome.retry_after_s
            if delay is None:
                delay = BACKOFF_S * 2 ** (attempt - 1)
            if delay > MAX_RETRY_WAIT_S:
                break
            logger.info("Telegram %s: %s; retrying in %.1fs", method, outcome.error, delay)
            self._sleep(delay)
        logger.warning("Telegram %s failed: %s", method, outcome.error)
        return None

    def _attempt(self, method: str, payload: dict[str, Any], timeout_s: float) -> _Outcome:
        try:
            response = self._client.post(f"/bot{_TOKEN_PLACEHOLDER}/{method}", json=payload, timeout=timeout_s)
        except httpx.TransportError as exc:
            return _Outcome(error=self._describe(exc), retryable=True)
        except Exception as exc:  # keeps callers' never-raise promise
            return _Outcome(error=self._describe(exc))
        body = _json_object(response)
        if response.status_code == 429:
            retry_after = _retry_after(body, response)
            wait = "" if retry_after is None else f", retry after {retry_after:g}s"
            return _Outcome(error=f"rate limited (HTTP 429{wait})", retryable=True, retry_after_s=retry_after)
        if response.status_code >= 500:
            return _Outcome(error=f"HTTP {response.status_code}", retryable=True)
        if body is None or body.get("ok") is not True:
            description = body.get("description") if body is not None else None
            detail = self._redact(str(description)[:200]) if description else "no Bot API response"
            return _Outcome(error=f"HTTP {response.status_code}: {detail}")
        return _Outcome(result=body.get("result"))

    def _describe(self, exc: Exception) -> str:
        return self._redact(f"{type(exc).__name__}: {exc}")

    def _redact(self, text: str) -> str:
        secret = self._token.get_secret_value()
        return text.replace(secret, "***").replace(quote(secret, safe=""), "***")


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _retry_after(body: dict[str, Any] | None, response: httpx.Response) -> float | None:
    parameters = body.get("parameters") if body is not None else None
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if value is None:
        value = response.headers.get("Retry-After")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class ConsoleNotifier:
    """Logs instead of messaging. Nothing can press Approve, so approvals expire (fail-safe)."""

    def send(self, text: str) -> None:
        logger.info("notify: %s", text)

    def request_approval(self, approval_id: int, text: str) -> int | None:
        logger.info("approval #%d requested; no Telegram, so it will expire unanswered: %s", approval_id, text)
        return None

    def poll(self) -> list[Command]:
        return []


def build_notifier(settings: Settings, store: KeyValueStore | None = None) -> Notifier:
    """Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both set, else the console."""
    if settings.has_telegram:
        return TelegramNotifier.from_settings(settings, store)
    logger.info("Telegram is not configured; notifications go to the log")
    return ConsoleNotifier()
