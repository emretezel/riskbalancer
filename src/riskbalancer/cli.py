from __future__ import annotations

"""
RiskBalancer command-line interface utilities.

Author: Emre Tezel
"""

import argparse
import csv
import sys
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Mapping

import yaml
from collections import defaultdict

from .adapters import AJBellCSVAdapter
from .configuration import load_portfolio_plan_from_yaml
from .models import CategoryPath, Investment

DEFAULT_CATEGORY = CategoryPath("Uncategorized", "Pending Review")
DEFAULT_LEAF_VOLATILITY = 0.15
PORTFOLIO_DIR = Path("portfolios")
ADAPTERS = {
    "ajbell": AJBellCSVAdapter,
}


@dataclass
class InstrumentMapping:
    """User-defined allocation metadata and optional volatility override."""

    allocations: List["CategoryAllocation"]
    volatility: Optional[float] = None

    def normalized_allocations(self) -> List["CategoryAllocation"]:
        if not self.allocations:
            raise ValueError("Instrument mapping must contain at least one category")
        total = sum(allocation.weight for allocation in self.allocations)
        if total <= 0:
            raise ValueError("Allocation weights must be positive")
        return [
            CategoryAllocation(path=allocation.path, weight=allocation.weight / total)
            for allocation in self.allocations
        ]


@dataclass
class CategoryAllocation:
    """Represents a single category allocation entry."""

    path: CategoryPath
    weight: float = 1.0


@dataclass
class SourceSpec:
    """Descriptor for a statement source specified via CLI flags."""

    adapter: str
    statement: Path
    mappings: Path


class PlanIndex:
    """Helper that resolves free-form category labels to canonical paths."""

    def __init__(self, labels: Dict[str, CategoryPath]):
        self._labels = labels

    @classmethod
    def from_plan(cls, plan) -> "PlanIndex":
        labels = {}
        for target in plan.targets:
            label = target.path.label()
            labels[_normalize_label(label)] = target.path
        return cls(labels)

    def resolve(self, raw: str) -> Optional[CategoryPath]:
        return self._labels.get(_normalize_label(raw))

    def available_labels(self) -> List[str]:
        return sorted(path.label() for path in self._labels.values())


def _normalize_label(label: str) -> str:
    parts = [part.strip() for part in label.split("/") if part.strip()]
    return " / ".join(parts).lower()


def _parse_category_label(label: str) -> CategoryPath:
    parts = [part.strip() for part in label.split("/") if part.strip()]
    return CategoryPath(parts)


