from __future__ import annotations

import argparse
import sys
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import yaml

from .adapters import AJBellCSVAdapter
from .configuration import load_portfolio_plan_from_yaml
from .models import CategoryPath, Investment
from .portfolio import Portfolio, PortfolioAnalyzer

DEFAULT_CATEGORY = CategoryPath("Uncategorized", "Pending Review")
ADAPTERS = {
    "ajbell": AJBellCSVAdapter,
}


@dataclass
class InstrumentMapping:
    allocations: List["CategoryAllocation"]
    volatility: Optional[float] = None

    def normalized_allocations(self) -> List["CategoryAllocation"]:
        total = sum(allocation.weight for allocation in self.allocations)
        if total <= 0:
            raise ValueError("Allocation weights must be positive")
        return [
            CategoryAllocation(path=allocation.path, weight=allocation.weight / total)
            for allocation in self.allocations
        ]


@dataclass
class CategoryAllocation:
    path: CategoryPath
    weight: float


class PlanIndex:
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
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mappings: Dict[str, InstrumentMapping] = {}
    for instrument, payload in data.items():
        allocations_data = payload.get("allocations")
        if not allocations_data and payload.get("category"):
            allocations_data = [
                {"category": payload["category"], "weight": payload.get("weight", 1.0)}
            ]
        if not allocations_data:
            continue
        allocations: List[CategoryAllocation] = []
        for entry in allocations_data:
            category_label = entry.get("category")
            if not category_label:
                continue
            weight = float(entry.get("weight", 1.0))
            allocations.append(CategoryAllocation(path=_parse_category_label(category_label), weight=weight))
        if not allocations:
            continue
        volatility = payload.get("volatility")
        mappings[instrument] = InstrumentMapping(allocations=allocations, volatility=volatility)
    return mappings


def save_mappings(path: Path, mappings: Dict[str, InstrumentMapping]) -> None:
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


def _parse_weight_input(raw: str) -> float:
    cleaned = raw.strip().rstrip("%")
    if not cleaned:
        raise ValueError("Weight value is required")
    value = float(cleaned)
    if value > 1:
        value = value / 100.0
    if value <= 0:
        raise ValueError("Weights must be positive")
    return value


def parse_allocation_input(user_input: str, plan_index: PlanIndex) -> List[CategoryAllocation]:
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
    total = sum(alloc.weight for alloc in allocations)
    if not math.isclose(total, 1.0, abs_tol=0.01):
        raise ValueError("Allocation weights must sum to 100% (allowing small rounding)")
    return allocations


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
    print("Format: 'Category A=70, Category B=30'. Type 'list' to view options or 'quit' to abort.")
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
        mapping.allocations = mapping.normalized_allocations()
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


def cmd_categorize(args: argparse.Namespace) -> int:
    plan = load_portfolio_plan_from_yaml(
        args.plan, default_leaf_volatility=args.default_leaf_volatility
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


def format_percentage(value: float) -> str:
    return f"{value * 100:6.2f}%"


def print_category_statuses(plan, investments: List[Investment], adapter_name: str) -> None:
    portfolio = Portfolio()
    portfolio.extend(investments)
    analyzer = PortfolioAnalyzer(plan, portfolio)
    statuses = analyzer.category_status()
    total_value = portfolio.total_value()
    print(f"Processed {len(investments)} investments from {adapter_name}, total value £{total_value:,.2f}")
    header = f"{'Category':50} {'Actual':>10} {'Target':>10} {'Delta':>10} Status"
    print(header)
    print("-" * len(header))
    for status in statuses:
        label = status.path.label()
        actual = format_percentage(status.actual_weight)
        target = format_percentage(status.target_cash_weight)
        delta = format_percentage(status.delta)
        print(f"{label:50} {actual:>10} {target:>10} {delta:>10} {status.status}")


def cmd_analyze(args: argparse.Namespace) -> int:
    plan = load_portfolio_plan_from_yaml(
        args.plan, default_leaf_volatility=args.default_leaf_volatility
    )
    mapping_path = Path(args.mappings)
    mappings = load_mappings(mapping_path)
    investments = parse_statement(Path(args.statement), args.adapter)
    missing = sorted({inv.instrument_id for inv in investments if inv.instrument_id not in mappings})
    if missing:
        print(
            "Warning: these instruments are not mapped and will use the default category: "
            + ", ".join(missing)
        )
    expanded = apply_mappings_to_investments(investments, mappings)
    print_category_statuses(plan, expanded, args.adapter)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RiskBalancer CLI")
    parser.add_argument(
        "--default-leaf-volatility",
        type=float,
        default=0.15,
        help="Fallback volatility for categories lacking explicit values",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    categorize = subparsers.add_parser("categorize", help="Assign categories to unmapped instruments")
    categorize.add_argument("--adapter", default="ajbell", choices=ADAPTERS.keys())
    categorize.add_argument("--statement", required=True, help="Path to broker CSV statement")
    categorize.add_argument("--plan", required=True, help="Path to categories YAML")
    categorize.add_argument(
        "--mappings", required=True, help="Path to YAML mapping file to read/update"
    )
    categorize.set_defaults(func=cmd_categorize)

    analyze = subparsers.add_parser("analyze", help="Ingest statement and report category status")
    analyze.add_argument("--adapter", default="ajbell", choices=ADAPTERS.keys())
    analyze.add_argument("--statement", required=True, help="Path to broker CSV statement")
    analyze.add_argument("--plan", required=True, help="Path to categories YAML")
    analyze.add_argument(
        "--mappings", required=True, help="Path to YAML mapping file with instrument categories"
    )
    analyze.set_defaults(func=cmd_analyze)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
