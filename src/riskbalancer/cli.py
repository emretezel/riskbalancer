from __future__ import annotations

import argparse
import sys
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Mapping

import yaml

from .adapters import AJBellCSVAdapter
from .configuration import load_portfolio_plan_from_yaml
from .models import CategoryPath, Investment
from .portfolio import Portfolio, PortfolioAnalyzer

DEFAULT_CATEGORY = CategoryPath("Uncategorized", "Pending Review")
PORTFOLIO_DIR = Path("portfolios")
ADAPTERS = {
    "ajbell": AJBellCSVAdapter,
}


@dataclass
class InstrumentMapping:
    allocations: List["CategoryAllocation"]
    volatility: Optional[float] = None

    def normalized_allocations(self) -> List["CategoryAllocation"]:
        if not self.allocations:
            raise ValueError("Instrument mapping must contain at least one category")
        share = 1.0 / len(self.allocations)
        return [
            CategoryAllocation(path=allocation.path, weight=share)
            for allocation in self.allocations
        ]


@dataclass
class CategoryAllocation:
    path: CategoryPath
    weight: float = 1.0


@dataclass
class SourceSpec:
    adapter: str
    statement: Path
    mappings: Path


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
            allocations_data = [payload["category"]]
        if not allocations_data:
            continue
        allocations: List[CategoryAllocation] = []
        for entry in allocations_data:
            if isinstance(entry, str):
                category_label = entry
            else:
                category_label = entry.get("category")
            if not category_label:
                continue
            allocations.append(CategoryAllocation(path=_parse_category_label(category_label)))
        if not allocations:
            continue
        volatility = payload.get("volatility")
        mappings[instrument] = InstrumentMapping(allocations=allocations, volatility=volatility)
    return mappings


def save_mappings(path: Path, mappings: Dict[str, InstrumentMapping]) -> None:
    serializable = {
        instrument: {
            "allocations": [allocation.path.label() for allocation in mapping.allocations],
            **({"volatility": mapping.volatility} if mapping.volatility else {}),
        }
        for instrument, mapping in mappings.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(serializable, sort_keys=True), encoding="utf-8")


def parse_allocation_input(user_input: str, plan_index: PlanIndex) -> List[CategoryAllocation]:
    entries = [entry.strip() for entry in user_input.split(",") if entry.strip()]
    if not entries:
        raise ValueError("At least one allocation must be provided")
    allocations: List[CategoryAllocation] = []
    for entry in entries:
        category_label = entry
        resolved = plan_index.resolve(category_label)
        if not resolved:
            raise ValueError(f"Unknown category path '{category_label.strip()}'")
        allocations.append(CategoryAllocation(path=resolved))
    return allocations


def parse_source_spec(spec: str) -> SourceSpec:
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
    return {
        "instrument_id": investment.instrument_id,
        "description": investment.description,
        "market_value": investment.market_value,
        "category": investment.category.label(),
        "source": investment.source,
    }


def investments_to_dicts(investments: Iterable[Investment]) -> List[Dict[str, object]]:
    return [investment_to_dict(inv) for inv in investments]


def investment_from_dict(payload: Mapping[str, object]) -> Investment:
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
    return [investment_from_dict(item) for item in items]


def resolve_portfolio_path(value: str) -> Path:
    path = Path(value)
    if path.is_dir():
        raise ValueError("Portfolio path must be a file, not a directory")
    if not path.suffix:
        path = PORTFOLIO_DIR / f"{path}.json"
    return path


def save_portfolio_snapshot(path: Path, plan_path: Path, investments: List[Investment]) -> None:
    data = {
        "plan": str(plan_path),
        "created_at": datetime.utcnow().isoformat() + "Z",
        "investments": investments_to_dicts(investments),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_portfolio_snapshot(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


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
        "Enter comma-separated category paths (e.g., 'Equities / Developed / NAM, Equities / Developed / Europe')."
    )
    print("Type 'list' to view options or 'quit' to abort. Holdings will be split evenly across entries.")
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
        plan_path, default_leaf_volatility=args.default_leaf_volatility
    )
    investments = investments_from_dicts(snapshot["investments"])
    print_category_statuses(plan, investments, f"portfolio:{portfolio_path.stem}")
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
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
