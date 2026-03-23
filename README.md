# riskbalancer
RiskBalancer is a Python tool that ingests multi-broker statements, categorises your investments, computes risk-parity target weights, and flags over- or under-invested portfolio buckets.

## Project layout

```
.
├── pyproject.toml         # packaging metadata and pytest config
├── config/                # YAML category configuration + instrument mappings
│   └── fx.yaml            # optional manual FX rates (GBP base)
├── portfolios/            # stored portfolio snapshots (JSON, gitignored)
├── src/
│   └── riskbalancer/
│       ├── adapters/      # broker specific ingestion adapters
│       ├── models.py      # dataclasses for categories and investments
│       └── portfolio.py   # allocation plan + analysis orchestration
└── tests/                 # pytest suite (python -m pytest)
```

The package uses the adapter pattern so each broker statement source can provide its own parser that produces normalized `Investment` objects. Manual entries re-use the same normalization pipeline to keep reporting consistent regardless of origin.

### Available adapters

- `AJBellCSVAdapter` ingests AJ Bell statement exports and exposes hooks to supply category mappings per ticker.
- `IBKRCSVAdapter` ingests Interactive Brokers MTM CSV exports (converting to GBP using `private/fx.yaml` by default).
- `MS401KCSVAdapter` ingests Morgan Stanley 401(k) CSV exports (the download available from their participant portal) and converts USD balances to GBP using `private/fx.yaml` by default.
- `SchwabCSVAdapter` ingests Charles Schwab positions exports (USD) and also converts via `private/fx.yaml` by default.
- `CitiCSVAdapter` ingests Citibank holdings exports (USD) and converts using `private/fx.yaml` by default.

## Category configuration (YAML)

RiskBalancer models arbitrary category depth via YAML to keep risk weights editable without touching code. The repository ships with `config/categories.yaml`, which is generated from the provided `categories.csv`:

```yaml
assets:
  - name: Equities
    weight: 0.55        # top-level asset weight (fractions or percentages accepted)
    children:
      - name: Developed
        weight: 0.75
        children:
          - name: NAM
            weight: 0.34
            adjustment: 1.0
          - name: EMEA
            weight: 0.33
            children:
              - name: UK
                weight: 0.2
                adjustment: 1.0
              - name: Non -UK
                weight: 0.8
                adjustment: 1.0
  - name: Bonds
    weight: 0.2
    children:
      - name: Developed
        weight: 0.75
        children:
          - name: NAM
            weight: 0.33
            children:
              - name: Govt
                weight: 0.36
                adjustment: 1.0
              - name: Corp
                weight: 0.27
                adjustment: 1.0
  - name: Alternative
    weight: 0.25
    children:
      - name: Gold
        weight: 0.18
        adjustment: 1.0
      - name: Systematic
        weight: 0.7
        adjustment: 1.0
  - name: Cash
    weight: 0.0
    adjustment: 1.0
```

Use `load_portfolio_plan_from_yaml("config/categories.yaml", default_leaf_volatility=0.2)` to materialise a `PortfolioPlan`. Volatilities can be specified at any level; leaves that omit them fall back to the supplied default. Each leaf may declare an `adjustment` (default `1.0`) to scale its raw risk weight before the loader normalises the weights across all leaves. Every resulting `CategoryTarget` exposes both the raw `risk_weight` (product of weights × adjustment) and the `target_weight` (`normalized_risk_weight`). Weight validations tolerate small rounding errors (e.g., `33%` entries) but can be tightened by providing a smaller `tolerance`.

### Installation

RiskBalancer follows the standard `src/` layout and exposes a CLI entry point. To install locally:

1. (Optional) Create/activate a virtual environment (e.g., `python -m venv .venv && source .venv/bin/activate`).
2. Install in editable mode with the development extras:
   ```bash
   pip install -e .[dev]
   ```
   This pulls in `pyyaml` plus pytest for local testing.
3. Confirm the CLI is available:
   ```bash
   riskbalancer --help
   ```

### FX rates

The live FX file is `private/fx.yaml`, which is git-ignored and treated as mutable runtime data. A checked-in template lives at `config/fx.example.yaml`. Each value means `1 unit of foreign currency in GBP`, so `USD: 0.76` means `1 USD = 0.76 GBP`.

```bash
riskbalancer fx update
```

The `fx update` command refreshes `private/fx.yaml` from the European Central Bank daily reference rates. If the private file does not exist yet, the command bootstraps it from the tracked currencies in `config/fx.example.yaml`. Use `--currency` repeatedly to bootstrap or replace the tracked set explicitly.

```bash
riskbalancer fx update --currency USD --currency EUR --currency CHF
```

The resulting YAML looks like:

```yaml
date: 2025-11-16
base: GBP
rates:
  USD: 0.79
  EUR: 0.86
  CHF: 0.90
```

