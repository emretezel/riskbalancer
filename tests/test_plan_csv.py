"""
Unit tests for `riskbalancer.plan_csv`.

Covers the depth-column CSV format: header parsing, round-trip fidelity
(YAML → CSV → YAML must be byte-stable), order-independence on read,
optional cell handling, and the catalogue of typed parse errors.

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

# Includes a 4-deep tree so the header writer is exercised at varying widths.
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
    """Write `nodes` to YAML via the project's writer and return the file text.

    Round-trip equality is asserted at the YAML level rather than on the
    CategoryNode instances directly because the loader normalises some
    values (e.g. `volatility: 0.0` → None) — comparing the YAML byte stream
    is the strongest "the user sees the same plan" check.
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
    """The committed seed plan (the broadest example we ship) round-trips cleanly."""
    seed = Path(__file__).resolve().parents[1] / "config" / "seed_plan.yaml"
    original = load_category_nodes_from_yaml(seed)

    rebuilt = _roundtrip_through_csv(original)

    assert _yaml_form(original, tmp_path, "before.yaml") == _yaml_form(
        rebuilt, tmp_path, "after.yaml"
    )


# ---------------------------------------------------------------------------
# Header writing
# ---------------------------------------------------------------------------


def test_header_width_matches_max_depth(tmp_path):
    """A 4-deep plan emits `level1..level4` plus the trailing triple."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(DEEP_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)

    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    header = buffer.getvalue().splitlines()[0]
    assert header == "level1,level2,level3,level4,weight,volatility,adjustment"


def test_export_branches_have_blank_volatility_and_explicit_adjustment(tmp_path):
    """Branch rows leave volatility blank but write adjustment explicitly."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(SIMPLE_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)

    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    rows = list(buffer.getvalue().splitlines())
    # Row for top-level "Equities" branch (vol blank, adjustment 1.0 explicit).
    equities_row = next(line for line in rows if line.startswith("Equities,,"))
    cells = equities_row.split(",")
    # cells: ['Equities', '', 'weight', 'volatility', 'adjustment']
    assert cells[2] == "0.6"
    assert cells[3] == ""
    assert cells[4] == "1.0"


# ---------------------------------------------------------------------------
# Order-independence
# ---------------------------------------------------------------------------


def test_read_is_order_agnostic_for_shuffled_input(tmp_path):
    """Shuffling rows in the CSV (keeping the header) yields the same plan."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(SIMPLE_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)
    expected_yaml = _yaml_form(nodes, tmp_path, "expected.yaml")

    buffer = io.StringIO()
    write_plan_csv(nodes, buffer)
    lines = buffer.getvalue().splitlines()
    header, rest = lines[0], lines[1:]
    rng = random.Random(1234)
    rng.shuffle(rest)
    shuffled = "\n".join([header, *rest]) + "\n"

    rebuilt = read_plan_csv(io.StringIO(shuffled))
    assert _yaml_form(rebuilt, tmp_path, "actual.yaml") == expected_yaml


# ---------------------------------------------------------------------------
# Optional cells
# ---------------------------------------------------------------------------


def test_blank_volatility_and_adjustment_default_correctly():
    """Empty volatility cell → None; empty adjustment cell → 1.0."""
    csv_text = textwrap.dedent(
        """\
        level1,weight,volatility,adjustment
        Bonds,1.0,,
        """
    )
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert len(nodes) == 1
    assert nodes[0].name == "Bonds"
    assert nodes[0].volatility is None
    assert nodes[0].adjustment == pytest.approx(1.0)


def test_percent_suffix_on_weight_is_accepted():
    """`55%` should be accepted, mirroring the YAML loader."""
    csv_text = textwrap.dedent(
        """\
        level1,weight,volatility,adjustment
        A,55%,0.1,1.0
        B,45%,0.1,1.0
        """
    )
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert nodes[0].weight == pytest.approx(0.55)
    assert nodes[1].weight == pytest.approx(0.45)


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_missing_header_trailing_columns_errors():
    csv_text = "level1,weight,volatility\nA,1.0,0.1\n"
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 1
    assert "weight,volatility,adjustment" in str(excinfo.value)


def test_misnamed_level_column_errors():
    csv_text = "category,weight,volatility,adjustment\nA,1.0,0.1,1.0\n"
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 1
    assert "level1" in str(excinfo.value)


def test_empty_csv_errors():
    with pytest.raises(PlanCSVError):
        read_plan_csv(io.StringIO(""))


def test_missing_parent_errors_with_row_number():
    csv_text = textwrap.dedent(
        """\
        level1,level2,weight,volatility,adjustment
        Equities,NAM,0.5,0.18,1.0
        """
    )
    # NAM has no parent row; rows are sorted by depth so the depth-2 row
    # is processed without ever seeing a depth-1 'Equities' row.
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2
    assert "parent" in str(excinfo.value)


def test_duplicate_path_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight,volatility,adjustment
        Bonds,0.5,0.07,1.0
        Bonds,0.5,0.07,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    # Sorted by (depth, row_number) — the duplicate is at row 3 in source order.
    assert excinfo.value.row_number == 3
    assert "duplicate" in str(excinfo.value)


def test_non_numeric_weight_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight,volatility,adjustment
        Bonds,abc,0.07,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2


def test_missing_weight_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight,volatility,adjustment
        Bonds,,0.07,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2
    assert "weight" in str(excinfo.value)


def test_gap_in_level_columns_errors():
    """`Equities, , NAM` is a silent way to lose hierarchy and must be rejected."""
    csv_text = textwrap.dedent(
        """\
        level1,level2,level3,weight,volatility,adjustment
        Equities,,NAM,0.5,0.18,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2
    assert "contiguous" in str(excinfo.value)


def test_empty_path_row_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight,volatility,adjustment
        ,0.5,0.07,1.0
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2


def test_too_many_cells_errors():
    csv_text = textwrap.dedent(
        """\
        level1,weight,volatility,adjustment
        Bonds,0.5,0.07,1.0,extra
        """
    )
    with pytest.raises(PlanCSVError) as excinfo:
        read_plan_csv(io.StringIO(csv_text))
    assert excinfo.value.row_number == 2


def test_blank_rows_are_skipped_silently():
    """Trailing/embedded blank rows from spreadsheets shouldn't error."""
    csv_text = textwrap.dedent(
        """\
        level1,weight,volatility,adjustment
        A,0.5,0.1,1.0

        B,0.5,0.1,1.0

        """
    )
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert [node.name for node in nodes] == ["A", "B"]


# ---------------------------------------------------------------------------
# Internal helpers (used only via the public API)
# ---------------------------------------------------------------------------


def test_short_rows_are_padded_with_blanks():
    """Spreadsheets often trim trailing empty cells — the reader pads them back."""
    # Header is 4 columns wide (level1, weight, volatility, adjustment); the
    # data row provides only 3 cells. The trailing blank should be treated
    # as the (optional) adjustment cell.
    csv_text = "level1,weight,volatility,adjustment\nBonds,0.5,0.07\n"
    nodes = read_plan_csv(io.StringIO(csv_text))
    assert nodes[0].adjustment == pytest.approx(1.0)


def test_yaml_loader_round_trip_via_loader(tmp_path):
    """Belt-and-braces: ensure the rebuilt tree validates through the YAML loader."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(SIMPLE_PLAN_YAML, encoding="utf-8")
    nodes = load_category_nodes_from_yaml(plan_path)
    rebuilt = _roundtrip_through_csv(nodes)
    out_path = tmp_path / "rebuilt.yaml"
    write_plan_yaml(out_path, rebuilt)
    # If the loader can re-read it without error, the structure is valid.
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
