# riskbalancer

RiskBalancer ingests broker statements for one or more household members,
maps holdings into a per-user nested category plan, converts values to GBP
when needed, and compares actual holdings against risk-parity targets.

All working data — users, categories, instruments, mappings, plans, FX
rates, statement imports and positions — lives in a single SQLite database
at `private/riskbalancer.db`. Raw broker statements and generated reports
are the only other things kept on disk.

## Installation

Use the `riskbalancer` conda environment:

```bash
conda activate riskbalancer
python -m pip install -e '.[dev]'
riskbalancer --help
```

`private/` is gitignored — never commit it.

## Setting up from zero

```bash
# 1. Create the database (idempotent).
riskbalancer db init

# 2. Register a user. Pre-creates private/users/<name>/{statements,reports}/.
riskbalancer user create --user emre

# 3. Pull today's ECB FX rates into the fx_rate table.
riskbalancer fx update --currency USD --currency EUR --currency CHF

# 4. Add the categories you care about (one-off; tree is global across users).
#    Categories carry their volatility/adjustment fundamentals.
riskbalancer category add --name "Equities"
riskbalancer category add --parent "Equities" --name "Developed"
riskbalancer category add --parent "Equities / Developed" --name "NAM" \
    --volatility 0.16 --adjustment 1.00

# 5. Bootstrap the user's plan from the category tree.
#    Walks the tree interactively, prompting for inclusion + risk weight.
riskbalancer plan create --user emre
#    Or clone an existing user's plan as a starting point:
riskbalancer plan create --user emre --from spouse
```

## Main workflow

### Import a statement

```bash
riskbalancer portfolio import \
  --user emre \
  --adapter ajbell \
  --account sipp \
  --statement ~/Downloads/2026-03-23-positions.csv
```

The CLI copies the statement under
`private/users/<user>/statements/<adapter>/<account>/<YYYY>/<MM>/` so it
can be re-parsed later, then upserts positions into the DB keyed by
`(account, as-of)`. After the parse, any uncategorised instrument
triggers an interactive prompt offering to: type an existing leaf
category, create a brand-new leaf inline, skip, or quit the loop. Use
`--non-interactive` to skip the prompt for scripted imports.

Re-importing the same `(adapter, account, as-of)` replaces the previous
positions atomically. Different accounts at the same broker (e.g. SIPP
vs Dealing) coexist as separate `account` rows.

Supported adapters: `aegon`, `ajbell`, `citi`, `ibkr`, `ms401k`, `schwab`.

### Run the report

```bash
# Console summary.
riskbalancer portfolio report --user emre

# CSV export (defaults to private/users/emre/reports/<today>.csv).
riskbalancer portfolio report --user emre --export
```

The report resolves each position's instrument → mapping → plan-leaf,
converts native amounts to GBP via the most recent FX rate at or before
the import's as-of date, and reports actual vs target weights side by
side.

## CRUD reference

Every per-user command requires `--user <name>` — there is no
default-user resolution.

```bash
# Users
riskbalancer user list / create / delete

# Categories (global tree, vol/adj live on the row)
riskbalancer category list / add / update / delete

# Instruments (global, keyed by (source, id_text))
riskbalancer instrument list / add / update / delete

# Mappings (global, leaf-only target categories)
riskbalancer mapping list / add / update / delete

# Plans (per-user)
riskbalancer plan create / list / validate / adjust / delete
riskbalancer plan export / import       # CSV round-trip

# FX (shared across users)
riskbalancer fx update                  # ECB feed → fx_rate table
```

See `docs/architecture.md` for the component map and data-flow diagrams,
and `docs/database-schema.md` for the full schema reference.
