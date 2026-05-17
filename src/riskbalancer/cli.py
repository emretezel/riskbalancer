"""
RiskBalancer command-line interface utilities.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, cast

import yaml

from . import repositories
from .adapters import (
    AegonCSVAdapter,
    AJBellCSVAdapter,
    CitiCSVAdapter,
    IBKRCSVAdapter,
    MS401KCSVAdapter,
    SchwabCSVAdapter,
)
from .configuration import (
    CategoryNode,
    build_portfolio_plan_from_nodes,
    collect_category_weight_validation_failures,
    format_category_weight_validation_failures,
    load_category_nodes_from_yaml,
    load_portfolio_plan_from_yaml,
)
from .db import Database
from .models import CategoryPath, Investment
from .paths import UserPaths, resolve_default_user
from .plan_adjust import (
    apply_targeted,
    confirm_changes,
    filter_under,
    iter_leaf_nodes,
    render_list,
    walk_adjustments,
)
from .plan_bootstrap import (
    IO,
    PlanCreationAborted,
    StdIO,
    _prompt_yes_no,
    build_catalog_from_db,
    count_unique_categories,
    describe_catalog_sources_from_db,
    walk_catalog_interactive,
)
from .plan_csv import PlanCSVError, read_plan_csv, write_plan_csv
from .portfolio import PortfolioPlan
from .seed import seed_from_yaml

DEFAULT_CATEGORY = CategoryPath("Uncategorized", "Pending Review")
DEFAULT_LEAF_VOLATILITY = 0.15
ECB_DAILY_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FX_HTTP_USER_AGENT = "riskbalancer/1.0"


def _paths_from_args(args: argparse.Namespace) -> UserPaths:
    """Resolve the `UserPaths` for a parsed CLI invocation.

    Reads `args.user` first (set by the `--user` flag) and falls back to the
    default-user lookup (`RISKBALANCER_USER` env var, then
    `config/riskbalancer.yaml`). Missing values resolve to the empty string,
    which produces sensible-but-unusable per-user paths so commands that do
    not need a user (such as `fx update`) still work.
    """
    user = getattr(args, "user", None) or resolve_default_user() or ""
    return UserPaths.for_user(user)


def _default_paths() -> UserPaths:
    """Fallback `UserPaths` for tests that do not pass an explicit `paths`."""
    user = resolve_default_user() or ""
    return UserPaths.for_user(user)


def _ingestion_now() -> datetime:
    """The "current" time used for statement filing.

    Wrapped in a function so tests can monkeypatch it to a frozen value
    without freezing every other clock in the module.
    """
    return datetime.now(UTC)


def _autofile_statement(
    source: Path,
    paths: UserPaths,
    *,
    adapter: str,
    account: str,
    move: bool = False,
) -> Path:
    """File a statement under `<statements_dir>/<adapter>/<account>/<YYYY>/<MM>/`.

    Returns the canonical path the import workflow should use. When the
    source already lives under `paths.statements_dir`, it is left alone and
    returned as-is (the user pre-filed it manually). Otherwise the source
    is copied (or moved when `move=True`) into a year/month folder named
    after the current ingestion date, with a numeric suffix appended on
    filename conflict.
    """
    source = source.resolve()
    statements_root = paths.statements_dir.resolve()
    try:
        source.relative_to(statements_root)
        return source
    except ValueError:
        pass  # source is outside the user's statements tree → file it.

    now = _ingestion_now()
    dest_dir = paths.statements_dir / adapter / account / f"{now.year:04d}" / f"{now.month:02d}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    destination = dest_dir / source.name
    counter = 2
    while destination.exists():
        destination = dest_dir / f"{source.stem}-{counter}{source.suffix}"
        counter += 1
        if counter > 1000:
            raise RuntimeError(f"Refusing to find a free name in {dest_dir} after 1000 attempts")

    if move:
        shutil.move(str(source), str(destination))
    else:
        shutil.copy2(str(source), str(destination))
    return destination


def _require_user(paths: UserPaths) -> bool:
    """Return True when `paths.user` is set, otherwise print a clear error.

    Every user-keyed command must call this before touching per-user paths.
    With an empty user, `UserPaths` produces nonsensical paths that resolve
    to `private/users` itself — running into one of those produces either a
    confusing FileNotFoundError or, worse, silent writes to the users root.
    """
    if paths.user:
        return True
    print(
        "No user resolved. Pass --user <name>, set RISKBALANCER_USER, or copy "
        "config/riskbalancer.example.yaml to config/riskbalancer.yaml and set "
        "default_user.",
        file=sys.stderr,
    )
    return False


def _open_database(paths: UserPaths) -> Database:
    """Open (and migrate) the project database. Auto-seeds on first open.

    The database lives at `paths.db_path` (`private/riskbalancer.db` by
    default). On a brand-new repo the file is created automatically by
    `Database.connect`; if the resulting database is empty (no shared
    mappings), we run the seed loader so the user gets a usable catalog
    without having to remember `db seed` manually before the first
    `plan create`.
    """
    db = Database.connect(paths.db_path)
    row = db.connection.execute("SELECT COUNT(*) AS n FROM mapping").fetchone()
    if int(row["n"]) == 0 and paths.seed_plan.exists():
        seed_from_yaml(
            db.connection,
            seed_plan_path=paths.seed_plan,
            mappings_dir=paths.shared_mappings_dir,
        )
    return db


def _ensure_user_in_db(
    connection: sqlite3.Connection,
    name: str,
) -> int:
    """Return the user's id, creating the row on first use.

    Keeps `user create` and (e.g.) `plan create --user new-user` working
    without forcing the caller to remember a separate `user create` step
    once the on-disk directory exists. The CLI's user-CRUD commands still
    explicitly create / delete rows via `repositories.create_user` and
    `repositories.delete_user` so the lifecycle is observable.
    """
    return repositories.find_or_create_user(connection, name)


ADAPTERS = {
    "aegon": AegonCSVAdapter,
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
    """Metadata describing a broker statement imported into a portfolio.

    `(adapter, account)` is the stable key — re-imports of the same broker
    account replace the prior record.
    """

    adapter: str
    account: str
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


def resolve_mapping_path(
    adapter: str,
    raw_path: Optional[str] = None,
    *,
    paths: Optional[UserPaths] = None,
) -> Path:
    """Resolve the mappings path for a broker adapter.

    Falls back to `<paths.shared_mappings_dir>/<adapter>.yaml` when no
    explicit path is supplied. The default `paths` is the system layout.
    """
    if raw_path:
        mappings_path = Path(raw_path)
    else:
        resolved_paths = paths if paths is not None else _default_paths()
        mappings_path = resolved_paths.adapter_mappings_path(adapter)
    mappings_path.parent.mkdir(parents=True, exist_ok=True)
    return mappings_path


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def load_fx_rates(
    path: Optional[str] = None,
    *,
    paths: Optional[UserPaths] = None,
) -> Dict[str, float]:
    """Load FX rates (currency -> GBP) from YAML."""
    if path:
        fx_path = Path(path)
    else:
        resolved_paths = paths if paths is not None else _default_paths()
        fx_path = resolved_paths.fx
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


def _normalize_currency_codes(currencies: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for raw_currency in currencies:
        currency = raw_currency.strip().upper()
        if not currency:
            continue
        if currency == "GBP":
            raise ValueError("GBP is the base currency and should not be stored in fx.yaml rates")
        if currency not in seen:
            normalized.append(currency)
            seen.add(currency)
    return sorted(normalized)


def tracked_fx_currencies(path: Path) -> List[str]:
    """Return the currencies currently tracked in an FX YAML file."""
    if not path.exists():
        raise FileNotFoundError(
            f"FX file {path} not found. Use --currency to bootstrap a new FX file."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("FX file must contain a mapping")
    base = data.get("base", "GBP")
    if not isinstance(base, str) or base.upper() != "GBP":
        raise ValueError("FX file must use GBP as the base currency")
    rates = data.get("rates")
    if not isinstance(rates, dict) or not rates:
        raise ValueError(
            f"FX file {path} does not contain any tracked rates. Use --currency to bootstrap it."
        )
    raw_currencies: List[str] = []
    for currency in rates:
        if not isinstance(currency, str):
            raise ValueError("FX file rate keys must be currency codes")
        raw_currencies.append(currency)
    return _normalize_currency_codes(raw_currencies)


def resolve_tracked_fx_currencies(
    path: Path,
    *,
    fallback_template: Optional[Path] = None,
) -> List[str]:
    """Resolve tracked currencies from the target file or a checked-in template.

    The caller decides when to allow a template fallback by passing
    `fallback_template`; the function itself does not assume a layout.
    """
    if path.exists():
        return tracked_fx_currencies(path)
    if fallback_template is not None and fallback_template.exists():
        return tracked_fx_currencies(fallback_template)
    raise FileNotFoundError(f"FX file {path} not found. Use --currency to bootstrap a new FX file.")


def parse_ecb_reference_rates_xml(xml_payload: str) -> tuple[str, Dict[str, float]]:
    """Parse the ECB daily XML feed into a provider date and EUR-based rates."""
    try:
        root = ET.fromstring(xml_payload)
    except ET.ParseError as exc:
        raise ValueError("Malformed ECB FX payload") from exc

    dated_cube = next((element for element in root.iter() if "time" in element.attrib), None)
    if dated_cube is None:
        raise ValueError("ECB FX payload does not contain a dated rates section")

    provider_date = dated_cube.attrib.get("time", "").strip()
    if not provider_date:
        raise ValueError("ECB FX payload does not contain a provider date")

    rates: Dict[str, float] = {}
    for child in dated_cube:
        currency = child.attrib.get("currency", "").strip().upper()
        rate_text = child.attrib.get("rate", "").strip()
        if not currency or not rate_text:
            continue
        rates[currency] = float(rate_text)

    if not rates:
        raise ValueError("ECB FX payload does not contain any rates")
    return provider_date, rates


def fetch_ecb_reference_rates() -> tuple[str, Dict[str, float]]:
    """Download and parse the latest ECB reference FX rates."""
    request = urllib.request.Request(
        ECB_DAILY_RATES_URL,
        headers={"User-Agent": FX_HTTP_USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        xml_payload = response.read().decode("utf-8")
    return parse_ecb_reference_rates_xml(xml_payload)


def derive_gbp_fx_rates(
    euro_reference_rates: Mapping[str, float],
    currencies: Iterable[str],
) -> Dict[str, float]:
    """Convert ECB EUR-based quotes into GBP-per-currency rates."""
    gbp_per_eur = euro_reference_rates.get("GBP")
    if gbp_per_eur is None:
        raise ValueError("ECB FX payload does not include GBP")

    rates: Dict[str, float] = {}
    for currency in _normalize_currency_codes(currencies):
        if currency == "EUR":
            gbp_per_currency = gbp_per_eur
        else:
            eur_to_currency = euro_reference_rates.get(currency)
            if eur_to_currency is None:
                raise ValueError(f"ECB FX payload does not include {currency}")
            gbp_per_currency = gbp_per_eur / eur_to_currency
        rates[currency] = round(gbp_per_currency, 6)
    return rates


def save_fx_rates(path: Path, *, provider_date: str, rates: Mapping[str, float]) -> None:
    """Persist GBP-based FX rates to YAML in canonical order."""
    payload = {
        "date": provider_date,
        "base": "GBP",
        "rates": {currency: float(rates[currency]) for currency in sorted(rates)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


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
    if investment.adapter is not None:
        payload["adapter"] = investment.adapter
    if investment.account is not None:
        payload["account"] = investment.account
    return payload


def investments_to_dicts(investments: Iterable[Investment]) -> List[Dict[str, object]]:
    """Serialise a list of investments for storage."""
    return [investment_to_dict(inv) for inv in investments]


def _coerce_float(value: object, *, field_name: str) -> float:
    """Convert JSON-loaded numeric payloads into floats."""
    if isinstance(value, (int, float, str)):
        return float(value)
    raise ValueError(f"{field_name} must be numeric")


def import_record_to_dict(record: ImportRecord) -> Dict[str, str]:
    return {
        "adapter": record.adapter,
        "account": record.account,
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
        adapter = item.get("adapter")
        account = item.get("account")
        statement = item.get("statement")
        mappings = item.get("mappings")
        imported_at = item.get("imported_at")
        if not all(
            isinstance(value, str) for value in (adapter, account, statement, mappings, imported_at)
        ):
            raise ValueError("Stored import metadata must use string values")
        imports.append(
            ImportRecord(
                adapter=cast(str, adapter),
                account=cast(str, account),
                statement=cast(str, statement),
                mappings=cast(str, mappings),
                imported_at=cast(str, imported_at),
            )
        )
    return imports


def _snapshot_plan_path(
    snapshot: Mapping[str, object],
    *,
    fallback: Optional[Path] = None,
) -> Path:
    """Read a snapshot's ``plan`` field, falling back when it is absent.

    The fallback is provided by the caller (typically `paths.plan` for the
    user being acted on) so this function does not embed a layout decision.
    """
    plan_value = snapshot.get("plan")
    if isinstance(plan_value, str) and plan_value:
        return Path(plan_value)
    if plan_value is not None:
        raise ValueError("Stored plan path must be a string")
    if fallback is not None:
        return fallback
    raise ValueError("Portfolio snapshot does not record a plan path and no fallback was provided")


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
    adapter_value = payload.get("adapter")
    if adapter_value is not None and not isinstance(adapter_value, str):
        raise ValueError("adapter must be a string when present")
    account_value = payload.get("account")
    if account_value is not None and not isinstance(account_value, str):
        raise ValueError("account must be a string when present")
    return Investment(
        instrument_id=str(payload["instrument_id"]),
        description=str(payload.get("description", "")),
        market_value=_coerce_float(payload.get("market_value", 0.0), field_name="market_value"),
        category=_parse_category_label(category_label),
        volatility=_coerce_float(payload.get("volatility", 0.0), field_name="volatility") or 0.0001,
        source=str(payload.get("source", "portfolio")),
        adapter=cast(Optional[str], adapter_value),
        account=cast(Optional[str], account_value),
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


def summarize_sources(investments: Iterable[Investment]) -> tuple[float, List[tuple[str, float]]]:
    """Aggregate GBP market values by source label for terminal reporting."""
    totals: defaultdict[str, float] = defaultdict(float)
    total_value = 0.0
    for investment in investments:
        if investment.source == "manual":
            label = "manual"
        elif investment.adapter and investment.account:
            label = f"{investment.adapter}/{investment.account}"
        else:
            label = investment.source or "unknown"
        totals[label] += investment.market_value
        total_value += investment.market_value

    rows = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return total_value, rows


def print_source_breakdown(total_value: float, rows: List[tuple[str, float]]) -> None:
    header = f"{'Source Breakdown (GBP)':40}{'Market Value £':>18}"
    print(header)
    print("-" * len(header))
    for label, market_value in rows:
        print(f"{label:40}{market_value:18,.2f}")
    print("-" * len(header))
    print(f"{'Total':40}{total_value:18,.2f}")


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


def load_layered_mappings(
    adapter: str,
    paths: UserPaths,
    *,
    log_overrides: bool = False,
) -> Dict[str, InstrumentMapping]:
    """Merge the shared adapter mappings with the per-user override file.

    The shared file (`config/mappings/<adapter>.yaml`) provides defaults for
    instruments held by anyone in the household; the per-user override
    (`private/users/<user>/mappings/<adapter>.yaml`) replaces individual
    entries by instrument id. New mappings learned at import time are written
    only to the override file so the shared catalog stays curated.
    """
    merged = load_mappings(paths.adapter_mappings_path(adapter))
    overrides = load_mappings(paths.adapter_overrides_path(adapter))
    for instrument, mapping in overrides.items():
        if log_overrides and instrument in merged:
            print(f"Using user override for {instrument}", file=sys.stderr)
        merged[instrument] = mapping
    return merged


def resolve_mapping_sources(
    adapter: str,
    raw_path: Optional[str],
    paths: UserPaths,
    *,
    log_overrides: bool = False,
) -> tuple[Dict[str, InstrumentMapping], Path]:
    """Return the (read-time, write-time) pair for instrument mappings.

    When `raw_path` is provided the caller is pointing at a single explicit
    file: that file is both the read and write target (no layering, by
    request). Otherwise the read view is the layered union of shared +
    per-user override and new entries are written only to the override.
    """
    if raw_path:
        write_path = Path(raw_path)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        return load_mappings(write_path), write_path
    write_path = paths.adapter_overrides_path(adapter)
    write_path.parent.mkdir(parents=True, exist_ok=True)
    return load_layered_mappings(adapter, paths, log_overrides=log_overrides), write_path


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
            expanded.append(
                Investment(
                    instrument_id=investment.instrument_id,
                    description=investment.description,
                    market_value=value,
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
    existing_mappings: Optional[Dict[str, InstrumentMapping]] = None,
) -> tuple[Dict[str, InstrumentMapping], int]:
    """Prompt for missing mappings and persist new entries.

    `existing_mappings` lets the caller pass an already-merged view (e.g. the
    layered shared+override result) so the lookup uses the full picture even
    when new entries are written to a narrower file. Missing entries are
    written to `mapping_path`, merged with whatever already lives there, so
    the file accumulates over time.
    """
    if existing_mappings is None:
        existing_mappings = load_mappings(mapping_path)
    missing = sorted(
        {inv.instrument_id for inv in investments if inv.instrument_id not in existing_mappings}
    )
    if not missing:
        return existing_mappings, 0
    new_entries = gather_missing_mappings(
        missing,
        plan_index=plan_index,
        input_func=input_func,
    )
    persisted = load_mappings(mapping_path)
    persisted.update(new_entries)
    save_mappings(mapping_path, persisted)
    existing_mappings.update(new_entries)
    return existing_mappings, len(new_entries)


def tag_imported_investments(
    investments: Iterable[Investment], *, adapter: str, account: str
) -> List[Investment]:
    """Stamp the broker `(adapter, account)` provenance on each investment."""
    return [replace(investment, adapter=adapter, account=account) for investment in investments]


def cmd_db_init(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb db init` — create the database file and apply all migrations.

    Idempotent: running it against an existing DB just verifies that
    every migration is current. Useful as a sanity check or as the
    first command in a fresh checkout. Subsequent commands open the
    DB lazily, so `db init` is rarely required by itself.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    db = Database.connect(paths.db_path)
    try:
        version = int(db.connection.execute("PRAGMA user_version").fetchone()[0])
        print(f"DB initialised at {paths.db_path} (schema version {version})")
    finally:
        db.close()
    return 0


def cmd_db_seed(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb db seed` — load committed YAML catalog into the database.

    Idempotent and YAML-authoritative for mapping rows: per-adapter
    wipes wiping happens before each reload, so removing an entry from
    `config/mappings/<adapter>.yaml` and re-running `db seed` actually
    drops it. Per-user state (plans, statement imports, positions) is
    never touched — those rows live only in the DB and the seed loader
    has no concept of them.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    if not paths.seed_plan.exists():
        print(f"Seed plan {paths.seed_plan} not found", file=sys.stderr)
        return 1
    db = Database.connect(paths.db_path)
    try:
        seed_from_yaml(
            db.connection,
            seed_plan_path=paths.seed_plan,
            mappings_dir=paths.shared_mappings_dir,
        )
        category_count = db.connection.execute("SELECT COUNT(*) AS n FROM category").fetchone()["n"]
        mapping_count = db.connection.execute("SELECT COUNT(*) AS n FROM mapping").fetchone()["n"]
        print(f"Seeded {paths.db_path}: {category_count} categories, {mapping_count} mappings")
    finally:
        db.close()
    return 0


def cmd_fx_update(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    paths = paths if paths is not None else _default_paths()
    fx_path = Path(args.fx)
    fallback = paths.fx_template if fx_path == paths.fx else None
    try:
        currencies = (
            _normalize_currency_codes(args.currency)
            if args.currency
            else resolve_tracked_fx_currencies(fx_path, fallback_template=fallback)
        )
        if not currencies:
            raise ValueError("At least one currency must be specified or already tracked")
        provider_date, euro_reference_rates = fetch_ecb_reference_rates()
        gbp_rates = derive_gbp_fx_rates(euro_reference_rates, currencies)
        save_fx_rates(fx_path, provider_date=provider_date, rates=gbp_rates)
    except (FileNotFoundError, OSError, urllib.error.URLError, ValueError) as exc:
        print(f"Failed to update FX rates: {exc}", file=sys.stderr)
        return 1

    print(f"Updated {fx_path} with {len(gbp_rates)} rate(s) dated {provider_date}")
    return 0


def cmd_categorize(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    plan_arg = getattr(args, "plan", None)
    if plan_arg is not None:
        plan = load_portfolio_plan_from_yaml(
            Path(plan_arg), default_leaf_volatility=DEFAULT_LEAF_VOLATILITY
        )
    else:
        plan = _load_user_portfolio_plan_from_db(paths)
    plan_index = PlanIndex.from_plan(plan)
    mappings, write_path = resolve_mapping_sources(args.adapter, args.mappings, paths)
    fx_rates = load_fx_rates(paths=paths)
    investments = parse_statement(Path(args.statement), args.adapter, fx_rates=fx_rates)
    missing = [inv.instrument_id for inv in investments if inv.instrument_id not in mappings]
    if not missing:
        print("All instruments already have mappings. Nothing to do.")
        return 0
    _, new_count = ensure_mappings_for_investments(
        investments,
        write_path,
        plan_index=plan_index,
        existing_mappings=mappings,
    )
    print(f"Stored {new_count} new mappings in {write_path}")
    return 0


def _resolve_user_plan_path(paths: UserPaths) -> Path:
    """Return the plan file for this user, falling back to the seed plan.

    Deprecated: the database is the authoritative plan store now. Kept
    here for the `cmd_categorize` / `portfolio_create` paths until they
    are rewired to use `_load_user_portfolio_plan_from_db` directly.
    """
    if paths.plan.exists():
        return paths.plan
    return paths.seed_plan


def _load_user_portfolio_plan_from_db(paths: UserPaths) -> PortfolioPlan:
    """Load the user's plan from the DB as a `PortfolioPlan`.

    Raises `FileNotFoundError` (the existing portfolio-command failure
    convention) when the user has no plan rows yet so the caller's
    error-handling path doesn't need to change.
    """
    db = _open_database(paths)
    try:
        user_id = repositories.find_user_id(db.connection, paths.user)
        if user_id is None or not repositories.plan_exists(db.connection, user_id):
            raise FileNotFoundError(
                f"No plan in the database for user '{paths.user}'. Run `rb plan create` first."
            )
        nodes = repositories.load_plan_tree(db.connection, user_id)
        plan: PortfolioPlan = build_portfolio_plan_from_nodes(
            nodes, default_leaf_volatility=DEFAULT_LEAF_VOLATILITY
        )
        return plan
    finally:
        db.close()


def cmd_portfolio_create(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """Create an empty portfolio snapshot for the resolved user.

    Retained as a helper so test fixtures and one-shot bootstraps can build
    an empty `portfolio.json` without going through `portfolio import`.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    plan_arg = getattr(args, "plan", None)
    plan_path = Path(plan_arg) if plan_arg else _resolve_user_plan_path(paths)
    overwrite = bool(getattr(args, "overwrite", False))
    if paths.portfolio.exists() and not overwrite:
        raise FileExistsError(f"{paths.portfolio} already exists. Use --overwrite to replace it.")
    save_portfolio_snapshot(paths.portfolio, plan_path, [], imports=[])
    print(f"Created empty portfolio at {paths.portfolio}")
    return 0


