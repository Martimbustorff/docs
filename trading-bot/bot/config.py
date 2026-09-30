"""Configuration: secrets from the environment (.env), rules from strategy.md.

strategy.md is the single source of truth for strategy rules, risk limits and Jev thresholds.
The bot reads the YAML block between `<!-- BEGIN CONFIG -->` and `<!-- END CONFIG -->` and
refuses to start if it is missing or invalid. Human-readable sections live between
`<!-- BEGIN <name> -->` / `<!-- END <name> -->` markers so tools can regenerate them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = ROOT / "strategy.md"
CONFIG_BEGIN = "<!-- BEGIN CONFIG -->"
CONFIG_END = "<!-- END CONFIG -->"
LIVE_ACK_PHRASE = "I accept that live trading can lose real money"


class ConfigError(Exception):
    """strategy.md or the environment is missing something the bot needs."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- strategy.md schema


def check_symbol(symbol: str) -> str:
    """US stock/ETF tickers ("SPY") or crypto pairs against USD ("BTC/USD"). Anything else, such as
    Yahoo's "BTC-USD", is refused: it would be priced and annualised as a stock."""
    if not re.fullmatch(r"[A-Z]{1,5}|[A-Z]{2,6}/USD", symbol):
        raise ValueError(f"unsupported symbol {symbol!r}; use e.g. SPY or BTC/USD")
    return symbol


class AssetRule(_Strict):
    enabled: bool = True
    strategy: str  # registry name: trend | breakout | meanrev | momentum
    params: dict[str, float | int | bool | str] = Field(default_factory=dict)


class RiskConfig(_Strict):
    capital_usd: float = Field(gt=0)  # slice of the account the bot manages
    risk_per_trade_pct: float = Field(gt=0, le=2.0)  # equity risked between entry and stop
    max_position_pct: float = Field(gt=0, le=100)  # per symbol, % of capital
    max_position_usd: float = Field(gt=0)  # hard per-symbol cap
    max_total_exposure_pct: float = Field(gt=0, le=100)  # no leverage
    daily_loss_limit_pct: float = Field(gt=0, le=10)  # blocks new entries for the rest of the day
    max_drawdown_kill_pct: float = Field(gt=0, le=50)  # trips the kill switch
    max_orders_per_day: int = Field(gt=0)  # runaway-loop guard; trips the kill switch
    max_consecutive_errors: int = Field(gt=0)  # trips the kill switch
    approval_threshold_usd: float = Field(ge=0)  # ENTRY orders above this wait for Telegram approval
    approval_timeout_minutes: int = Field(gt=0)  # unanswered approvals are rejected
    approval_max_price_drift_pct: float = Field(gt=0)  # approved but price moved too far -> skip
    kill_switch_flatten: bool = True  # kill switch closes all bot positions


class JevGateRule(_Strict):
    outcome: str  # "yes"/"no" for noul, a label for choice, a level ("0".."n") for score
    min: float | None = Field(default=None, ge=0, le=1)
    max: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _one_bound(self) -> "JevGateRule":
        if (self.min is None) == (self.max is None):
            raise ValueError("gate needs exactly one of min or max")
        return self


class JevQuestion(_Strict):
    type: Literal["noul", "choice", "score"]
    instructions: str
    criteria: dict[str, str] | list[str] | None = None  # noul: {"true","false"}; choice: label->desc; score: list
    uses_headlines: bool = False  # skipped (auto-pass) when there are no headlines
    applies_to: list[str] | None = None  # strategy names this question gates; None = all
    gate: JevGateRule


class JevConfig(_Strict):
    mode: Literal["gate", "shadow", "off"] = "gate"  # gate blocks trades; shadow only logs
    model: str = "jev-latest"
    timeout_s: float = Field(default=3.0, gt=0, le=30)
    on_error: Literal["block"] = "block"  # fail-closed is the only option
    price_per_million_input_tokens: float = Field(ge=0)
    max_headlines: int = Field(default=10, ge=0, le=50)
    headline_lookback_hours: int = Field(default=24, gt=0)
    questions: dict[str, JevQuestion]


