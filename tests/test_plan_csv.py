"""
Unit tests for `riskbalancer.plan_csv`.

Covers the leaves-only interleaved CSV format: header shape with
`level1,weight1,...,levelN,weightN,volatility,adjustment`, round-trip
fidelity, conflict detection on per-level weights repeated across
sibling leaves, contiguous-path validation, and the catalogue of typed
parse errors.

Author: Emre Tezel
"""

from __future__ import annotations

import io
import random
import textwrap
from pathlib import Path

import pytest
import yaml

from riskbalancer.configuration import (
    CategoryNode,
    load_category_nodes_from_yaml,
)
from riskbalancer.plan_bootstrap import write_plan_yaml
from riskbalancer.plan_csv import PlanCSVError, read_plan_csv, write_plan_csv

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_PLAN_YAML = """\
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
"""

# 4-deep so the header writer is exercised at varying widths.
DEEP_PLAN_YAML = """\
assets:
  - name: Equities
    weight: 0.5
    children:
      - name: Developed
        weight: 1.0
        children:
          - name: NAM
            weight: 1.0
            children:
              - name: US
                weight: 1.0
                volatility: 0.175
                adjustment: 1.0
  - name: Bonds
    weight: 0.5
    volatility: 0.07
    adjustment: 1.0
"""


def _roundtrip_through_csv(nodes: list[CategoryNode]) -> list[CategoryNode]:
    """Write `nodes` to a CSV buffer, read it back, return the new tree."""
    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    buffer.seek(0)
    return read_plan_csv(buffer)


def _yaml_form(nodes: list[CategoryNode], tmp_path: Path, name: str) -> str:
    """Write `nodes` via the project's writer and return the file text.

    Round-trip equality is asserted at the YAML level (rather than on the
    CategoryNode instances directly) because the loader normalises some
    values — comparing the YAML byte stream is the strongest "the user
    sees the same plan" check.
    """
    target = tmp_path / name
    write_plan_yaml(target, nodes)
    return target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_roundtrip_simple_plan_byte_stable(tmp_path):
    """Simple two-level plan: YAML → CSV → YAML produces identical bytes."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(SIMPLE_PLAN_YAML, encoding="utf-8")
    original = load_category_nodes_from_yaml(plan_path)

    rebuilt = _roundtrip_through_csv(original)

    assert _yaml_form(original, tmp_path, "before.yaml") == _yaml_form(
        rebuilt, tmp_path, "after.yaml"
    )


def test_roundtrip_deep_plan_byte_stable(tmp_path):
    """4-level plan: round-trip preserves the nested structure exactly."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(DEEP_PLAN_YAML, encoding="utf-8")
    original = load_category_nodes_from_yaml(plan_path)

    rebuilt = _roundtrip_through_csv(original)

    assert _yaml_form(original, tmp_path, "before.yaml") == _yaml_form(
        rebuilt, tmp_path, "after.yaml"
    )


def test_roundtrip_committed_seed_plan(tmp_path):
    """The committed seed plan (broadest example we ship) round-trips cleanly."""
    seed = Path(__file__).resolve().parents[1] / "config" / "seed_plan.yaml"
    original = load_category_nodes_from_yaml(seed)

    rebuilt = _roundtrip_through_csv(original)

    assert _yaml_form(original, tmp_path, "before.yaml") == _yaml_form(
        rebuilt, tmp_path, "after.yaml"
    )


# ---------------------------------------------------------------------------
# Header writing & shape
# ---------------------------------------------------------------------------


def test_header_has_interleaved_level_and_weight_columns(tmp_path):
    """4-deep plan emits `level1,weight1,...,level4,weight4,vol,adj`."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(DEEP_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)

    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    header = buffer.getvalue().splitlines()[0]
    assert header == (
        "level1,weight1,level2,weight2,level3,weight3,level4,weight4,volatility,adjustment"
    )


def test_export_writes_only_leaves(tmp_path):
    """Branches must not appear in the CSV — every data row is a leaf."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(SIMPLE_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)

    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    rows = buffer.getvalue().splitlines()[1:]  # drop header
    # 3 leaves: Equities/NAM, Equities/EMEA, Bonds.
    assert len(rows) == 3
    leaf_paths = [
        tuple(cell for i, cell in enumerate(line.split(",")) if i % 2 == 0 and cell)
        for line in rows
    ]
    assert tuple(leaf_paths[0][:2]) == ("Equities", "NAM")
    assert tuple(leaf_paths[1][:2]) == ("Equities", "EMEA")
    assert leaf_paths[2][:1] == ("Bonds",)


