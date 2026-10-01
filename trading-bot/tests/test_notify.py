"""bot/notify.py against a fake Bot API served through httpx.MockTransport (no network)."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from bot.config import ConfigError, load_settings
from bot.notify import (
    MAX_ATTEMPTS,
    MAX_MESSAGE_UNITS,
    MAX_PARTS,
    OFFSET_KEY,
    OFFSET_MAX_AGE,
    API_BASE,
    Command,
    ConsoleNotifier,
    Notifier,
    TelegramNotifier,
    _https_proxy_for,
    build_notifier,
    chunk_text,
    parse_command,
)

TOKEN = "123456:TEST-secret_token"
CHAT = "4242"
FOREIGN_CHAT = 999
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

Reply = httpx.Response | Callable[[httpx.Request], httpx.Response]


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def fail(status: int, description: str = "error", **parameters: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(status, json=body)


def raising(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


class FakeTelegram:
    """Just enough of the Bot API. getUpdates confirms (forgets) updates below `offset`, as
    Telegram does; `script` queues one-off replies per method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.paths: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self._scripted: dict[str, list[Reply]] = defaultdict(list)
        self._message_id = 100

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def script(self, method: str, *replies: Reply) -> None:
        self._scripted[method].extend(replies)

    def payloads(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((method, payload))
        self.paths.append(request.url.path)
        if self._scripted[method]:
            reply = self._scripted[method].pop(0)
            return reply if isinstance(reply, httpx.Response) else reply(request)
        if method == "getUpdates":
            if "offset" in payload:
                self.updates = [u for u in self.updates if u.get("update_id", 0) >= payload["offset"]]
            return ok(list(self.updates))
        if method == "sendMessage":
            self._message_id += 1
            return ok({"message_id": self._message_id, "chat": {"id": int(CHAT)}, "text": payload["text"]})
        return ok(True)


class FakeStore:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.kv.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.kv[key] = value


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def message_update(update_id: int, text: Any, chat_id: int = int(CHAT)) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "date": 0, "chat": {"id": chat_id, "type": "private"}, "text": text},
    }


def callback_update(
    update_id: int, data: Any, chat_id: int = int(CHAT), text: str | None = "Approve BUY SPY $1,500?"
) -> dict[str, Any]:
    message: dict[str, Any] = {"message_id": 77, "date": 0, "chat": {"id": chat_id}}
    if text is not None:
        message["text"] = text
    return {
        "update_id": update_id,
        "callback_query": {"id": f"cb{update_id}", "from": {"id": 1}, "message": message, "data": data},
    }


@pytest.fixture
def api() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make(api: FakeTelegram, sleeps: list[float]) -> Callable[..., TelegramNotifier]:
    created: list[TelegramNotifier] = []

    def factory(store: FakeStore | None = None, clock: Clock | None = None, **kwargs: Any) -> TelegramNotifier:
        notifier = TelegramNotifier(
            SecretStr(TOKEN),
            CHAT,
            store,
            transport=api.transport(),
            sleep=sleeps.append,
            clock=clock or Clock(),
            **kwargs,
        )
        created.append(notifier)
        return notifier

    yield factory
    for notifier in created:
        notifier.close()


# --------------------------------------------------------------------------- sending


def test_send_is_plain_text_and_injects_token_only_on_the_wire(api, make):
    make().send("*BUY* SPY_X [link](http://x)")

    [payload] = api.payloads("sendMessage")
    assert payload["text"] == "*BUY* SPY_X [link](http://x)"
    assert payload["chat_id"] == CHAT
    assert "parse_mode" not in payload
    assert api.paths == [f"/bot{TOKEN}/sendMessage"]


def test_send_splits_long_text_into_parts_under_the_limit(api, make):
    lines = [f"line {i:04d} " + "x" * 90 + "\n" for i in range(100)]  # 100 lines of 100 chars
    text = "".join(lines)

    make().send(text)

    sent = [p["text"] for p in api.payloads("sendMessage")]
    assert len(sent) == 3
    assert all(len(part) <= MAX_MESSAGE_UNITS for part in sent)
    assert all(part.endswith("\n") for part in sent)  # broken at line boundaries
    assert "".join(sent) == text


def test_send_skips_blank_text(api, make):
    notifier = make()
    notifier.send("")
    notifier.send("  \n\t ")
    assert api.calls == []


