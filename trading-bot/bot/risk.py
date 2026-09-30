"""Risk rules and the kill switch.

`RiskManager.check` applies the ordered rules from ARCHITECTURE.md to ENTRY orders. Orders that
reduce risk are always allowed, except by the live-trading guard, which blocks everything when
the deployment is misconfigured. Every entry-rule failure blocks (fail-closed).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.broker import assert_trading_allowed, floor_qty
from bot.config import ConfigError, RiskConfig, Settings
from bot.models import OrderIntent, RiskAction, RiskVerdict, Side, Signal, SignalKind
from bot.timeutil import ny_trading_day, utcnow

if TYPE_CHECKING:
    from bot.store import Store

log = logging.getLogger(__name__)

PEAK_KEY = "peak_bot_equity"
ERRORS_KEY = "consecutive_errors"
DAILY_LOSS_KEY = "daily_loss_block_day"  # New York date on which the daily loss limit was hit
MIN_ORDER_USD = 1.0  # Alpaca's minimum notional for fractional orders


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class KillSwitch:
    """Tripped when `var/KILL_SWITCH` exists or KILL_SWITCH=1 is set in the environment.

    The file survives restarts. If it cannot be written, the switch stays tripped in memory for
    the life of this process.
    """

    def __init__(self, settings: Settings, store: Store | None = None) -> None:
        self.settings = settings
        self.path = settings.kill_switch_path
        self._store = store
        self._unpersisted: dict[str, Any] | None = None

    def is_tripped(self) -> bool:
        return self.settings.kill_switch or self._unpersisted is not None or self.path.exists()

    def status(self) -> dict | None:
        """`{"reason","ts","source"}` from the file; the environment override reports source "env"."""
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = None
        except (OSError, ValueError):
            data = {"reason": "kill switch file is unreadable", "ts": None, "source": "unknown"}
        if isinstance(data, dict):
            return {k: data.get(k) for k in ("reason", "ts", "source")}
        if data is not None:
            return {"reason": "kill switch file is malformed", "ts": None, "source": "unknown"}
        if self._unpersisted is not None:
            return dict(self._unpersisted)
        if self.settings.kill_switch:
            return {"reason": "KILL_SWITCH is set in the environment", "ts": None, "source": "env"}
        return None

    def trip(self, reason: str, source: str) -> None:
        """Idempotent: the first reason is kept while the switch stays tripped."""
        if self.path.exists() or self._unpersisted is not None:
            log.info("kill switch already tripped; ignoring trip from %s: %s", source, reason)
            return
        record = {"reason": reason, "ts": utcnow().isoformat(), "source": source}
        try:
            _atomic_write_json(self.path, record)
        except OSError:
            log.exception("could not write %s; kill switch held in memory only", self.path)
            self._unpersisted = record
        log.critical("KILL SWITCH TRIPPED by %s: %s", source, reason)
        self._event("critical", "kill_switch_tripped", f"kill switch tripped by {source}: {reason}", record)

    def reset(self, confirm: bool) -> None:
        """CLI only (`python -m bot resume --confirm`)."""
        if confirm is not True:
            raise ValueError("resetting the kill switch needs confirm=True (python -m bot resume --confirm)")
        if self.settings.kill_switch:
            raise ConfigError("KILL_SWITCH is set in the environment; unset it before resuming")
        previous = self.status()
        self.path.unlink(missing_ok=True)
        self._unpersisted = None
        log.warning("kill switch reset (was: %s)", previous)
        self._event("warning", "kill_switch_reset", "kill switch reset from the CLI", {"previous": previous})

    def _event(self, level: str, kind: str, message: str, data: dict[str, Any]) -> None:
        if self._store is None:
            return
        try:
            self._store.log_event(level, kind, message, data=data)
        except Exception:
            log.exception("could not record %s event", kind)


@dataclass
class RiskContext:
    bot_equity: float
    start_of_day_equity: float
    open_exposure_usd: float
    symbol_exposure_usd: float
    orders_today: int  # risk-increasing orders already sent on this New York day
    now: datetime  # timezone-aware


def _block(reason: str) -> RiskVerdict:
    return RiskVerdict(RiskAction.BLOCK, reason)


def _finite_positive(*values: float) -> bool:
    return all(math.isfinite(v) and v > 0 for v in values)


class RiskManager:
    def __init__(self, cfg: RiskConfig, store: Store, kill: KillSwitch) -> None:
        self.cfg = cfg
        self._store = store
        self._kill = kill
        self._errors = self._load_errors()

    @property
    def consecutive_errors(self) -> int:
        return self._errors

    # ------------------------------------------------------------------ sizing

    def size_entry(self, signal: Signal, bot_equity: float) -> float:
        """min(risk budget / stop distance, per-symbol pct cap, USD cap) in units; 0 if invalid."""
        price, stop = signal.price, signal.stop_price
        if signal.kind is not SignalKind.ENTRY or stop is None or not math.isfinite(stop):
            return 0.0
        if not _finite_positive(price, bot_equity) or stop >= price:
            return 0.0
        cfg = self.cfg
        qty = min(
            bot_equity * cfg.risk_per_trade_pct / 100 / (price - stop),
            bot_equity * cfg.max_position_pct / 100 / price,
            cfg.max_position_usd / price,
        )
        return floor_qty(qty)

    # ------------------------------------------------------------------ order check

    def check(self, intent: OrderIntent, ctx: RiskContext) -> RiskVerdict:
        if not intent.purpose.increases_risk:
            guard = self._live_guard_problem()
            if guard is not None:
                return _block(guard)
            return RiskVerdict(RiskAction.ALLOW, f"{intent.purpose.value} reduces risk")
        try:
            return self._check_entry(intent, ctx)
        except Exception as exc:
            log.exception("risk check failed for %s", intent.client_order_id)
            return _block(f"risk check error: {exc}")

    def _check_entry(self, intent: OrderIntent, ctx: RiskContext) -> RiskVerdict:
        cfg = self.cfg
        # 1. kill switch (then the live guard, per the security invariants)
        if self._kill.is_tripped():
            return _block("kill switch is tripped")
        guard = self._live_guard_problem()
        if guard is not None:
            return _block(guard)
        invalid = _invalid_entry(intent, ctx)
        if invalid is not None:
            return _block(invalid)
        # 2. runaway-loop guard
        if ctx.orders_today >= cfg.max_orders_per_day:
            reason = f"{ctx.orders_today} entry orders today reached max_orders_per_day={cfg.max_orders_per_day}"
            self._kill.trip(reason, source="risk")
            return _block(reason)
        # 3. daily loss limit, sticky for the rest of the New York day
        daily = self._daily_loss_problem(ctx)
        if daily is not None:
            return _block(daily)
        # 4. position caps
        qty, capped_by = self._fit_caps(intent, ctx)
        notional = qty * intent.ref_price
        if notional < MIN_ORDER_USD:
            return _block(f"order would be ${notional:.2f} (< ${MIN_ORDER_USD:.0f}) after caps: {capped_by or 'size'}")
        adjusted = qty if qty < intent.qty else None
        note = f"; shrunk to {qty:g} by {capped_by}" if adjusted is not None else ""
        # 5. manual approval
        if notional > cfg.approval_threshold_usd:
            return RiskVerdict(
                RiskAction.NEEDS_APPROVAL,
                f"notional ${notional:,.2f} > approval threshold ${cfg.approval_threshold_usd:,.2f}{note}",
                adjusted,
            )
        # 6.
        return RiskVerdict(RiskAction.ALLOW, f"within limits (${notional:,.2f}){note}", adjusted)

    def _live_guard_problem(self) -> str | None:
        settings = self._kill.settings
        try:
            assert_trading_allowed(settings, settings.live_gate_path)
        except ConfigError as exc:
            return f"live-trading guard: {exc}"
        return None

    def _daily_loss_problem(self, ctx: RiskContext) -> str | None:
        today = ny_trading_day(ctx.now).isoformat()
        if self._store.kv_get(DAILY_LOSS_KEY) == today:
            return "daily loss limit hit earlier today; entries blocked until the next New York day"
        start = ctx.start_of_day_equity
        loss = start - ctx.bot_equity
        limit = start * self.cfg.daily_loss_limit_pct / 100
        if loss < limit:
            return None
        reason = f"daily loss ${loss:,.2f} >= limit ${limit:,.2f} ({self.cfg.daily_loss_limit_pct}% of ${start:,.2f})"
        self._store.kv_set(DAILY_LOSS_KEY, today)
        self._store.log_event("warning", "daily_loss_limit", reason, data={"day": today, "loss": loss})
        log.warning(reason)
        return reason

    def _fit_caps(self, intent: OrderIntent, ctx: RiskContext) -> tuple[float, str]:
        """The largest qty <= intent.qty that fits every cap, and the caps that bound it."""
        cfg = self.cfg
        rooms = {
            "per-symbol pct cap": ctx.bot_equity * cfg.max_position_pct / 100 - ctx.symbol_exposure_usd,
            "per-symbol USD cap": cfg.max_position_usd - ctx.symbol_exposure_usd,
            "total exposure cap": ctx.bot_equity * cfg.max_total_exposure_pct / 100 - ctx.open_exposure_usd,
        }
        binding = [name for name, room in rooms.items() if room < intent.notional]
        if not binding:
            return intent.qty, ""
        room = max(0.0, min(rooms.values()))
        return floor_qty(room / intent.ref_price), ", ".join(binding)

    # ------------------------------------------------------------------ error tracking

    def after_error(self) -> None:
        self._errors += 1
        count = self._errors
        if count >= self.cfg.max_consecutive_errors:
            self._errors = 0  # the trip has fired; a resumed bot starts counting afresh
            self._kill.trip(f"{count} consecutive errors", source="risk")
        self._save_errors()

    def after_success(self) -> None:
        if self._errors:
            self._errors = 0
            self._save_errors()

    def _load_errors(self) -> int:
        try:
            value = self._store.kv_get(ERRORS_KEY)
            return int(float(value)) if value is not None else 0
        except Exception:
            log.exception("could not load the consecutive error count; starting at 0")
            return 0

    def _save_errors(self) -> None:
        try:
            self._store.kv_set(ERRORS_KEY, str(self._errors))
        except Exception:
            log.exception("could not persist the consecutive error count")

    # ------------------------------------------------------------------ drawdown

    def check_drawdown(self, bot_equity: float) -> None:
        """Track peak bot equity in kv and trip the kill switch at max_drawdown_kill_pct.

        After a drawdown trip the peak is re-based to the current equity, so a manual resume
        does not re-trip on the same loss.
        """
        if not math.isfinite(bot_equity):
            raise ValueError(f"bot equity must be finite, got {bot_equity}")
        stored = self._stored_peak()
        peak = bot_equity if stored is None else max(stored, bot_equity)
        if peak != stored:
            self._store.kv_set(PEAK_KEY, repr(peak))
        if bot_equity <= 0:
            self._trip_drawdown(f"bot equity {bot_equity:,.2f} is not positive", bot_equity)
            return
        drawdown_pct = (peak - bot_equity) / peak * 100
        if drawdown_pct >= self.cfg.max_drawdown_kill_pct:
            self._trip_drawdown(
                f"drawdown {drawdown_pct:.2f}% from peak ${peak:,.2f} >= {self.cfg.max_drawdown_kill_pct}%",
                bot_equity,
            )

    def _stored_peak(self) -> float | None:
        value = self._store.kv_get(PEAK_KEY)
        if value is None:
            return None
        try:
            peak = float(value)
        except (TypeError, ValueError):
            log.warning("ignoring unreadable %s=%r", PEAK_KEY, value)
            return None
        return peak if math.isfinite(peak) else None

    def _trip_drawdown(self, reason: str, bot_equity: float) -> None:
        self._kill.trip(reason, source="risk")
        self._store.kv_set(PEAK_KEY, repr(bot_equity))


def _invalid_entry(intent: OrderIntent, ctx: RiskContext) -> str | None:
    if intent.side is not Side.BUY:
        return "long-only: an entry must be a buy"
    if not _finite_positive(intent.qty, intent.ref_price):
        return f"invalid order: qty={intent.qty}, ref_price={intent.ref_price}"
    if not _finite_positive(ctx.bot_equity, ctx.start_of_day_equity):
        return f"invalid equity: bot={ctx.bot_equity}, start_of_day={ctx.start_of_day_equity}"
    exposures = (ctx.open_exposure_usd, ctx.symbol_exposure_usd)
    if not all(math.isfinite(x) and x >= 0 for x in exposures):
        return f"invalid exposure: open={ctx.open_exposure_usd}, symbol={ctx.symbol_exposure_usd}"
    return None
