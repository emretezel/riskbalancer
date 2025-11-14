from pathlib import Path

from riskbalancer import CategoryPath
from riskbalancer.cli import (
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
        "AMD": InstrumentMapping(category=CategoryPath("Equities", "US"), volatility=0.2),
        "IEF": InstrumentMapping(category=CategoryPath("Bonds", "US")),
    }
    save_mappings(path, original)
    loaded = load_mappings(path)
    assert loaded["AMD"].category.levels() == ("Equities", "US")
    assert loaded["AMD"].volatility == 0.2
    assert loaded["IEF"].volatility is None


def test_gather_missing_mappings_validates_inputs(monkeypatch, tmp_path):
    plan = load_portfolio_plan_from_yaml("config/categories.yaml")
    plan_index = PlanIndex.from_plan(plan)
    inputs = iter(
        [
            "invalid",
            "list",
            "Equities / Developed / NAM",
            "0.3",
        ]
    )
    result = gather_missing_mappings(["AMD"], plan_index=plan_index, input_func=lambda prompt="": next(inputs))
    mapping = result["AMD"]
    assert mapping.category.levels() == ("Equities", "Developed", "NAM")
    assert mapping.volatility == 0.3