class ExecutionConfig(_Strict):
    poll_seconds: int = Field(default=60, ge=5)
    stock_entry_delay_minutes: int = Field(default=1, ge=0)  # minutes after the open to send stock orders
    crypto_bar_close_utc: str = "00:00"
    slippage_bps: dict[Literal["stock", "crypto"], float]
    fee_bps: dict[Literal["stock", "crypto"], float]
    daily_report_time: str = "17:15"  # America/New_York
    timezone: str = "America/New_York"


class TournamentFilters(_Strict):
    max_drawdown_pct: float
    min_win_rate: float = Field(ge=0, le=1)
    min_profit_factor: float
    min_trades: int
    require_positive_in_sample: bool = True
    require_positive_out_of_sample: bool = True


class TournamentConfig(_Strict):
    in_sample: tuple[str, str]
    out_of_sample: tuple[str, str]
    filters: TournamentFilters


class LiveGateConfig(_Strict):
    min_paper_days: int = 30
    min_paper_trades: int = 8
    min_signal_match_rate: float = Field(default=0.9, ge=0, le=1)
    max_return_gap_pct: float = 3.0
    kill_switch_drill_max_age_days: int = 30


class StrategyConfig(_Strict):
    version: int = 1
    timeframe: Literal["1d"] = "1d"
    assets: dict[str, AssetRule]
    risk: RiskConfig
    jev: JevConfig
    execution: ExecutionConfig
    tournament: TournamentConfig
    live_gate: LiveGateConfig

    @field_validator("assets")
    @classmethod
    def _known_symbols(cls, value: dict[str, AssetRule]) -> dict[str, AssetRule]:
        for symbol in value:
            check_symbol(symbol)
        return value

    @property
    def enabled_assets(self) -> dict[str, AssetRule]:
        return {s: r for s, r in self.assets.items() if r.enabled}


def read_config_block(text: str) -> dict[str, Any]:
    start, end = text.find(CONFIG_BEGIN), text.find(CONFIG_END)
    if start < 0 or end < 0 or end < start:
        raise ConfigError(f"strategy.md has no {CONFIG_BEGIN} ... {CONFIG_END} block")
    block = text[start + len(CONFIG_BEGIN) : end]
    match = re.search(r"```ya?ml\n(.*?)```", block, re.S)
    if not match:
        raise ConfigError("the CONFIG block in strategy.md must contain a ```yaml fenced block")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ConfigError("the CONFIG block in strategy.md is empty")
    return data


def load_strategy(path: Path | str = STRATEGY_PATH) -> StrategyConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found; risk rules must exist before anything runs")
    try:
        return StrategyConfig.model_validate(read_config_block(path.read_text()))
    except ConfigError:
        raise
    except Exception as exc:  # pydantic.ValidationError, yaml.YAMLError
        raise ConfigError(f"invalid CONFIG block in {path}: {exc}") from exc


def write_config_block(data: dict[str, Any], path: Path | str = STRATEGY_PATH) -> None:
    """Replace the YAML inside the CONFIG block, validating first so a bad write never lands.

    Only the top-level sections whose values changed are re-serialised; the others keep their
    hand-written text (comments, folded strings, flow mappings).
    """
    StrategyConfig.model_validate(data)
    path = Path(path)
    text = path.read_text()
    start, end = text.find(CONFIG_BEGIN), text.find(CONFIG_END)
    if start < 0 or end < 0:
        raise ConfigError("strategy.md has no CONFIG block to replace")
    yaml_text = _merge_yaml_sections(text[start + len(CONFIG_BEGIN) : end], data)
    if yaml.safe_load(yaml_text) != data:  # the splice must reproduce `data` exactly
        yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    new_block = f"{CONFIG_BEGIN}\n\n```yaml\n{yaml_text}```\n\n"
    _atomic_write(path, text[:start] + new_block + text[end:])


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file and os.replace, keeping the file's permissions, so a crash or a
    full disk can never leave the bot's rulebook half-written."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    os.replace(tmp, path)


