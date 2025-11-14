from __future__ import annotations

import argparse
import sys
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
    category: CategoryPath
    volatility: Optional[float] = None


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


def load_mappings(path: Path) -> Dict[str, InstrumentMapping]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mappings: Dict[str, InstrumentMapping] = {}
    for instrument, payload in data.items():
        category_label = payload.get("category")
        if not category_label:
            continue
        category = CategoryPath(part.strip() for part in category_label.split("/"))
        volatility = payload.get("volatility")
        mappings[instrument] = InstrumentMapping(category=category, volatility=volatility)
    return mappings


def save_mappings(path: Path, mappings: Dict[str, InstrumentMapping]) -> None:
    serializable = {
        instrument: {
            "category": mapping.category.label(),
            **({"volatility": mapping.volatility} if mapping.volatility else {}),
        }
        for instrument, mapping in mappings.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(serializable, sort_keys=True), encoding="utf-8")


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
    print("Assign categories for the following instruments (use 'list' to view options).")
    labels = plan_index.available_labels()
    for instrument in missing_list:
        category: Optional[CategoryPath] = None
        while category is None:
            user_input = input_func(
                f"{instrument} category path (e.g., Equities/Developed/NAM): "
            ).strip()
            lowered = user_input.lower()
            if lowered in {"quit", "exit"}:
                print("Aborting categorization at user request.")
                raise SystemExit(1)
            if lowered == "list":
                for label in labels:
                    print(f" - {label}")
                continue
            resolved = plan_index.resolve(user_input)
            if resolved is None:
                print("Unknown category path. Type 'list' for valid options.")
                continue
            category = resolved

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

        new_mappings[instrument] = InstrumentMapping(category=category, volatility=volatility)
    return new_mappings


def build_adapter(name: str, mappings: Dict[str, InstrumentMapping]):
    adapter_cls = ADAPTERS.get(name.lower())
    if not adapter_cls:
        raise ValueError(f"Unknown adapter '{name}'. Available: {', '.join(ADAPTERS)}")
    category_map = {instrument: mapping.category for instrument, mapping in mappings.items()}
    volatility_map = {
        instrument: mapping.volatility
        for instrument, mapping in mappings.items()
        if mapping.volatility
    }
    return adapter_cls(
        default_category=DEFAULT_CATEGORY,
        category_map=category_map,
        volatility_map=volatility_map,
    )


def parse_statement(statement_path: Path, adapter_name: str, mappings: Dict[str, InstrumentMapping]):
    adapter = build_adapter(adapter_name, mappings)
    return adapter.parse_path(statement_path)


def cmd_categorize(args: argparse.Namespace) -> int:
    plan = load_portfolio_plan_from_yaml(
        args.plan, default_leaf_volatility=args.default_leaf_volatility
    )
    plan_index = PlanIndex.from_plan(plan)
    mapping_path = Path(args.mappings)
    mappings = load_mappings(mapping_path)
    investments = parse_statement(Path(args.statement), args.adapter, mappings)
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
    investments = parse_statement(Path(args.statement), args.adapter, mappings)
    missing = sorted({inv.instrument_id for inv in investments if inv.instrument_id not in mappings})
    if missing:
        print(
            "Warning: these instruments are not mapped and will use the default category: "
            + ", ".join(missing)
        )
    print_category_statuses(plan, investments, args.adapter)
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
