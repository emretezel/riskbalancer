# riskbalancer

RiskBalancer ingests broker statements, maps holdings into a nested category plan, converts values to GBP when needed, and compares actual holdings against risk-parity targets.

## Project Layout

```text
.
├── pyproject.toml            # packaging, tooling, pytest config
├── config/
│   ├── categories.yaml       # target categories, weights, volatilities
│   ├── fx.example.yaml       # tracked FX currency template
│   └── mappings/             # persistent broker + manual instrument mappings
├── portfolios/               # stored portfolio snapshots (gitignored)
├── private/                  # local runtime data (gitignored)
│   ├── fx.yaml               # live FX rates written by the CLI
│   └── statements/           # broker statement files
├── reports/                  # exported CSV reports (gitignored)
├── src/riskbalancer/         # CLI, adapters, models, portfolio logic
├── tests/                    # pytest suite
├── AGENTS.md                 # Codex instructions
└── CLAUDE.md                 # Claude instructions
```

`private/`, `portfolios/`, and `reports/` are local working directories and should not be committed.

## Installation

Use the `riskbalancer` conda environment for project commands:

```bash
conda activate riskbalancer
python -m pip install -e '.[dev]'
riskbalancer --help
```

## Category Plan

Edit `config/categories.yaml` first. This file defines the target hierarchy, category weights, and standard deviations (`volatility`).

Rules:

- Top-level categories must sum to 100%.
- Every sibling set must also sum to 100% recursively.
- Leaf categories need a volatility, either directly or inherited from a parent.
- `adjustment` is optional and scales raw risk weight before normalization.

`portfolio report` validates these totals before producing a report. If any level does not add to 100%, it prints every failure and stops.

## Main Usage

### 1. Finalise categories, weights, and standard deviations

Update `config/categories.yaml` until the hierarchy reflects the portfolio you want to manage.

### 2. Create an empty portfolio

```bash
riskbalancer portfolio create \
  --plan config/categories.yaml \
  --portfolio emre_portfolio
```

This creates `portfolios/emre_portfolio.json` with the stored plan path, timestamps, no imports, and no investments.

### 3. Update FX rates

If you import non-GBP statements, refresh FX first:

```bash
riskbalancer fx update --currency USD --currency EUR --currency CHF
```

This creates `private/` and `private/fx.yaml` if they do not already exist. Stored rates are GBP-based, so `USD: 0.76` means `1 USD = 0.76 GBP`.

### 4. Import broker statements

Store statement files under `private/statements/`, for example:

```text
private/statements/ajbell/sipp/2026/2026-03-23-positions.csv
private/statements/ibkr/taxable/2026/U10049818_20260320.csv
private/statements/ms401k/401k/2026/2026-03-23-positions.csv
private/statements/citi/taxable/2026/2026-03-23-positions.csv
```

Import one account at a time with a stable `source_id`:

```bash
riskbalancer portfolio import \
  --portfolio emre_portfolio \
  --source-id ajbell-sipp \
  --adapter ajbell \
  --statement private/statements/ajbell/sipp/2026/2026-03-23-positions.csv
```

```bash
riskbalancer portfolio import \
  --portfolio emre_portfolio \
  --source-id ibkr-taxable \
  --adapter ibkr \
  --statement private/statements/ibkr/taxable/2026/U10049818_20260320.csv \
  --fx private/fx.yaml
```

Import behavior:

- If a holding has no mapping yet, the CLI prompts for category allocations and saves them in `config/mappings/<adapter>.yaml`.
- If you re-import the same `source_id`, the CLI replaces only that source’s imported positions.
- Manual holdings and other broker sources are left untouched.

Supported adapters in the main workflow are `ajbell`, `ibkr`, `ms401k`, `schwab`, and `citi`.

### 5. Add manual investments

Use `portfolio add` for holdings that do not come from broker statements, such as cash, gold, or private investments.

Examples:

```bash
riskbalancer portfolio add \
  --portfolio emre_portfolio \
  --instrument-id CASH_GBP \
  --description "GBP Cash" \
  --market-value 25000 \
  --category "Cash"
```

```bash
riskbalancer portfolio add \
  --portfolio emre_portfolio \
  --instrument-id GOLD \
  --description "Physical Gold" \
  --market-value 15000 \
  --category "Alternative / Gold"
```

```bash
riskbalancer portfolio add \
  --portfolio emre_portfolio \
  --instrument-id SYSTEMATICA \
  --description "Systematic Strategy" \
  --market-value 40000 \
  --category "Alternative / Systematic"
```

If you omit `--category`, the CLI looks in `config/mappings/manual.yaml`. For a new manual instrument it prompts once, stores the mapping, and reuses it on future adds.

### 6. Run the report

```bash
riskbalancer portfolio report --portfolio emre_portfolio
```

Optional CSV export:

```bash
riskbalancer portfolio report \
  --portfolio emre_portfolio \
  --export reports/emre_portfolio.csv
```

Report behavior:

- Terminal output includes the category summary and a GBP source breakdown.
- The source breakdown aggregates each imported `source_id` plus all manual additions.
- The CSV export contains only the category summary.
- If the category plan is invalid, the command prints all weight-validation failures and exits without producing the report.

## Supporting Commands

```bash
riskbalancer portfolio list
riskbalancer portfolio delete --portfolio emre_portfolio
riskbalancer fx update
```