def load_mappings(path: Path) -> Dict[str, InstrumentMapping]:
    """Load instrument mappings from a YAML file."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mappings: Dict[str, InstrumentMapping] = {}
    for instrument, payload in data.items():
        allocations_data = payload.get("allocations")
        if not allocations_data and payload.get("category"):
            allocations_data = [payload["category"]]
        if not allocations_data:
            continue
        allocations: List[CategoryAllocation] = []
        for entry in allocations_data:
            if isinstance(entry, str):
                category_label = entry
                weight = 1.0
            else:
                category_label = entry.get("category")
                weight = float(entry.get("weight", 1.0))
            if not category_label:
                continue
            allocations.append(
                CategoryAllocation(path=_parse_category_label(category_label), weight=weight)
            )
        if not allocations:
            continue
        volatility = payload.get("volatility")
        mappings[instrument] = InstrumentMapping(allocations=allocations, volatility=volatility)
    return mappings


def save_mappings(path: Path, mappings: Dict[str, InstrumentMapping]) -> None:
    """Persist instrument mappings to YAML."""
    serializable = {
        instrument: {
            "allocations": [
                {"category": allocation.path.label(), "weight": allocation.weight}
                for allocation in mapping.allocations
            ],
            **({"volatility": mapping.volatility} if mapping.volatility else {}),
        }
        for instrument, mapping in mappings.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(serializable, sort_keys=True), encoding="utf-8")


def parse_allocation_input(user_input: str, plan_index: PlanIndex) -> List[CategoryAllocation]:
    """Parse user-provided comma-separated category labels into allocations."""
    entries = [entry.strip() for entry in user_input.split(",") if entry.strip()]
    if not entries:
        raise ValueError("At least one allocation must be provided")
    allocations: List[CategoryAllocation] = []
    for entry in entries:
        if "=" in entry:
            category_label, weight_text = entry.split("=", 1)
        elif ":" in entry:
            category_label, weight_text = entry.split(":", 1)
        else:
            category_label, weight_text = entry, "100"
        resolved = plan_index.resolve(category_label)
        if not resolved:
            raise ValueError(f"Unknown category path '{category_label.strip()}'")
        weight = _parse_weight_input(weight_text)
        allocations.append(CategoryAllocation(path=resolved, weight=weight))
    total = sum(allocation.weight for allocation in allocations)
    if total <= 0:
        raise ValueError("Allocation weights must be positive")
    return allocations


def _parse_weight_input(raw: str) -> float:
    """Parse textual weights (e.g. 70 or 70%) into fractions."""
    cleaned = raw.strip().rstrip("%")
    if not cleaned:
        raise ValueError("Weight value is required")
    value = float(cleaned)
    if value > 1:
        value = value / 100.0
    if value <= 0:
        raise ValueError("Weights must be positive")
    return value


def parse_source_spec(spec: str) -> SourceSpec:
    """Parse --source adapter/statement/mapping definitions."""
    parts = {}
    for segment in spec.split(","):
        if not segment.strip():
            continue
        if "=" not in segment:
            raise ValueError(f"Invalid source specification segment '{segment}'")
        key, value = segment.split("=", 1)
        parts[key.strip()] = value.strip()
    adapter = parts.get("adapter")
    statement = parts.get("statement")
    mappings = parts.get("mappings")
    if not adapter or not statement or not mappings:
        raise ValueError(
            "Source spec must include adapter=..., statement=..., mappings=..."
        )
    return SourceSpec(
        adapter=adapter,
        statement=Path(statement),
        mappings=Path(mappings),
    )


def investment_to_dict(investment: Investment) -> Dict[str, object]:
    """Convert an Investment to a serialisable dict."""
    return {
        "instrument_id": investment.instrument_id,
        "description": investment.description,
        "market_value": investment.market_value,
        "category": investment.category.label(),
        "source": investment.source,
    }


def investments_to_dicts(investments: Iterable[Investment]) -> List[Dict[str, object]]:
    """Serialise a list of investments for storage."""
    return [investment_to_dict(inv) for inv in investments]


def investment_from_dict(payload: Mapping[str, object]) -> Investment:
    """Hydrate an Investment from stored JSON data."""
    category_label = payload["category"]
    if not isinstance(category_label, str):
        raise ValueError("Category label must be a string")
    return Investment(
        instrument_id=str(payload["instrument_id"]),
        description=str(payload.get("description", "")),
        market_value=float(payload.get("market_value", 0.0)),
        category=_parse_category_label(category_label),
        volatility=float(payload.get("volatility", 0.0)) or 0.0001,
        source=str(payload.get("source", "portfolio")),
    )


def investments_from_dicts(items: Iterable[Mapping[str, object]]) -> List[Investment]:
    """Hydrate multiple investments from stored JSON data."""
    return [investment_from_dict(item) for item in items]


def summarize_portfolio(plan, investments: List[Investment]):
    """Aggregate the portfolio and compute risk/cash weights + target values."""
    totals = defaultdict(float)
    for investment in investments:
        totals[investment.category] += investment.market_value
    total_value = sum(totals.values())

    normalized_weights = {}
    risk_over_vol = {}
    for target in plan:
        normalized = target.target_weight
        normalized_weights[target.path] = normalized
        risk_over_vol[target.path] = normalized / target.volatility

    cash_weight_denominator = sum(risk_over_vol.values()) or 1.0
    summary = []
    for target in plan:
        actual_value = totals.get(target.path, 0.0)
        actual_weight = (actual_value / total_value) if total_value else 0.0
        normalized_risk = normalized_weights[target.path]
        risk_weight = target.risk_weight
        cash_weight = risk_over_vol[target.path] / cash_weight_denominator
        target_value = cash_weight * total_value
        summary.append(
            {
                "path": target.path,
                "label": target.path.label(),
                "risk_weight_raw": risk_weight,
                "risk_weight_normalized": normalized_risk,
                "adjustment": getattr(target, "adjustment", 1.0),
                "volatility": target.volatility,
                "cash_weight": cash_weight,
                "actual_value": actual_value,
                "actual_weight": actual_weight,
                "target_value": target_value,
                "target_weight": cash_weight,
            }
        )
    return total_value, summary


def print_summary_table(total_value: float, rows: List[Dict[str, float]]) -> None:
    header = (
        f"{'Category':55}"
        f"{'Risk Wt':>10}"
        f"{'Norm Wt':>10}"
        f"{'Adj':>6}"
        f"{'Vol':>8}"
        f"{'Cash Wt':>10}"
        f"{'Actual £':>14}"
        f"{'Target £':>14}"
        f"{'Delta £':>14}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        label = row["label"]
        risk = row["risk_weight_raw"]
        norm = row["risk_weight_normalized"]
        adjustment = row["adjustment"]
        vol = row["volatility"]
        cash = row["cash_weight"]
        actual_value = row["actual_value"]
        target_value = row["target_value"]
        delta = actual_value - target_value
        print(
            f"{label:55}"
            f"{risk:10.3f}"
            f"{norm:10.3f}"
            f"{adjustment:6.2f}"
            f"{vol:8.3f}"
            f"{cash:10.3f}"
            f"{actual_value:14,.2f}"
            f"{target_value:14,.2f}"
            f"{delta:14,.2f}"
        )
    print("-" * len(header))
    print(f"{'Total Portfolio Value:':>110} {total_value:14,.2f}")


def export_summary_to_csv(path: Path, rows: List[Dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Category",
                "RiskWeightRaw",
                "RiskWeightNormalized",
                "Adjustment",
                "Volatility",
                "CashWeight",
                "ActualValueGBP",
                "TargetValueGBP",
                "DeltaGBP",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["label"],
                    row["risk_weight_raw"],
                    row["risk_weight_normalized"],
                    row["adjustment"],
                    row["volatility"],
                    row["cash_weight"],
                    row["actual_value"],
                    row["target_value"],
                    row["actual_value"] - row["target_value"],
                ]
            )
def resolve_portfolio_path(value: str) -> Path:
    """Resolve portfolio names into JSON file paths."""
    path = Path(value)
    if path.is_dir():
        raise ValueError("Portfolio path must be a file, not a directory")
    if not path.suffix:
        path = PORTFOLIO_DIR / f"{path}.json"
    return path


def save_portfolio_snapshot(path: Path, plan_path: Path, investments: List[Investment]) -> None:
    """Persist a full portfolio snapshot (investments + metadata)."""
    data = {
        "plan": str(plan_path),
        "created_at": datetime.utcnow().isoformat() + "Z",
        "investments": investments_to_dicts(investments),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_portfolio_snapshot(path: Path) -> Dict[str, object]:
    """Load previously stored portfolio snapshot JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def append_manual_investment(
    path: Path,
    *,
    instrument_id: str,
    description: str,
    market_value: float,
    category_label: str,
    source: str = "manual",
) -> None:
    snapshot = load_portfolio_snapshot(path)
    investments = snapshot.get("investments", [])
    investments.append(
        {
            "instrument_id": instrument_id,
            "description": description,
            "market_value": market_value,
            "category": category_label,
            "source": source,
        }
    )
    snapshot["investments"] = investments
    snapshot["updated_at"] = datetime.utcnow().isoformat() + "Z"
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def gather_missing_mappings(
    missing: Iterable[str],
    *,
    plan_index: PlanIndex,
    input_func: Optional[Callable[[str], str]] = None,
) -> Dict[str, InstrumentMapping]:
    if input_func is None:
        input_func = input
    new_mappings: Dict[str, InstrumentMapping] = {}
    if not missing:
        return new_mappings
    missing_list = sorted(set(missing))
    print("Assign categories (supports multiple allocations) for the following instruments.")
    print(
        "Enter comma-separated category paths with optional weights (e.g., 'Equities / Developed / NAM=70, Equities / Developed / Europe=30')."
    )
    print("Type 'list' to view options or 'quit' to abort.")
    labels = plan_index.available_labels()
    for instrument in missing_list:
        allocations: Optional[List[CategoryAllocation]] = None
        while allocations is None:
            user_input = input_func(f"{instrument} allocations: ").strip()
            lowered = user_input.lower()
            if lowered in {"quit", "exit"}:
                print("Aborting categorization at user request.")
                raise SystemExit(1)
            if lowered == "list":
                for label in labels:
                    print(f" - {label}")
                continue
            try:
                allocations = parse_allocation_input(user_input, plan_index)
            except ValueError as exc:
                print(f"{exc}")
                allocations = None

        volatility: Optional[float] = None
        while True:
            raw_vol = input_func(
                f"{instrument} custom volatility (blank to defer to statement/default): "
            ).strip()
            if not raw_vol:
                break
            try:
                volatility_value = float(raw_vol)
                if volatility_value <= 0:
                    raise ValueError
                volatility = volatility_value
                break
            except ValueError:
                print("Please enter a positive number for volatility or leave empty.")

        mapping = InstrumentMapping(allocations=allocations, volatility=volatility)
        new_mappings[instrument] = mapping
    return new_mappings


