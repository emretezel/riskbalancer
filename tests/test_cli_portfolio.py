from riskbalancer import CategoryPath, Investment
from riskbalancer.cli import (
    CategoryAllocation,
    InstrumentMapping,
    apply_mappings_to_investments,
    investment_from_dict,
    investment_to_dict,
    parse_source_spec,
)


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
            CategoryAllocation(path=CategoryPath("Equities", "Developed", "NAM")),
            CategoryAllocation(path=CategoryPath("Equities", "Developed", "Europe")),
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
    assert sorted(inv.market_value for inv in result) == [500.0, 500.0]
