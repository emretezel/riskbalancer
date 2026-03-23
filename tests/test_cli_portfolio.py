import argparse
import json
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
    resolve_mapping_path,
    save_mappings,
    summarize_portfolio,
)
from riskbalancer.portfolio import PortfolioPlan

AJ_BELL_FIXTURE = Path("tests/fixtures/aj_bell_sample.csv")


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_resolve_mapping_path_defaults_by_adapter():
    path = resolve_mapping_path("ajbell")
    assert path == Path("config/mappings/ajbell.yaml")


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
    result = cmd_portfolio_create(
        argparse.Namespace(
            portfolio=str(portfolio_path),
            plan="config/categories.yaml",
            overwrite=False,
        )
    )
    assert result == 0

    snapshot = load_snapshot(portfolio_path)
    assert snapshot["plan"] == "config/categories.yaml"
    assert snapshot["imports"] == []
    assert snapshot["investments"] == []
    assert snapshot["created_at"] == snapshot["updated_at"]


def test_build_parser_replaces_build_with_create_and_import():
    parser = build_parser()
    create_args = parser.parse_args(["portfolio", "create", "--portfolio", "demo"])
    assert create_args.portfolio == "demo"

    import_args = parser.parse_args(
        [
            "portfolio",
            "import",
            "--portfolio",
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

    try:
        parser.parse_args(["portfolio", "build"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("portfolio build should no longer parse")


def test_cmd_portfolio_import_prompts_and_persists_missing_mappings(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "portfolio.json"
    mapping_path = tmp_path / "ajbell.yaml"
    cmd_portfolio_create(
        argparse.Namespace(
            portfolio=str(portfolio_path),
            plan="config/categories.yaml",
            overwrite=False,
        )
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
            portfolio=str(portfolio_path),
            source_id="ajbell-sipp",
            adapter="ajbell",
            statement=str(AJ_BELL_FIXTURE),
            mappings=str(mapping_path),
            fx=None,
        )
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
    cmd_portfolio_create(
        argparse.Namespace(
            portfolio=str(portfolio_path),
            plan="config/categories.yaml",
            overwrite=False,
        )
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
        portfolio=str(portfolio_path),
        source_id="ajbell-sipp",
        adapter="ajbell",
        statement="statement.csv",
        mappings=str(mapping_path),
        fx=None,
    )
    cmd_portfolio_import(args)
    cmd_portfolio_import(args)

    snapshot = load_snapshot(portfolio_path)
    assert len(snapshot["imports"]) == 1
    assert len(snapshot["investments"]) == 1
    assert snapshot["investments"][0]["market_value"] == 250.0
    assert snapshot["investments"][0]["source_id"] == "ajbell-sipp"


def test_cmd_portfolio_import_preserves_other_sources(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "portfolio.json"
    mapping_path = tmp_path / "broker.yaml"
    cmd_portfolio_create(
        argparse.Namespace(
            portfolio=str(portfolio_path),
            plan="config/categories.yaml",
            overwrite=False,
        )
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
            portfolio=str(portfolio_path),
            source_id="source-a",
            adapter="ajbell",
            statement="a.csv",
            mappings=str(mapping_path),
            fx=None,
        )
    )
    cmd_portfolio_import(
        argparse.Namespace(
            portfolio=str(portfolio_path),
            source_id="source-b",
            adapter="ibkr",
            statement="b.csv",
            mappings=str(mapping_path),
            fx=None,
        )
    )

    snapshot = load_snapshot(portfolio_path)
    assert {entry["source_id"] for entry in snapshot["investments"]} == {"source-a", "source-b"}
    assert len(snapshot["imports"]) == 2


def test_cmd_portfolio_add_with_explicit_category_splits_investment(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "portfolio.json"
    manual_mapping_path = tmp_path / "manual.yaml"
    cmd_portfolio_create(
        argparse.Namespace(
            portfolio=str(portfolio_path),
            plan="config/categories.yaml",
            overwrite=False,
        )
    )
    monkeypatch.setattr(cli_module, "MANUAL_MAPPINGS_PATH", manual_mapping_path)

    result = cmd_portfolio_add_instrument(
        argparse.Namespace(
            portfolio=str(portfolio_path),
            instrument_id="GOLD",
            description="Physical Gold",
            market_value=1000.0,
            category="Alternative / Gold=60, Cash=40",
        )
    )
    assert result == 0

    snapshot = load_snapshot(portfolio_path)
    assert len(snapshot["investments"]) == 2
    assert sorted(entry["market_value"] for entry in snapshot["investments"]) == [400.0, 600.0]
    assert manual_mapping_path.exists()


def test_cmd_portfolio_add_without_category_reuses_manual_mapping(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "portfolio.json"
    manual_mapping_path = tmp_path / "manual.yaml"
    cmd_portfolio_create(
        argparse.Namespace(
            portfolio=str(portfolio_path),
            plan="config/categories.yaml",
            overwrite=False,
        )
    )
    monkeypatch.setattr(cli_module, "MANUAL_MAPPINGS_PATH", manual_mapping_path)

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
        portfolio=str(portfolio_path),
        instrument_id="CASH1",
        description="Cash Position",
        market_value=500.0,
        category=None,
    )
    cmd_portfolio_add_instrument(args)
    cmd_portfolio_add_instrument(args)

    snapshot = load_snapshot(portfolio_path)
    assert call_count["value"] == 1
    assert len(snapshot["investments"]) == 2
    assert all(entry["category"] == "Cash" for entry in snapshot["investments"])


def test_cmd_portfolio_report_reads_legacy_snapshot_without_imports(tmp_path, capsys):
    portfolio_path = tmp_path / "legacy.json"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": "config/categories.yaml",
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
        argparse.Namespace(
            portfolio=str(portfolio_path),
            plan=None,
            export=None,
        )
    )
    assert result == 0
    captured = capsys.readouterr()
    assert "Loaded 1 investments" in captured.out


def test_cmd_portfolio_import_adds_import_metadata_to_legacy_snapshot(tmp_path, monkeypatch):
    portfolio_path = tmp_path / "legacy.json"
    mapping_path = tmp_path / "broker.yaml"
    portfolio_path.write_text(
        json.dumps(
            {
                "plan": "config/categories.yaml",
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
            portfolio=str(portfolio_path),
            source_id="ajbell-sipp",
            adapter="ajbell",
            statement="statement.csv",
            mappings=str(mapping_path),
            fx=None,
        )
    )

    assert result == 0
    snapshot = load_snapshot(portfolio_path)
    assert len(snapshot["imports"]) == 1
    assert snapshot["imports"][0]["source_id"] == "ajbell-sipp"
    assert {entry["source"] for entry in snapshot["investments"]} == {"manual", "ajbell"}
    assert {entry.get("source_id") for entry in snapshot["investments"]} == {None, "ajbell-sipp"}


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