def build_adapter(name: str):
    adapter_cls = ADAPTERS.get(name.lower())
    if not adapter_cls:
        raise ValueError(f"Unknown adapter '{name}'. Available: {', '.join(ADAPTERS)}")
    return adapter_cls(default_category=DEFAULT_CATEGORY)


def parse_statement(statement_path: Path, adapter_name: str):
    adapter = build_adapter(adapter_name)
    return adapter.parse_path(statement_path)


def apply_mappings_to_investments(
    investments: List[Investment],
    mappings: Dict[str, InstrumentMapping],
) -> List[Investment]:
    """Split each investment across mapped categories and return the flattened list."""
    expanded: List[Investment] = []
    for investment in investments:
        mapping = mappings.get(investment.instrument_id)
        if not mapping:
            expanded.append(investment)
            continue
        normalized_allocations = mapping.normalized_allocations()
        for allocation in normalized_allocations:
            value = investment.market_value * allocation.weight
            quantity = (
                investment.quantity * allocation.weight if investment.quantity is not None else None
            )
            expanded.append(
                Investment(
                    instrument_id=investment.instrument_id,
                    description=investment.description,
                    market_value=value,
                    quantity=quantity,
                    category=allocation.path,
                    volatility=mapping.volatility or investment.volatility,
                    source=investment.source,
                )
            )
    return expanded


