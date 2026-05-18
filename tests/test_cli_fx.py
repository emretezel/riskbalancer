"""
Tests for `rb fx update` and the supporting ECB-parsing / GBP-derivation
helpers, plus the FX repository accessors.

The full HTTP fetch is monkey-patched in every CLI-level test so the suite
never hits the live ECB endpoint.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from conftest import sandboxed_paths
from riskbalancer.cli import (
    cmd_fx_update,
    derive_gbp_fx_rates,
    parse_ecb_reference_rates_xml,
)
from riskbalancer.db import Database
from riskbalancer.repositories import (
    fraction_to_micros,
    get_fx_rate,
    latest_fx_rate_on_or_before,
    upsert_fx_rate,
)

# A minimal but realistic ECB envelope. The parser keys off any element with
# a `time` attribute, so the namespace prefix is intentionally omitted for
# brevity in the rates section.
SAMPLE_ECB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <gesmes:Sender>
    <gesmes:name>European Central Bank</gesmes:name>
  </gesmes:Sender>
  <Cube>
    <Cube time="2026-05-17">
      <Cube currency="USD" rate="1.08"/>
      <Cube currency="GBP" rate="0.85"/>
      <Cube currency="JPY" rate="160.0"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


# ---------------------------------------------------------------------------
# parse_ecb_reference_rates_xml
# ---------------------------------------------------------------------------


def test_parse_ecb_xml_extracts_date_and_rates() -> None:
    provider_date, rates = parse_ecb_reference_rates_xml(SAMPLE_ECB_XML)
    assert provider_date == "2026-05-17"
    assert rates == {"USD": 1.08, "GBP": 0.85, "JPY": 160.0}


def test_parse_ecb_xml_rejects_malformed_payload() -> None:
    with pytest.raises(ValueError, match="Malformed ECB FX payload"):
        parse_ecb_reference_rates_xml("not actually xml")


def test_parse_ecb_xml_rejects_payload_with_no_dated_cube() -> None:
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01">
  <gesmes:subject>Reference rates</gesmes:subject>
</gesmes:Envelope>
"""
    with pytest.raises(ValueError, match="dated rates section"):
        parse_ecb_reference_rates_xml(payload)


# ---------------------------------------------------------------------------
# derive_gbp_fx_rates
# ---------------------------------------------------------------------------


def test_derive_gbp_fx_rates_cross_rate_via_eur() -> None:
    """USD→GBP = (GBP per EUR) / (USD per EUR) = 0.85 / 1.08."""
    eur_rates = {"USD": 1.08, "GBP": 0.85, "JPY": 160.0}
    rates = derive_gbp_fx_rates(eur_rates, ["USD"])
    assert rates == {"USD": round(0.85 / 1.08, 6)}


def test_derive_gbp_fx_rates_eur_uses_gbp_per_eur_directly() -> None:
    """EUR→GBP is just the GBP/EUR quote — no cross-rate division needed."""
    eur_rates = {"USD": 1.08, "GBP": 0.85}
    rates = derive_gbp_fx_rates(eur_rates, ["EUR"])
    assert rates == {"EUR": 0.85}


def test_derive_gbp_fx_rates_filters_gbp_and_blanks() -> None:
    """GBP itself isn't stored; empty / whitespace currencies are ignored."""
    eur_rates = {"USD": 1.08, "GBP": 0.85}
    rates = derive_gbp_fx_rates(eur_rates, ["GBP", "", "  ", "USD"])
    assert set(rates) == {"USD"}


def test_derive_gbp_fx_rates_errors_when_target_missing() -> None:
    eur_rates = {"USD": 1.08, "GBP": 0.85}
    with pytest.raises(ValueError, match="JPY"):
        derive_gbp_fx_rates(eur_rates, ["JPY"])


def test_derive_gbp_fx_rates_errors_when_gbp_missing_from_feed() -> None:
    """A feed without GBP cannot anchor any rate — surface that loudly."""
    eur_rates = {"USD": 1.08}
    with pytest.raises(ValueError, match="GBP"):
        derive_gbp_fx_rates(eur_rates, ["USD"])


# ---------------------------------------------------------------------------
# repositories: upsert_fx_rate, get_fx_rate, latest_fx_rate_on_or_before
# ---------------------------------------------------------------------------