def _ensure_portfolio_exists(paths: UserPaths) -> None:
    """Create an empty portfolio snapshot for this user if none exists yet."""
    if paths.portfolio.exists():
        return
    plan_path = _resolve_user_plan_path(paths)
    save_portfolio_snapshot(paths.portfolio, plan_path, [], imports=[])


def cmd_portfolio_import(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    _ensure_portfolio_exists(paths)
    snapshot = load_portfolio_snapshot(paths.portfolio)
    # `plan_path` is preserved in the snapshot as an audit-trail string only.
    # The actual `PortfolioPlan` is loaded from the database, which is now
    # authoritative for plan content.
    plan_path = _snapshot_plan_path(snapshot, fallback=_resolve_user_plan_path(paths))
    plan = _load_user_portfolio_plan_from_db(paths)
    plan_index = PlanIndex.from_plan(plan)

    canonical_statement = _autofile_statement(
        Path(args.statement),
        paths,
        adapter=args.adapter,
        account=args.account,
        move=bool(getattr(args, "move", False)),
    )
    if canonical_statement != Path(args.statement).resolve():
        action = "Moved" if getattr(args, "move", False) else "Copied"
        print(f"{action} statement to {canonical_statement}")

    mappings, write_path = resolve_mapping_sources(
        args.adapter, args.mappings, paths, log_overrides=True
    )
    fx_rates = load_fx_rates(args.fx, paths=paths)
    parsed_investments = parse_statement(canonical_statement, args.adapter, fx_rates=fx_rates)
    mappings, new_count = ensure_mappings_for_investments(
        parsed_investments,
        write_path,
        plan_index=plan_index,
        existing_mappings=mappings,
    )
    imported_investments = tag_imported_investments(
        apply_mappings_to_investments(parsed_investments, mappings),
        adapter=args.adapter,
        account=args.account,
    )

    # `(adapter, account)` is the stable key for a brokerage account.
    # Re-imports of the same account replace its prior positions and record.
    existing_investments = investments_from_dicts(_snapshot_investments(snapshot))
    preserved_investments = [
        investment
        for investment in existing_investments
        if (investment.adapter, investment.account) != (args.adapter, args.account)
    ]
    replaced_count = len(existing_investments) - len(preserved_investments)

    imports = [
        record
        for record in _snapshot_imports(snapshot)
        if (record.adapter, record.account) != (args.adapter, args.account)
    ]
    imports.append(
        ImportRecord(
            adapter=args.adapter,
            account=args.account,
            statement=str(canonical_statement),
            mappings=str(write_path),
            imported_at=_utc_timestamp(),
        )
    )
    save_portfolio_snapshot(
        paths.portfolio,
        plan_path,
        preserved_investments + imported_investments,
        imports=imports,
        created_at=_snapshot_created_at(snapshot),
    )
    account_label = f"{args.adapter}/{args.account}"
    if replaced_count:
        print(f"Replaced {replaced_count} existing position(s) for '{account_label}'.")
    if new_count:
        print(f"Stored {new_count} new mapping(s) in {write_path}.")
    print(
        f"Imported {len(imported_investments)} position(s) from {canonical_statement} "
        f"into {paths.portfolio} as '{account_label}'."
    )
    return 0


def _resolve_export_path(args: argparse.Namespace, paths: UserPaths) -> Optional[Path]:
    """Return the CSV export destination, or None if no export was requested.

    `--export` accepts an optional value: if the flag is present without an
    argument, the report is exported to the user's reports directory using
    today's date as the filename.
    """
    raw = getattr(args, "export", None)
    if raw is None:
        return None
    if isinstance(raw, str) and raw and raw != "__default__":
        return Path(raw)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return paths.reports_dir / f"{today}.csv"


def cmd_portfolio_report(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    if not paths.portfolio.exists():
        raise FileNotFoundError(f"Portfolio file {paths.portfolio} not found")
    snapshot = load_portfolio_snapshot(paths.portfolio)
    plan_arg = getattr(args, "plan", None)
    if plan_arg is not None:
        category_nodes = load_category_nodes_from_yaml(Path(plan_arg))
        validation_failures = collect_category_weight_validation_failures(category_nodes)
        if validation_failures:
            print(
                format_category_weight_validation_failures(validation_failures),
                file=sys.stderr,
            )
            return 1
        plan = build_portfolio_plan_from_nodes(
            category_nodes,
            default_leaf_volatility=DEFAULT_LEAF_VOLATILITY,
        )
    else:
        plan = _load_user_portfolio_plan_from_db(paths)
    investments = investments_from_dicts(_snapshot_investments(snapshot))
    total_value, summary = summarize_portfolio(plan, investments)
    source_total_value, source_rows = summarize_sources(investments)
    print(f"Loaded {len(investments)} investments from {paths.portfolio}")
    print_summary_table(total_value, summary)
    print()
    print_source_breakdown(source_total_value, source_rows)
    export_path = _resolve_export_path(args, paths)
    if export_path is not None:
        export_summary_to_csv(export_path, summary)
        print(f"Wrote summary to {export_path}")
    return 0


def cmd_plan_create(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    overwrite = bool(getattr(args, "overwrite", False))
    db = _open_database(paths)
    try:
        user_id = _ensure_user_in_db(db.connection, paths.user)
        if repositories.plan_exists(db.connection, user_id) and not overwrite:
            print(
                f"Plan already exists for user '{paths.user}'. Use --overwrite to replace it.",
                file=sys.stderr,
            )
            return 1

        source_user = getattr(args, "from_user", None)
        if source_user:
            source_user_id = repositories.find_user_id(db.connection, source_user)
            if source_user_id is None:
                print(
                    f"Source user '{source_user}' has no plan in the database.",
                    file=sys.stderr,
                )
                return 1
            source_tree = repositories.load_plan_tree(db.connection, source_user_id)
            if not source_tree:
                print(
                    f"Source user '{source_user}' has no plan to clone from.",
                    file=sys.stderr,
                )
                return 1
            failures = collect_category_weight_validation_failures(source_tree)
            if failures:
                print(format_category_weight_validation_failures(failures), file=sys.stderr)
                return 1
            repositories.write_plan_tree(db.connection, user_id, source_tree)
            print(f"Cloned plan from user '{source_user}' to '{paths.user}'.")
            return 0

        catalog = build_catalog_from_db(db.connection, current_user_id=user_id)
        print(describe_catalog_sources_from_db(db.connection, current_user_name=paths.user))
        print(f"Catalog contains {count_unique_categories(catalog)} unique categories.")
        io = StdIO()
        try:
            plan_nodes = walk_catalog_interactive(catalog, io)
            failures = collect_category_weight_validation_failures(plan_nodes)
            if failures:
                print(format_category_weight_validation_failures(failures), file=sys.stderr)
                return 1
            if not _confirm_plan_summary(plan_nodes, io, paths.user):
                raise PlanCreationAborted("user declined to save plan")
        except PlanCreationAborted as exc:
            # User typed quit/exit, pressed Ctrl+C, sent EOF, or declined the
            # final confirmation. Nothing is written; surface a single-line
            # message and exit non-zero so scripts can detect the abort.
            print(f"plan create aborted: {exc}", file=sys.stderr)
            return 1
        repositories.write_plan_tree(db.connection, user_id, plan_nodes)
        leaf_count = sum(1 for _ in _iter_leaves(plan_nodes))
        print(f"Wrote plan for user '{paths.user}' ({leaf_count} leaf categories).")
        return 0
    finally:
        db.close()


def _confirm_plan_summary(
    plan_nodes: Sequence[CategoryNode],
    io: IO,
    user: str,
) -> bool:
    """Print the plan summary and ask for y/N confirmation.

    Mirrors what `confirm_and_write_plan` used to do for the YAML path —
    factored out so the DB-backed write path can keep the same UX
    without resurrecting the YAML writer.
    """
    from .plan_bootstrap import _render_plan_tree

    io.info("\n—— Plan summary ——")
    io.info(_render_plan_tree(plan_nodes))
    return _prompt_yes_no(
        io,
        f"\nSave this plan for user '{user}'? [y/N]: ",
        default=False,
    )


def _iter_leaves(nodes: Iterable) -> Iterable:
    for node in nodes:
        if node.children:
            yield from _iter_leaves(node.children)
        else:
            yield node


def cmd_plan_validate(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan validate` — verify sibling weight totals sum to 100%.

    With `--path`, validates a stand-alone YAML file (used for one-off
    inspection of legacy or hand-authored plans). Otherwise loads the
    user's plan from the database.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    explicit = getattr(args, "path", None)
    if explicit is not None:
        plan_path = Path(explicit)
        if not plan_path.exists():
            print(f"Plan file {plan_path} not found", file=sys.stderr)
            return 1
        nodes = load_category_nodes_from_yaml(plan_path)
        failures = collect_category_weight_validation_failures(nodes)
        if failures:
            print(format_category_weight_validation_failures(failures), file=sys.stderr)
            return 1
        print(f"{plan_path} is valid.")
        return 0

    if not _require_user(paths):
        return 1
    db_nodes = _load_plan_tree_or_complain(paths)
    if db_nodes is None:
        return 1
    failures = collect_category_weight_validation_failures(db_nodes)
    if failures:
        print(format_category_weight_validation_failures(failures), file=sys.stderr)
        return 1
    print(f"Plan for user '{paths.user}' is valid.")
    return 0


def _load_plan_tree_or_complain(paths: UserPaths) -> Optional[list[CategoryNode]]:
    """Return the user's DB-stored plan tree, or print a missing-plan error.

    Returns `None` when the user is unknown to the database or has no
    `plan_node` rows yet. Callers should propagate the `None` as an exit
    code so the user sees a single, consistent "run plan create" hint.
    """
    db = _open_database(paths)
    try:
        user_id = repositories.find_user_id(db.connection, paths.user)
        if user_id is None or not repositories.plan_exists(db.connection, user_id):
            print(
                f"No plan found for user '{paths.user}'. Run `rb plan create` first.",
                file=sys.stderr,
            )
            return None
        return repositories.load_plan_tree(db.connection, user_id)
    finally:
        db.close()


def cmd_plan_list(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan list` — print every leaf's weight, vol, and adjustment.

    Read-only — never writes. Zero-weight leaves are included so the user
    can see the whole plan at a glance (unlike `plan adjust`, where the
    walker silently skips them).
    """
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    nodes = _load_plan_tree_or_complain(paths)
    if nodes is None:
        return 1
    print(render_list(list(iter_leaf_nodes(nodes))))
    return 0


def cmd_plan_adjust(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan adjust` — review or change leaf adjustments on a user's plan.

    Two mutually exclusive modes:

    - Positional `path` plus `value`: targeted single-leaf set, with a
      y/N confirm that `--yes` can skip.
    - Default: interactive walker over every non-zero-weight leaf, with
      an optional `--under` subtree filter; a single y/N confirm applies
      the whole batch.

    Read-only inspection of the plan lives at `rb plan list`. All write
    paths here flow through `write_plan_yaml`, which writes atomically.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1

    under = getattr(args, "under", None)
    path_label = getattr(args, "path", None)
    value = getattr(args, "value", None)
    skip_confirm = bool(getattr(args, "yes", False))

    # Mutually exclusive combinations are rejected up front so each branch
    # below can assume a clean shape. argparse alone can't express these
    # combos because `path` and `value` are positional and optional.
    if path_label is not None and value is None:
        print(
            "plan adjust: a positional path requires a value (e.g. "
            '`plan adjust "Bonds / Developed > UK > Govt" 0.95`)',
            file=sys.stderr,
        )
        return 1
    if path_label is None and value is not None:
        # Unreachable through argparse (positionals are consumed left to
        # right), but kept as a guard so hand-built Namespaces fail loudly.
        print("plan adjust: a value requires a positional path", file=sys.stderr)
        return 1
    if path_label is not None and under is not None:
        print(
            "plan adjust: --under cannot be combined with a positional path",
            file=sys.stderr,
        )
        return 1

    db = _open_database(paths)
    try:
        user_id = repositories.find_user_id(db.connection, paths.user)
        if user_id is None or not repositories.plan_exists(db.connection, user_id):
            print(
                f"No plan found for user '{paths.user}'. Run `rb plan create` first.",
                file=sys.stderr,
            )
            return 1
        nodes = repositories.load_plan_tree(db.connection, user_id)

        io: IO = StdIO()

        if path_label is not None:
            # Translate the doc/help-text `>` separator into the project's
            # canonical `/` before handing off to the existing label parser.
            parts = _parse_category_label(path_label.replace(">", "/")).parts
            # The mutex check above guarantees `value is not None` whenever
            # `path_label` is not None — this assert narrows the type for mypy.
            assert value is not None
            try:
                change = apply_targeted(nodes, parts, float(value))
            except ValueError as exc:
                print(f"plan adjust failed: {exc}", file=sys.stderr)
                return 1
            try:
                should_write = confirm_changes(paths.plan, [change], io, skip_prompt=skip_confirm)
            except PlanCreationAborted as exc:
                print(f"plan adjust aborted: {exc}", file=sys.stderr)
                return 1
            if not should_write:
                print("plan adjust aborted: user declined.")
                return 0
            repositories.write_plan_tree(db.connection, user_id, nodes)
            print(f"Wrote updated plan for user '{paths.user}' (1 leaf changed).")
            return 0

        # Walker mode (with optional --under).
        try:
            leaves = filter_under(iter_leaf_nodes(nodes), under)
        except ValueError as exc:
            print(f"plan adjust failed: {exc}", file=sys.stderr)
            return 1
        try:
            changes = walk_adjustments(leaves, io)
            if not changes:
                print("No changes.")
                return 0
            should_write = confirm_changes(paths.plan, changes, io)
        except PlanCreationAborted as exc:
            print(f"plan adjust aborted: {exc}", file=sys.stderr)
            return 1
        if not should_write:
            print("plan adjust aborted: user declined.")
            return 0
        repositories.write_plan_tree(db.connection, user_id, nodes)
        print(f"Wrote updated plan for user '{paths.user}' ({len(changes)} leaf changes).")
        return 0
    finally:
        db.close()


def cmd_plan_export(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan export` — write the user's plan as a depth-column CSV.

    Default destination is stdout (pipe-friendly); pass `--out PATH` to write
    a file. The CSV format is documented in `plan_csv.write_plan_csv` and is
    designed to round-trip through `rb plan import` without loss.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    nodes = _load_plan_tree_or_complain(paths)
    if nodes is None:
        return 1

    out_path: Optional[Path] = getattr(args, "out", None)
    if out_path is None:
        # `csv.writer` writes `\r\n` line terminators by default; that's the
        # RFC 4180 convention and matches what spreadsheet apps expect on
        # import, so we let it through to stdout untouched.
        write_plan_csv(nodes, sys.stdout)
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # `newline=""` tells the file object not to translate the `\r\n` that
    # csv.writer emits — without it, Windows would end up with `\r\r\n`.
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        write_plan_csv(nodes, handle)
    print(f"Wrote plan CSV to {out_path}")
    return 0


def cmd_plan_import(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan import` — replace the user's plan from a depth-column CSV.

    Reads and validates the CSV first (header shape, weight totals, leaf
    structure). On success, prints a leaves-added/removed/changed summary
    and asks for y/N confirmation before writing to the database. Pass
    `--yes` to skip the prompt. Any CSV-parse or validation error returns
    exit code 2 with the failing row's number, and the existing plan is
    left untouched.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"CSV file {csv_path} not found", file=sys.stderr)
        return 1

    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            new_nodes = read_plan_csv(handle)
    except PlanCSVError as exc:
        print(f"plan import failed: {exc}", file=sys.stderr)
        return 2

    # Run the same sibling-weight validator the YAML loader uses so the CSV
    # path and `plan validate` cannot disagree on what counts as a valid plan.
    failures = collect_category_weight_validation_failures(new_nodes)
    if failures:
        print(format_category_weight_validation_failures(failures), file=sys.stderr)
        return 2

    skip_confirm = bool(getattr(args, "yes", False))

    db = _open_database(paths)
    try:
        user_id = _ensure_user_in_db(db.connection, paths.user)
        had_plan = repositories.plan_exists(db.connection, user_id)
        if had_plan:
            old_nodes = repositories.load_plan_tree(db.connection, user_id)
            print(_render_import_summary(old_nodes, new_nodes))
        else:
            new_leaf_count = sum(1 for _ in iter_leaf_nodes(new_nodes))
            print(
                f"User '{paths.user}' has no existing plan; importing will create "
                f"a new plan with {new_leaf_count} leaves."
            )

        if not skip_confirm:
            io: IO = StdIO()
            try:
                ok = _prompt_yes_no(
                    io,
                    f"\nReplace plan for user '{paths.user}'? [y/N]: ",
                    default=False,
                )
            except PlanCreationAborted as exc:
                print(f"plan import aborted: {exc}", file=sys.stderr)
                return 1
            if not ok:
                print("plan import aborted: user declined.")
                return 0

        repositories.write_plan_tree(db.connection, user_id, new_nodes)
        leaf_count = sum(1 for _ in iter_leaf_nodes(new_nodes))
        print(f"Wrote plan for user '{paths.user}' ({leaf_count} leaves).")
        return 0
    finally:
        db.close()


def _render_import_summary(
    old_nodes: Sequence[CategoryNode],
    new_nodes: Sequence[CategoryNode],
) -> str:
    """Render a leaves-added/removed/changed summary between two plan trees.

    Diff compares **cumulative** leaf weights (the product of every
    ancestor's per-level weight) so a branch-level edit shows up on every
    affected leaf — without that, a user changing `Equities` from 0.55 to
    0.60 would see no leaf changes in the summary, only structural
    re-derivation. Leaves are also compared on their resolved volatility
    and own adjustment.
    """
    old_leaves = _collect_leaf_summary(old_nodes)
    new_leaves = _collect_leaf_summary(new_nodes)
    added = sorted(new_leaves.keys() - old_leaves.keys())
    removed = sorted(old_leaves.keys() - new_leaves.keys())
    changed: list[tuple[str, ...]] = []
    for path in old_leaves.keys() & new_leaves.keys():
        if old_leaves[path] != new_leaves[path]:
            changed.append(path)
    lines = [
        f"Import summary: {len(new_leaves)} leaves total "
        f"(was {len(old_leaves)}; +{len(added)} added, "
        f"-{len(removed)} removed, ~{len(changed)} changed)"
    ]
    if added:
        lines.append("Added:")
        lines.extend(f"  + {' / '.join(path)}" for path in added)
    if removed:
        lines.append("Removed:")
        lines.extend(f"  - {' / '.join(path)}" for path in removed)
    if changed:
        lines.append("Changed:")
        lines.extend(f"  ~ {' / '.join(path)}" for path in sorted(changed))
    return "\n".join(lines)


def _collect_leaf_summary(
    nodes: Sequence[CategoryNode],
) -> dict[tuple[str, ...], tuple[float, Optional[float], float]]:
    """Return `{path: (cumulative_weight, resolved_volatility, adjustment)}` for every leaf.

    Cumulative weight is the running product of per-level weights from
    root to leaf. Resolved volatility is the leaf's own value falling
    back to the nearest ancestor that defined one — same convention as
    the loader's leaf-volatility inheritance, so the diff stays
    consistent across CSV round-trips that don't carry branch volatility.
    """
    summary: dict[tuple[str, ...], tuple[float, Optional[float], float]] = {}

    def walk(
        children: Sequence[CategoryNode],
        prefix: tuple[str, ...],
        parent_weight: float,
        inherited_volatility: Optional[float],
    ) -> None:
        for node in children:
            path = prefix + (node.name,)
            cumulative = parent_weight * node.weight
            next_inherited = (
                node.volatility if node.volatility is not None else inherited_volatility
            )
            if node.children:
                walk(node.children, path, cumulative, next_inherited)
            else:
                resolved_volatility = (
                    node.volatility if node.volatility is not None else inherited_volatility
                )
                summary[path] = (cumulative, resolved_volatility, node.adjustment)

    walk(nodes, prefix=(), parent_weight=1.0, inherited_volatility=None)
    return summary


def cmd_user_list(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb user list` — show every user in the database with a plan summary.

    Database is authoritative for the user roster. A user with rows but no
    plan yet is shown so the freshly-bootstrapped user remains discoverable.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    db = _open_database(paths)
    try:
        names = repositories.list_user_names(db.connection)
        if not names:
            print("No stored users.")
            return 0
        for name in names:
            user_id = repositories.find_user_id(db.connection, name)
            assert user_id is not None
            leaf_count = db.connection.execute(
                """
                SELECT COUNT(*) AS n
                FROM plan_node pn
                LEFT JOIN plan_node child ON child.parent_id = pn.id
                WHERE pn.user_id = ? AND child.id IS NULL
                """,
                (user_id,),
            ).fetchone()["n"]
            if leaf_count:
                print(f"{name:<25} plan_leaves={leaf_count}")
            else:
                print(f"{name:<25} (no plan yet)")
        return 0
    finally:
        db.close()


def cmd_user_create(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """Create a user row in the DB and the corresponding on-disk directory.

    The DB row is the authoritative existence record for the user; the
    directory under `private/users/<user>/` exists to hold statements
    (raw broker CSVs) and reports (derived outputs). Plan bootstrap is
    intentionally separate — `plan create` owns that flow.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    db = _open_database(paths)
    try:
        if repositories.find_user_id(db.connection, paths.user) is not None:
            print(
                f"User '{paths.user}' already exists. "
                f"Use `riskbalancer user delete --user {paths.user} --confirm` "
                "first if you really want to start over.",
                file=sys.stderr,
            )
            return 1
        repositories.create_user(db.connection, paths.user)
    finally:
        db.close()
    # The on-disk directory holds statements and reports — both are still
    # filesystem-backed in this milestone. `statements/`, `reports/` are
    # created lazily by their owning commands.
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    print(f"Created user '{paths.user}' (DB row + {paths.user_dir}).")
    print(f"Next: riskbalancer plan create --user {paths.user} (or --from <peer>)")
    return 0


def cmd_user_delete(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """Delete the user's DB row (cascades through plan/sources/positions)
    and remove their on-disk directory."""
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    if not getattr(args, "confirm", False):
        raise ValueError(
            f"Refusing to delete user '{paths.user}' without --confirm "
            "(this removes the DB row, statements, reports — everything)."
        )
    db = _open_database(paths)
    try:
        user_id = repositories.find_user_id(db.connection, paths.user)
        if user_id is None and not paths.user_dir.exists():
            raise FileNotFoundError(
                f"User '{paths.user}' does not exist in the database or on disk."
            )
        if user_id is not None:
            repositories.delete_user(db.connection, user_id)
    finally:
        db.close()
    if paths.user_dir.exists():
        shutil.rmtree(paths.user_dir)
    print(f"Deleted user '{paths.user}'.")
    return 0


def cmd_portfolio_add_instrument(
    args: argparse.Namespace, paths: Optional[UserPaths] = None
) -> int:
    paths = paths if paths is not None else _paths_from_args(args)
    if not _require_user(paths):
        return 1
    _ensure_portfolio_exists(paths)
    snapshot = load_portfolio_snapshot(paths.portfolio)
    plan_path = _snapshot_plan_path(snapshot, fallback=_resolve_user_plan_path(paths))
    plan = _load_user_portfolio_plan_from_db(paths)
    plan_index = PlanIndex.from_plan(plan)

    manual_path = paths.manual_mappings
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
        paths.portfolio,
        plan_path,
        existing_investments,
        imports=imports,
        created_at=_snapshot_created_at(snapshot),
    )

    print(f"Added {args.instrument_id} to {paths.portfolio} using manual mappings")
    return 0


def _user_parent_parser() -> argparse.ArgumentParser:
    """Argparse parent that adds `--user` to every subcommand that needs it."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--user",
        help=(
            "User name. Defaults to the RISKBALANCER_USER environment variable "
            "or the default_user field in config/riskbalancer.yaml."
        ),
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RiskBalancer CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    user_parent = _user_parent_parser()

    # db — schema lifecycle and seed-ingest. Shared across users; no --user.
    db_parser = subparsers.add_parser("db", help="Manage the project database")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    db_init = db_sub.add_parser(
        "init",
        help="Create private/riskbalancer.db and apply pending migrations",
    )
    db_init.set_defaults(func=cmd_db_init)
    db_seed = db_sub.add_parser(
        "seed",
        help=(
            "Load committed YAML catalog (seed plan + adapter mappings) into the DB. "
            "Idempotent and YAML-authoritative for mapping rows."
        ),
    )
    db_seed.set_defaults(func=cmd_db_seed)

    # fx — shared across users; no --user.
    fx_parser = subparsers.add_parser("fx", help="Manage FX rate data")
    fx_sub = fx_parser.add_subparsers(dest="fx_command", required=True)
    fx_update = fx_sub.add_parser("update", help="Update private/fx.yaml from ECB reference rates")
    fx_update.add_argument(
        "--fx",
        default="private/fx.yaml",
        help="Path to FX YAML (defaults to private/fx.yaml)",
    )
    fx_update.add_argument(
        "--currency",
        action="append",
        help="Currency code to track; repeat to overwrite the tracked set",
    )
    fx_update.set_defaults(func=cmd_fx_update)

    # categorize — operates on a user's plan and adapter mappings.
    categorize = subparsers.add_parser(
        "categorize",
        parents=[user_parent],
        help="Assign categories to unmapped instruments",
    )
    categorize.add_argument("--adapter", default="ajbell", choices=ADAPTERS.keys())
    categorize.add_argument("--statement", required=True, help="Path to broker CSV statement")
    categorize.add_argument(
        "--plan",
        help="Path to categories YAML (defaults to the user's plan.yaml or seed_plan.yaml)",
    )
    categorize.add_argument(
        "--mappings",
        help=(
            "Optional explicit mappings file. When omitted, the layered "
            "shared+per-user override resolution is used."
        ),
    )
    categorize.set_defaults(func=cmd_categorize)

    # portfolio — user-keyed actions on this user's snapshot.
    portfolio_parser = subparsers.add_parser("portfolio", help="Manage portfolio data")
    portfolio_sub = portfolio_parser.add_subparsers(dest="portfolio_command", required=True)

    portfolio_import = portfolio_sub.add_parser(
        "import",
        parents=[user_parent],
        help="Import a single broker statement into the user's portfolio",
    )
    portfolio_import.add_argument("--adapter", required=True, choices=ADAPTERS.keys())
    portfolio_import.add_argument(
        "--account",
        required=True,
        help=(
            "Account name within the broker (e.g. sipp/isa/dealing). Together with "
            "--adapter this forms the stable key for the imported positions; "
            "re-imports against the same (adapter, account) pair replace the prior "
            "positions. Also used to file the statement under "
            "private/users/<user>/statements/<adapter>/<account>/<YYYY>/<MM>/."
        ),
    )
    portfolio_import.add_argument("--statement", required=True, help="Path to broker CSV statement")
    portfolio_import.add_argument(
        "--move",
        action="store_true",
        help=(
            "Remove the source statement after copying it into the user's "
            "statements tree. Default is to keep the source intact."
        ),
    )
    portfolio_import.add_argument(
        "--mappings",
        help=(
            "Optional explicit mappings file. When omitted, the layered "
            "shared+per-user override resolution is used."
        ),
    )
    portfolio_import.add_argument(
        "--fx",
        help="Optional FX rate YAML (base GBP) used to convert non-GBP statements",
    )
    portfolio_import.set_defaults(func=cmd_portfolio_import)

    report = portfolio_sub.add_parser(
        "report", parents=[user_parent], help="Analyze the user's portfolio snapshot"
    )
    report.add_argument(
        "--plan",
        help="Optional plan path override (defaults to the plan stored with the portfolio)",
    )
    report.add_argument(
        "--export",
        nargs="?",
        const="__default__",
        default=None,
        help=(
            "Export the category summary as CSV. Pass an explicit path or use the bare "
            "flag to write to <user>/reports/<YYYY-MM-DD>.csv."
        ),
    )
    report.set_defaults(func=cmd_portfolio_report)

    add_manual = portfolio_sub.add_parser(
        "add", parents=[user_parent], help="Manually append an instrument to this portfolio"
    )
    add_manual.add_argument("--instrument-id", required=True)
    add_manual.add_argument("--description", required=True)
    add_manual.add_argument("--market-value", required=True, type=float)
    add_manual.add_argument(
        "--category",
        help="Optional category path(s); prompt/manual mappings used if omitted",
    )
    add_manual.set_defaults(func=cmd_portfolio_add_instrument)

    # plan — bootstrap and validate per-user category plans.
    plan_parser = subparsers.add_parser("plan", help="Manage user category plans")
    plan_sub = plan_parser.add_subparsers(dest="plan_command", required=True)

    plan_create = plan_sub.add_parser(
        "create",
        parents=[user_parent],
        help="Bootstrap a new plan.yaml for the user, either interactively or by cloning",
    )
    plan_create.add_argument(
        "--from",
        dest="from_user",
        help="Clone the plan from another user instead of walking the catalog interactively",
    )
    plan_create.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the user's existing plan if one is already on disk",
    )
    plan_create.set_defaults(func=cmd_plan_create)

    plan_validate = plan_sub.add_parser(
        "validate",
        parents=[user_parent],
        help="Validate a plan's sibling weight totals; exits 0 on success, 1 on failure",
    )
    plan_validate.add_argument(
        "--path",
        help="Optional explicit plan path (defaults to the user's plan.yaml)",
    )
    plan_validate.set_defaults(func=cmd_plan_validate)

    plan_list = plan_sub.add_parser(
        "list",
        parents=[user_parent],
        help="Print every leaf's weight, volatility, and adjustment (read-only)",
    )
    plan_list.set_defaults(func=cmd_plan_list)

    plan_adjust = plan_sub.add_parser(
        "adjust",
        parents=[user_parent],
        help="Review or change adjustment values on leaf categories",
    )
    plan_adjust.add_argument(
        "path",
        nargs="?",
        help=(
            'Category path like "Bonds / Developed > UK > Govt". '
            "When given, the next positional must be the new adjustment value."
        ),
    )
    plan_adjust.add_argument(
        "value",
        nargs="?",
        type=float,
        help="New adjustment value (only valid with a positional path)",
    )
    plan_adjust.add_argument(
        "--under",
        help=(
            "Restrict the walker to leaves under the given subtree "
            '(e.g. "Bonds / Developed"). Mutually exclusive with the positional path.'
        ),
    )
    plan_adjust.add_argument(
        "--yes",
        action="store_true",
        help="Skip the y/N confirm for targeted single-leaf edits",
    )
    plan_adjust.set_defaults(func=cmd_plan_adjust)

    plan_export = plan_sub.add_parser(
        "export",
        parents=[user_parent],
        help="Export the user's plan as a depth-column CSV (stdout by default)",
    )
    plan_export.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the CSV to this path instead of stdout.",
    )
    plan_export.set_defaults(func=cmd_plan_export)

    plan_import = plan_sub.add_parser(
        "import",
        parents=[user_parent],
        help="Replace the user's plan from a depth-column CSV",
    )
    plan_import.add_argument(
        "csv_path",
        help="CSV file to import (produced by `rb plan export` or hand-edited).",
    )
    plan_import.add_argument(
        "--yes",
        action="store_true",
        help="Skip the y/N confirm before overwriting plan.yaml.",
    )
    plan_import.set_defaults(func=cmd_plan_import)

    # user — manage which users exist in private/users/.
    user_parser = subparsers.add_parser("user", help="Manage users")
    user_sub = user_parser.add_subparsers(dest="user_command", required=True)
    user_list = user_sub.add_parser("list", help="List stored users")
    user_list.set_defaults(func=cmd_user_list)
    user_create = user_sub.add_parser(
        "create",
        parents=[user_parent],
        help="Create an empty user directory under private/users/",
    )
    user_create.set_defaults(func=cmd_user_create)
    user_delete = user_sub.add_parser(
        "delete",
        parents=[user_parent],
        help="Remove a user's directory (statements, mappings, plan, portfolio, reports)",
    )
    user_delete.add_argument(
        "--confirm",
        action="store_true",
        help="Required acknowledgement: this wipes the entire user directory",
    )
    user_delete.set_defaults(func=cmd_user_delete)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = _paths_from_args(args)
    return args.func(args, paths)


if __name__ == "__main__":
    sys.exit(main())
