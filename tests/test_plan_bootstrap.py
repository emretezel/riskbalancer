"""
Tests for the plan-bootstrap module.

Author: Emre Tezel
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from riskbalancer.configuration import (
    collect_category_weight_validation_failures,
    load_category_nodes_from_yaml,
)
from riskbalancer.paths import UserPaths
from riskbalancer.plan_bootstrap import (
    ScriptedIO,
    build_catalog,
    clone_plan,
    walk_catalog_interactive,
    write_plan_yaml,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SEED_YAML = """\
assets:
  - name: Equities
    weight: 0.6
    children:
      - name: Developed
        weight: 0.7
        children:
          - name: NAM
            weight: 0.5
            volatility: 0.18
            adjustment: 1.0
          - name: EMEA
            weight: 0.5
            volatility: 0.18
            adjustment: 0.9
      - name: EM
        weight: 0.3
        volatility: 0.25
  - name: Bonds
    weight: 0.4
    volatility: 0.07
"""


def _build_paths(tmp_path: Path, *, user: str = "wife") -> UserPaths:
    """Construct a UserPaths rooted at tmp_path with a seed plan in place."""
    paths = UserPaths.for_user(user, root=tmp_path)
    paths.seed_plan.parent.mkdir(parents=True, exist_ok=True)
    paths.seed_plan.write_text(SEED_YAML, encoding="utf-8")
    return paths


# ---------------------------------------------------------------------------
# Catalog construction
# ---------------------------------------------------------------------------


def test_build_catalog_from_seed_only(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)
    names = [node.name for node in catalog]
    assert names == ["Equities", "Bonds"]
    equities = catalog[0]
    assert equities.suggested_weight == pytest.approx(0.6)
    children = [child.name for child in equities.children]
    assert children == ["Developed", "EM"]


def test_build_catalog_unions_peer_plan_with_seed(tmp_path):
    paths = _build_paths(tmp_path, user="kid")
    # A peer (emre) plan introduces a category not in the seed.
    peer = UserPaths.for_user("emre", root=tmp_path)
    peer.user_dir.mkdir(parents=True, exist_ok=True)
    peer.plan.write_text(
        """\
assets:
  - name: Equities
    weight: 0.5
    children:
      - name: Developed
        weight: 1.0
        children:
          - name: APAC
            weight: 1.0
            volatility: 0.20
            adjustment: 1.1
  - name: Cash
    weight: 0.5
    volatility: 0.01
""",
        encoding="utf-8",
    )

    catalog = build_catalog(paths)
    names = [node.name for node in catalog]
    # Equities and Bonds from seed, plus Cash from peer; peers are merged
    # first so Cash appears before the seed's entries did.
    assert "Cash" in names
    assert "Equities" in names
    assert "Bonds" in names

    equities = next(node for node in catalog if node.name == "Equities")
    developed = next(child for child in equities.children if child.name == "Developed")
    developed_children = {child.name for child in developed.children}
    # Union of seed (NAM, EMEA) and peer (APAC).
    assert developed_children == {"NAM", "EMEA", "APAC"}


def test_build_catalog_marks_mapping_only_leaves(tmp_path):
    paths = _build_paths(tmp_path)
    paths.shared_mappings_dir.mkdir(parents=True, exist_ok=True)
    (paths.shared_mappings_dir / "ajbell.yaml").write_text(
        """\
TICKER:
  allocations:
    - category: Alternative / Crypto
      weight: 1.0
