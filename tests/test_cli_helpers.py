from pathlib import Path

from riskbalancer import CategoryPath
from riskbalancer.cli import (
    CategoryAllocation,
    InstrumentMapping,
    PlanIndex,
    gather_missing_mappings,
    load_mappings,
    save_mappings,
)
from riskbalancer.configuration import load_portfolio_plan_from_yaml


def test_load_and_save_mappings(tmp_path):
    path = tmp_path / "mappings.yaml"
    original = {
        "AMD": InstrumentMapping(
            allocations=[
                CategoryAllocation(path=CategoryPath("Equities", "US"), weight=0.7),
                CategoryAllocation(path=CategoryPath("Equities", "International"), weight=0.3),
            ],
            volatility=0.2,
        ),
        "IEF": InstrumentMapping(
            allocations=[CategoryAllocation(path=CategoryPath("Bonds", "US"), weight=1.0)]
        ),
    }
    save_mappings(path, original)
    loaded = load_mappings(path)
    amd_allocs = loaded["AMD"].allocations
    assert len(amd_allocs) == 2
    assert amd_allocs[0].path.levels()[0] == "Equities"
    assert loaded["AMD"].volatility == 0.2
    assert loaded["IEF"].volatility is None


def test_gather_missing_mappings_validates_inputs(monkeypatch, tmp_path):
    plan = load_portfolio_plan_from_yaml("config/categories.yaml")
    plan_index = PlanIndex.from_plan(plan)
    inputs = iter(
        [
            "invalid",
            "list",
            "Equities / Developed / NAM=100",
            "0.3",
        ]
    )
    result = gather_missing_mappings(["AMD"], plan_index=plan_index, input_func=lambda prompt="": next(inputs))
    mapping = result["AMD"]
    assert len(mapping.allocations) == 1
    assert mapping.allocations[0].path.levels() == ("Equities", "Developed", "NAM")
    assert mapping.volatility == 0.3
