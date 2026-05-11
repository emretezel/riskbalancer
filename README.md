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
│   ├── riskbalancer.example.yaml        # template for the local default-user config
│   ├── riskbalancer.yaml                # local-only override, gitignored (holds default_user)
│   ├── mappings/<adapter>.yaml          # shared adapter mappings
│   └── fx.example.yaml                  # FX template
├── private/                             # gitignored
│   ├── fx.yaml                          # shared GBP FX rates (one file across all users)
│   └── users/<user>/
│       ├── plan.yaml                    # this user's category plan (target weights)
│       ├── portfolio.json               # this user's portfolio snapshot
│       ├── mappings/                    # per-user override directory
│       │   ├── manual.yaml              # always per-user
│       │   └── <adapter>.yaml           # optional override of shared mappings
│       ├── statements/<broker>/<account>/<YYYY>/<MM>/...
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

The repository ships only `config/riskbalancer.example.yaml` (no personal
defaults committed). To set a default user on your machine:

```bash
cp config/riskbalancer.example.yaml config/riskbalancer.yaml
# then edit the file and uncomment `default_user: your_name`
```

Or skip the file and `export RISKBALANCER_USER=your_name` in your shell
rc. With no default set, every per-user command exits 1 with a clear
message asking for `--user`; `fx update` and `user list` still work.

## Setting up a new user

Onboarding a new household member from zero to first report:

```bash
# 1. Register the user on disk.
riskbalancer user create --user wife

# 2. Bootstrap their plan. Either clone an existing one as a starting point
#    or walk the catalog interactively.
riskbalancer plan create --user wife --from emre
# OR
riskbalancer plan create --user wife

# 3. Drop a broker statement anywhere on disk and import it. The CLI
#    auto-files the statement under
#    private/users/wife/statements/<adapter>/<account>/<YYYY>/<MM>/ using
#    today's date and creates private/users/wife/portfolio.json on first
#    run. The source file stays put unless you pass --move.
riskbalancer portfolio import \
  --user wife \
  --source-id ajbell-isa \
  --adapter ajbell \
  --account isa \
  --statement ~/Downloads/wife-isa-snapshot.csv

# 4. Optional: add manual holdings (cash, gold, alternatives).
riskbalancer portfolio add \
  --user wife \
  --instrument-id GOLD \
  --description "Physical Gold" \
  --market-value 5000 \
  --category "Alternative / Gold"

# 5. Run the report (use bare --export to land at
#    private/users/wife/reports/<today>.csv).
riskbalancer portfolio report --user wife --export
```

The "Main Workflow" section below covers the same commands in steady-state
form once the user already exists.

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

To review the plan's leaves (read-only):

```bash
riskbalancer plan list --user wife
```

To change leaf `adjustment` values without hand-editing YAML:

```bash
# Walk every leaf with weight > 0 and prompt for a new adjustment.
# Press Enter to keep, type a number to replace, or `q` to stop early.
riskbalancer plan adjust --user wife

# Restrict the walk to a subtree.
riskbalancer plan adjust --user wife --under "Bonds / Developed"

# Set a single leaf without the walker (use `--yes` to skip the y/N confirm).
riskbalancer plan adjust --user wife "Bonds / Developed > UK > Govt" 0.95
```

### 2. Refresh FX rates (shared across users)

```bash
riskbalancer fx update --currency USD --currency EUR --currency CHF
```

FX is not per-user — exchange rates are the same for everyone. This writes
`private/fx.yaml`.

### 3. Import each statement (auto-files into the user's directory)

Point `--statement` at any path on disk — typically wherever your broker
left it (`~/Downloads/...`). The CLI copies the statement under
`private/users/<user>/statements/<adapter>/<account>/<YYYY>/<MM>/` using
today's date as the year/month folder, creating directories as needed:

```bash
riskbalancer portfolio import \
  --user emre \
  --source-id ajbell-sipp \
  --adapter ajbell \
  --account sipp \
  --statement ~/Downloads/2026-03-23-positions.csv
```

Pass `--move` to remove the source after copying. If a file with the same
name already exists at the destination, the new copy is suffixed
(`foo.csv` → `foo-2.csv`) so prior statements are never overwritten. If
your `--statement` is already inside the user's `statements/` tree, the
file is left exactly where it is.

Mapping resolution is layered: the shared file at
`config/mappings/<adapter>.yaml` is read first, then the per-user override
at `private/users/<user>/mappings/<adapter>.yaml` replaces individual
entries by instrument id. New mappings learned interactively are written
only to the override file so the shared catalog stays curated.

`portfolio import` re-imports replace by `--source-id` so re-running with
the same source updates that source's positions and leaves other sources
alone.

Supported adapters: `ajbell`, `citi`, `ibkr`, `ms401k`, `schwab`.

### 4. Add manual investments

```bash
riskbalancer portfolio add \
  --user emre \
  --instrument-id GOLD \
  --description "Physical Gold" \
  --market-value 15000 \
  --category "Alternative / Gold"
```

Manual mappings live in `private/users/<user>/mappings/manual.yaml`.

### 5. Run the report

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
riskbalancer user create --user wife             # register a new user on disk
riskbalancer user list                           # list every user under private/users/
riskbalancer user delete --user wife --confirm   # wipe a user's directory
```
