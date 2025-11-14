from pathlib import Path

from riskbalancer import CategoryPath
from riskbalancer.adapters import AJBellCSVAdapter


FIXTURE = Path(__file__).parent / "fixtures" / "aj_bell_sample.csv"


def test_aj_bell_adapter_parses_sample_rows():
    adapter = AJBellCSVAdapter(
        category_map={
            "AMD": CategoryPath("Equities", "US", "Tech"),
            "MTIX": CategoryPath("Fixed Income", "International", "Inflation"),
        },
        default_category=CategoryPath("Other", "Other", "Other"),
        volatility_map={"AMD": 0.35},
        default_volatility=0.15,
    )

    investments = adapter.parse_path(FIXTURE)
    assert len(investments) == 3

    amd = next(inv for inv in investments if inv.instrument_id == "AMD")
    assert amd.market_value == 17717.24
    assert amd.category.level2 == "US"
    assert amd.volatility == 0.35

    net = next(inv for inv in investments if inv.instrument_id == "NET")
    assert net.category.level1 == "Other"
    assert net.volatility == 0.15
