"""
RiskBalancer command-line interface.

Every per-user command requires `--user`; there is no default-user
resolution. The DB at `private/riskbalancer.db` is authoritative for every
mutable concept (users, accounts, categories, instruments, mappings,
positions, plans, FX rates, statement imports). Raw broker statements
remain on disk under `private/users/<u>/statements/...` and CSV reports
under `private/users/<u>/reports/`.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import repositories
from .adapters import (
    AegonCSVAdapter,
    AJBellCSVAdapter,
    CitiCSVAdapter,
    IBKRCSVAdapter,
    MS401KCSVAdapter,
    SchwabCSVAdapter,
)
from .adapters.base import StatementAdapter
from .configuration import (
    CategoryNode,
    build_portfolio_plan_from_nodes,
    collect_category_weight_validation_failures,
    format_category_weight_validation_failures,
)
from .db import Database
from .paths import UserPaths
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
    fill_missing_leaf_vol_adj,
    walk_catalog_interactive,
)
from .plan_csv import PlanCSVError, read_plan_csv, write_plan_csv
from .repositories import MICROS_SCALE

DEFAULT_LEAF_VOLATILITY = 0.15
ECB_DAILY_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FX_HTTP_USER_AGENT = "riskbalancer/1.0"


# All adapters now take no constructor arguments — FX conversion happens at
# report time using the `fx_rate` table, not at parse time.
ADAPTERS: Dict[str, type[StatementAdapter]] = {
    "aegon": AegonCSVAdapter,
    "ajbell": AJBellCSVAdapter,
    "citi": CitiCSVAdapter,
    "ibkr": IBKRCSVAdapter,
    "ms401k": MS401KCSVAdapter,
    "schwab": SchwabCSVAdapter,
}


# ---------------------------------------------------------------------------
# Path / database lifecycle helpers
# ---------------------------------------------------------------------------


def _paths_from_args(args: argparse.Namespace) -> UserPaths:
    """Resolve the `UserPaths` for a parsed CLI invocation.

    `--user` is required on every per-user command via argparse. For shared
    commands (`db init`, `fx update`) the value is the empty string and
    only the DB / shared paths on `UserPaths` are meaningful.
    """
    return UserPaths.for_user(getattr(args, "user", None) or "")


def _open_database(paths: UserPaths) -> Database:
    """Open (and migrate) the project database."""
    return Database.connect(paths.db_path)


def _ingestion_now() -> datetime:
    """The "current" time used for statement filing.

    Wrapped so tests can monkeypatch it to a frozen value without
    freezing every other clock in the module.
    """
    return datetime.now(UTC)


def _utc_timestamp() -> str:
    return _ingestion_now().isoformat().replace("+00:00", "Z")


def _today_iso() -> str:
    return _ingestion_now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Statement filing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Adapter pipeline
# ---------------------------------------------------------------------------


def build_adapter(name: str) -> StatementAdapter:
    """Instantiate a broker adapter by its name."""
    cls = ADAPTERS.get(name.lower())
    if not cls:
        raise ValueError(f"Unknown adapter '{name}'. Available: {', '.join(ADAPTERS)}")
    return cls()


def parse_statement(statement_path: Path, adapter_name: str):
    """Parse a broker statement at `statement_path` using the named adapter."""
    return build_adapter(adapter_name).parse_path(statement_path)


# ---------------------------------------------------------------------------
# ECB FX feed
# ---------------------------------------------------------------------------


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
    euro_reference_rates: Dict[str, float],
    currencies: Iterable[str],
) -> Dict[str, float]:
    """Convert ECB EUR-based quotes into GBP-per-currency rates.

    The ECB feed quotes everything against EUR. GBP-per-currency = (GBP per
    EUR) / (currency per EUR); EUR-per-currency is read directly from the
    feed.
    """
    gbp_per_eur = euro_reference_rates.get("GBP")
    if gbp_per_eur is None:
        raise ValueError("ECB FX payload does not include GBP")

    rates: Dict[str, float] = {}
    seen: set[str] = set()
    for raw_currency in currencies:
        currency = raw_currency.strip().upper()
        if not currency or currency == "GBP" or currency in seen:
            continue
        seen.add(currency)
        if currency == "EUR":
            gbp_per_currency = gbp_per_eur
        else:
            eur_to_currency = euro_reference_rates.get(currency)
            if eur_to_currency is None:
                raise ValueError(f"ECB FX payload does not include {currency}")
            gbp_per_currency = gbp_per_eur / eur_to_currency
        rates[currency] = round(gbp_per_currency, 6)
    return rates


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Plan tree helpers used by multiple commands
# ---------------------------------------------------------------------------


def _load_plan_tree_or_complain(paths: UserPaths) -> Optional[list[CategoryNode]]:
    """Return the user's DB-stored plan tree, or print a missing-plan error."""
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