def test_send_truncates_runaway_messages(api, make):
    make().send("y" * (MAX_MESSAGE_UNITS * (MAX_PARTS + 5)))

    sent = [p["text"] for p in api.payloads("sendMessage")]
    assert len(sent) == MAX_PARTS + 1
    assert sent[-1] == "[truncated: 5 more parts not sent]"


def test_send_stops_after_a_failed_part(api, make):
    api.script("sendMessage", fail(400, "Bad Request: chat not found"))
    make().send("a" * (MAX_MESSAGE_UNITS + 10))
    assert len(api.payloads("sendMessage")) == 1


def test_chunk_text_prefers_line_breaks():
    assert chunk_text("aaa\nbbb\nccc\n", limit=8) == ["aaa\nbbb\n", "ccc\n"]


def test_chunk_text_hard_splits_long_lines():
    assert chunk_text("abcdefghij", limit=4) == ["abcd", "efgh", "ij"]


def test_chunk_text_counts_utf16_units():
    assert chunk_text("😀😀😀", limit=4) == ["😀😀", "😀"]
    assert chunk_text("x" * 10 + "\ud800", limit=100) == ["x" * 10 + "\ud800"]  # lone surrogate


def test_chunk_text_default_limit_holds_for_a_single_long_line():
    parts = chunk_text("z" * 9001)
    assert [len(p) for p in parts] == [4000, 4000, 1001]


# --------------------------------------------------------------------------- approvals


def test_request_approval_sends_inline_keyboard_and_returns_message_id(api, make):
    message_id = make().request_approval(17, "BUY 10 SPY @ 150 ($1,500). Approve?")

    [payload] = api.payloads("sendMessage")
    assert payload["text"] == "BUY 10 SPY @ 150 ($1,500). Approve?"
    assert payload["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Approve", "callback_data": "approve:17"}],
            [{"text": "Reject", "callback_data": "reject:17"}],
        ]
    }
    assert "parse_mode" not in payload
    assert message_id == 101