def _merge_yaml_sections(block: str, data: dict[str, Any]) -> str:
    """The existing YAML with each changed top-level key re-dumped in place; new keys appended."""
    match = re.search(r"```ya?ml\n(.*?)```", block, re.S)
    if not match:
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    old_text = match.group(1)
    old = yaml.safe_load(old_text) or {}
    sections: dict[str, str] = {}
    order: list[str] = []
    current: str | None = None
    for line in old_text.splitlines(keepends=True):
        key = re.match(r"([A-Za-z_][\w/]*):", line)
        if key:
            current = key.group(1)
            order.append(current)
            sections[current] = ""
        if current is not None:
            sections[current] += line
    out = []
    for key in order:
        if key not in data:
            continue
        if old.get(key) == data[key]:
            out.append(sections[key])
        else:
            out.append(yaml.safe_dump({key: data[key]}, sort_keys=False, allow_unicode=True, width=100))
    for key in data:
        if key not in sections:
            out.append(yaml.safe_dump({key: data[key]}, sort_keys=False, allow_unicode=True, width=100))
    return "".join(out)


def replace_section(name: str, markdown: str, path: Path | str = STRATEGY_PATH) -> None:
    """Replace the text between `<!-- BEGIN name -->` and `<!-- END name -->`.

    A body containing section markers is refused: it could forge or break the CONFIG block."""
    if re.search(r"<!--\s*(BEGIN|END)\b", markdown):
        raise ConfigError(f"section {name!r} body must not contain BEGIN/END markers")
    path = Path(path)
    text = path.read_text()
    begin, end = f"<!-- BEGIN {name} -->", f"<!-- END {name} -->"
    i, j = text.find(begin), text.find(end)
    if i < 0 or j < 0 or j < i:
        raise ConfigError(f"strategy.md has no {begin} ... {end} section")
    _atomic_write(path, text[: i + len(begin)] + "\n\n" + markdown.strip() + "\n\n" + text[j:])


# --------------------------------------------------------------------------- environment


class Settings(BaseModel):
    """Secrets and deployment settings. Read from the environment; `.env` is loaded if present.

    Secrets are SecretStr so they never appear in logs, reprs or the dashboard.
    """

    model_config = ConfigDict(extra="ignore")

    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alpaca_paper: bool = True
    typesafe_api_key: SecretStr | None = None  # Jev
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    dashboard_user: str = "admin"
    dashboard_password: SecretStr | None = None
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    trading_mode: Literal["paper", "live"] = "paper"
    live_trading_ack: str | None = None
    kill_switch: bool = False  # KILL_SWITCH=1 forces the kill switch on
    data_dir: Path = ROOT / "var"

    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def has_jev(self) -> bool:
        return self.typesafe_api_key is not None

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.sqlite3"

    @property
    def kill_switch_path(self) -> Path:
        return self.data_dir / "KILL_SWITCH"

    @property
    def live_gate_path(self) -> Path:
        return self.data_dir / "live_gate.json"

    @property
    def drill_path(self) -> Path:
        return self.data_dir / "kill_switch_drill.json"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


_ENV_KEYS = {
    "ALPACA_API_KEY": "alpaca_api_key",
    "ALPACA_SECRET_KEY": "alpaca_secret_key",
    "ALPACA_PAPER": "alpaca_paper",
    "TYPESAFE_API_KEY": "typesafe_api_key",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    "DASHBOARD_USER": "dashboard_user",
    "DASHBOARD_PASSWORD": "dashboard_password",
    "DASHBOARD_HOST": "dashboard_host",
    "DASHBOARD_PORT": "dashboard_port",
    "TRADING_MODE": "trading_mode",
    "LIVE_TRADING_ACK": "live_trading_ack",
    "KILL_SWITCH": "kill_switch",
    "BOT_DATA_DIR": "data_dir",
}


def load_settings(env_file: Path | str | None = ROOT / ".env", environ: dict[str, str] | None = None) -> Settings:
    """Build Settings from `environ` (default: os.environ after loading `.env` without overriding)."""
    if environ is None:
        if env_file is not None and Path(env_file).exists():
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        environ = dict(os.environ)
    values = {field: environ[key] for key, field in _ENV_KEYS.items() if environ.get(key, "").strip()}
    settings = Settings.model_validate(values)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