def test_upsert_fx_rate_inserts_then_overwrites(tmp_path: Path) -> None:
    db = Database.connect(tmp_path / "rb.db")
    try:
        upsert_fx_rate(db.connection, rate_date="2026-05-17", currency="USD", gbp_rate=0.78)
        assert get_fx_rate(db.connection, rate_date="2026-05-17", currency="USD") == 0.78

        # Second upsert for the same (date, currency) replaces the value.
        upsert_fx_rate(db.connection, rate_date="2026-05-17", currency="USD", gbp_rate=0.80)
        assert get_fx_rate(db.connection, rate_date="2026-05-17", currency="USD") == 0.80

        # And only one row exists per (date, currency).
        row = db.connection.execute(
            "SELECT COUNT(*) AS n FROM fx_rate WHERE rate_date = ? AND currency = ?",
            ("2026-05-17", "USD"),
        ).fetchone()
        assert row["n"] == 1
    finally:
        db.close()


def test_upsert_fx_rate_normalises_currency_to_uppercase(tmp_path: Path) -> None:
    db = Database.connect(tmp_path / "rb.db")
    try:
        upsert_fx_rate(db.connection, rate_date="2026-05-17", currency="usd", gbp_rate=0.78)
        # The schema CHECK requires uppercase; the helper must normalise so the row lands.
        row = db.connection.execute(
            "SELECT currency, gbp_rate_micros FROM fx_rate WHERE rate_date = ?",
            ("2026-05-17",),
        ).fetchone()
        assert row["currency"] == "USD"
        assert row["gbp_rate_micros"] == fraction_to_micros(0.78)
    finally:
        db.close()


def test_latest_fx_rate_on_or_before_picks_most_recent(tmp_path: Path) -> None:
    db = Database.connect(tmp_path / "rb.db")
    try:
        upsert_fx_rate(db.connection, rate_date="2026-05-15", currency="USD", gbp_rate=0.75)
        upsert_fx_rate(db.connection, rate_date="2026-05-17", currency="USD", gbp_rate=0.78)
        upsert_fx_rate(db.connection, rate_date="2026-05-20", currency="USD", gbp_rate=0.80)

        # As-of 2026-05-18: the 0.78 quote from the 17th is the latest one
        # at or before. The 20th must not leak through.
        assert latest_fx_rate_on_or_before(
            db.connection, as_of="2026-05-18", currency="USD"
        ) == pytest.approx(0.78)
    finally:
        db.close()


def test_latest_fx_rate_on_or_before_returns_none_when_no_match(tmp_path: Path) -> None:
    db = Database.connect(tmp_path / "rb.db")
    try:
        upsert_fx_rate(db.connection, rate_date="2026-05-20", currency="USD", gbp_rate=0.80)
        # No row at or before 2026-05-19; resolver must return None so the
        # caller can surface a "run rb fx update" hint.
        assert (
            latest_fx_rate_on_or_before(db.connection, as_of="2026-05-19", currency="USD") is None
        )
    finally:
        db.close()


def test_latest_fx_rate_on_or_before_gbp_is_identity(tmp_path: Path) -> None:
    """GBP → GBP is always 1.0 — no DB lookup needed, no row required."""
    db = Database.connect(tmp_path / "rb.db")
    try:
        assert latest_fx_rate_on_or_before(db.connection, as_of="2026-05-17", currency="GBP") == 1.0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# cmd_fx_update
# ---------------------------------------------------------------------------


def _fx_args(*, currencies: list[str] | None = None) -> argparse.Namespace:
    """Build the argparse Namespace shape `cmd_fx_update` expects."""
    return argparse.Namespace(currency=currencies)


def _patch_ecb(monkeypatch, xml_payload: str = SAMPLE_ECB_XML) -> None:
    """Short-circuit the live ECB HTTP call with a known XML payload."""
    from riskbalancer.cli import parse_ecb_reference_rates_xml as _parser

    def fake_fetch() -> tuple[str, dict[str, float]]:
        return _parser(xml_payload)

    monkeypatch.setattr("riskbalancer.cli.fetch_ecb_reference_rates", fake_fetch)