""",
        encoding="utf-8",
    )

    catalog = build_catalog(paths)
    alternative = next((n for n in catalog if n.name == "Alternative"), None)
    assert alternative is not None, "Alternative should be added from the mapping file"
    assert alternative.from_mappings is True
    crypto = alternative.children[0]
    assert crypto.name == "Crypto"
    assert crypto.from_mappings is True


# ---------------------------------------------------------------------------
# Interactive walk
# ---------------------------------------------------------------------------


def test_walk_catalog_picks_subset_with_explicit_weights(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    # Wife wants Equities only at top level, Developed only within Equities,
    # NAM only within Developed (single leaf with vol/adj from catalog).
    answers = [
        # Top level: Equities=100
        "Equities",
        "100",
        "n",
        # Equities children: Developed=100 (skip EM)
        "Developed",
        "100",
        "n",
        # Developed children: NAM=100 (skip EMEA)
        "NAM",
        "100",
        "n",
        # NAM leaf: accept catalog vol and adj
        "",
        "",
    ]
    io = ScriptedIO(answers)
    plan = walk_catalog_interactive(catalog, io)

    assert [node.name for node in plan] == ["Equities"]
    assert [child.name for child in plan[0].children] == ["Developed"]
    leaf = plan[0].children[0].children[0]
    assert leaf.name == "NAM"
    assert leaf.weight == pytest.approx(1.0)
    assert leaf.volatility == pytest.approx(0.18)
    assert leaf.adjustment == pytest.approx(1.0)
    failures = collect_category_weight_validation_failures(plan)
    assert failures == []


def test_walk_catalog_reprompts_when_level_weights_do_not_sum_to_100(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    # First attempt: Equities=60, Bonds=20 → 80%, will fail; then re-prompt
    # asks for two new weights for the same picks, this time 60 and 40.
    answers = [
        "Equities",
        "60",
        "y",
        "Bonds",
        "20",
        "n",
        # Re-prompt for Equities then Bonds — weights now sum to 100.
        "60",
        "40",
        # Equities children: take Developed=60 + EM=40
        "Developed",
        "60",
        "y",
        "EM",
        "40",
        "n",
        # Developed children: NAM=50 + EMEA=50
        "NAM",
        "50",
        "y",
        "EMEA",
        "50",
        "n",
        # NAM leaf, EMEA leaf, EM leaf, Bonds leaf — accept catalog defaults.
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    io = ScriptedIO(answers)
    plan = walk_catalog_interactive(catalog, io)

    assert {node.name for node in plan} == {"Equities", "Bonds"}
    failures = collect_category_weight_validation_failures(plan)
    assert failures == []
    # The validator must have surfaced the failure once before succeeding.
    assert any("totals" in msg for msg in io.warn_log)


def test_walk_catalog_treats_added_node_with_no_children_as_leaf(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    # Pick Bonds only (a leaf in the seed catalog) and accept catalog vol/adj.
    answers = [
        "Bonds",
        "100",
        "n",
        "",  # volatility — accept suggestion 0.07
        "",  # adjustment — accept default 1.0
    ]
    io = ScriptedIO(answers)
    plan = walk_catalog_interactive(catalog, io)
    assert len(plan) == 1
    assert plan[0].name == "Bonds"
    assert plan[0].volatility == pytest.approx(0.07)
    assert plan[0].adjustment == pytest.approx(1.0)
    assert plan[0].children == []


# ---------------------------------------------------------------------------
# Persistence + clone
# ---------------------------------------------------------------------------


def test_write_plan_yaml_round_trips(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)
    answers = ["Bonds", "100", "n", "", ""]
    plan = walk_catalog_interactive(catalog, ScriptedIO(answers))
    out = tmp_path / "out.yaml"
    write_plan_yaml(out, plan)
    parsed = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert parsed["assets"][0]["name"] == "Bonds"
    # Round-trip via load_category_nodes_from_yaml.
    nodes = load_category_nodes_from_yaml(out)
    assert nodes[0].name == "Bonds"


def test_clone_plan_copies_and_validates(tmp_path):
    src_paths = _build_paths(tmp_path, user="emre")
    src_paths.user_dir.mkdir(parents=True, exist_ok=True)
    src_paths.plan.write_text(SEED_YAML, encoding="utf-8")

    target_paths = UserPaths.for_user("kid", root=tmp_path)
    clone_plan(src_paths, target_paths)

    assert target_paths.plan.exists()
    assert target_paths.plan.read_text(encoding="utf-8") == SEED_YAML


def test_clone_plan_rejects_invalid_source(tmp_path):
    src_paths = _build_paths(tmp_path, user="emre")
    src_paths.user_dir.mkdir(parents=True, exist_ok=True)
    src_paths.plan.write_text(
        "assets:\n  - name: Equities\n    weight: 0.5\n    volatility: 0.2\n",
        encoding="utf-8",
    )

    target_paths = UserPaths.for_user("kid", root=tmp_path)
    with pytest.raises(ValueError, match="Category weight validation failed"):
        clone_plan(src_paths, target_paths)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_build_catalog_is_deterministic_across_runs(tmp_path):
    paths = _build_paths(tmp_path)
    paths.shared_mappings_dir.mkdir(parents=True, exist_ok=True)
    (paths.shared_mappings_dir / "ajbell.yaml").write_text(
        "FOO:\n  allocations:\n    - {category: 'Equities / Developed / NAM', weight: 1.0}\n",
        encoding="utf-8",
    )
    first = build_catalog(paths)
    second = build_catalog(paths)
    assert _shape(first) == _shape(second)


def _shape(catalog) -> list:
    """Return a (name, [children]) recursive structure for comparison."""
    return [(node.name, _shape(node.children)) for node in catalog]


# ---------------------------------------------------------------------------
# replace() interaction sanity check
# ---------------------------------------------------------------------------


def test_user_paths_replace_supports_field_overrides(tmp_path):
    base = UserPaths.for_user("test", root=tmp_path)
    override = replace(base, plan=tmp_path / "alt.yaml")
    assert override.plan == tmp_path / "alt.yaml"
    assert override.user == "test"
