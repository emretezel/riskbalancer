"""
Tests for the plan-adjust module.

Author: Emre Tezel
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riskbalancer.configuration import CategoryNode, load_category_nodes_from_yaml
from riskbalancer.plan_adjust import (
    LeafChange,
    PlanCreationAborted,
    apply_targeted,
    confirm_changes,
    filter_under,
    iter_leaf_nodes,
    normalize_under,
    render_diff,
    render_list,
    walk_adjustments,
)
from riskbalancer.plan_bootstrap import ScriptedIO, write_plan_yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# A four-leaf tree with one zero-weight leaf (Cash) so we can assert the
# walker's "skip weight == 0" rule. Two leaves share a sub-branch
# (Bonds / Developed / UK / Govt and Bonds / Developed / UK / Corp) so
# the `--under` prefix matching has something non-trivial to anchor on.
SAMPLE_PLAN_YAML = """\
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
        adjustment: 0.85
  - name: Bonds
    weight: 0.4
    children:
      - name: Developed
        weight: 1.0
        children:
          - name: UK
            weight: 1.0
            children:
              - name: Govt
                weight: 0.5
                volatility: 0.055
                adjustment: 0.9
              - name: Corp
                weight: 0.5
                volatility: 0.06
                adjustment: 1.0
"""


# A second fixture that also includes a zero-weight Cash leaf alongside
# non-zero leaves, so the walker tests can prove Cash is silently skipped.
PLAN_WITH_CASH_YAML = """\
assets:
  - name: Equities
    weight: 0.6
    children:
      - name: NAM
        weight: 0.5
        volatility: 0.18
        adjustment: 1.0
      - name: EMEA
        weight: 0.5
        volatility: 0.18
        adjustment: 0.9
  - name: Bonds
    weight: 0.4
    volatility: 0.07
    adjustment: 1.0
  - name: Cash
    weight: 0.0
    volatility: 0.01
    adjustment: 1.0
"""


def _write_plan(tmp_path: Path, yaml_text: str) -> Path:
    """Drop a plan.yaml fixture into a temp dir and return the path."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml_text, encoding="utf-8")
    return plan_path


def _load(tmp_path: Path, yaml_text: str = SAMPLE_PLAN_YAML) -> list[CategoryNode]:
    return load_category_nodes_from_yaml(_write_plan(tmp_path, yaml_text))


# ---------------------------------------------------------------------------
# iter_leaf_nodes
# ---------------------------------------------------------------------------


def test_iter_leaf_nodes_yields_paths_in_plan_order(tmp_path):
    nodes = _load(tmp_path)
    yielded = [(path, node.name) for path, node in iter_leaf_nodes(nodes)]
    assert yielded == [
        (("Equities", "Developed", "NAM"), "NAM"),
        (("Equities", "Developed", "EMEA"), "EMEA"),
        (("Equities", "EM"), "EM"),
        (("Bonds", "Developed", "UK", "Govt"), "Govt"),
        (("Bonds", "Developed", "UK", "Corp"), "Corp"),
    ]


def test_iter_leaf_nodes_returns_actual_nodes_for_mutation(tmp_path):
    """Mutating a yielded node must mutate the underlying tree."""
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    first_path, first_node = leaves[0]
    first_node.adjustment = 0.42
    # Re-iterate and confirm the change persisted in the original tree.
    re_yielded = dict(iter_leaf_nodes(nodes))
    assert re_yielded[first_path].adjustment == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# filter_under
# ---------------------------------------------------------------------------


