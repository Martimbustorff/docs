import shutil

import pytest

from bot.config import ConfigError, load_strategy, read_config_block, replace_section, write_config_block
from conftest import ROOT


@pytest.fixture
def strategy_file(tmp_path):
    path = tmp_path / "strategy.md"
    shutil.copy(ROOT / "strategy.md", path)
    return path


def test_shipped_strategy_md_is_valid():
    cfg = load_strategy(ROOT / "strategy.md")
    assert cfg.risk.approval_threshold_usd == 1000
    assert cfg.jev.on_error == "block"


def test_write_config_block_keeps_untouched_sections_verbatim(strategy_file):
    before = strategy_file.read_text()
    data = read_config_block(before)
    data["assets"]["SPY"]["enabled"] = not data["assets"]["SPY"]["enabled"]
    write_config_block(data, strategy_file)
    after = strategy_file.read_text()
    assert read_config_block(after) == data
    jev_before = before[before.index("\njev:") : before.index("\nexecution:")]
    assert jev_before in after  # folded strings and flow-style gates survive a write to assets


def test_write_config_block_refuses_invalid_data(strategy_file):
    before = strategy_file.read_text()
    data = read_config_block(before)
    data["risk"]["risk_per_trade_pct"] = 50
    with pytest.raises(Exception):
        write_config_block(data, strategy_file)
    assert strategy_file.read_text() == before


def test_missing_config_block_is_a_config_error(tmp_path):
    path = tmp_path / "strategy.md"
    path.write_text("# no config here\n")
    with pytest.raises(ConfigError):
        load_strategy(path)


def test_replace_section_only_touches_its_markers(strategy_file):
    replace_section("FINAL_CHECK", "New verdict.", strategy_file)
    text = strategy_file.read_text()
    assert "<!-- BEGIN FINAL_CHECK -->\n\nNew verdict.\n\n<!-- END FINAL_CHECK -->" in text
    load_strategy(strategy_file)