def test_fx_update_writes_rates_to_db(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = sandboxed_paths(tmp_path)
    _patch_ecb(monkeypatch)

    rc = cmd_fx_update(_fx_args(currencies=["USD", "JPY"]), paths=paths)
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        rows = db.connection.execute(
            "SELECT rate_date, currency, gbp_rate_micros FROM fx_rate ORDER BY currency"
        ).fetchall()
        assert [(r["rate_date"], r["currency"]) for r in rows] == [
            ("2026-05-17", "JPY"),
            ("2026-05-17", "USD"),
        ]
        # USD = GBP/EUR ÷ USD/EUR = 0.85 / 1.08.
        usd_micros = next(r["gbp_rate_micros"] for r in rows if r["currency"] == "USD")
        assert usd_micros == fraction_to_micros(round(0.85 / 1.08, 6))
    finally:
        db.close()

    out = capsys.readouterr().out
    assert "2026-05-17" in out
    assert "USD" in out


def test_fx_update_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Running the command twice must not duplicate rows for (date, currency)."""
    paths = sandboxed_paths(tmp_path)
    _patch_ecb(monkeypatch)

    assert cmd_fx_update(_fx_args(currencies=["USD"]), paths=paths) == 0
    assert cmd_fx_update(_fx_args(currencies=["USD"]), paths=paths) == 0

    db = Database.connect(paths.db_path)
    try:
        n = db.connection.execute("SELECT COUNT(*) AS n FROM fx_rate").fetchone()["n"]
        assert n == 1
    finally:
        db.close()


def test_fx_update_with_no_args_refreshes_existing_currencies(tmp_path: Path, monkeypatch) -> None:
    """No `--currency` flags: refresh every currency already in fx_rate."""
    paths = sandboxed_paths(tmp_path)

    # Pre-seed the table with two currencies on an older date.
    db = Database.connect(paths.db_path)
    try:
        upsert_fx_rate(db.connection, rate_date="2026-05-10", currency="USD", gbp_rate=0.79)
        upsert_fx_rate(db.connection, rate_date="2026-05-10", currency="JPY", gbp_rate=0.0053)
    finally:
        db.close()

    _patch_ecb(monkeypatch)
    rc = cmd_fx_update(_fx_args(), paths=paths)
    assert rc == 0

    # Fresh quotes from 2026-05-17 must have landed alongside the older ones.
    db = Database.connect(paths.db_path)
    try:
        currencies = {
            row["currency"]
            for row in db.connection.execute(
                "SELECT DISTINCT currency FROM fx_rate WHERE rate_date = '2026-05-17'"
            ).fetchall()
        }
        assert currencies == {"USD", "JPY"}
    finally:
        db.close()


def test_fx_update_with_no_args_and_empty_db_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = sandboxed_paths(tmp_path)
    _patch_ecb(monkeypatch)

    rc = cmd_fx_update(_fx_args(), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--currency" in err


def test_fx_update_with_unsupported_currency_reports_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    paths = sandboxed_paths(tmp_path)
    _patch_ecb(monkeypatch)

    rc = cmd_fx_update(_fx_args(currencies=["XYZ"]), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "XYZ" in err
    # Nothing should have been written for the bad currency.
    db = Database.connect(paths.db_path)
    try:
        n = db.connection.execute("SELECT COUNT(*) AS n FROM fx_rate").fetchone()["n"]
        assert n == 0
    finally:
        db.close()


def test_fx_update_skips_gbp_in_currency_args(tmp_path: Path, monkeypatch, capsys) -> None:
    """GBP is the base currency — passing it should be a silent no-op for that one,
    but other currencies still get processed."""
    paths = sandboxed_paths(tmp_path)
    _patch_ecb(monkeypatch)

    rc = cmd_fx_update(_fx_args(currencies=["GBP", "USD"]), paths=paths)
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        rows = db.connection.execute("SELECT currency FROM fx_rate").fetchall()
        currencies = {row["currency"] for row in rows}
        assert currencies == {"USD"}
    finally:
        db.close()


def test_fx_update_rolls_back_on_currency_resolution_failure(tmp_path: Path, monkeypatch) -> None:
    """One bad currency aborts the whole batch — no partial writes."""
    paths = sandboxed_paths(tmp_path)
    _patch_ecb(monkeypatch)

    rc = cmd_fx_update(_fx_args(currencies=["USD", "ZZZ"]), paths=paths)
    assert rc == 1

    db = Database.connect(paths.db_path)
    try:
        n = db.connection.execute("SELECT COUNT(*) AS n FROM fx_rate").fetchone()["n"]
        assert n == 0
    finally:
        db.close()


def test_fx_update_schema_rejects_negative_rate(tmp_path: Path) -> None:
    """Belt-and-braces check that the schema CHECK on gbp_rate_micros is enforced."""
    db = Database.connect(tmp_path / "rb.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            upsert_fx_rate(db.connection, rate_date="2026-05-17", currency="USD", gbp_rate=-0.5)
    finally:
        db.close()