def test_filter_under_returns_all_when_under_is_none(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    assert filter_under(leaves, None) == leaves
    assert filter_under(leaves, "") == leaves


def test_filter_under_matches_slash_separator(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    matched = filter_under(leaves, "Bonds / Developed")
    names = [path[-1] for path, _ in matched]
    assert names == ["Govt", "Corp"]


def test_filter_under_accepts_arrow_separator(tmp_path):
    """`--under "Bonds > Developed"` is a synonym for `Bonds / Developed`."""
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    arrow = filter_under(leaves, "Bonds > Developed")
    slash = filter_under(leaves, "Bonds / Developed")
    assert arrow == slash


def test_filter_under_top_level_leaf_does_not_match_itself(tmp_path):
    """A path like `Bonds` shouldn't match the top-level leaf `Bonds` itself."""
    nodes = [
        CategoryNode(name="Bonds", weight=1.0, volatility=0.05),
    ]
    leaves = list(iter_leaf_nodes(nodes))
    with pytest.raises(ValueError, match="did not match any leaf"):
        filter_under(leaves, "Bonds")


def test_filter_under_raises_with_candidates_on_no_match(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    with pytest.raises(ValueError, match="did not match any leaf"):
        filter_under(leaves, "Nope / Doesnt / Exist")


def test_normalize_under_is_case_insensitive():
    assert normalize_under("Bonds > Developed") == "bonds / developed"
    assert normalize_under("bonds/DEVELOPED") == "bonds / developed"
    assert normalize_under(" Bonds /  Developed ") == "bonds / developed"


# ---------------------------------------------------------------------------
# apply_targeted
# ---------------------------------------------------------------------------


def test_apply_targeted_updates_leaf_and_returns_change(tmp_path):
    nodes = _load(tmp_path)
    change = apply_targeted(nodes, ["Bonds", "Developed", "UK", "Govt"], 0.95)
    assert change == LeafChange(path=("Bonds", "Developed", "UK", "Govt"), old=0.9, new=0.95)
    # In-place mutation: walking the tree again must see the new value.
    leaves = {path: node for path, node in iter_leaf_nodes(nodes)}
    assert leaves[("Bonds", "Developed", "UK", "Govt")].adjustment == pytest.approx(0.95)


def test_apply_targeted_rejects_branch_path(tmp_path):
    nodes = _load(tmp_path)
    with pytest.raises(ValueError, match="branch, not a leaf"):
        apply_targeted(nodes, ["Bonds", "Developed", "UK"], 0.95)


def test_apply_targeted_rejects_missing_path(tmp_path):
    nodes = _load(tmp_path)
    with pytest.raises(ValueError, match="Unknown category path"):
        apply_targeted(nodes, ["Bonds", "Magic", "Pony"], 0.95)


def test_apply_targeted_rejects_negative_value(tmp_path):
    nodes = _load(tmp_path)
    with pytest.raises(ValueError, match="non-negative"):
        apply_targeted(nodes, ["Equities", "EM"], -0.1)


def test_apply_targeted_rejects_empty_path(tmp_path):
    nodes = _load(tmp_path)
    with pytest.raises(ValueError, match="empty category path"):
        apply_targeted(nodes, [], 0.95)


def test_apply_targeted_case_insensitive(tmp_path):
    """Path lookup should not be sensitive to user casing."""
    nodes = _load(tmp_path)
    change = apply_targeted(nodes, ["bonds", "developed", "uk", "govt"], 0.95)
    assert change.new == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# walk_adjustments
# ---------------------------------------------------------------------------


def test_walker_skips_zero_weight_leaves(tmp_path):
    """Cash (weight=0) must never appear in the per-leaf prompts."""
    nodes = _load(tmp_path, PLAN_WITH_CASH_YAML)
    leaves = list(iter_leaf_nodes(nodes))
    # Three non-zero leaves: NAM, EMEA, Bonds → three blank answers.
    io = ScriptedIO(["", "", ""])
    changes = walk_adjustments(leaves, io)
    assert changes == []
    # Each prompt's info banner names the leaf it asked about. Cash must
    # never appear in the info log.
    joined_info = "\n".join(io.info_log)
    assert "Cash" not in joined_info
    # The walker prompted exactly three times (one per non-zero leaf).
    assert io._index == 3


def test_walker_blank_keeps_existing_value(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    # Five eligible leaves; press Enter on each.
    io = ScriptedIO([""] * 5)
    changes = walk_adjustments(leaves, io)
    assert changes == []
    # Tree adjustments unchanged.
    by_path = {path: node for path, node in iter_leaf_nodes(nodes)}
    assert by_path[("Equities", "Developed", "NAM")].adjustment == pytest.approx(1.0)
    assert by_path[("Equities", "Developed", "EMEA")].adjustment == pytest.approx(0.9)


def test_walker_numeric_replaces_value(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    # NAM 1.0 → 0.95, skip the rest.
    io = ScriptedIO(["0.95", "", "", "", ""])
    changes = walk_adjustments(leaves, io)
    assert len(changes) == 1
    assert changes[0] == LeafChange(path=("Equities", "Developed", "NAM"), old=1.0, new=0.95)
    nam = next(n for p, n in iter_leaf_nodes(nodes) if p[-1] == "NAM")
    assert nam.adjustment == pytest.approx(0.95)


def test_walker_no_change_when_value_equals_current(tmp_path):
    """Typing the existing value should not generate a LeafChange."""
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    # NAM is already 1.0 — typing "1.0" must not record a change.
    io = ScriptedIO(["1.0", "", "", "", ""])
    changes = walk_adjustments(leaves, io)
    assert changes == []


def test_walker_q_stops_iteration_and_returns_changes_so_far(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    # First leaf: set to 0.95; second leaf prompt: type "q".
    io = ScriptedIO(["0.95", "q"])
    changes = walk_adjustments(leaves, io)
    assert len(changes) == 1
    assert changes[0].new == pytest.approx(0.95)


def test_walker_aborts_on_quit(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    io = ScriptedIO(["quit"])
    with pytest.raises(PlanCreationAborted):
        walk_adjustments(leaves, io)


def test_walker_rejects_negative_input_and_reprompts(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    # Negative is rejected with a warning, then valid input is accepted.
    io = ScriptedIO(["-1", "0.85", "", "", "", ""])
    changes = walk_adjustments(leaves, io)
    assert len(changes) == 1
    assert changes[0].new == pytest.approx(0.85)
    assert any("non-negative" in msg for msg in io.warn_log)


def test_walker_rejects_non_numeric_and_reprompts(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    io = ScriptedIO(["abc", "", "", "", "", ""])
    changes = walk_adjustments(leaves, io)
    assert changes == []
    assert any("non-negative number" in msg for msg in io.warn_log)


def test_walker_info_log_includes_progress_counter(tmp_path):
    nodes = _load(tmp_path)
    leaves = list(iter_leaf_nodes(nodes))
    io = ScriptedIO([""] * 5)
    walk_adjustments(leaves, io)
    joined = "\n".join(io.info_log)
    assert "[1/5]" in joined
    assert "[5/5]" in joined


def test_walker_handles_no_eligible_leaves(tmp_path):
    """Plan with only zero-weight leaves prints an info message and returns."""
    nodes = [CategoryNode(name="Cash", weight=0.0, volatility=0.01)]
    leaves = list(iter_leaf_nodes(nodes))
    io = ScriptedIO([])
    changes = walk_adjustments(leaves, io)
    assert changes == []
    assert any("positive weight" in msg for msg in io.info_log)


# ---------------------------------------------------------------------------
# render_diff / render_list
# ---------------------------------------------------------------------------


def test_render_diff_empty_returns_no_changes_marker():
    assert render_diff([]) == "(no changes)"


def test_render_diff_shows_old_and_new(tmp_path):
    changes = [
        LeafChange(path=("Bonds", "Developed", "UK", "Govt"), old=0.9, new=0.95),
        LeafChange(path=("Equities", "EM"), old=0.85, new=1.0),
    ]
    out = render_diff(changes)
    assert "Bonds / Developed / UK / Govt" in out
    assert "0.9" in out and "0.95" in out
    assert "Equities / EM" in out
    assert "0.85" in out


def test_render_list_includes_zero_weight_leaves(tmp_path):
    nodes = _load(tmp_path, PLAN_WITH_CASH_YAML)
    leaves = list(iter_leaf_nodes(nodes))
    out = render_list(leaves)
    assert "Cash" in out
    assert "NAM" in out
    # Header is present.
    assert "PATH" in out and "WEIGHT" in out and "ADJ" in out


def test_render_list_empty():
    assert render_list([]) == "(no leaves)"


# ---------------------------------------------------------------------------
# confirm_changes
# ---------------------------------------------------------------------------


def test_confirm_changes_yes_returns_true(tmp_path):
    changes = [LeafChange(path=("Equities", "EM"), old=0.85, new=0.9)]
    io = ScriptedIO(["y"])
    assert confirm_changes(tmp_path / "plan.yaml", changes, io) is True


def test_confirm_changes_no_returns_false(tmp_path):
    changes = [LeafChange(path=("Equities", "EM"), old=0.85, new=0.9)]
    io = ScriptedIO(["n"])
    assert confirm_changes(tmp_path / "plan.yaml", changes, io) is False


def test_confirm_changes_skip_prompt_short_circuits(tmp_path):
    changes = [LeafChange(path=("Equities", "EM"), old=0.85, new=0.9)]
    io = ScriptedIO([])  # no answers consumed
    assert confirm_changes(tmp_path / "plan.yaml", changes, io, skip_prompt=True) is True


def test_confirm_changes_empty_changes_returns_false(tmp_path):
    io = ScriptedIO([])
    assert confirm_changes(tmp_path / "plan.yaml", [], io) is False


# ---------------------------------------------------------------------------
# End-to-end: walker → write_plan_yaml round-trip
# ---------------------------------------------------------------------------


def test_walker_changes_round_trip_through_write_plan_yaml(tmp_path):
    """A mutated leaf survives the write/load round-trip via write_plan_yaml."""
    plan_path = _write_plan(tmp_path, SAMPLE_PLAN_YAML)
    nodes = load_category_nodes_from_yaml(plan_path)
    leaves = list(iter_leaf_nodes(nodes))
    # Change EMEA 0.9 → 0.95 only.
    io = ScriptedIO(["", "0.95", "", "", ""])
    changes = walk_adjustments(leaves, io)
    assert len(changes) == 1

    write_plan_yaml(plan_path, nodes)
    reloaded = load_category_nodes_from_yaml(plan_path)
    emea = next(n for p, n in iter_leaf_nodes(reloaded) if p[-1] == "EMEA")
    assert emea.adjustment == pytest.approx(0.95)