def test_top_level_leaf_pads_trailing_cells(tmp_path):
    """A shallower leaf (depth 1 in a depth-2 plan) leaves trailing cells blank."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(SIMPLE_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)

    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    rows = buffer.getvalue().splitlines()
    bonds_row = next(line for line in rows if line.startswith("Bonds,"))
    cells = bonds_row.split(",")
    # Header is level1,weight1,level2,weight2,volatility,adjustment.
    assert cells[0] == "Bonds"
    assert cells[1] == "0.4"
    assert cells[2] == ""  # blank level2
    assert cells[3] == ""  # blank weight2
    assert cells[4] == "0.07"
    assert cells[5] == "1.0"


def test_resolved_volatility_uses_inherited_value(tmp_path):
    """A leaf with no own volatility inherits from the nearest ancestor that has one."""
    yaml_text = textwrap.dedent(
        """\
        assets:
          - name: Bonds
            weight: 1.0
            volatility: 0.08
            children:
              - name: Govt
                weight: 1.0
                adjustment: 1.0
        """
    )
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml_text, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)

    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    rows = buffer.getvalue().splitlines()
    # Header: level1,weight1,level2,weight2,volatility,adjustment.
    govt_row = next(line for line in rows if line.startswith("Bonds,"))
    cells = govt_row.split(",")
    # The leaf's resolved volatility is the inherited 0.08, not blank.
    assert cells[4] == "0.08"


# ---------------------------------------------------------------------------
# Order-independence
# ---------------------------------------------------------------------------


def test_read_is_structurally_order_agnostic_for_shuffled_input(tmp_path):
    """Shuffling rows in the CSV (keeping the header) yields a structurally equivalent plan.

    Sibling order in a leaves-only CSV is determined by the row order, so
    a shuffled file legitimately produces a tree with re-ordered siblings.
    The check is that the same set of paths with the same per-leaf values
    survives — order-equivalence, not byte-equality.
    """
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(SIMPLE_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)

    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    lines = buffer.getvalue().splitlines()
    header, rest = lines[0], lines[1:]
    rng = random.Random(1234)
    rng.shuffle(rest)
    shuffled = "\n".join([header, *rest]) + "\n"

    rebuilt = read_plan_csv(io.StringIO(shuffled))
    assert _flat_leaf_set(nodes) == _flat_leaf_set(rebuilt)


def _flat_leaf_set(
    nodes: list[CategoryNode],
) -> set[tuple[tuple[str, ...], float, float | None, float]]:
    """Return `{(path, cumulative_weight, volatility, adjustment)}` for every leaf."""
    out: set[tuple[tuple[str, ...], float, float | None, float]] = set()

    def walk(
        children: list[CategoryNode],
        prefix: tuple[str, ...],
        weight: float,
        inherited_vol: float | None,
    ) -> None:
        for child in children:
            path = prefix + (child.name,)
            cumulative = weight * child.weight
            next_vol = child.volatility if child.volatility is not None else inherited_vol
            if child.children:
                walk(child.children, path, cumulative, next_vol)
            else:
                out.add((path, cumulative, next_vol, child.adjustment))

    walk(nodes, prefix=(), weight=1.0, inherited_vol=None)
    return out


# ---------------------------------------------------------------------------
# Optional cells & loader-compatible parsing
# ---------------------------------------------------------------------------


def test_blank_volatility_and_adjustment_default_correctly():
    """Empty volatility cell → None; empty adjustment cell → 1.0."""
    csv_text = textwrap.dedent(
        """\
        level1,weight1,volatility,adjustment
        Bonds,1.0,,
        """
    )
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert len(nodes) == 1
    assert nodes[0].name == "Bonds"
    assert nodes[0].volatility is None
    assert nodes[0].adjustment == pytest.approx(1.0)


def test_percent_suffix_on_weight_is_accepted():
    """`55%` is accepted, mirroring the YAML loader."""
    csv_text = textwrap.dedent(
        """\
        level1,weight1,volatility,adjustment
        A,55%,0.1,1.0
        B,45%,0.1,1.0
        """
    )
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert nodes[0].weight == pytest.approx(0.55)
    assert nodes[1].weight == pytest.approx(0.45)


def test_blank_rows_are_skipped_silently():
    """Trailing/embedded blank rows from spreadsheets shouldn't error."""
    csv_text = textwrap.dedent(
        """\
        level1,weight1,volatility,adjustment
        A,0.5,0.1,1.0

        B,0.5,0.1,1.0

        """
    )
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert [node.name for node in nodes] == ["A", "B"]


def test_short_rows_are_padded_with_blanks():
    """Spreadsheets often trim trailing empty cells — the reader pads them back."""
    csv_text = "level1,weight1,volatility,adjustment\nBonds,0.5\n"
    nodes = read_plan_csv(io.StringIO(csv_text))
    # Bonds gets the loader's blank-volatility (None) and default adjustment (1.0).
    assert nodes[0].volatility is None
    assert nodes[0].adjustment == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


