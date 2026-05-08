import argparse
import json
from dataclasses import replace
from pathlib import Path

import riskbalancer.cli as cli_module
from riskbalancer import CategoryPath, CategoryTarget, Investment
from riskbalancer.cli import (
    CategoryAllocation,
    InstrumentMapping,
    build_parser,
    cmd_portfolio_add_instrument,
    cmd_portfolio_create,
    cmd_portfolio_import,
    cmd_portfolio_report,
    investment_from_dict,
    investment_to_dict,
    load_fx_rates,
    load_layered_mappings,
    resolve_mapping_path,
    save_mappings,
    summarize_portfolio,
)
from riskbalancer.paths import UserPaths
from riskbalancer.portfolio import PortfolioPlan

AJ_BELL_FIXTURE = Path("tests/fixtures/aj_bell_sample.csv")


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _paths_with(**overrides) -> UserPaths:
    return replace(UserPaths.for_user(""), **overrides)


def test_resolve_mapping_path_defaults_by_adapter():
    path = resolve_mapping_path("ajbell")
    assert path == Path("config/mappings/ajbell.yaml")


def test_load_layered_mappings_user_override_wins(tmp_path, capsys):
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    overrides_dir = tmp_path / "overrides"
    overrides_dir.mkdir()
    paths = _paths_with(
        shared_mappings_dir=shared_dir,
        overrides_dir=overrides_dir,
    )

    nam = CategoryPath("Equities", "Developed", "NAM")
    em = CategoryPath("Equities", "EM")
    save_mappings(
        paths.adapter_mappings_path("ajbell"),
        {
            "VWRL": InstrumentMapping(allocations=[CategoryAllocation(path=nam, weight=1.0)]),
            "VHYL": InstrumentMapping(allocations=[CategoryAllocation(path=nam, weight=1.0)]),
        },
    )
    save_mappings(
        paths.adapter_overrides_path("ajbell"),
        {
            # Override an existing shared key
            "VWRL": InstrumentMapping(allocations=[CategoryAllocation(path=em, weight=1.0)]),
            # Add a key not in the shared file
            "ICAP": InstrumentMapping(allocations=[CategoryAllocation(path=em, weight=1.0)]),
        },
    )

    merged = load_layered_mappings("ajbell", paths, log_overrides=True)
    err = capsys.readouterr().err

    # Override wins for VWRL
    assert merged["VWRL"].allocations[0].path.levels() == ("Equities", "EM")
    # Shared retained for VHYL
    assert merged["VHYL"].allocations[0].path.levels() == ("Equities", "Developed", "NAM")
    # Override-only key is included
    assert merged["ICAP"].allocations[0].path.levels() == ("Equities", "EM")
    # The override notice is emitted only for keys that existed in shared
    assert "Using user override for VWRL" in err
    assert "ICAP" not in err


def test_investment_serialization_round_trip_preserves_source_id():
    investment = Investment(
        instrument_id="ETF",
        description="Global ETF",
        market_value=1000.0,
        quantity=10.0,
        category=CategoryPath("Equities", "Developed", "NAM"),
        volatility=0.2,
        source="aj_bell",
        source_id="ajbell-sipp",
    )
    payload = investment_to_dict(investment)
    restored = investment_from_dict(payload)
    assert restored.instrument_id == investment.instrument_id
    assert restored.market_value == investment.market_value
    assert restored.category.levels() == investment.category.levels()
    assert restored.quantity == 10.0
    assert restored.source_id == "ajbell-sipp"