def test_request_approval_puts_buttons_on_the_last_part(api, make):
    message_id = make().request_approval(3, "q" * (MAX_MESSAGE_UNITS + 1))

    first, last = api.payloads("sendMessage")
    assert "reply_markup" not in first
    assert last["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:3"
    assert message_id == 102


def test_request_approval_returns_none_on_failure(api, make):
    api.script("sendMessage", fail(403, "Forbidden: bot was blocked by the user"))
    assert make().request_approval(1, "Approve?") is None


def test_request_approval_returns_none_on_network_error(api, make):
    api.script("sendMessage", *[raising(httpx.ConnectError("down"))] * MAX_ATTEMPTS)
    assert make().request_approval(1, "Approve?") is None


# --------------------------------------------------------------------------- polling


def test_poll_is_non_blocking_by_default_and_limits_update_types(api, make):
    make().poll()
    [payload] = api.payloads("getUpdates")
    assert payload["timeout"] == 0
    assert payload["allowed_updates"] == ["message", "callback_query"]
    assert "offset" not in payload


def test_poll_long_polls_when_asked(api, make):
    make(poll_timeout_s=25).poll()
    assert api.payloads("getUpdates")[0]["timeout"] == 25


@pytest.mark.parametrize(
    ("text", "kind", "argument"),
    [
        ("/kill spreads look wrong", "kill", "spreads look wrong"),
        ("/kill", "kill", ""),
        ("/KILL@MyTradingBot  flash crash ", "kill", "flash crash"),
        ("/status", "status", ""),
        ("/pnl", "pnl", ""),
        ("/help", "help", ""),
        ("/start", "help", ""),
        ("/resume", "unknown", ""),
        ("hello bot", "unknown", "bot"),
    ],
)
def test_poll_parses_commands(api, make, text, kind, argument):
    api.updates = [message_update(1, text)]

    [command] = make().poll()

    assert command.kind == kind
    assert command.approval_id is None
    assert command.chat_id == CHAT
    assert command.text == text.strip()
    assert command.argument == argument


def test_parse_command_handles_empty_text():
    assert parse_command("") == "unknown"
    assert parse_command("   ") == "unknown"


def test_approve_callback_is_answered_and_the_message_edited(api, make):
    api.updates = [callback_update(5, "approve:17")]

    commands = make().poll()

    assert commands == [Command(kind="approve", approval_id=17, chat_id=CHAT, text="approve:17", message_id=77)]
    assert api.payloads("answerCallbackQuery") == [{"callback_query_id": "cb5", "text": "Approve received"}]
    assert api.payloads("editMessageText") == [
        {
            "chat_id": CHAT,
            "message_id": 77,
            "reply_markup": {"inline_keyboard": []},
            "text": "Approve BUY SPY $1,500?\n\nApprove received",
        }
    ]


def test_reject_callback_without_message_text_only_removes_buttons(api, make):
    api.updates = [callback_update(6, "reject:4", text=None)]

    commands = make().poll()

    assert commands == [Command(kind="reject", approval_id=4, chat_id=CHAT, text="reject:4", message_id=77)]
    assert api.payloads("answerCallbackQuery")[0]["text"] == "Reject received"
    assert api.payloads("editMessageReplyMarkup") == [
        {"chat_id": CHAT, "message_id": 77, "reply_markup": {"inline_keyboard": []}}
    ]
    assert api.payloads("editMessageText") == []


@pytest.mark.parametrize("data", ["approve:", "approve:x", "approve:-1", "approve:²", "delete:1", "", None, 5])
def test_unrecognised_callback_data_is_answered_but_yields_no_command(api, make, data):
    api.updates = [callback_update(7, data)]

    assert make().poll() == []
    assert api.payloads("answerCallbackQuery") == [{"callback_query_id": "cb7", "text": "Unknown button"}]
    assert "editMessageText" not in api.methods()


def test_foreign_chats_are_ignored_and_logged_once_per_chat(api, make, caplog):
    caplog.set_level(logging.WARNING, logger="bot.notify")
    api.updates = [
        message_update(1, "/kill", chat_id=FOREIGN_CHAT),
        message_update(2, "/kill again", chat_id=FOREIGN_CHAT),
        callback_update(3, "approve:1", chat_id=FOREIGN_CHAT),
        callback_update(4, "approve:2", chat_id=-100555),
        message_update(5, "/status"),
    ]

    commands = make().poll()

    assert [c.kind for c in commands] == ["status"]
    assert api.methods() == ["getUpdates"]  # no answer or edit for foreign button presses
    warnings = [r.getMessage() for r in caplog.records if "unauthorised chat" in r.getMessage()]
    assert warnings == [
        f"ignoring Telegram updates from unauthorised chat {FOREIGN_CHAT}",
        "ignoring Telegram updates from unauthorised chat -100555",
    ]


def test_malformed_updates_are_tolerated(api, make):
    api.script(
        "getUpdates",
        ok(
            [
                "not a dict",
                {"no_update_id": True},
                {"update_id": "8"},
                {"update_id": True},
                {"update_id": 10},
                {"update_id": 11, "message": "text"},
                {"update_id": 12, "message": {"text": "/kill"}},
                {"update_id": 13, "message": {"chat": {"id": "4242"}, "text": "/kill"}},
                {"update_id": 14, "message": {"chat": {"id": int(CHAT)}, "text": None}},
                {"update_id": 15, "message": {"chat": {"id": int(CHAT)}, "sticker": {}}},
                {"update_id": 16, "callback_query": {"id": "cb", "data": "approve:1"}},
                {"update_id": 17, "callback_query": {"message": {"chat": {"id": int(CHAT)}}, "data": "approve:2"}},
                {"update_id": 18, "callback_query": {"id": 5, "message": {"chat": None}, "data": "approve:3"}},
                message_update(19, "/pnl"),
            ]
        ),
    )
    notifier = make()

    commands = notifier.poll()

    # 17 is from the right chat with valid data but no query id or message id: it still counts,
    # there is just nothing to answer or edit.
    assert [(c.kind, c.approval_id) for c in commands] == [("approve", 2), ("pnl", None)]
    assert "answerCallbackQuery" not in api.methods()
    notifier.poll()
    assert api.payloads("getUpdates")[-1]["offset"] == 20


@pytest.mark.parametrize(
    "reply",
    [
        ok({"not": "a list"}),
        httpx.Response(200, text="<html>bad gateway</html>"),
        httpx.Response(200, json=["ok"]),
        fail(409, "Conflict: terminated by other getUpdates request"),
        fail(401, "Unauthorized"),
    ],
)
def test_bad_get_updates_responses_yield_no_commands(api, make, reply):
    api.script("getUpdates", reply)
    assert make().poll() == []


def test_poll_survives_a_bug_in_one_update(api, make, monkeypatch):
    notifier = make()
    original = notifier._handle_message

    def flaky(message):
        if message.get("text") == "/boom":
            raise RuntimeError("bug")
        return original(message)

    monkeypatch.setattr(notifier, "_handle_message", flaky)
    api.updates = [message_update(1, "/boom"), message_update(2, "/kill")]

    assert [c.kind for c in notifier.poll()] == ["kill"]


# --------------------------------------------------------------------------- offset persistence


def test_offset_is_persisted_and_restarts_do_not_replay(api, make):
    store = FakeStore()
    api.updates = [message_update(41, "/kill"), message_update(42, "/status")]

    assert [c.kind for c in make(store).poll()] == ["kill", "status"]
    assert json.loads(store.kv[OFFSET_KEY]) == {"offset": 43, "ts": NOW.isoformat()}

    restarted = make(store)  # e.g. the process crashed before Telegram saw the next offset
    assert restarted.poll() == []
    assert api.payloads("getUpdates")[-1]["offset"] == 43

    api.updates.append(message_update(43, "/pnl"))
    assert [c.kind for c in restarted.poll()] == ["pnl"]
    assert json.loads(store.kv[OFFSET_KEY])["offset"] == 44


def test_without_a_store_a_restart_replays_unconfirmed_updates(api, make):
    api.updates = [message_update(1, "/kill")]
    make().poll()
    assert [c.kind for c in make().poll()] == ["kill"]


def test_offset_advances_in_memory_between_polls(api, make):
    notifier = make()
    api.updates = [message_update(7, "/status")]
    notifier.poll()
    notifier.poll()
    assert api.payloads("getUpdates")[-1]["offset"] == 8


@pytest.mark.parametrize(
    "raw",
    ["", "garbage", "43", json.dumps({"offset": "43", "ts": NOW.isoformat()}),
     json.dumps({"offset": 43, "ts": "2026-09-30T12:00:00"}), json.dumps({"offset": 43})],
)
def test_unreadable_stored_offset_is_ignored(api, make, raw):
    store = FakeStore()
    store.kv[OFFSET_KEY] = raw
    make(store).poll()
    assert "offset" not in api.payloads("getUpdates")[0]


def test_stale_offset_is_dropped_so_reset_update_ids_are_not_swallowed(api, make):
    clock = Clock()
    store = FakeStore()
    api.updates = [message_update(5000, "/status")]
    notifier = make(store, clock)
    notifier.poll()

    clock.now += OFFSET_MAX_AGE + timedelta(minutes=1)
    api.updates = [message_update(12, "/kill")]  # Telegram restarted ids below our offset

    assert [c.kind for c in notifier.poll()] == ["kill"]
    assert "offset" not in api.payloads("getUpdates")[-1]
    assert [c.kind for c in make(store, clock).poll()] == []  # the new offset (13) was persisted


def test_store_failures_do_not_break_polling(api, make):
    class BrokenStore(FakeStore):
        def kv_get(self, key):
            raise RuntimeError("database is locked")

        def kv_set(self, key, value):
            raise RuntimeError("database is locked")

    api.updates = [message_update(1, "/kill")]
    assert [c.kind for c in make(BrokenStore()).poll()] == ["kill"]


# --------------------------------------------------------------------------- retries


def test_429_waits_for_retry_after_then_succeeds(api, make, sleeps):
    api.script("sendMessage", fail(429, "Too Many Requests: retry after 3", retry_after=3))

    make().send("hello")

    assert sleeps == [3.0]
    assert len(api.payloads("sendMessage")) == 2


def test_429_falls_back_to_the_retry_after_header(api, make, sleeps):
    api.script("sendMessage", httpx.Response(429, headers={"Retry-After": "2"}, text="slow down"))
    make().send("hello")
    assert sleeps == [2.0]


def test_429_with_an_excessive_wait_gives_up(api, make, sleeps):
    api.script("sendMessage", fail(429, "Too Many Requests", retry_after=600))
    make().send("hello")
    assert sleeps == []
    assert len(api.payloads("sendMessage")) == 1


def test_5xx_backs_off_and_gives_up_after_max_attempts(api, make, sleeps):
    api.script("sendMessage", *[httpx.Response(502, text="Bad Gateway")] * MAX_ATTEMPTS)

    make().send("hello")  # must not raise

    assert len(api.payloads("sendMessage")) == MAX_ATTEMPTS
    assert sleeps == [0.5, 1.0]


def test_network_errors_are_retried(api, make, sleeps):
    api.script("getUpdates", raising(httpx.ReadTimeout("timed out")))
    api.updates = [message_update(1, "/status")]

    assert [c.kind for c in make().poll()] == ["status"]
    assert sleeps == [0.5]


def test_client_errors_are_not_retried(api, make, sleeps):
    api.script("sendMessage", fail(400, "Bad Request: message text is empty"))
    make().send("hello")
    assert len(api.payloads("sendMessage")) == 1
    assert sleeps == []


def test_send_never_raises_on_unexpected_errors(api, make):
    api.script("sendMessage", raising(RuntimeError("boom")))
    make().send("hello")
    make().send(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- secrets


def test_token_never_reaches_the_logs(api, make, caplog):
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "bot.notify"):
        caplog.set_level(logging.DEBUG, logger=name)

    def leak(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not reach {request.url}")

    def leak_other(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"unexpected failure at {request.url}")

    api.script(
        "sendMessage",
        fail(429, "Too Many Requests", retry_after=1),
        ok({"message_id": 1}),
        *[leak] * MAX_ATTEMPTS,
        leak_other,
    )
    api.script("getUpdates", httpx.Response(502))
    api.updates = [callback_update(1, "approve:9"), message_update(2, "/kill", chat_id=FOREIGN_CHAT)]
    notifier = make(FakeStore())

    notifier.send("first")  # 429, then ok
    notifier.send("second")  # network errors whose message contains the real URL
    notifier.send("third")  # a non-httpx error containing the real URL
    notifier.request_approval(9, "Approve?")
    notifier.poll()  # 502, then updates with a callback and a foreign chat
    notifier.close()

    records = caplog.records
    assert any(r.name == "httpx" and "HTTP Request" in r.getMessage() for r in records)
    assert any("could not reach" in r.getMessage() for r in records)
    assert any("unexpected failure" in r.getMessage() for r in records)
    everything = caplog.text + "\n".join(r.getMessage() for r in records)
    assert TOKEN not in everything
    assert "TEST-secret_token" not in everything
    assert "123456%3ATEST" not in everything
    assert f"{API_BASE}/botREDACTED/sendMessage" in everything


def test_constructor_rejects_missing_values():
    with pytest.raises(ValueError):
        TelegramNotifier(SecretStr(" "), CHAT)
    with pytest.raises(ValueError):
        TelegramNotifier(SecretStr(TOKEN), "")
    with pytest.raises(ValueError):
        TelegramNotifier(SecretStr(TOKEN), CHAT, poll_timeout_s=-1)


def test_default_transport_honours_proxy_environment(monkeypatch):
    for name in ("https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert _https_proxy_for(API_BASE) is None

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    assert _https_proxy_for(API_BASE) == "http://proxy.internal:3128"

    monkeypatch.setenv("NO_PROXY", "api.telegram.org")
    assert _https_proxy_for(API_BASE) is None


# --------------------------------------------------------------------------- console and wiring


def test_console_notifier_logs_and_never_approves(caplog):
    caplog.set_level(logging.INFO, logger="bot.notify")
    console = ConsoleNotifier()

    console.send("filled BUY 1 SPY")

    assert console.request_approval(3, "Approve?") is None
    assert console.poll() == []
    assert "filled BUY 1 SPY" in caplog.text
    assert "approval #3" in caplog.text


def test_both_notifiers_satisfy_the_protocol(make):
    assert isinstance(ConsoleNotifier(), Notifier)
    assert isinstance(make(), Notifier)


def test_build_notifier_picks_telegram_only_when_configured(tmp_path):
    base = {"BOT_DATA_DIR": str(tmp_path / "var")}
    assert isinstance(build_notifier(load_settings(env_file=None, environ=base)), ConsoleNotifier)

    half = load_settings(env_file=None, environ={**base, "TELEGRAM_BOT_TOKEN": TOKEN})
    assert isinstance(build_notifier(half), ConsoleNotifier)
    with pytest.raises(ConfigError):
        TelegramNotifier.from_settings(half)

    full = load_settings(env_file=None, environ={**base, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
    notifier = build_notifier(full)
    assert isinstance(notifier, TelegramNotifier)
    notifier.close()
