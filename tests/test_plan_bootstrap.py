"""
Tests for the plan-bootstrap module.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml

from riskbalancer.cli import cmd_plan_create
from riskbalancer.configuration import (
    collect_category_weight_validation_failures,
    load_category_nodes_from_yaml,
)
from riskbalancer.paths import UserPaths
from riskbalancer.plan_bootstrap import (
    PlanCreationAborted,
    ScriptedIO,
    build_catalog,
    clone_plan,
    confirm_and_write_plan,
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


# ---------------------------------------------------------------------------
# `+ new` sentinel: adding categories that aren't in the catalog
# ---------------------------------------------------------------------------


def test_walk_catalog_supports_added_leaf_at_top_level(tmp_path):
    """User picks `+ new`, names a brand-new leaf, supplies vol/adj."""
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    answers = [
        # Top level: pick the sentinel, name "Crypto", no sub-categories,
        # weight 100%, then explicit vol/adj for the leaf.
        "+ new",
        "Crypto",
        "n",  # no sub-categories
        "100",
        "n",  # don't add another at top level
        "0.6",  # volatility
        "1.0",  # adjustment
    ]
    plan = walk_catalog_interactive(catalog, ScriptedIO(answers))
    assert [n.name for n in plan] == ["Crypto"]
    assert plan[0].volatility == pytest.approx(0.6)
    assert plan[0].adjustment == pytest.approx(1.0)
    assert plan[0].children == []


def test_walk_catalog_supports_added_branch_with_added_children(tmp_path):
    """`+ new` branch with sub-categories recurses into a level whose only
    available option is itself `+ new` — the user adds two synthetic leaves."""
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    answers = [
        # Top level: `+ new` named "Alternative", branch with sub-categories.
        "+ new",
        "Alternative",
        "y",  # has sub-categories
        "100",  # weight
        "n",  # don't add another at top level
        # Sub-level (Alternative): only `+ new` is available. Add two leaves.
        "+ new",
        "Crypto",
        "n",  # leaf
        "60",
        "y",  # add another
        "+ new",
        "RealEstate",
        "n",  # leaf
        "40",
        "n",  # done
        # Leaf metadata for Crypto, then RealEstate.
        "0.6",
        "1.0",
        "0.15",
        "1.0",
    ]
    plan = walk_catalog_interactive(catalog, ScriptedIO(answers))
    assert [n.name for n in plan] == ["Alternative"]
    children = plan[0].children
    assert [c.name for c in children] == ["Crypto", "RealEstate"]
    assert children[0].volatility == pytest.approx(0.6)
    assert children[1].volatility == pytest.approx(0.15)
    failures = collect_category_weight_validation_failures(plan)
    assert failures == []


def test_walk_catalog_rejects_new_category_name_collision(tmp_path):
    """A synthetic category cannot collide with a sibling at the same level."""
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    answers = [
        # Try to name the new category "Bonds" — collides with a remaining
        # catalog sibling — re-prompt; then "Equities" — also collides; then
        # accept "Crypto".
        "+ new",
        "Bonds",
        "Equities",
        "Crypto",
        "n",  # leaf
        "100",
        "n",  # don't add another
        "0.5",
        "1.0",
    ]
    io = ScriptedIO(answers)
    plan = walk_catalog_interactive(catalog, io)
    assert [n.name for n in plan] == ["Crypto"]
    # Two collision warnings should have been surfaced.
    assert sum(1 for msg in io.warn_log if "already exists" in msg) == 2


# ---------------------------------------------------------------------------
# Exit at any prompt: quit / exit / Ctrl+C
# ---------------------------------------------------------------------------


def test_walk_catalog_aborts_on_quit_at_first_pick(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    answers = ["quit"]
    with pytest.raises(PlanCreationAborted):
        walk_catalog_interactive(catalog, ScriptedIO(answers))


def test_walk_catalog_aborts_on_exit_during_new_category_name(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    answers = ["+ new", "exit"]
    with pytest.raises(PlanCreationAborted):
        walk_catalog_interactive(catalog, ScriptedIO(answers))


@dataclass
class _RaisingIO:
    """Test IO that raises a configured exception on the Nth prompt call.

    Used to simulate Ctrl+C (KeyboardInterrupt) and EOF (EOFError) without
    needing a real TTY. The first `before` prompts return scripted answers;
    the next prompt raises `exc`.
    """

    answers: list[str]
    exc: BaseException
    before: int = 0
    info_log: list[str] = field(default_factory=list)
    warn_log: list[str] = field(default_factory=list)
    _index: int = 0

    def prompt(self, message: str) -> str:
        if self._index == self.before:
            self._index += 1
            raise self.exc
        answer = self.answers[self._index]
        self._index += 1
        return answer

    def info(self, message: str) -> None:
        self.info_log.append(message)

    def warn(self, message: str) -> None:
        self.warn_log.append(message)


def test_walk_catalog_aborts_on_keyboard_interrupt(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    io = _RaisingIO(answers=[], exc=KeyboardInterrupt(), before=0)
    with pytest.raises(PlanCreationAborted):
        walk_catalog_interactive(catalog, io)


def test_walk_catalog_aborts_on_eof(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    io = _RaisingIO(answers=[], exc=EOFError(), before=0)
    with pytest.raises(PlanCreationAborted):
        walk_catalog_interactive(catalog, io)


# ---------------------------------------------------------------------------
# Final confirmation: tree summary + save y/N
# ---------------------------------------------------------------------------


def _drive_simple_plan(catalog) -> tuple[list, ScriptedIO]:
    """Run the walker with a minimal scripted flow (Bonds 100% leaf)."""
    answers = ["Bonds", "100", "n", "", ""]
    io = ScriptedIO(answers)
    plan = walk_catalog_interactive(catalog, io)
    return plan, io


def test_confirm_and_write_plan_writes_when_user_accepts(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)
    plan, _ = _drive_simple_plan(catalog)

    out = tmp_path / "out.yaml"
    confirm_io = ScriptedIO(["y"])
    confirm_and_write_plan(out, plan, confirm_io)
    assert out.exists()
    parsed = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert parsed["assets"][0]["name"] == "Bonds"
    # The summary must have been printed before the save prompt.
    assert any("Plan summary" in msg for msg in confirm_io.info_log)
    assert any("Bonds" in msg for msg in confirm_io.info_log)


def test_confirm_and_write_plan_aborts_on_decline(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)
    plan, _ = _drive_simple_plan(catalog)

    out = tmp_path / "out.yaml"
    with pytest.raises(PlanCreationAborted):
        confirm_and_write_plan(out, plan, ScriptedIO(["n"]))
    assert not out.exists()


def test_confirm_and_write_plan_aborts_on_quit(tmp_path):
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)
    plan, _ = _drive_simple_plan(catalog)

    out = tmp_path / "out.yaml"
    with pytest.raises(PlanCreationAborted):
        confirm_and_write_plan(out, plan, ScriptedIO(["quit"]))
    assert not out.exists()


# ---------------------------------------------------------------------------
# CLI-level integration: cmd_plan_create exits cleanly on abort
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Weight parsing: zero is allowed; small suggestions render with precision
# ---------------------------------------------------------------------------


def test_walk_catalog_accepts_zero_weight_when_siblings_cover_100(tmp_path):
    """Picking a category at 0% is allowed if siblings make up the level."""
    paths = _build_paths(tmp_path)
    catalog = build_catalog(paths)

    # Top level: Equities=100, Bonds=0. Level still sums to 100% so the
    # validator accepts it. Then drill through to the NAM leaf.
    answers = [
        "Equities",
        "100",
        "y",
        "Bonds",
        "0",
        "n",
        # Equities children: Developed=100 (skip EM)
        "Developed",
        "100",
        "n",
        # Developed children: NAM=100 (skip EMEA)
        "NAM",
        "100",
        "n",
        # NAM leaf metadata, then Bonds leaf metadata.
        "",
        "",
        "",
        "",
    ]
    plan = walk_catalog_interactive(catalog, ScriptedIO(answers))
    bonds = next(n for n in plan if n.name == "Bonds")
    assert bonds.weight == pytest.approx(0.0)
    failures = collect_category_weight_validation_failures(plan)
    assert failures == []


def test_format_weight_suggestion_renders_small_value_with_precision():
    """A sub-percent suggestion must not display as `0%` and mislead users."""
    from riskbalancer.plan_bootstrap import _format_weight_suggestion

    assert _format_weight_suggestion(0.004) == " (catalog suggests 0.40%)"
    assert _format_weight_suggestion(0.0) == " (catalog suggests 0%)"
    assert _format_weight_suggestion(0.55) == " (catalog suggests 55%)"
    assert _format_weight_suggestion(None) == ""


def test_cmd_plan_create_aborts_cleanly_on_quit(tmp_path, monkeypatch, capsys):
    paths = _build_paths(tmp_path, user="emre")
    paths.user_dir.mkdir(parents=True, exist_ok=True)

    # The walker reads via `input()` through `StdIO.prompt`. Replace it with
    # a script that types `quit` at the very first prompt.
    answers = iter(["quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    args = argparse.Namespace(user="emre", overwrite=False, from_user=None)
    rc = cmd_plan_create(args, paths=paths)
    assert rc == 1
    assert not paths.plan.exists()
    err = capsys.readouterr().err
    assert "aborted" in err