def test_cmd_portfolio_create_creates_empty_snapshot(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    paths = _paths_with(portfolio=portfolio_path)
    result = cmd_portfolio_create(
        argparse.Namespace(plan="config/seed_plan.yaml", overwrite=False),
        paths=paths,
    )
    assert result == 0

    snapshot = load_snapshot(portfolio_path)
    assert snapshot["plan"] == "config/seed_plan.yaml"
    assert snapshot["imports"] == []
    assert snapshot["investments"] == []
    assert snapshot["created_at"] == snapshot["updated_at"]


def test_build_parser_uses_user_flag_and_drops_portfolio():
    parser = build_parser()

    # `--user` is recognised on user-keyed subcommands.
    import_args = parser.parse_args(
        [
            "portfolio",
            "import",
            "--user",
            "demo",
            "--source-id",
            "ajbell-sipp",
            "--adapter",
            "ajbell",
            "--statement",
            str(AJ_BELL_FIXTURE),
        ]
    )
    assert import_args.source_id == "ajbell-sipp"
    assert import_args.user == "demo"

    add_args = parser.parse_args(
        [
            "portfolio",
            "add",
            "--user",
            "demo",
            "--instrument-id",
            "CASH_GBP",
            "--description",
            "GBP Cash",
            "--market-value",
            "1000",
        ]
    )
    assert add_args.category is None
    assert add_args.user == "demo"

    # `portfolio create` is removed; the user/portfolio is auto-initialised
    # by `portfolio import` or `portfolio add`.
    try:
        parser.parse_args(["portfolio", "create", "--user", "demo"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("portfolio create should no longer parse")

    # `--portfolio` is gone everywhere.
    try:
        parser.parse_args(["portfolio", "report", "--portfolio", "demo"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("--portfolio should no longer parse")

    # `user list` and `user delete` exist.
    list_args = parser.parse_args(["user", "list"])
    assert list_args.user_command == "list"
    delete_args = parser.parse_args(["user", "delete", "--user", "demo", "--confirm"])
    assert delete_args.user == "demo"
    assert delete_args.confirm is True


def test_cmd_portfolio_import_prompts_and_persists_missing_mappings(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "portfolio.json"
    mapping_path = tmp_path / "ajbell.yaml"
    paths = _paths_with(portfolio=portfolio_path)
    cmd_portfolio_create(
        argparse.Namespace(plan="config/seed_plan.yaml", overwrite=False),
        paths=paths,
    )

    def fake_gather_missing(missing, *, plan_index, input_func=None):
        target = plan_index.resolve("Equities / Developed / NAM")
        assert target is not None
        return {
            instrument: InstrumentMapping(allocations=[CategoryAllocation(path=target, weight=1.0)])
            for instrument in missing
        }

    monkeypatch.setattr(cli_module, "gather_missing_mappings", fake_gather_missing)

    result = cmd_portfolio_import(
        argparse.Namespace(
            source_id="ajbell-sipp",
            adapter="ajbell",
            statement=str(AJ_BELL_FIXTURE),
            mappings=str(mapping_path),
            fx=None,
        ),
        paths=paths,
    )
    assert result == 0

    snapshot = load_snapshot(portfolio_path)
    assert len(snapshot["imports"]) == 1
    assert snapshot["imports"][0]["source_id"] == "ajbell-sipp"
    assert snapshot["imports"][0]["adapter"] == "ajbell"
    assert snapshot["imports"][0]["mappings"] == str(mapping_path)
    assert len(snapshot["investments"]) == 3
    assert all(entry["source_id"] == "ajbell-sipp" for entry in snapshot["investments"])
    assert mapping_path.exists()


def test_cmd_portfolio_import_replaces_existing_source_id(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "portfolio.json"
    mapping_path = tmp_path / "broker.yaml"
    paths = _paths_with(portfolio=portfolio_path)
    cmd_portfolio_create(
        argparse.Namespace(plan="config/seed_plan.yaml", overwrite=False),
        paths=paths,
    )
    save_mappings(
        mapping_path,
        {
            "AAA": InstrumentMapping(
                allocations=[
                    CategoryAllocation(
                        path=CategoryPath("Equities", "Developed", "NAM"),
                        weight=1.0,
                    )
                ]
            )
        },
    )

    statements = iter(
        [
            [
                Investment(
                    instrument_id="AAA",
                    description="First Import",
                    market_value=100.0,
                    category=CategoryPath("Uncategorized", "Pending Review"),
                    volatility=0.2,
                    source="ajbell",
                )
            ],
            [
                Investment(
                    instrument_id="AAA",
                    description="Replacement Import",
                    market_value=250.0,
                    category=CategoryPath("Uncategorized", "Pending Review"),
                    volatility=0.2,
                    source="ajbell",
                )
            ],
        ]
    )

    monkeypatch.setattr(
        cli_module,
        "parse_statement",
        lambda statement_path, adapter_name, fx_rates=None: next(statements),
    )

    args = argparse.Namespace(
        source_id="ajbell-sipp",
        adapter="ajbell",
        statement="statement.csv",
        mappings=str(mapping_path),
        fx=None,
    )
    cmd_portfolio_import(args, paths=paths)
    cmd_portfolio_import(args, paths=paths)

    snapshot = load_snapshot(portfolio_path)
    assert len(snapshot["imports"]) == 1
    assert len(snapshot["investments"]) == 1
    assert snapshot["investments"][0]["market_value"] == 250.0
    assert snapshot["investments"][0]["source_id"] == "ajbell-sipp"


def test_cmd_portfolio_import_preserves_other_sources(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "portfolio.json"
    mapping_path = tmp_path / "broker.yaml"
    paths = _paths_with(portfolio=portfolio_path)
    cmd_portfolio_create(
        argparse.Namespace(plan="config/seed_plan.yaml", overwrite=False),
        paths=paths,
    )
    save_mappings(
        mapping_path,
        {
            "AAA": InstrumentMapping(
                allocations=[
                    CategoryAllocation(
                        path=CategoryPath("Equities", "Developed", "NAM"),
                        weight=1.0,
                    )
                ]
            ),
            "BBB": InstrumentMapping(
                allocations=[
                    CategoryAllocation(
                        path=CategoryPath("Bonds", "Developed", "NAM", "Govt"),
                        weight=1.0,
                    )
                ]
            ),
        },
    )

    statements = iter(
        [
            [
                Investment(
                    instrument_id="AAA",
                    description="Source A",
                    market_value=100.0,
                    category=CategoryPath("Uncategorized", "Pending Review"),
                    volatility=0.2,
                    source="ajbell",
                )
            ],
            [
                Investment(
                    instrument_id="BBB",
                    description="Source B",
                    market_value=200.0,
                    category=CategoryPath("Uncategorized", "Pending Review"),
                    volatility=0.2,
                    source="ibkr",
                )
            ],
        ]
    )

    monkeypatch.setattr(
        cli_module,
        "parse_statement",
        lambda statement_path, adapter_name, fx_rates=None: next(statements),
    )

    cmd_portfolio_import(
        argparse.Namespace(
            source_id="source-a",
            adapter="ajbell",
            statement="a.csv",
            mappings=str(mapping_path),
            fx=None,
        ),
        paths=paths,
    )
    cmd_portfolio_import(
        argparse.Namespace(
            source_id="source-b",
            adapter="ibkr",
            statement="b.csv",
            mappings=str(mapping_path),
            fx=None,
        ),
        paths=paths,
    )

    snapshot = load_snapshot(portfolio_path)
    assert {entry["source_id"] for entry in snapshot["investments"]} == {"source-a", "source-b"}
    assert len(snapshot["imports"]) == 2


def test_cmd_portfolio_add_with_explicit_category_splits_investment(tmp_path):
    portfolio_path = tmp_path / "portfolio.json"
    manual_mapping_path = tmp_path / "manual.yaml"
    paths = _paths_with(portfolio=portfolio_path, manual_mappings=manual_mapping_path)
    cmd_portfolio_create(
        argparse.Namespace(plan="config/seed_plan.yaml", overwrite=False),
        paths=paths,
    )

    result = cmd_portfolio_add_instrument(
        argparse.Namespace(
            instrument_id="GOLD",
            description="Physical Gold",
            market_value=1000.0,
            category="Alternative / Gold=60, Cash=40",
        ),
        paths=paths,
    )
    assert result == 0

    snapshot = load_snapshot(portfolio_path)
    assert len(snapshot["investments"]) == 2
    assert sorted(entry["market_value"] for entry in snapshot["investments"]) == [400.0, 600.0]
    assert manual_mapping_path.exists()


def test_cmd_portfolio_add_without_category_reuses_manual_mapping(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "portfolio.json"
    manual_mapping_path = tmp_path / "manual.yaml"
    paths = _paths_with(portfolio=portfolio_path, manual_mappings=manual_mapping_path)
    cmd_portfolio_create(
        argparse.Namespace(plan="config/seed_plan.yaml", overwrite=False),
        paths=paths,
    )

    call_count = {"value": 0}

    def fake_gather_missing(missing, *, plan_index, input_func=None):
        call_count["value"] += 1
        target = plan_index.resolve("Cash")
        assert target is not None
        return {
            instrument: InstrumentMapping(allocations=[CategoryAllocation(path=target, weight=1.0)])
            for instrument in missing
        }

    monkeypatch.setattr(cli_module, "gather_missing_mappings", fake_gather_missing)

    args = argparse.Namespace(
        instrument_id="CASH1",
        description="Cash Position",
        market_value=500.0,
        category=None,
    )
    cmd_portfolio_add_instrument(args, paths=paths)
    cmd_portfolio_add_instrument(args, paths=paths)

    snapshot = load_snapshot(portfolio_path)
    assert call_count["value"] == 1
    assert len(snapshot["investments"]) == 2
    assert all(entry["category"] == "Cash" for entry in snapshot["investments"])


def test_cmd_portfolio_report_reads_legacy_snapshot_without_imports(tmp_path, capsys):
    portfolio_path = tmp_path / "legacy.json"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": "config/seed_plan.yaml",
                "created_at": "2026-03-23T12:00:00Z",
                "investments": [
                    {
                        "instrument_id": "ETF",
                        "description": "Legacy ETF",
                        "market_value": 1000.0,
                        "category": "Equities / Developed / NAM",
                        "volatility": 0.2,
                        "source": "legacy",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cmd_portfolio_report(
        argparse.Namespace(plan=None, export=None),
        paths=_paths_with(portfolio=portfolio_path),
    )
    assert result == 0
    captured = capsys.readouterr()
    assert "Loaded 1 investments" in captured.out
    assert "Source Breakdown (GBP)" in captured.out
    assert "legacy" in captured.out


def test_cmd_portfolio_report_rejects_invalid_top_level_weights(tmp_path, capsys):
    plan_path = tmp_path / "invalid-root.yaml"
    plan_path.write_text(
        """
assets:
  - name: Equities
    weight: 0.6
    volatility: 0.2
  - name: Bonds
    weight: 0.6
    volatility: 0.1
""",
        encoding="utf-8",
    )
    portfolio_path = tmp_path / "invalid-root.json"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": str(plan_path),
                "created_at": "2026-03-23T12:00:00Z",
                "investments": [
                    {
                        "instrument_id": "ETF",
                        "description": "Holding",
                        "market_value": 1000.0,
                        "category": "Equities",
                        "volatility": 0.2,
                        "source": "legacy",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cmd_portfolio_report(
        argparse.Namespace(plan=None, export=None),
        paths=_paths_with(portfolio=portfolio_path),
    )

    assert result == 1
    captured = capsys.readouterr()
    assert "Category weight validation failed:" in captured.err
    assert "root assets totals 120.00%" in captured.err
    assert "Loaded 1 investments" not in captured.out
    assert "Source Breakdown (GBP)" not in captured.out


def test_cmd_portfolio_report_rejects_invalid_nested_weights_and_skips_export(tmp_path, capsys):
    plan_path = tmp_path / "invalid-nested.yaml"
    export_path = tmp_path / "report.csv"
    plan_path.write_text(
        """
assets:
  - name: Equities
    weight: 1.0
    children:
      - name: Developed
        weight: 0.7
        volatility: 0.2
      - name: EM
        weight: 0.2
        volatility: 0.25
""",
        encoding="utf-8",
    )
    portfolio_path = tmp_path / "invalid-nested.json"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": str(plan_path),
                "created_at": "2026-03-23T12:00:00Z",
                "investments": [
                    {
                        "instrument_id": "ETF",
                        "description": "Holding",
                        "market_value": 1000.0,
                        "category": "Equities / Developed",
                        "volatility": 0.2,
                        "source": "legacy",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cmd_portfolio_report(
        argparse.Namespace(plan=None, export=str(export_path)),
        paths=_paths_with(portfolio=portfolio_path),
    )

    assert result == 1
    captured = capsys.readouterr()
    assert "Category weight validation failed:" in captured.err
    assert "Equities totals 90.00%" in captured.err
    assert "Category" not in captured.out
    assert "Source Breakdown (GBP)" not in captured.out
    assert not export_path.exists()


def test_cmd_portfolio_report_lists_all_validation_failures(tmp_path, capsys):
    plan_path = tmp_path / "invalid-multiple.yaml"
    plan_path.write_text(
        """
assets:
  - name: Equities
    weight: 0.7
    children:
      - name: Developed
        weight: 0.6
        volatility: 0.2
      - name: EM
        weight: 0.2
        volatility: 0.25
  - name: Bonds
    weight: 0.4
    volatility: 0.1
""",
        encoding="utf-8",
    )
    portfolio_path = tmp_path / "invalid-multiple.json"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": str(plan_path),
                "created_at": "2026-03-23T12:00:00Z",
                "investments": [
                    {
                        "instrument_id": "ETF",
                        "description": "Holding",
                        "market_value": 1000.0,
                        "category": "Equities / Developed",
                        "volatility": 0.2,
                        "source": "legacy",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cmd_portfolio_report(
        argparse.Namespace(plan=None, export=None),
        paths=_paths_with(portfolio=portfolio_path),
    )

    assert result == 1
    captured = capsys.readouterr()
    assert "root assets totals 110.00%" in captured.err
    assert "Equities totals 80.00%" in captured.err


def test_cmd_portfolio_import_adds_import_metadata_to_legacy_snapshot(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "legacy.json"
    mapping_path = tmp_path / "broker.yaml"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": "config/seed_plan.yaml",
                "created_at": "2026-03-23T12:00:00Z",
                "investments": [
                    {
                        "instrument_id": "MANUAL",
                        "description": "Existing Holding",
                        "market_value": 500.0,
                        "category": "Cash",
                        "volatility": 0.15,
                        "source": "manual",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    save_mappings(
        mapping_path,
        {
            "AAA": InstrumentMapping(
                allocations=[
                    CategoryAllocation(
                        path=CategoryPath("Equities", "Developed", "NAM"),
                        weight=1.0,
                    )
                ]
            )
        },
    )
    monkeypatch.setattr(
        cli_module,
        "parse_statement",
        lambda statement_path, adapter_name, fx_rates=None: [
            Investment(
                instrument_id="AAA",
                description="Imported Holding",
                market_value=250.0,
                category=CategoryPath("Uncategorized", "Pending Review"),
                volatility=0.2,
                source="ajbell",
            )
        ],
    )

    result = cmd_portfolio_import(
        argparse.Namespace(
            source_id="ajbell-sipp",
            adapter="ajbell",
            statement="statement.csv",
            mappings=str(mapping_path),
            fx=None,
        ),
        paths=_paths_with(portfolio=portfolio_path),
    )

    assert result == 0
    snapshot = load_snapshot(portfolio_path)
    assert len(snapshot["imports"]) == 1
    assert snapshot["imports"][0]["source_id"] == "ajbell-sipp"
    assert {entry["source"] for entry in snapshot["investments"]} == {"manual", "ajbell"}
    assert {entry.get("source_id") for entry in snapshot["investments"]} == {None, "ajbell-sipp"}


def test_cmd_portfolio_report_prints_source_breakdown_and_keeps_csv_unchanged(tmp_path, capsys):
    portfolio_path = tmp_path / "report.json"
    export_path = tmp_path / "report.csv"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": "config/seed_plan.yaml",
                "created_at": "2026-03-23T12:00:00Z",
                "imports": [
                    {
                        "source_id": "ibkr-taxable",
                        "adapter": "ibkr",
                        "statement": "ibkr.csv",
                        "mappings": "config/mappings/ibkr.yaml",
                        "imported_at": "2026-03-23T12:00:00Z",
                    },
                    {
                        "source_id": "ajbell-sipp",
                        "adapter": "ajbell",
                        "statement": "ajbell.csv",
                        "mappings": "config/mappings/ajbell.yaml",
                        "imported_at": "2026-03-23T12:01:00Z",
                    },
                ],
                "investments": [
                    {
                        "instrument_id": "AAA",
                        "description": "IBKR Holding",
                        "market_value": 700.0,
                        "category": "Equities / Developed / NAM",
                        "volatility": 0.2,
                        "source": "ibkr",
                        "source_id": "ibkr-taxable",
                    },
                    {
                        "instrument_id": "BBB",
                        "description": "AJ Bell Holding",
                        "market_value": 500.0,
                        "category": "Equities / Developed / Europe",
                        "volatility": 0.25,
                        "source": "aj_bell",
                        "source_id": "ajbell-sipp",
                    },
                    {
                        "instrument_id": "CASH1",
                        "description": "Manual Cash",
                        "market_value": 300.0,
                        "category": "Cash",
                        "volatility": 0.15,
                        "source": "manual",
                    },
                    {
                        "instrument_id": "GOLD",
                        "description": "Manual Gold",
                        "market_value": 200.0,
                        "category": "Alternative / Gold",
                        "volatility": 0.15,
                        "source": "manual",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cmd_portfolio_report(
        argparse.Namespace(plan=None, export=str(export_path)),
        paths=_paths_with(portfolio=portfolio_path),
    )

    assert result == 0
    captured = capsys.readouterr()
    assert "Source Breakdown (GBP)" in captured.out
    assert "ibkr-taxable" in captured.out
    assert "ajbell-sipp" in captured.out
    assert "manual" in captured.out
    assert "1,700.00" in captured.out
    csv_output = export_path.read_text(encoding="utf-8")
    assert "Category,RiskWeightRaw" in csv_output
    assert "Source Breakdown" not in csv_output


def test_cmd_portfolio_report_source_breakdown_sorts_by_value(tmp_path, capsys):
    portfolio_path = tmp_path / "sorting.json"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": "config/seed_plan.yaml",
                "created_at": "2026-03-23T12:00:00Z",
                "investments": [
                    {
                        "instrument_id": "AAA",
                        "description": "Small Imported",
                        "market_value": 100.0,
                        "category": "Equities / Developed / NAM",
                        "volatility": 0.2,
                        "source": "ajbell",
                        "source_id": "small-source",
                    },
                    {
                        "instrument_id": "BBB",
                        "description": "Large Imported",
                        "market_value": 900.0,
                        "category": "Equities / Developed / Europe",
                        "volatility": 0.25,
                        "source": "ibkr",
                        "source_id": "large-source",
                    },
                    {
                        "instrument_id": "CCC",
                        "description": "Manual Cash",
                        "market_value": 400.0,
                        "category": "Cash",
                        "volatility": 0.15,
                        "source": "manual",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = cmd_portfolio_report(
        argparse.Namespace(plan=None, export=None),
        paths=_paths_with(portfolio=portfolio_path),
    )

    assert result == 0
    captured = capsys.readouterr()
    large_idx = captured.out.index("large-source")
    manual_idx = captured.out.index("manual")
    small_idx = captured.out.index("small-source")
    assert large_idx < manual_idx < small_idx


def test_summarize_portfolio_calculates_cash_and_targets():
    plan = PortfolioPlan(
        [
            CategoryTarget(
                path=CategoryPath("Equities", "Developed", "NAM"),
                normalized_risk_weight=0.6,
                volatility=0.2,
                risk_weight=0.3,
            ),
            CategoryTarget(
                path=CategoryPath("Equities", "Developed", "Europe"),
                normalized_risk_weight=0.4,
                volatility=0.25,
                risk_weight=0.2,
            ),
        ]
    )
    investments = [
        Investment(
            instrument_id="ETF",
            description="World ETF",
            market_value=600.0,
            category=CategoryPath("Equities", "Developed", "NAM"),
            volatility=0.2,
        ),
        Investment(
            instrument_id="ETF2",
            description="World ETF 2",
            market_value=400.0,
            category=CategoryPath("Equities", "Developed", "Europe"),
            volatility=0.25,
        ),
    ]

    total_value, summary = summarize_portfolio(plan, investments)
    assert total_value == 1000.0
    by_label = {row["label"]: row for row in summary}
    assert by_label["Equities / Developed / NAM"]["actual_value"] == 600.0
    assert by_label["Equities / Developed / Europe"]["actual_value"] == 400.0
    assert abs(sum(row["cash_weight"] for row in summary) - 1.0) < 1e-9


def test_load_fx_rates(tmp_path):
    fx_file = tmp_path / "fx.yaml"
    fx_file.write_text(
        "base: GBP\nrates:\n  USD: 0.8\n  EUR: 0.9\n",
        encoding="utf-8",
    )
    rates = load_fx_rates(str(fx_file))
    assert rates["USD"] == 0.8
