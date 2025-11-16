from riskbalancer import CategoryPath, CategoryTarget, Investment
from riskbalancer.cli import (
    CategoryAllocation,
    InstrumentMapping,
    apply_mappings_to_investments,
    investment_from_dict,
    investment_to_dict,
    load_fx_rates,
    parse_source_spec,
    summarize_portfolio,
)
from riskbalancer.portfolio import PortfolioPlan


def test_parse_source_spec_valid():
    spec = parse_source_spec("adapter=ajbell,statement=s.csv,mappings=m.yaml")
    assert spec.adapter == "ajbell"
    assert str(spec.statement) == "s.csv"
    assert str(spec.mappings) == "m.yaml"


def test_parse_source_spec_missing_field():
    try:
        parse_source_spec("adapter=ajbell,statement=s.csv")
    except ValueError as exc:
        assert "adapter=..., statement=..., mappings=..." in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_investment_serialization_round_trip():
    investment = Investment(
        instrument_id="ETF",
        description="Global ETF",
        market_value=1000.0,
        quantity=10.0,
        category=CategoryPath("Equities", "Developed", "NAM"),
        volatility=0.2,
        source="aj_bell",
    )
    payload = investment_to_dict(investment)
    restored = investment_from_dict(payload)
    assert restored.instrument_id == investment.instrument_id
    assert restored.market_value == investment.market_value
    assert restored.category.levels() == investment.category.levels()


def test_apply_mappings_splits_investment():
    mapping = InstrumentMapping(
        allocations=[
            CategoryAllocation(path=CategoryPath("Equities", "Developed", "NAM"), weight=0.7),
            CategoryAllocation(path=CategoryPath("Equities", "Developed", "Europe"), weight=0.3),
        ],
        volatility=0.25,
    )
    investment = Investment(
        instrument_id="ETF",
        description="World ETF",
        market_value=1000.0,
        quantity=5.0,
        category=CategoryPath("Uncategorized", "Pending Review"),
        volatility=0.15,
    )
    result = apply_mappings_to_investments([investment], {"ETF": mapping})
    assert len(result) == 2
    assert sum(inv.market_value for inv in result) == 1000.0
    assert result[0].category.levels() == ("Equities", "Developed", "NAM")
    assert result[0].volatility == 0.25
    assert sorted(round(inv.market_value, 2) for inv in result) == [300.0, 700.0]


def test_summarize_portfolio_calculates_cash_and_targets():
    plan = PortfolioPlan(
        [
            CategoryTarget(
                path=CategoryPath("Equities", "Developed", "NAM"),
                normalized_risk_weight=0.6,
                volatility=0.2,
                risk_weight=0.3,
            ),
            CategoryTarget(
                path=CategoryPath("Equities", "Developed", "Europe"),
                normalized_risk_weight=0.4,
                volatility=0.25,
                risk_weight=0.2,
            ),
        ]
    )
    investments = [
        Investment(
            instrument_id="ETF",
            description="World ETF",
            market_value=600.0,
            category=CategoryPath("Equities", "Developed", "NAM"),
            volatility=0.2,
        ),
        Investment(
            instrument_id="ETF2",
            description="World ETF 2",
            market_value=400.0,
            category=CategoryPath("Equities", "Developed", "Europe"),
            volatility=0.25,
        ),
    ]

    total_value, summary = summarize_portfolio(plan, investments)
    assert total_value == 1000.0
    by_label = {row["label"]: row for row in summary}
    assert by_label["Equities / Developed / NAM"]["actual_value"] == 600.0
    assert by_label["Equities / Developed / Europe"]["actual_value"] == 400.0
    assert abs(sum(row["cash_weight"] for row in summary) - 1.0) < 1e-9


def test_load_fx_rates(tmp_path):
    fx_file = tmp_path / "fx.yaml"
    fx_file.write_text(
        "base: GBP\nrates:\n  USD: 0.8\n  EUR: 0.9\n",
        encoding="utf-8",
    )
    rates = load_fx_rates(str(fx_file))
    assert rates["USD"] == 0.8
