"""
Tests for the FX CLI workflow.

Author: Emre Tezel
"""

import argparse
from pathlib import Path
from urllib.error import URLError

import pytest
import yaml

import riskbalancer.cli as cli_module
from riskbalancer.cli import (
    build_parser,
    cmd_fx_update,
    derive_gbp_fx_rates,
    load_fx_rates,
    parse_ecb_reference_rates_xml,
)

ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2026-03-23">
      <Cube currency="USD" rate="1.25"/>
      <Cube currency="GBP" rate="0.80"/>
      <Cube currency="CHF" rate="1.00"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


def load_fx_payload(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_build_parser_includes_fx_update_command():
    parser = build_parser()
    args = parser.parse_args(
        [
            "fx",
            "update",
            "--fx",
            "private/fx.yaml",
            "--currency",
            "usd",
            "--currency",
            "eur",
        ]
    )
    assert args.command == "fx"
    assert args.fx_command == "update"
    assert args.fx == "private/fx.yaml"
    assert args.currency == ["usd", "eur"]


def test_parse_ecb_reference_rates_xml_reads_provider_date_and_rates():
    provider_date, rates = parse_ecb_reference_rates_xml(ECB_XML)
    assert provider_date == "2026-03-23"
    assert rates["GBP"] == 0.8
    assert rates["USD"] == 1.25


def test_parse_ecb_reference_rates_xml_rejects_malformed_payload():
    with pytest.raises(ValueError, match="Malformed ECB FX payload"):
        parse_ecb_reference_rates_xml("<not-xml")


def test_derive_gbp_fx_rates_uses_cross_rates():
    rates = derive_gbp_fx_rates(
        {"GBP": 0.8, "USD": 1.25, "CHF": 1.0},
        ["usd", "eur", "chf"],
    )
    assert rates == {
        "CHF": 0.8,
        "EUR": 0.8,
        "USD": 0.64,
    }


def test_cmd_fx_update_refreshes_existing_tracked_currencies(tmp_path, monkeypatch, capsys):
    fx_file = tmp_path / "fx.yaml"
    fx_file.write_text(
        "date: 2025-01-01\nbase: GBP\nrates:\n  USD: 0.70\n  EUR: 0.90\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_ecb_reference_rates",
        lambda: ("2026-03-23", {"GBP": 0.8, "USD": 1.25, "CHF": 1.0}),
    )

    result = cmd_fx_update(argparse.Namespace(fx=str(fx_file), currency=None))

    assert result == 0
    payload = load_fx_payload(fx_file)
    assert payload == {
        "date": "2026-03-23",
        "base": "GBP",
        "rates": {"EUR": 0.8, "USD": 0.64},
    }
    assert load_fx_rates(str(fx_file)) == {"EUR": 0.8, "USD": 0.64}
    captured = capsys.readouterr()
    assert "Updated" in captured.out


def test_cmd_fx_update_bootstraps_from_currency_flags(tmp_path, monkeypatch):
    fx_file = tmp_path / "fx.yaml"
    monkeypatch.setattr(
        cli_module,
        "fetch_ecb_reference_rates",
        lambda: ("2026-03-23", {"GBP": 0.8, "USD": 1.25, "CHF": 1.0}),
    )

    result = cmd_fx_update(
        argparse.Namespace(
            fx=str(fx_file),
            currency=["usd", "chf"],
        )
    )

    assert result == 0
    payload = load_fx_payload(fx_file)
    assert payload["rates"] == {"CHF": 0.8, "USD": 0.64}


def test_cmd_fx_update_bootstraps_default_private_file_from_example(tmp_path, monkeypatch):
    private_fx = tmp_path / "private" / "fx.yaml"
    example_fx = tmp_path / "config" / "fx.example.yaml"
    example_fx.parent.mkdir(parents=True, exist_ok=True)
    example_fx.write_text(
        "date: 2025-01-01\nbase: GBP\nrates:\n  USD: 0.70\n  EUR: 0.90\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "DEFAULT_FX_PATH", private_fx)
    monkeypatch.setattr(cli_module, "FX_TEMPLATE_PATH", example_fx)
    monkeypatch.setattr(
        cli_module,
        "fetch_ecb_reference_rates",
        lambda: ("2026-03-23", {"GBP": 0.8, "USD": 1.25, "CHF": 1.0}),
    )

    result = cmd_fx_update(argparse.Namespace(fx=str(private_fx), currency=None))

    assert result == 0
    assert private_fx.exists()
    payload = load_fx_payload(private_fx)
    assert payload["rates"] == {"EUR": 0.8, "USD": 0.64}


def test_cmd_fx_update_requires_currency_bootstrap_for_missing_custom_file(tmp_path, capsys):
    fx_file = tmp_path / "custom.yaml"

    result = cmd_fx_update(argparse.Namespace(fx=str(fx_file), currency=None))

    assert result == 1
    assert not fx_file.exists()
    captured = capsys.readouterr()
    assert "Use --currency to bootstrap" in captured.err


def test_cmd_fx_update_leaves_file_unchanged_on_fetch_error(tmp_path, monkeypatch, capsys):
    fx_file = tmp_path / "fx.yaml"
    original = "date: 2025-01-01\nbase: GBP\nrates:\n  USD: 0.70\n"
    fx_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        cli_module,
        "fetch_ecb_reference_rates",
        lambda: (_ for _ in ()).throw(URLError("down")),
    )

    result = cmd_fx_update(argparse.Namespace(fx=str(fx_file), currency=None))

    assert result == 1
    assert fx_file.read_text(encoding="utf-8") == original
    captured = capsys.readouterr()
    assert "Failed to update FX rates" in captured.err


def test_cmd_fx_update_leaves_file_unchanged_on_missing_gbp(tmp_path, monkeypatch):
    fx_file = tmp_path / "fx.yaml"
    original = "date: 2025-01-01\nbase: GBP\nrates:\n  USD: 0.70\n"
    fx_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        cli_module,
        "fetch_ecb_reference_rates",
        lambda: ("2026-03-23", {"USD": 1.25}),
    )

    result = cmd_fx_update(argparse.Namespace(fx=str(fx_file), currency=None))

    assert result == 1
    assert fx_file.read_text(encoding="utf-8") == original


def test_cmd_fx_update_leaves_file_unchanged_on_missing_requested_currency(
    tmp_path,
    monkeypatch,
):
    fx_file = tmp_path / "fx.yaml"
    original = "date: 2025-01-01\nbase: GBP\nrates:\n  USD: 0.70\n"
    fx_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        cli_module,
        "fetch_ecb_reference_rates",
        lambda: ("2026-03-23", {"GBP": 0.8, "USD": 1.25}),
    )

    result = cmd_fx_update(
        argparse.Namespace(
            fx=str(fx_file),
            currency=["USD", "CHF"],
        )
    )

    assert result == 1
    assert fx_file.read_text(encoding="utf-8") == original
