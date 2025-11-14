from pathlib import Path

from riskbalancer.adapters import AJBellCSVAdapter


FIXTURE = Path(__file__).parent / "fixtures" / "aj_bell_sample.csv"


def test_aj_bell_adapter_parses_sample_rows():
    adapter = AJBellCSVAdapter(default_volatility=0.15)

    investments = adapter.parse_path(FIXTURE)
    assert len(investments) == 3

    amd = next(inv for inv in investments if inv.instrument_id == "AMD")
    assert amd.market_value == 17717.24
    assert amd.category.levels()[0] == "Uncategorized"
    assert amd.volatility == 0.15
