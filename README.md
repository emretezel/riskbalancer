# riskbalancer

RiskBalancer ingests broker statements for one or more household members,
maps holdings into a per-user nested category plan, converts values to GBP
when needed, and compares actual holdings against risk-parity targets.

## Project Layout

```text
.
├── pyproject.toml                       # packaging, tooling, pytest config
├── config/                              # committed
│   ├── seed_plan.yaml                   # catalog floor (the first user's starting point)
│   ├── riskbalancer.yaml                # holds default_user
│   ├── mappings/<adapter>.yaml          # shared adapter mappings
│   └── fx.example.yaml                  # FX template
├── private/                             # gitignored
│   ├── fx.yaml                          # shared GBP FX rates (one file across all users)
│   ├── inbox/                           # shared landing zone for unfiled statements
│   └── users/<user>/
│       ├── plan.yaml                    # this user's category plan (target weights)
│       ├── portfolio.json               # this user's portfolio snapshot
│       ├── mappings/                    # per-user override directory
│       │   ├── manual.yaml              # always per-user
│       │   └── <adapter>.yaml           # optional override of shared mappings
│       ├── statements/<broker>/...
│       └── reports/<YYYY-MM-DD>.csv
├── src/riskbalancer/                    # CLI, adapters, models, portfolio logic
└── tests/                               # pytest suite
```

`private/` is gitignored — never commit it. `config/` is committed.

## Installation

Use the `riskbalancer` conda environment:

```bash
conda activate riskbalancer
python -m pip install -e '.[dev]'
riskbalancer --help
```

## Picking the user

Every per-user command takes `--user <name>`. The flag falls back to:

1. the `RISKBALANCER_USER` environment variable, then
2. the `default_user` field in `config/riskbalancer.yaml`.

The committed default is `default_user: emre`, so omitting `--user` runs
commands against `emre`. Override per-command with `--user wife`,
`--user kid1`, etc.

## Main Workflow

### 1. Bootstrap a plan for the user

If you are setting up a new user, build their `plan.yaml` interactively from
the catalog the system already knows about:

```bash
riskbalancer plan create --user wife
```

The walk goes one level at a time. At every level it asks which categories
to include and what risk weight to give each, then recurses into the chosen
subtree. Leaves prompt for `volatility` and `adjustment` with the catalog's
suggestion as the default.

To clone an existing user's plan as a starting point:

```bash
riskbalancer plan create --user kid1 --from emre
```

To check that a plan is well-formed:

```bash
riskbalancer plan validate --user wife
```

### 2. Refresh FX rates (shared across users)

```bash
riskbalancer fx update --currency USD --currency EUR --currency CHF
```

FX is not per-user — exchange rates are the same for everyone. This writes
`private/fx.yaml`.

### 3. Drop broker statements into the user's directory

Statements live under `private/users/<user>/statements/<broker>/<account>/<year>/`.
For example:

```text
private/users/emre/statements/ajbell/sipp/2026/2026-03-23-positions.csv
private/users/emre/statements/ibkr/taxable/2026/U10049818_20260320.csv
private/users/wife/statements/ajbell/isa/2026/portfolio-AB8LNFI-ISA.csv
```

Use `private/inbox/` as a shared landing zone if you need to triage a file
before deciding which user it belongs to.

### 4. Import each statement

```bash
riskbalancer portfolio import \
  --user emre \
  --source-id ajbell-sipp \
  --adapter ajbell \
  --statement private/users/emre/statements/ajbell/sipp/2026/2026-03-23-positions.csv
```

Mapping resolution is layered: the shared file at
`config/mappings/<adapter>.yaml` is read first, then the per-user override
at `private/users/<user>/mappings/<adapter>.yaml` replaces individual
entries by instrument id. New mappings learned interactively are written
only to the override file so the shared catalog stays curated.

`portfolio import` re-imports replace by `--source-id` so re-running with the
same source updates that source's positions and leaves other sources alone.

Supported adapters: `ajbell`, `citi`, `ibkr`, `ms401k`, `schwab`.

### 5. Add manual investments

```bash
riskbalancer portfolio add \
  --user emre \
  --instrument-id GOLD \
  --description "Physical Gold" \
  --market-value 15000 \
  --category "Alternative / Gold"
```

Manual mappings live in `private/users/<user>/mappings/manual.yaml`.

### 6. Run the report

```bash
riskbalancer portfolio report --user emre
```

To export the category summary as CSV:

```bash
riskbalancer portfolio report --user emre --export
```

The bare `--export` writes to
`private/users/emre/reports/<YYYY-MM-DD>.csv`. Pass an explicit path to
override.

## Supporting Commands

```bash
riskbalancer user list                           # list all users with portfolios
riskbalancer user delete --user wife --confirm   # wipe a user's directory
```