def _iter_leaves(nodes: Iterable[CategoryNode]) -> Iterable[CategoryNode]:
    for node in nodes:
        if node.children:
            yield from _iter_leaves(node.children)
        else:
            yield node


# ---------------------------------------------------------------------------
# Commands: db
# ---------------------------------------------------------------------------


def cmd_db_init(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb db init` — create the database file and apply all migrations."""
    paths = paths if paths is not None else _paths_from_args(args)
    db = Database.connect(paths.db_path)
    try:
        version = int(db.connection.execute("PRAGMA user_version").fetchone()[0])
        print(f"DB initialised at {paths.db_path} (schema version {version})")
    finally:
        db.close()
    return 0


# ---------------------------------------------------------------------------
# Commands: fx
# ---------------------------------------------------------------------------


def cmd_fx_update(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb fx update` — fetch ECB rates and upsert into the `fx_rate` table.

    The set of currencies tracked comes from `--currency` (repeatable). If
    no flag is passed, every currency the DB already has at least one rate
    for is refreshed; if the DB is empty the command errors with a hint to
    pass `--currency` at least once.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    db = _open_database(paths)
    try:
        currencies: List[str]
        if args.currency:
            currencies = sorted(
                {
                    c.strip().upper()
                    for c in args.currency
                    if c.strip() and c.strip().upper() != "GBP"
                }
            )
        else:
            rows = db.connection.execute(
                "SELECT DISTINCT currency FROM fx_rate ORDER BY currency"
            ).fetchall()
            currencies = [str(row["currency"]) for row in rows]
        if not currencies:
            print(
                "No currencies to refresh. Pass `--currency USD --currency EUR …` at least once "
                "to seed the set.",
                file=sys.stderr,
            )
            return 1

        provider_date, euro_reference_rates = fetch_ecb_reference_rates()
        try:
            gbp_rates = derive_gbp_fx_rates(euro_reference_rates, currencies)
        except ValueError as exc:
            print(f"Failed to update FX rates: {exc}", file=sys.stderr)
            return 1

        db.connection.execute("BEGIN")
        try:
            for currency, gbp_rate in gbp_rates.items():
                repositories.upsert_fx_rate(
                    db.connection,
                    rate_date=provider_date,
                    currency=currency,
                    gbp_rate=gbp_rate,
                )
            db.connection.execute("COMMIT")
        except Exception:
            db.connection.execute("ROLLBACK")
            raise

        print(
            f"Updated FX rates for {len(gbp_rates)} currency(ies) dated {provider_date}: "
            f"{', '.join(sorted(gbp_rates))}"
        )
        return 0
    except (urllib.error.URLError, OSError) as exc:
        print(f"Failed to update FX rates: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Commands: portfolio
# ---------------------------------------------------------------------------


def cmd_portfolio_import(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb portfolio import` — parse a statement and upsert positions in the DB.

    Side effects:
    - Files the statement under `private/users/<u>/statements/<adapter>/<account>/<YYYY>/<MM>/`.
    - Inserts (or replaces by `(account, as_of)`) a `statement_import` row.
    - Inserts one `position` row per parsed holding, in the native currency.
    - Auto-creates `instrument` rows for any new ticker.
    - Reports any positions whose instrument has no `mapping` row at the end
      so the user can categorise them via `rb mapping add`.
    """
    paths = paths if paths is not None else _paths_from_args(args)

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

    parsed = parse_statement(canonical_statement, args.adapter)
    as_of = (args.as_of or _today_iso()).strip()

    db = _open_database(paths)
    try:
        user_id = repositories.find_or_create_user(db.connection, paths.user)
        source_id = repositories.get_source_id(db.connection, args.adapter)
        account_id = repositories.find_or_create_account(
            db.connection,
            user_id=user_id,
            source_id=source_id,
            name=args.account,
        )

        # Store the canonical statement path relative to the project root
        # so the row is portable across machines.
        try:
            rel_statement = str(canonical_statement.relative_to(paths.root.resolve()))
        except ValueError:
            rel_statement = str(canonical_statement)

        db.connection.execute("BEGIN")
        try:
            statement_import_id = repositories.replace_statement_import(
                db.connection,
                account_id=account_id,
                as_of=as_of,
                statement_path=rel_statement,
            )
            for inv in parsed:
                instrument_id = repositories.find_or_create_instrument(
                    db.connection,
                    source_id=source_id,
                    instrument_id_text=inv.instrument_id,
                    description=inv.description,
                )
                repositories.insert_position(
                    db.connection,
                    statement_import_id=statement_import_id,
                    instrument_id=instrument_id,
                    description=inv.description,
                    market_value_native=inv.market_value,
                    currency=inv.currency,
                )
            db.connection.execute("COMMIT")
        except Exception:
            db.connection.execute("ROLLBACK")
            raise

        unmapped_ids = repositories.list_unmapped_instrument_ids(db.connection, user_id=user_id)
        if unmapped_ids:
            print(
                f"Imported {len(parsed)} position(s) from {canonical_statement} into "
                f"({args.adapter}/{args.account}) as-of {as_of}; {len(unmapped_ids)} "
                "instrument(s) are uncategorised — add mappings with `rb mapping add`."
            )
        else:
            print(
                f"Imported {len(parsed)} position(s) from {canonical_statement} into "
                f"({args.adapter}/{args.account}) as-of {as_of}."
            )
        return 0
    finally:
        db.close()


def cmd_portfolio_report(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb portfolio report` — aggregate current positions against the plan.

    Reads positions from the `current_position` view (latest import per
    account), resolves each holding's mapping to the user's plan-leaf via
    `repositories.resolve_category_to_plan_leaf`, converts to GBP using the
    most recent `fx_rate` row at or before the import's `as_of`, and renders
    a summary table.
    """
    paths = paths if paths is not None else _paths_from_args(args)
    db = _open_database(paths)
    try:
        user_id = repositories.find_user_id(db.connection, paths.user)
        if user_id is None or not repositories.plan_exists(db.connection, user_id):
            print(
                f"No plan found for user '{paths.user}'. Run `rb plan create` first.",
                file=sys.stderr,
            )
            return 1

        plan_nodes = repositories.load_plan_tree(db.connection, user_id)
        plan = build_portfolio_plan_from_nodes(
            plan_nodes,
            default_leaf_volatility=DEFAULT_LEAF_VOLATILITY,
        )

        # Aggregate GBP value per plan-leaf node id.
        totals_by_plan_node: Dict[int, float] = defaultdict(float)
        # Track per-source GBP value for the secondary breakdown table.
        source_totals: Dict[str, float] = defaultdict(float)
        # Cache FX rates by (currency, as_of) to avoid repeated lookups.
        fx_cache: Dict[tuple[str, str], float] = {}
        uncategorised_value = 0.0
        uncategorised_tickers: set[str] = set()

        for position in repositories.iter_current_positions(db.connection, user_id=user_id):
            currency = position["currency"]
            as_of = position["as_of"]
            cache_key = (currency, as_of)
            if cache_key in fx_cache:
                rate = fx_cache[cache_key]
            else:
                resolved = repositories.latest_fx_rate_on_or_before(
                    db.connection, as_of=as_of, currency=currency
                )
                if resolved is None:
                    print(
                        f"Missing FX rate for {currency} on or before {as_of}; run `rb fx update`.",
                        file=sys.stderr,
                    )
                    return 1
                rate = resolved
                fx_cache[cache_key] = rate
            gbp_value = position["market_value_native"] * rate

            source_label = f"{position['adapter']}/{position['account_name']}"
            source_totals[source_label] += gbp_value

            mappings = repositories.get_mappings_for_instrument(
                db.connection, position["instrument_id"]
            )
            if not mappings:
                uncategorised_value += gbp_value
                uncategorised_tickers.add(position["instrument_id_text"])
                continue
            for category_id, weight_micros in mappings:
                weight_fraction = weight_micros / MICROS_SCALE
                share = gbp_value * weight_fraction
                plan_node_id = repositories.resolve_category_to_plan_leaf(
                    db.connection, user_id=user_id, category_id=category_id
                )
                if plan_node_id is None:
                    uncategorised_value += share
                    uncategorised_tickers.add(position["instrument_id_text"])
                    continue
                totals_by_plan_node[plan_node_id] += share

        # Materialise plan-leaves into rows the printer expects.
        leaf_info_by_path: Dict[str, dict] = {}
        for plan_node_id, gbp in totals_by_plan_node.items():
            info = repositories.get_plan_leaf_for_node(db.connection, plan_node_id=plan_node_id)
            info["actual_value"] = gbp
            leaf_info_by_path[info["path"]] = info

        total_value = sum(totals_by_plan_node.values()) + uncategorised_value

        # Compute risk / cash weights using plan targets.
        normalized_weights: Dict[str, float] = {}
        risk_over_vol: Dict[str, float] = {}
        for target in plan:
            path = target.path.label()
            normalized_weights[path] = target.target_weight
            risk_over_vol[path] = target.target_weight / target.volatility
        cash_weight_denominator = sum(risk_over_vol.values()) or 1.0

        summary_rows: List[Dict[str, float]] = []
        for target in plan:
            path = target.path.label()
            actual_value = leaf_info_by_path.get(path, {}).get("actual_value", 0.0)
            actual_weight = (actual_value / total_value) if total_value else 0.0
            cash_weight = risk_over_vol[path] / cash_weight_denominator
            target_value = cash_weight * total_value
            summary_rows.append(
                {
                    "path": path,
                    "label": path,
                    "risk_weight_raw": target.risk_weight,
                    "risk_weight_normalized": normalized_weights[path],
                    "adjustment": getattr(target, "adjustment", 1.0),
                    "volatility": target.volatility,
                    "cash_weight": cash_weight,
                    "actual_value": actual_value,
                    "actual_weight": actual_weight,
                    "target_value": target_value,
                    "target_weight": cash_weight,
                }
            )

        total_positions = sum(
            1 for _ in repositories.iter_current_positions(db.connection, user_id=user_id)
        )
        print(f"Loaded {total_positions} position(s) for user '{paths.user}'")
        if uncategorised_tickers:
            print(
                f"Warning: {len(uncategorised_tickers)} instrument(s) uncategorised "
                f"(£{uncategorised_value:,.2f}): {', '.join(sorted(uncategorised_tickers))}"
            )
        print_summary_table(total_value, summary_rows)
        print()
        source_rows = sorted(source_totals.items(), key=lambda item: (-item[1], item[0]))
        print_source_breakdown(sum(source_totals.values()), source_rows)

        export_path = _resolve_export_path(args, paths)
        if export_path is not None:
            export_summary_to_csv(export_path, summary_rows)
            print(f"Wrote summary to {export_path}")
        return 0
    finally:
        db.close()


def _resolve_export_path(args: argparse.Namespace, paths: UserPaths) -> Optional[Path]:
    """Return the CSV export destination, or None if no export was requested."""
    raw = getattr(args, "export", None)
    if raw is None:
        return None
    if isinstance(raw, str) and raw and raw != "__default__":
        return Path(raw)
    today = _today_iso()
    return paths.reports_dir / f"{today}.csv"


# ---------------------------------------------------------------------------
# Commands: plan
# ---------------------------------------------------------------------------


def cmd_plan_create(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan create` — interactive walker (or `--from <peer>` clone)."""
    paths = paths if paths is not None else _paths_from_args(args)
    overwrite = bool(getattr(args, "overwrite", False))
    db = _open_database(paths)
    try:
        user_id = repositories.find_or_create_user(db.connection, paths.user)
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
                print(f"Source user '{source_user}' has no plan in the database.", file=sys.stderr)
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
        if not catalog:
            print(
                "No categories defined. Run `rb portfolio import` against a statement "
                "(which auto-creates instruments and prompts for categories) or have "
                "an existing user share their plan via `rb plan create --from <peer>`.",
                file=sys.stderr,
            )
            return 1
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
    """Print the plan summary and ask for y/N confirmation."""
    from .plan_bootstrap import _render_plan_tree

    io.info("\n—— Plan summary ——")
    io.info(_render_plan_tree(plan_nodes))
    return _prompt_yes_no(
        io,
        f"\nSave this plan for user '{user}'? [y/N]: ",
        default=False,
    )


def cmd_plan_validate(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan validate` — verify sibling weight totals sum to 100%."""
    paths = paths if paths is not None else _paths_from_args(args)
    db_nodes = _load_plan_tree_or_complain(paths)
    if db_nodes is None:
        return 1
    failures = collect_category_weight_validation_failures(db_nodes)
    if failures:
        print(format_category_weight_validation_failures(failures), file=sys.stderr)
        return 1
    print(f"Plan for user '{paths.user}' is valid.")
    return 0


def cmd_plan_list(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan list` — print every leaf's weight, volatility, and adjustment."""
    paths = paths if paths is not None else _paths_from_args(args)
    nodes = _load_plan_tree_or_complain(paths)
    if nodes is None:
        return 1
    print(render_list(list(iter_leaf_nodes(nodes))))
    return 0


def cmd_plan_adjust(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan adjust` — review or change leaf adjustments on a user's plan."""
    paths = paths if paths is not None else _paths_from_args(args)
    under = getattr(args, "under", None)
    path_label = getattr(args, "path", None)
    value = getattr(args, "value", None)
    skip_confirm = bool(getattr(args, "yes", False))

    if path_label is not None and value is None:
        print(
            "plan adjust: a positional path requires a value (e.g. "
            '`plan adjust "Bonds / Developed > UK > Govt" 0.95`)',
            file=sys.stderr,
        )
        return 1
    if path_label is None and value is not None:
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
            parts = tuple(
                part.strip() for part in path_label.replace(">", "/").split("/") if part.strip()
            )
            assert value is not None
            try:
                change = apply_targeted(nodes, parts, float(value))
            except ValueError as exc:
                print(f"plan adjust failed: {exc}", file=sys.stderr)
                return 1
            try:
                should_write = confirm_changes(
                    paths.db_path, [change], io, skip_prompt=skip_confirm
                )
            except PlanCreationAborted as exc:
                print(f"plan adjust aborted: {exc}", file=sys.stderr)
                return 1
            if not should_write:
                print("plan adjust aborted: user declined.")
                return 0
            repositories.write_plan_tree(db.connection, user_id, nodes)
            print(f"Wrote updated plan for user '{paths.user}' (1 leaf changed).")
            return 0

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
            should_write = confirm_changes(paths.db_path, changes, io)
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
    """`rb plan export` — write the user's plan as a depth-column CSV."""
    paths = paths if paths is not None else _paths_from_args(args)
    nodes = _load_plan_tree_or_complain(paths)
    if nodes is None:
        return 1

    out_path: Optional[Path] = getattr(args, "out", None)
    if out_path is None:
        write_plan_csv(nodes, sys.stdout)
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        write_plan_csv(nodes, handle)
    print(f"Wrote plan CSV to {out_path}")
    return 0


def cmd_plan_import(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb plan import` — replace the user's plan from a depth-column CSV."""
    paths = paths if paths is not None else _paths_from_args(args)
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

    failures = collect_category_weight_validation_failures(new_nodes)
    if failures:
        print(format_category_weight_validation_failures(failures), file=sys.stderr)
        return 2

    skip_confirm = bool(getattr(args, "yes", False))
    io: IO = StdIO()

    db = _open_database(paths)
    try:
        user_id = repositories.find_or_create_user(db.connection, paths.user)

        try:
            fill_missing_leaf_vol_adj(db.connection, new_nodes, io)
        except PlanCreationAborted as exc:
            print(f"plan import aborted: {exc}", file=sys.stderr)
            return 1

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
    """Render a leaves-added/removed/changed summary between two plan trees."""
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
    """Return `{path: (cumulative_weight, resolved_volatility, adjustment)}`."""
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


# ---------------------------------------------------------------------------
# Commands: user
# ---------------------------------------------------------------------------


def cmd_user_list(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb user list` — show every user in the DB with a plan summary."""
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
    """`rb user create` — insert a user row and create their on-disk directory."""
    paths = paths if paths is not None else _paths_from_args(args)
    db = _open_database(paths)
    try:
        if repositories.find_user_id(db.connection, paths.user) is not None:
            print(
                f"User '{paths.user}' already exists. Use "
                f"`riskbalancer user delete --user {paths.user} --confirm` first.",
                file=sys.stderr,
            )
            return 1
        repositories.create_user(db.connection, paths.user)
    finally:
        db.close()
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    print(f"Created user '{paths.user}' (DB row + {paths.user_dir}).")
    print(f"Next: riskbalancer plan create --user {paths.user} (or --from <peer>)")
    return 0


def cmd_user_delete(args: argparse.Namespace, paths: Optional[UserPaths] = None) -> int:
    """`rb user delete` — cascade-delete the user's DB rows and on-disk dir."""
    paths = paths if paths is not None else _paths_from_args(args)
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


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def _user_parent_parser() -> argparse.ArgumentParser:
    """Argparse parent that requires `--user` on every command that uses it."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--user", required=True, help="User name (required).")
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RiskBalancer CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    user_parent = _user_parent_parser()

    # db — shared; no --user.
    db_parser = subparsers.add_parser("db", help="Manage the project database")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    db_init = db_sub.add_parser(
        "init",
        help="Create private/riskbalancer.db and apply pending migrations",
    )
    db_init.set_defaults(func=cmd_db_init)

    # fx — shared; no --user.
    fx_parser = subparsers.add_parser("fx", help="Manage FX rate data")
    fx_sub = fx_parser.add_subparsers(dest="fx_command", required=True)
    fx_update = fx_sub.add_parser(
        "update",
        help="Fetch ECB reference rates and upsert into the fx_rate table",
    )
    fx_update.add_argument(
        "--currency",
        action="append",
        help=(
            "Currency code to refresh; repeat for multiple. Defaults to every "
            "currency already in the fx_rate table."
        ),
    )
    fx_update.set_defaults(func=cmd_fx_update)

    # portfolio — user-keyed.
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
            "re-imports against the same (adapter, account, as-of) replace the prior "
            "import in a single transaction."
        ),
    )
    portfolio_import.add_argument("--statement", required=True, help="Path to broker CSV statement")
    portfolio_import.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Statement as-of date (YYYY-MM-DD). Defaults to today.",
    )
    portfolio_import.add_argument(
        "--move",
        action="store_true",
        help="Remove the source statement after copying it into the user's statements tree.",
    )
    portfolio_import.set_defaults(func=cmd_portfolio_import)

    report = portfolio_sub.add_parser(
        "report",
        parents=[user_parent],
        help="Analyze the user's current portfolio against their plan",
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

    # plan — per-user CRUD.
    plan_parser = subparsers.add_parser("plan", help="Manage user category plans")
    plan_sub = plan_parser.add_subparsers(dest="plan_command", required=True)

    plan_create = plan_sub.add_parser(
        "create",
        parents=[user_parent],
        help="Bootstrap a new plan for the user, either interactively or by cloning",
    )
    plan_create.add_argument(
        "--from",
        dest="from_user",
        help="Clone the plan from another user instead of walking the catalog interactively",
    )
    plan_create.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace the user's existing plan if one is already in the DB",
    )
    plan_create.set_defaults(func=cmd_plan_create)

    plan_validate = plan_sub.add_parser(
        "validate",
        parents=[user_parent],
        help="Validate sibling weight totals; exits 0 on success, 1 on failure",
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
        help="Skip the y/N confirm before replacing the existing plan.",
    )
    plan_import.set_defaults(func=cmd_plan_import)

    # user — manage which users exist.
    user_parser = subparsers.add_parser("user", help="Manage users")
    user_sub = user_parser.add_subparsers(dest="user_command", required=True)
    user_list = user_sub.add_parser("list", help="List stored users")
    user_list.set_defaults(func=cmd_user_list)
    user_create = user_sub.add_parser(
        "create",
        parents=[user_parent],
        help="Create a user row in the DB and an on-disk directory under private/users/",
    )
    user_create.set_defaults(func=cmd_user_create)
    user_delete = user_sub.add_parser(
        "delete",
        parents=[user_parent],
        help="Remove a user's DB rows and on-disk directory",
    )
    user_delete.add_argument(
        "--confirm",
        action="store_true",
        help="Required acknowledgement: this wipes the entire user directory and DB rows",
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
