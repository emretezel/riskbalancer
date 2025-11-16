from pathlib import Path

from riskbalancer import CategoryPath
from riskbalancer.adapters import AJBellCSVAdapter, IBKRCSVAdapter


FIXTURE = Path(__file__).parent / "fixtures" / "aj_bell_sample.csv"
IBKR_FIXTURE = Path(__file__).parent / "fixtures" / "ibkr_sample.csv"


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