def test_conflicting_branch_weight_errors_with_both_rows():
    """Two sibling leaves under the same parent must agree on the parent's weight."""
    csv_text = textwrap.dedent(
        """\
        level1,weight1,level2,weight2,volatility,adjustment
        Equities,0.6,NAM,0.5,0.18,1.0
        Equities,0.7,EMEA,0.5,0.18,0.9
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    message = str(excinfo.value)
    assert "Equities" in message
    assert "row 2" in message  # first occurrence
    assert excinfo.value.row_number == 3  # the disagreeing row


def test_matching_branch_weights_pass_conflict_check():
    """Sibling leaves agreeing on parent weight build a valid tree."""
    csv_text = textwrap.dedent(
        """\
        level1,weight1,level2,weight2,volatility,adjustment
        Equities,0.6,NAM,0.5,0.18,1.0
        Equities,0.6,EMEA,0.5,0.18,0.9
        Bonds,0.4,,,0.07,1.0
        """
    )
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert nodes[0].name == "Equities"
    assert nodes[0].weight == pytest.approx(0.6)
    assert [child.name for child in nodes[0].children] == ["NAM", "EMEA"]


def test_floating_point_close_branch_weights_are_accepted():
    """Tiny float noise on a repeated branch weight does not trigger a conflict."""
    csv_text = textwrap.dedent(
        """\
        level1,weight1,level2,weight2,volatility,adjustment
        Equities,0.6,NAM,0.5,0.18,1.0
        Equities,0.6000000000000001,EMEA,0.5,0.18,0.9
        """
    )
    # No exception expected — within absolute tolerance.
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert nodes[0].weight == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Header and row error cases
# ---------------------------------------------------------------------------


def test_missing_header_trailing_columns_errors():
    csv_text = "level1,weight1,volatility\nA,1.0,0.1\n"
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 1
    assert "volatility,adjustment" in str(excinfo.value)


def test_misnamed_level_column_errors():
    csv_text = "category,weight1,volatility,adjustment\nA,1.0,0.1,1.0\n"
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 1
    assert "level1" in str(excinfo.value)


def test_misnamed_weight_column_errors():
    csv_text = "level1,share,volatility,adjustment\nA,1.0,0.1,1.0\n"
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 1
    assert "weight1" in str(excinfo.value)


def test_odd_prefix_count_errors():
    """Header like `level1,weight1,level2,vol,adj` has an unpaired level2."""
    csv_text = "level1,weight1,level2,volatility,adjustment\nA,1.0,B,0.1,1.0\n"
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 1


def test_empty_csv_errors():
    with pytest.raises(PlanCSVError):
        read_plan_csv(io.StringIO(""))


def test_duplicate_leaf_path_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight1,volatility,adjustment
        Bonds,0.5,0.07,1.0
        Bonds,0.5,0.07,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 3
    assert "duplicate" in str(excinfo.value)


def test_leaf_under_leaf_errors():
    """A leaf whose path is a strict prefix of another row's path is invalid."""
    csv_text = textwrap.dedent(
        """\
        level1,weight1,level2,weight2,volatility,adjustment
        Equities,1.0,,,0.18,1.0
        Equities,1.0,NAM,0.5,0.18,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert "leaf" in str(excinfo.value)


def test_non_numeric_weight_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight1,volatility,adjustment
        Bonds,abc,0.07,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2


def test_missing_weight_when_level_is_filled_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight1,volatility,adjustment
        Bonds,,0.07,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2
    assert "level1" in str(excinfo.value) or "weight1" in str(excinfo.value)


def test_filled_weight_with_blank_level_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight1,level2,weight2,volatility,adjustment
        Equities,0.5,,0.5,0.18,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2


def test_gap_in_level_columns_errors():
    """`Equities,0.6,,,NAM,0.5,...` is a silent way to lose hierarchy."""
    csv_text = textwrap.dedent(
        """\
        level1,weight1,level2,weight2,level3,weight3,volatility,adjustment
        Equities,0.6,,,NAM,0.5,0.18,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2
    assert "contiguous" in str(excinfo.value)


def test_empty_path_row_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight1,volatility,adjustment
        ,,0.07,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2


def test_too_many_cells_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight1,volatility,adjustment
        Bonds,0.5,0.07,1.0,extra
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2


# ---------------------------------------------------------------------------
# Round-trip through the YAML loader (sanity check)
# ---------------------------------------------------------------------------


def test_yaml_loader_round_trip_via_loader(tmp_path):
    """Belt-and-braces: rebuilt tree validates through the YAML loader."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(SIMPLE_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)
    rebuilt = _roundtrip_through_csv(nodes)
    out_path = tmp_path / "rebuilt.yaml"
    write_plan_yaml(out_path, rebuilt)
    reloaded = load_category_nodes_from_yaml(out_path)
    assert yaml.safe_dump(
        [_node_to_plain(node) for node in rebuilt], sort_keys=False
    ) == yaml.safe_dump([_node_to_plain(node) for node in reloaded], sort_keys=False)


def _node_to_plain(node: CategoryNode) -> dict:
    """Plain-dict view of a CategoryNode used purely for test comparison."""
    return {
        "name": node.name,
        "weight": node.weight,
        "volatility": node.volatility,
        "adjustment": node.adjustment,
        "children": [_node_to_plain(child) for child in node.children],
    }