ECB reference rates are published on working days and are usually updated around 16:00 CET. The CLI stores the provider date from the ECB feed in the `date` field.

## CLI workflow

The package exposes a CLI entry point `riskbalancer` with three command groups:

1. `riskbalancer fx update [--fx private/fx.yaml] [--currency USD --currency EUR --currency CHF]`
   - Downloads the latest ECB daily reference rates and rewrites `private/fx.yaml`.
   - If `--currency` is omitted, the command refreshes only the currencies already tracked in the file.
   - If `--currency` is supplied, the tracked set is replaced with exactly those currencies.
   - If `private/fx.yaml` does not exist yet, the command creates it and seeds the tracked currencies from `config/fx.example.yaml`.

2. `riskbalancer categorize --statement private/portfolio.csv --plan config/categories.yaml --mappings config/mappings/ajbell.yaml`
   - Loads the statement with the AJ Bell adapter.
   - Prompts you to assign any unmapped instruments to one or more categories from the plan. Enter comma-separated category paths with optional weights (e.g., `Equities / Developed / NAM=70, Equities / Developed / Europe=30`). Holdings are split according to the weights supplied (defaults to an even split if weights are omitted). Optionally supply a custom volatility per instrument.
   - Stores the resulting allocations (per ticker) so future ingestions auto-categorize and automatically split holdings across the selected categories.
   - For non-GBP statements (e.g., IBKR), ensure `private/fx.yaml` contains up-to-date GBP-based conversion rates so holdings are converted automatically.

Instrument mappings are stored in YAML, supporting multiple category allocations per instrument (custom weights are optional; they default to 100% if omitted):

```yaml
AMD:
  allocations:
    - category: "Equities / Developed / NAM"
      weight: 0.7
    - category: "Equities / Developed / Europe"
      weight: 0.3
IEF:
  allocations:
    - category: "Bonds / Developed / NAM / Govt"
      weight: 1.0
```

Store one mapping file per broker (e.g., `config/mappings/ajbell.yaml`). `categorize` remains useful for pre-populating mappings, but portfolio imports can now prompt, persist new mappings, and continue in a single command.

### Portfolio snapshots

Use the `portfolio` command group to build a snapshot incrementally:

1. Create an empty portfolio:
   - `riskbalancer portfolio create --plan config/categories.yaml --portfolio my-portfolio`
   - This writes `portfolios/my-portfolio.json` with the stored plan path, timestamps, no imports, and no investments.
2. Import one broker statement at a time:
   - `riskbalancer portfolio import --portfolio my-portfolio --source-id ajbell-sipp --adapter ajbell --statement private/portfolio-AB8LNFS-SIPP.csv`
   - If `--mappings` is omitted, the CLI uses `config/mappings/<adapter>.yaml`.
   - When unmapped instruments are found, the import prompts for category allocations, saves the new mappings immediately, and continues the import in the same command.
   - Re-importing the same `--source-id` replaces the positions previously imported for that source without disturbing manual holdings or other broker feeds.
   - When a statement is not GBP-denominated, pass `--fx private/fx.yaml` so values are converted before they are persisted.
3. Add manual holdings that do not come from a broker statement:
   - `riskbalancer portfolio add --portfolio my-portfolio --instrument-id MANUAL1 --description "Special Holding" --market-value 10000 [--category "Equities / Developed / NAM=60, Equities / Developed / Europe=40"]`
   - If `--category` is omitted, the CLI checks `config/mappings/manual.yaml` and prompts only when it sees a new manual instrument for the first time. This is intended for cash, gold, private investments, and other off-statement positions.

Supporting commands:

- `riskbalancer portfolio list` shows stored snapshots along with their associated plan files and timestamps.
- `riskbalancer portfolio report --portfolio my-portfolio [--plan config/categories.yaml] [--export reports/my-portfolio.csv]` reloads the stored investments, optionally overrides the plan path, and produces (and optionally exports) the risk-parity summary table (category label, raw/normalized risk weights, volatility, cash weights, actual vs. target GBP values, deltas).
- `riskbalancer portfolio delete --portfolio my-portfolio` removes a snapshot when you no longer need it.

Portfolio files are JSON documents that capture normalized investments plus metadata such as the stored plan path, timestamps, and an `imports` list describing which broker statements have been loaded. Imported positions also store an optional `source_id`, which lets the CLI replace a single broker feed deterministically on re-import while leaving manual positions unchanged.

## Next steps

1. Drop sample broker CSV statements under a data folder and add concrete adapters in `src/riskbalancer/adapters`.
2. Extend `PortfolioPlan` with helper builders or configuration loaders (YAML/JSON) so targets and volatilities can be maintained outside code.
3. Implement CLIs or notebooks that ingest statements, instantiate a `PortfolioAnalyzer`, and emit diagnostics (tables, charts, alerts).
4. Add integration tests that validate adapter output against fixtures provided by each broker source.
