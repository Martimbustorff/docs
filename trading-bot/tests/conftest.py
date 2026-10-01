import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_settings(tmp_path):
    from bot.config import load_settings

    return load_settings(env_file=None, environ={"BOT_DATA_DIR": str(tmp_path / "var")})


@pytest.fixture
def strategy_cfg():
    from bot.config import load_strategy

    return load_strategy(ROOT / "strategy.md")


def make_bars(closes, start="2020-01-01", spread=0.01, volume=1_000_000.0):
    """Synthetic daily bars: open = previous close, high/low = +/- spread around max/min."""
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    idx = pd.date_range(start, periods=len(closes), freq="D", name="date")
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) * (1 + spread) for o, c in zip(opens, closes)],
            "low": [min(o, c) * (1 - spread) for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [volume] * len(closes),
        },
        index=idx,
    )


@pytest.fixture
def bars_factory():
    return make_bars
