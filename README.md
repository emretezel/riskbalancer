# riskbalancer
RiskBalancer is a Python tool that ingests multi-broker statements, categorises your investments, computes risk-parity target weights, and flags over- or under-invested portfolio buckets.

## Project layout

```
.
├── pyproject.toml         # packaging metadata and pytest config
├── config/                # YAML category configuration + instrument mappings
├── src/
│   └── riskbalancer/
│       ├── adapters/      # broker specific ingestion adapters
│       ├── models.py      # dataclasses for categories and investments
│       └── portfolio.py   # allocation plan + analysis orchestration
└── tests/                 # pytest suite (python -m pytest)
```

The package uses the adapter pattern so each broker statement source can provide its own parser that produces normalized `Investment` objects. Manual entries re-use the same normalization pipeline to keep reporting consistent regardless of origin.

### Available adapters

- `AJBellCSVAdapter` ingests AJ Bell statement exports and exposes hooks to supply category and volatility mappings per ticker so investments slot into your hierarchy automatically.

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

## CLI workflow

The package exposes a CLI entry point `riskbalancer` with two sub-commands:

1. `riskbalancer categorize --statement private/portfolio.csv --plan config/categories.yaml --mappings config/mappings/ajbell.yaml`
   - Loads the statement with the AJ Bell adapter.
   - Prompts you to assign any unmapped instruments to one or more categories from the plan. Use comma-separated entries with percentages (e.g., `Equities / Developed / NAM=70, Equities / Developed / Europe=30`). Optionally supply a custom volatility per instrument.
   - Stores the resulting allocations (per ticker) so future ingestions auto-categorize and automatically split holdings across the selected categories.
2. `riskbalancer analyze --statement private/portfolio.csv --plan config/categories.yaml --mappings config/mappings/ajbell.yaml`
   - Loads the plan, applies instrument mappings, ingests the statement, and prints a table showing actual vs. target weights along with an over/under invested flag for every leaf category.

Instrument mappings are stored in YAML, supporting multiple category allocations per instrument:

```yaml
AMD:
  allocations:
    - category: "Equities / Developed / NAM"
      weight: 0.7
    - category: "Equities / Developed / Europe"
      weight: 0.3
  volatility: 0.22
IEF:
  allocations:
    - category: "Bonds / Developed / NAM / Govt"
      weight: 1.0
```

Store one mapping file per broker (e.g., `config/mappings/ajbell.yaml`) and pass it to both `categorize` and `analyze`. Once every instrument is mapped, the analyze step runs without prompts.

## Next steps

1. Drop sample broker CSV statements under a data folder and add concrete adapters in `src/riskbalancer/adapters`.
2. Extend `PortfolioPlan` with helper builders or configuration loaders (YAML/JSON) so targets and volatilities can be maintained outside code.
3. Implement CLIs or notebooks that ingest statements, instantiate a `PortfolioAnalyzer`, and emit diagnostics (tables, charts, alerts).
4. Add integration tests that validate adapter output against fixtures provided by each broker source.