def gather_investments_from_sources(
    specs: List[SourceSpec],
    *,
    strict: bool = False,
) -> List[Investment]:
    """Parse multiple statements and combine the resulting mapped investments."""
    combined: List[Investment] = []
    for spec in specs:
        mappings = load_mappings(spec.mappings)
        base = parse_statement(spec.statement, spec.adapter)
        missing = sorted({inv.instrument_id for inv in base if inv.instrument_id not in mappings})
        if missing:
            message = (
                f"Missing mappings for {', '.join(missing)} in {spec.statement}. "
                "Use 'riskbalancer categorize' first."
            )
            if strict:
                raise ValueError(message)
            print("Warning:", message)
        combined.extend(apply_mappings_to_investments(base, mappings))
    return combined


def cmd_categorize(args: argparse.Namespace) -> int:
    plan = load_portfolio_plan_from_yaml(
        args.plan, default_leaf_volatility=DEFAULT_LEAF_VOLATILITY
    )
    plan_index = PlanIndex.from_plan(plan)
    mapping_path = Path(args.mappings)
    mappings = load_mappings(mapping_path)
    investments = parse_statement(Path(args.statement), args.adapter)
    missing = [inv.instrument_id for inv in investments if inv.instrument_id not in mappings]
    if not missing:
        print("All instruments already have mappings. Nothing to do.")
        return 0
    new_entries = gather_missing_mappings(missing, plan_index=plan_index)
    mappings.update(new_entries)
    save_mappings(mapping_path, mappings)
    print(f"Stored {len(new_entries)} new mappings in {mapping_path}")
    return 0


