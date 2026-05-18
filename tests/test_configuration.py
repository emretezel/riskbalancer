import math

import pytest

from riskbalancer.configuration import (
    collect_category_weight_validation_failures,
    format_category_weight_validation_failures,
    load_category_nodes_from_yaml,
    load_portfolio_plan_from_yaml,
)


def test_load_portfolio_plan_from_yaml_reports_root_weight_failure(tmp_path):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text(
        """
assets:
  - name: Equities
    weight: 0.6
  - name: Bonds
    weight: 0.6
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="root assets totals 120.00%"):
        load_portfolio_plan_from_yaml(bad_config)


def test_load_portfolio_plan_from_yaml_reports_nested_weight_failure(tmp_path):
    bad_config = tmp_path / "bad-nested.yaml"
    bad_config.write_text(
        """
assets:
  - name: Equities
    weight: 1.0
    children:
      - name: Developed
        weight: 0.7
      - name: EM
        weight: 0.2
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Equities totals 90.00%"):
        load_portfolio_plan_from_yaml(bad_config)


def test_collect_category_weight_validation_failures_returns_all_failures(tmp_path):
    bad_config = tmp_path / "bad-multiple.yaml"
    bad_config.write_text(
        """
assets:
  - name: Equities
    weight: 0.7
    children:
      - name: Developed
        weight: 0.6
      - name: EM
        weight: 0.2
  - name: Bonds
    weight: 0.4
""",
        encoding="utf-8",
    )

    nodes = load_category_nodes_from_yaml(bad_config)
    failures = collect_category_weight_validation_failures(nodes)

    assert [failure.message() for failure in failures] == [
        "root assets totals 110.00% (expected 100.00%)",
        "Equities totals 80.00% (expected 100.00%)",
    ]
    assert (
        format_category_weight_validation_failures(failures)
        == "Category weight validation failed:\n"
        "- root assets totals 110.00% (expected 100.00%)\n"
        "- Equities totals 80.00% (expected 100.00%)"
    )


def test_adjustments_change_normalized_weights(tmp_path):
    config = tmp_path / "adjusted.yaml"
    config.write_text(
        """
assets:
  - name: AssetA
    weight: 0.5
    children:
      - name: Leaf1
        weight: 1.0
        adjustment: 2.0
  - name: AssetB
    weight: 0.5
    children:
      - name: Leaf2
        weight: 1.0
""",
        encoding="utf-8",
    )
    plan = load_portfolio_plan_from_yaml(config)
    weights = {target.path.label(): target for target in plan.targets}
    assert weights["AssetA / Leaf1"].risk_weight == pytest.approx(1.0, rel=1e-6)
    assert weights["AssetB / Leaf2"].risk_weight == pytest.approx(0.5, rel=1e-6)
    assert math.isclose(weights["AssetA / Leaf1"].target_weight, 2 / 3, rel_tol=1e-6)
    assert math.isclose(weights["AssetB / Leaf2"].target_weight, 1 / 3, rel_tol=1e-6)
