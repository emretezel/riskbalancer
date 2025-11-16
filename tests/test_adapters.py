from pathlib import Path

from riskbalancer import CategoryPath
from riskbalancer.adapters import AJBellCSVAdapter, IBKRCSVAdapter, MS401KCSVAdapter


FIXTURE = Path(__file__).parent / "fixtures" / "aj_bell_sample.csv"
IBKR_FIXTURE = Path(__file__).parent / "fixtures" / "ibkr_sample.csv"
MS401K_FIXTURE = Path(__file__).parent / "fixtures" / "ms401k_sample.csv"


def test_aj_bell_adapter_parses_sample_rows():
    adapter = AJBellCSVAdapter(default_volatility=0.15)

    investments = adapter.parse_path(FIXTURE)
    assert len(investments) == 3

    amd = next(inv for inv in investments if inv.instrument_id == "AMD")
    assert amd.market_value == 17717.24
    assert amd.category.levels()[0] == "Uncategorized"
    assert amd.volatility == 0.15


def test_ibkr_adapter_converts_using_fx(tmp_path):
    adapter = IBKRCSVAdapter(
        default_category=CategoryPath("Other", "Other"),
        fx_rates={"USD": 0.8},
    )
    investments = adapter.parse_path(IBKR_FIXTURE)
    assert len(investments) == 2
    values = {inv.instrument_id: inv.market_value for inv in investments}
    assert values["EMIM"] == 3500.0  # GBP row unchanged
    assert values["PLTR"] == 10500 * 0.8  # USD converted via FX


def test_ms401k_adapter_requires_fx_and_converts():
    adapter = MS401KCSVAdapter(
        default_category=CategoryPath("Other", "Other"),
        fx_rates={"USD": 0.75},
    )
    investments = adapter.parse_path(MS401K_FIXTURE)
    assert len(investments) == 1
    investment = investments[0]
    assert investment.instrument_id == "Bond_Fund"
    assert investment.market_value == 1100.0 * 0.75