def cmd_portfolio_build(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    source_specs = [parse_source_spec(spec) for spec in args.source]
    investments = gather_investments_from_sources(source_specs, strict=True)
    portfolio_path = resolve_portfolio_path(args.portfolio)
    if portfolio_path.exists() and not args.overwrite:
        raise FileExistsError(f"{portfolio_path} already exists. Use --overwrite to replace it.")
    save_portfolio_snapshot(portfolio_path, plan_path, investments)
    print(f"Saved portfolio with {len(investments)} investments to {portfolio_path}")
    return 0


def cmd_portfolio_report(args: argparse.Namespace) -> int:
    portfolio_path = resolve_portfolio_path(args.portfolio)
    if not portfolio_path.exists():
        raise FileNotFoundError(f"Portfolio file {portfolio_path} not found")
    snapshot = load_portfolio_snapshot(portfolio_path)
    plan_path = Path(args.plan) if args.plan else Path(snapshot["plan"])
    plan = load_portfolio_plan_from_yaml(
        plan_path, default_leaf_volatility=DEFAULT_LEAF_VOLATILITY
    )
    investments = investments_from_dicts(snapshot["investments"])
    total_value, summary = summarize_portfolio(plan, investments)
    print(f"Loaded {len(investments)} investments from {portfolio_path}")
    print_summary_table(total_value, summary)
    if args.export:
        export_summary_to_csv(Path(args.export), summary)
        print(f"Wrote summary to {args.export}")
    return 0


def cmd_portfolio_list(args: argparse.Namespace) -> int:
    directory = PORTFOLIO_DIR
    if not directory.exists():
        print("No stored portfolios.")
        return 0
    files = sorted(directory.glob("*.json"))
    if not files:
        print("No stored portfolios.")
        return 0
    for file in files:
        snapshot = load_portfolio_snapshot(file)
        created = snapshot.get("created_at", "?")
        plan = snapshot.get("plan", "?")
        print(f"{file.name:<25} plan={plan} created={created}")
    return 0


def cmd_portfolio_delete(args: argparse.Namespace) -> int:
    portfolio_path = resolve_portfolio_path(args.portfolio)
    if not portfolio_path.exists():
        raise FileNotFoundError(f"Portfolio file {portfolio_path} not found")
    portfolio_path.unlink()
    print(f"Deleted {portfolio_path}")
    return 0


def cmd_portfolio_add_instrument(args: argparse.Namespace) -> int:
    portfolio_path = resolve_portfolio_path(args.portfolio)
    if not portfolio_path.exists():
        raise FileNotFoundError(f"Portfolio file {portfolio_path} not found")
    category = _parse_category_label(args.category)
    append_manual_investment(
        portfolio_path,
        instrument_id=args.instrument_id,
        description=args.description,
        market_value=args.market_value,
        category_label=category.label(),
        source="manual",
    )
    print(f"Added {args.instrument_id} to {portfolio_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RiskBalancer CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    categorize = subparsers.add_parser("categorize", help="Assign categories to unmapped instruments")
    categorize.add_argument("--adapter", default="ajbell", choices=ADAPTERS.keys())
    categorize.add_argument("--statement", required=True, help="Path to broker CSV statement")
    categorize.add_argument("--plan", required=True, help="Path to categories YAML")
    categorize.add_argument(
        "--mappings", required=True, help="Path to YAML mapping file to read/update"
    )
    categorize.set_defaults(func=cmd_categorize)

    portfolio_parser = subparsers.add_parser("portfolio", help="Manage stored portfolios")
    portfolio_sub = portfolio_parser.add_subparsers(dest="portfolio_command", required=True)

    build = portfolio_sub.add_parser("build", help="Construct and persist a portfolio snapshot")
    build.add_argument("--plan", required=True, help="Path to categories YAML")
    build.add_argument(
        "--portfolio",
        required=True,
        help="Portfolio name or file path (defaults to portfolios/<name>.json if no extension)",
    )
    build.add_argument(
        "--source",
        action="append",
        required=True,
        help="Adapter statement spec: adapter=...,statement=...,mappings=...",
    )
    build.add_argument("--overwrite", action="store_true", help="Overwrite existing portfolio file")
    build.set_defaults(func=cmd_portfolio_build)

    report = portfolio_sub.add_parser("report", help="Analyze a stored portfolio snapshot")
    report.add_argument(
        "--portfolio",
        required=True,
        help="Portfolio name or file path",
    )
    report.add_argument(
        "--plan",
        help="Optional plan path override (defaults to plan stored with the portfolio)",
    )
    report.add_argument(
        "--export", help="Optional CSV path to export the summary for Excel/analysis"
    )
    report.set_defaults(func=cmd_portfolio_report)

    plist = portfolio_sub.add_parser("list", help="List stored portfolio snapshots")
    plist.set_defaults(func=cmd_portfolio_list)

    delete = portfolio_sub.add_parser("delete", help="Delete a stored portfolio snapshot")
    delete.add_argument(
        "--portfolio",
        required=True,
        help="Portfolio name or file path to delete",
    )
    delete.set_defaults(func=cmd_portfolio_delete)

    add_manual = portfolio_sub.add_parser("add", help="Manually append an instrument to a portfolio")
    add_manual.add_argument("--portfolio", required=True, help="Portfolio name or path")
    add_manual.add_argument("--instrument-id", required=True)
    add_manual.add_argument("--description", required=True)
    add_manual.add_argument("--market-value", required=True, type=float)
    add_manual.add_argument("--category", required=True, help="Category path (e.g., Equities / Developed / NAM)")
    add_manual.set_defaults(func=cmd_portfolio_add_instrument)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
