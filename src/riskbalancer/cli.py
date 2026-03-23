"""
RiskBalancer command-line interface utilities.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, cast

import yaml

from .adapters import (
    AJBellCSVAdapter,
    CitiCSVAdapter,
    IBKRCSVAdapter,
    MS401KCSVAdapter,
    SchwabCSVAdapter,
)
from .configuration import load_portfolio_plan_from_yaml
from .models import CategoryPath, Investment

DEFAULT_CATEGORY = CategoryPath("Uncategorized", "Pending Review")
DEFAULT_LEAF_VOLATILITY = 0.15
PORTFOLIO_DIR = Path("portfolios")
MANUAL_MAPPINGS_PATH = Path("config/mappings/manual.yaml")
ADAPTERS = {
    "ajbell": AJBellCSVAdapter,
    "citi": CitiCSVAdapter,
    "ibkr": IBKRCSVAdapter,
    "ms401k": MS401KCSVAdapter,
    "schwab": SchwabCSVAdapter,
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
class ImportRecord:
    """Metadata describing a broker statement imported into a portfolio."""

    source_id: str
    adapter: str
    statement: str
    mappings: str
    imported_at: str


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


def resolve_mapping_path(adapter: str, raw_path: Optional[str] = None) -> Path:
    """Resolve the mappings path for a broker adapter."""
    mappings_path = Path(raw_path) if raw_path else Path(f"config/mappings/{adapter}.yaml")
    mappings_path.parent.mkdir(parents=True, exist_ok=True)
    return mappings_path


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_fx_rates(path: Optional[str] = None) -> Dict[str, float]:
    """Load FX rates (currency -> GBP) from YAML."""
    fx_path = Path(path or "config/fx.yaml")
    if not fx_path.exists():
        return {}
    data = yaml.safe_load(fx_path.read_text(encoding="utf-8"))
    if not data:
        return {}
    base = data.get("base", "GBP").upper()
    if base != "GBP":
        raise ValueError("FX file must use GBP as the base currency")
    rates = data.get("rates", {})
    return {currency.upper(): float(value) for currency, value in rates.items()}


def investment_to_dict(investment: Investment) -> Dict[str, object]:
    """Convert an Investment to a serialisable dict."""
    payload: Dict[str, object] = {
        "instrument_id": investment.instrument_id,
        "description": investment.description,
        "market_value": investment.market_value,
        "category": investment.category.label(),
        "volatility": investment.volatility,
        "source": investment.source,
    }
    if investment.quantity is not None:
        payload["quantity"] = investment.quantity
    if investment.source_id is not None:
        payload["source_id"] = investment.source_id
    return payload


def investments_to_dicts(investments: Iterable[Investment]) -> List[Dict[str, object]]:
    """Serialise a list of investments for storage."""
    return [investment_to_dict(inv) for inv in investments]


def _coerce_float(value: object, *, field_name: str) -> float:
    """Convert JSON-loaded numeric payloads into floats."""
    if isinstance(value, (int, float, str)):
        return float(value)
    raise ValueError(f"{field_name} must be numeric")


def _coerce_optional_float(value: object, *, field_name: str) -> Optional[float]:
    if value is None:
        return None
    return _coerce_float(value, field_name=field_name)


def import_record_to_dict(record: ImportRecord) -> Dict[str, str]:
    return {
        "source_id": record.source_id,
        "adapter": record.adapter,
        "statement": record.statement,
        "mappings": record.mappings,
        "imported_at": record.imported_at,
    }


def _snapshot_investments(snapshot: Mapping[str, object]) -> List[Dict[str, object]]:
    """Return stored investment payloads after validating their shape."""
    raw_investments = snapshot.get("investments", [])
    if not isinstance(raw_investments, list):
        raise ValueError("Stored investments must be a list")
    investments: List[Dict[str, object]] = []
    for item in raw_investments:
        if not isinstance(item, dict):
            raise ValueError("Each stored investment must be an object")
        investments.append(cast(Dict[str, object], item))
    return investments


def _snapshot_imports(snapshot: Mapping[str, object]) -> List[ImportRecord]:
    raw_imports = snapshot.get("imports", [])
    if raw_imports is None:
        return []
    if not isinstance(raw_imports, list):
        raise ValueError("Stored imports must be a list")
    imports: List[ImportRecord] = []
    for item in raw_imports:
        if not isinstance(item, dict):
            raise ValueError("Each stored import must be an object")
        source_id = item.get("source_id")
        adapter = item.get("adapter")
        statement = item.get("statement")
        mappings = item.get("mappings")
        imported_at = item.get("imported_at")
        if not all(
            isinstance(value, str)
            for value in (source_id, adapter, statement, mappings, imported_at)
        ):
            raise ValueError("Stored import metadata must use string values")
        imports.append(
            ImportRecord(
                source_id=cast(str, source_id),
                adapter=cast(str, adapter),
                statement=cast(str, statement),
                mappings=cast(str, mappings),
                imported_at=cast(str, imported_at),
            )
        )
    return imports


def _snapshot_plan_path(snapshot: Mapping[str, object]) -> Path:
    plan_value = snapshot.get("plan", "config/categories.yaml")
    if not isinstance(plan_value, str):
        raise ValueError("Stored plan path must be a string")
    return Path(plan_value)


def _snapshot_created_at(snapshot: Mapping[str, object]) -> str:
    created_at = snapshot.get("created_at")
    if isinstance(created_at, str):
        return created_at
    updated_at = snapshot.get("updated_at")
    if isinstance(updated_at, str):
        return updated_at
    return _utc_timestamp()


def investment_from_dict(payload: Mapping[str, object]) -> Investment:
    """Hydrate an Investment from stored JSON data."""
    category_label = payload["category"]
    if not isinstance(category_label, str):
        raise ValueError("Category label must be a string")
    source_id_value = payload.get("source_id")
    if source_id_value is not None and not isinstance(source_id_value, str):
        raise ValueError("source_id must be a string when present")
    return Investment(
        instrument_id=str(payload["instrument_id"]),
        description=str(payload.get("description", "")),
        market_value=_coerce_float(payload.get("market_value", 0.0), field_name="market_value"),
        category=_parse_category_label(category_label),
        volatility=_coerce_float(payload.get("volatility", 0.0), field_name="volatility") or 0.0001,
        quantity=_coerce_optional_float(payload.get("quantity"), field_name="quantity"),
        source=str(payload.get("source", "portfolio")),
        source_id=cast(Optional[str], source_id_value),
    )


def investments_from_dicts(items: Iterable[Mapping[str, object]]) -> List[Investment]:
    """Hydrate multiple investments from stored JSON data."""
    return [investment_from_dict(item) for item in items]


def summarize_portfolio(plan, investments: List[Investment]):
    """Aggregate the portfolio and compute risk/cash weights + target values."""
    totals: defaultdict[CategoryPath, float] = defaultdict(float)
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


def save_portfolio_snapshot(
    path: Path,
    plan_path: Path,
    investments: List[Investment],
    *,
    imports: Optional[List[ImportRecord]] = None,
    created_at: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> None:
    """Persist a full portfolio snapshot (investments + metadata)."""
    created = created_at or _utc_timestamp()
    updated = updated_at or created
    data = {
        "plan": str(plan_path),
        "created_at": created,
        "updated_at": updated,
        "imports": [import_record_to_dict(record) for record in (imports or [])],
        "investments": investments_to_dicts(investments),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_portfolio_snapshot(path: Path) -> Dict[str, object]:
    """Load previously stored portfolio snapshot JSON."""
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
        "Enter comma-separated category paths with optional weights "
        "(e.g., 'Equities / Developed / NAM=70, Equities / Developed / Europe=30')."
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


def build_adapter(name: str, fx_rates: Optional[Dict[str, float]] = None):
    adapter_cls = ADAPTERS.get(name.lower())
    if not adapter_cls:
        raise ValueError(f"Unknown adapter '{name}'. Available: {', '.join(ADAPTERS)}")
    if adapter_cls in {IBKRCSVAdapter, MS401KCSVAdapter, SchwabCSVAdapter, CitiCSVAdapter}:
        return adapter_cls(default_category=DEFAULT_CATEGORY, fx_rates=fx_rates)
    return adapter_cls(default_category=DEFAULT_CATEGORY)


def parse_statement(
    statement_path: Path, adapter_name: str, fx_rates: Optional[Dict[str, float]] = None
):
    adapter = build_adapter(adapter_name, fx_rates=fx_rates)
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


def ensure_mappings_for_investments(
    investments: Iterable[Investment],
    mapping_path: Path,
    *,
    plan_index: PlanIndex,
    input_func: Optional[Callable[[str], str]] = None,
) -> tuple[Dict[str, InstrumentMapping], int]:
    """Load mappings, prompt for missing instruments, and persist new entries."""
    mappings = load_mappings(mapping_path)
    missing = sorted(
        {inv.instrument_id for inv in investments if inv.instrument_id not in mappings}
    )
    if not missing:
        return mappings, 0
    new_entries = gather_missing_mappings(
        missing,
        plan_index=plan_index,
        input_func=input_func,
    )
    mappings.update(new_entries)
    save_mappings(mapping_path, mappings)
    return mappings, len(new_entries)


def tag_imported_investments(investments: Iterable[Investment], source_id: str) -> List[Investment]:
    return [replace(investment, source_id=source_id) for investment in investments]


def cmd_categorize(args: argparse.Namespace) -> int:
    plan = load_portfolio_plan_from_yaml(args.plan, default_leaf_volatility=DEFAULT_LEAF_VOLATILITY)
    plan_index = PlanIndex.from_plan(plan)
    mapping_path = resolve_mapping_path(args.adapter, args.mappings)
    mappings = load_mappings(mapping_path)
    fx_rates = load_fx_rates()
    investments = parse_statement(Path(args.statement), args.adapter, fx_rates=fx_rates)
    missing = [inv.instrument_id for inv in investments if inv.instrument_id not in mappings]
    if not missing:
        print("All instruments already have mappings. Nothing to do.")
        return 0
    _, new_count = ensure_mappings_for_investments(
        investments,
        mapping_path,
        plan_index=plan_index,
    )
    print(f"Stored {new_count} new mappings in {mapping_path}")
    return 0


def cmd_portfolio_create(args: argparse.Namespace) -> int:
    portfolio_path = resolve_portfolio_path(args.portfolio)
    plan_path = Path(args.plan)
    if portfolio_path.exists() and not args.overwrite:
        raise FileExistsError(f"{portfolio_path} already exists. Use --overwrite to replace it.")
    save_portfolio_snapshot(portfolio_path, plan_path, [], imports=[])
    print(f"Created empty portfolio at {portfolio_path}")
    return 0


def cmd_portfolio_import(args: argparse.Namespace) -> int:
    portfolio_path = resolve_portfolio_path(args.portfolio)
    if not portfolio_path.exists():
        raise FileNotFoundError(f"Portfolio file {portfolio_path} not found")
    snapshot = load_portfolio_snapshot(portfolio_path)
    plan_path = _snapshot_plan_path(snapshot)
    plan = load_portfolio_plan_from_yaml(plan_path, default_leaf_volatility=DEFAULT_LEAF_VOLATILITY)
    plan_index = PlanIndex.from_plan(plan)

    mapping_path = resolve_mapping_path(args.adapter, args.mappings)
    fx_rates = load_fx_rates(args.fx)
    parsed_investments = parse_statement(Path(args.statement), args.adapter, fx_rates=fx_rates)
    mappings, new_count = ensure_mappings_for_investments(
        parsed_investments,
        mapping_path,
        plan_index=plan_index,
    )
    imported_investments = tag_imported_investments(
        apply_mappings_to_investments(parsed_investments, mappings),
        args.source_id,
    )

    existing_investments = investments_from_dicts(_snapshot_investments(snapshot))
    preserved_investments = [
        investment for investment in existing_investments if investment.source_id != args.source_id
    ]
    replaced_count = len(existing_investments) - len(preserved_investments)

    imports = [
        record for record in _snapshot_imports(snapshot) if record.source_id != args.source_id
    ]
    imports.append(
        ImportRecord(
            source_id=args.source_id,
            adapter=args.adapter,
            statement=str(Path(args.statement)),
            mappings=str(mapping_path),
            imported_at=_utc_timestamp(),
        )
    )
    save_portfolio_snapshot(
        portfolio_path,
        plan_path,
        preserved_investments + imported_investments,
        imports=imports,
        created_at=_snapshot_created_at(snapshot),
    )
    if replaced_count:
        print(f"Replaced {replaced_count} existing position(s) for source '{args.source_id}'.")
    if new_count:
        print(f"Stored {new_count} new mapping(s) in {mapping_path}.")
    print(
        f"Imported {len(imported_investments)} position(s) from {args.statement} "
        f"into {portfolio_path} as '{args.source_id}'."
    )
    return 0


def cmd_portfolio_report(args: argparse.Namespace) -> int:
    portfolio_path = resolve_portfolio_path(args.portfolio)
    if not portfolio_path.exists():
        raise FileNotFoundError(f"Portfolio file {portfolio_path} not found")
    snapshot = load_portfolio_snapshot(portfolio_path)
    plan_path = Path(args.plan) if args.plan else _snapshot_plan_path(snapshot)
    plan = load_portfolio_plan_from_yaml(plan_path, default_leaf_volatility=DEFAULT_LEAF_VOLATILITY)
    investments = investments_from_dicts(_snapshot_investments(snapshot))
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
    snapshot = load_portfolio_snapshot(portfolio_path)
    plan_path = _snapshot_plan_path(snapshot)
    plan = load_portfolio_plan_from_yaml(plan_path, default_leaf_volatility=DEFAULT_LEAF_VOLATILITY)
    plan_index = PlanIndex.from_plan(plan)

    manual_path = MANUAL_MAPPINGS_PATH
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    manual_mappings = load_mappings(manual_path)

    mapping: InstrumentMapping | None = None
    if args.category:
        allocations = parse_allocation_input(args.category, plan_index)
        mapping = InstrumentMapping(allocations=allocations)
    else:
        mapping = manual_mappings.get(args.instrument_id)
        if not mapping:
            new_entries = gather_missing_mappings([args.instrument_id], plan_index=plan_index)
            mapping = new_entries[args.instrument_id]
            manual_mappings.update(new_entries)

    if mapping is None:
        raise ValueError("Unable to determine category allocation for manual instrument.")

    manual_mappings[args.instrument_id] = mapping
    save_mappings(manual_path, manual_mappings)

    existing_investments = investments_from_dicts(_snapshot_investments(snapshot))
    imports = _snapshot_imports(snapshot)
    allocations = mapping.normalized_allocations()
    for allocation in allocations:
        existing_investments.append(
            Investment(
                instrument_id=args.instrument_id,
                description=args.description,
                market_value=args.market_value * allocation.weight,
                category=allocation.path,
                volatility=mapping.volatility or DEFAULT_LEAF_VOLATILITY,
                source="manual",
            )
        )
    save_portfolio_snapshot(
        portfolio_path,
        plan_path,
        existing_investments,
        imports=imports,
        created_at=_snapshot_created_at(snapshot),
    )

    print(f"Added {args.instrument_id} to {portfolio_path} using manual mappings")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RiskBalancer CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    categorize = subparsers.add_parser(
        "categorize", help="Assign categories to unmapped instruments"
    )
    categorize.add_argument("--adapter", default="ajbell", choices=ADAPTERS.keys())
    categorize.add_argument("--statement", required=True, help="Path to broker CSV statement")
    categorize.add_argument(
        "--plan",
        default="config/categories.yaml",
        help="Path to categories YAML (defaults to config/categories.yaml)",
    )
    categorize.add_argument(
        "--mappings",
        help="Path to YAML mapping file (defaults to config/mappings/<adapter>.yaml)",
    )
    categorize.set_defaults(func=cmd_categorize)

    portfolio_parser = subparsers.add_parser("portfolio", help="Manage stored portfolios")
    portfolio_sub = portfolio_parser.add_subparsers(dest="portfolio_command", required=True)

    create = portfolio_sub.add_parser("create", help="Create an empty portfolio snapshot")
    create.add_argument(
        "--plan",
        default="config/categories.yaml",
        help="Path to categories YAML (defaults to config/categories.yaml)",
    )
    create.add_argument(
        "--portfolio",
        required=True,
        help="Portfolio name or file path (defaults to portfolios/<name>.json if no extension)",
    )
    create.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing portfolio snapshot",
    )
    create.set_defaults(func=cmd_portfolio_create)

    portfolio_import = portfolio_sub.add_parser(
        "import",
        help="Import a single broker statement into an existing portfolio",
    )
    portfolio_import.add_argument(
        "--portfolio",
        required=True,
        help="Portfolio name or file path",
    )
    portfolio_import.add_argument("--source-id", required=True, help="Stable source identifier")
    portfolio_import.add_argument("--adapter", required=True, choices=ADAPTERS.keys())
    portfolio_import.add_argument("--statement", required=True, help="Path to broker CSV statement")
    portfolio_import.add_argument(
        "--mappings",
        help="Path to YAML mapping file (defaults to config/mappings/<adapter>.yaml)",
    )
    portfolio_import.add_argument(
        "--fx",
        help="Optional FX rate YAML (base GBP) used to convert non-GBP statements",
    )
    portfolio_import.set_defaults(func=cmd_portfolio_import)

    report = portfolio_sub.add_parser("report", help="Analyze a stored portfolio snapshot")
    report.add_argument(
        "--portfolio",
        required=True,
        help="Portfolio name or file path",
    )
    report.add_argument(
        "--plan",
        help=(
            "Optional plan path override "
            "(defaults to the plan stored with the portfolio or config/categories.yaml)"
        ),
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

    add_manual = portfolio_sub.add_parser(
        "add", help="Manually append an instrument to a portfolio"
    )
    add_manual.add_argument("--portfolio", required=True, help="Portfolio name or path")
    add_manual.add_argument("--instrument-id", required=True)
    add_manual.add_argument("--description", required=True)
    add_manual.add_argument("--market-value", required=True, type=float)
    add_manual.add_argument(
        "--category", help="Optional category path(s); prompt/manual mappings used if omitted"
    )
    add_manual.set_defaults(func=cmd_portfolio_add_instrument)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
