"""
Adapter parsing tests.

Adapters now return native amounts and currencies — FX conversion to GBP
happens at report time using the `fx_rate` table, not in the adapter.
"""

from pathlib import Path

from riskbalancer.adapters import (
    AegonCSVAdapter,
    AJBellCSVAdapter,
    CitiCSVAdapter,
    IBKRCSVAdapter,
    MS401KCSVAdapter,
    SchwabCSVAdapter,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_aj_bell_adapter_parses_sample_rows():
    adapter = AJBellCSVAdapter()

    investments = adapter.parse_path(FIXTURES / "aj_bell_sample.csv")
    assert len(investments) == 3

    amd = next(inv for inv in investments if inv.instrument_id == "AMD")
    assert amd.market_value == 17717.24
    assert amd.currency == "GBP"
    assert amd.source == "aj_bell"


def test_ibkr_adapter_emits_native_currency():
    """IBKR rows carry their native currency through; no in-adapter FX."""
    adapter = IBKRCSVAdapter()
    investments = adapter.parse_path(FIXTURES / "ibkr_sample.csv")
    assert len(investments) == 2
    by_id = {inv.instrument_id: inv for inv in investments}
    # GBP row stays GBP at the row's native amount.
    assert by_id["EMIM"].currency == "GBP"
    assert by_id["EMIM"].market_value == 3500.0
    # USD row is still USD — the report layer converts.
    assert by_id["PLTR"].currency == "USD"
    assert by_id["PLTR"].market_value == 10500.0


def test_ms401k_adapter_emits_usd():
    adapter = MS401KCSVAdapter()
    investments = adapter.parse_path(FIXTURES / "ms401k_sample.csv")
    assert len(investments) == 1
    investment = investments[0]
    assert investment.instrument_id == "Bond_Fund"
    assert investment.market_value == 1100.0
    assert investment.currency == "USD"


def test_schwab_adapter_emits_usd():
    adapter = SchwabCSVAdapter()
    investments = adapter.parse_path(FIXTURES / "schwab_sample.csv")
    assert len(investments) == 2
    values = {inv.instrument_id: inv.market_value for inv in investments}
    currencies = {inv.instrument_id: inv.currency for inv in investments}
    assert values["AAPL"] == 2000
    assert values["Cash & Cash Investments"] == 500
    assert currencies["AAPL"] == "USD"


def test_citi_adapter_parses_after_header():
    adapter = CitiCSVAdapter()
    investments = adapter.parse_path(FIXTURES / "citi_sample.csv")
    assert len(investments) == 2
    values = {inv.instrument_id: inv.market_value for inv in investments}
    currencies = {inv.instrument_id: inv.currency for inv in investments}
    assert values["BDP"] == 1538.62
    assert values["C"] == 23871.40
    assert currencies["BDP"] == "USD"


def test_aegon_adapter_parses_and_skips_total():
    adapter = AegonCSVAdapter()

    investments = adapter.parse_path(FIXTURES / "aegon_sample.csv")
    assert len(investments) == 4

    ids = {inv.instrument_id for inv in investments}
    assert "TOTAL" not in ids

    world = next(
        inv for inv in investments if inv.instrument_id == "AGN BLK World (ex UK) Eq Idx (BLK)"
    )
    assert world.description == "AGN BLK World (ex UK) Eq Idx (BLK)"
    assert world.market_value == 20000.00
    assert world.source == "aegon"
    assert world.currency == "GBP"

    brsp = next(
        inv for inv in investments if inv.instrument_id == "BRSP Default Strategy 2046-2048"
    )
    assert brsp.market_value == 10000.00
