# RiskBalancer Architecture

> Read this before making any structural decision. The schema's
> companion reference lives at [`database-schema.md`](database-schema.md);
> this file covers the layers above the schema and the data flow across
> them.

RiskBalancer is a single-process Python CLI that ingests broker
statements for a household, normalises them into a SQLite database, and
reports each member's holdings against a per-user risk-parity plan.
Every mutable concept lives in SQLite — there are no YAML or JSON side
files for working data. Raw broker statements stay on disk under
`private/users/<user>/statements/` so they can be re-parsed when an
adapter changes; rendered CSV reports land alongside them under
`reports/`.

---

## 1. Component map

```
                                ┌─────────────────────────┐
                                │  argparse (`build_parser`) │
                                │  in `cli.py`               │
                                └────────────┬────────────┘
                                             │ dispatch
                                             ▼
   ┌────────────────────────────────────────────────────────────────┐
   │  cmd_* handlers in `cli.py`                                    │
   │  (one per subcommand: db init, user *, plan *, category *,     │
   │   instrument *, mapping *, fx update, portfolio import/report) │
   └─────┬──────────────────────────────────────────────────┬───────┘
         │                                                  │
         │ filesystem decisions                             │ data access
         ▼                                                  ▼
   ┌─────────────┐                            ┌──────────────────────┐
   │ paths.py    │                            │ repositories.py      │
   │ UserPaths   │                            │ typed SQL accessors  │
   └─────────────┘                            └──────┬───────────────┘
                                                     │
                                                     │ raw SQL
                                                     ▼
                                              ┌──────────────────┐
                                              │ db.py (Database) │
                                              │ + migrations.py  │
                                              └──────┬───────────┘
                                                     │
                                                     ▼
                                              ┌──────────────────┐
                                              │ private/         │
                                              │   riskbalancer.db│
                                              └──────────────────┘

   ┌────────────────────┐
   │ adapters/<broker>  │  Parse statement → list[Investment]
   │ (StatementAdapter) │  (native amount + currency only)
   └────────────────────┘

   ┌──────────────────────────┐
   │ plan_bootstrap.py        │  Interactive walker that drives
   │ (IO + walk_catalog_*)    │  `rb plan create`.
   └──────────────────────────┘

   ┌──────────────────────────┐
   │ plan_adjust.py           │  Walker / diff utilities for
   │                          │  `rb plan adjust`.
   └──────────────────────────┘

   ┌──────────────────────────┐
   │ plan_csv.py              │  Depth-column CSV ↔ CategoryNode
   │                          │  round-trip for `rb plan export/import`.
   └──────────────────────────┘

   ┌──────────────────────────┐
   │ configuration.py         │  CategoryNode model, weight-sum
   │                          │  validation, plan → PortfolioPlan.
   └──────────────────────────┘
```

### Module responsibilities

| Module | Purpose |
|---|---|
| `cli.py` | argparse wiring + one `cmd_*` handler per subcommand. Owns interactive prompts (statement filing, post-import categorisation, plan-summary confirmation). |
| `paths.py` | `UserPaths` dataclass — every filesystem decision for a user (statements dir, reports dir, DB path). The only valid construction is `UserPaths.for_user(user, root=…)`. |
| `db.py` | `Database.connect(path)` — opens SQLite with the right PRAGMAs (`foreign_keys=ON`, WAL, STRICT-compatible) and runs pending migrations. Never construct a raw `sqlite3.Connection`. |
| `migrations.py` | Append-only `MIGRATIONS` list. Each migration runs in its own transaction. Downgrades are rejected. |
| `repositories.py` | All SQL. Typed accessors for every table (users, categories, plans, mappings, instruments, accounts, statement imports, positions, FX). Callers above this layer never write SQL. |
| `models.py` | `CategoryPath`, `CategoryTarget`, `Investment`, `CategoryStatus`. The `Investment` carried out of adapters holds native amount + currency only. |
| `configuration.py` | `CategoryNode` (in-memory plan tree), sibling-weight validation, `build_portfolio_plan_from_nodes`. The YAML loader here is used only by tests (live data never round-trips through YAML). |
| `portfolio.py` | `PortfolioPlan` — the flat target-list view the report consumes. |
| `adapters/<broker>` | Subclass of `StatementAdapter`. Parses one broker's CSV into a `Sequence[Investment]` in native currency. No FX conversion, no category guessing — the import path handles both. |
| `plan_bootstrap.py` | Catalog construction + interactive walker that drives `rb plan create`. Reads peer plans / category fundamentals / mapping leaves entirely from the DB. |
| `plan_adjust.py` | Walker / diff helpers used by `rb plan adjust`. |
| `plan_csv.py` | Depth-column CSV round-trip used by `rb plan export` / `rb plan import`. |

---

## 2. Data flow: the three operations that matter

### 2.1 `rb portfolio import`

```
broker CSV ──▶ build_adapter(name) ──▶ parse_path(statement)
                                            │  list[Investment]
                                            ▼
            ┌──────────────────────────────────────────────────────┐
            │ inside one BEGIN / COMMIT:                           │
            │   replace_statement_import(account_id, as_of, path)  │
            │   for inv in parsed:                                 │
            │     find_or_create_instrument(source_id, id_text…)   │
            │     insert_position(statement_import_id, …,          │
            │                     market_value_native, currency)   │
            └──────────────────────────────────────────────────────┘
                                            │
                                            ▼
            list_unmapped_instruments_detailed(user_id)
                                            │
                ┌───────────────────────────┴─────────────────────────┐
                ▼                                                     ▼
         --non-interactive                                  interactive prompt
         (skip the prompt;                                  per unmapped instrument:
          report count at end)                                "leaf path" → add_mapping
                                                              "new"         → create_category + add_mapping
                                                              "skip"        → defer
                                                              "quit"        → stop loop
```

- The statement file is also copied (or moved with `--move`) into
  `private/users/<u>/statements/<adapter>/<account>/<YYYY>/<MM>/` for
  re-parseability. The DB stores the path of the canonical copy.
- Positions are stored in their native currency; GBP is derived at
  report time. This keeps statement imports immutable when FX rates
  later update.
- Re-imports replace by `(account_id, as_of)`: prior `statement_import`
  row cascades-delete its positions, then a fresh one is inserted.

### 2.2 `rb portfolio report`

```
load_plan_tree(user_id) ─▶ build_portfolio_plan_from_nodes ─▶ PortfolioPlan
                                                                │
iter_current_positions(user_id)  (joins `current_position` view)│
        │                                                       │
        for each position:                                      │
          ─ latest_fx_rate_on_or_before(currency, as_of)         │
            ─▶ native × rate = GBP value                         │
          ─ get_mappings_for_instrument(instrument_id)           │
            ─▶ for each (category_id, weight):                   │
                 resolve_category_to_plan_leaf(user, category)   │
                 ─▶ plan_node_id (deepest plan-leaf ancestor)     │
                 ─▶ accumulate gbp_value * weight onto that leaf  │
        │                                                       │
        ▼                                                       │
   totals by plan-leaf                                          │
        │                                                       ▼
        ▼                                            risk / cash weight math
   print_summary_table + print_source_breakdown ◀── (target.target_weight,
                                                     target.volatility,
                                                     target.adjustment)
        │
        ▼
   optional CSV at <reports_dir>/<YYYY-MM-DD>.csv via export_summary_to_csv
```

- The FX cache `(currency, as_of) -> rate` reused across positions
  keeps the per-position cost to one cache hit after the first lookup
  for that pair.
- A position whose instrument has no mapping (or whose mapping's
  category has no plan-leaf ancestor in the user's plan) is reported
  as "uncategorised" with the GBP total. The report still completes;
  the user can then fix the mapping or extend the plan.

### 2.3 `rb plan create` (interactive walker)

```
build_catalog_from_db(connection, current_user_id):
  1. iter_peer_plans(exclude_user_id)   ─▶ merge into catalog
  2. _merge_seed_leaves_into_catalog    ─▶ fill in vol/adj suggestions
                                          from `category` rows that
                                          already have fundamentals
  3. iter_mapping_paths                 ─▶ surface mapping-leaf paths
                                          even if no plan adopted them yet
                                            │
                                            ▼
walk_catalog_interactive(catalog, StdIO):
  recursive level-by-level pick:
    _prompt_pick_one (with `+ new` sentinel for ad-hoc names)
    _prompt_branch_or_leaf (decides per-pick whether to recurse)
    _prompt_weight (level weights must sum to 100%, validated inline)
    _prompt_leaf_metadata (collects vol/adj on every leaf)
                                            │
                                            ▼
_confirm_plan_summary  ─▶  if user confirms:
                              repositories.write_plan_tree(user_id, nodes)
                                            │
                                            ▼
write_plan_tree:
  - DELETE FROM plan_node WHERE user_id = ?
  - INSERT new tree row-by-row, find_or_create_category for each node
  - upsert vol/adj on the `category` row for every plan-leaf
```

`--from <peer>` clones an existing user's plan in one transaction
(`load_plan_tree` + `write_plan_tree`). An empty `category` table
surfaces a hard error pointing at `rb category add` / `rb portfolio
import` / `rb plan create --from`.

---

## 3. Schema overview

The schema is the contract; the full reference is
[`database-schema.md`](database-schema.md). The cliff notes:

- **`user`** — top-level per-household namespace. Deleting cascades
  through accounts, statement imports, positions, and plan nodes.
- **`category`** — global hierarchical tree. Vol/adj live here
  directly, paired by a CHECK (both set, or both NULL). Referenced by
  `plan_node` and `mapping` with `ON DELETE RESTRICT`.
- **`mapping`** — global instrument-to-category, leaf-only (enforced
  by triggers). Application-level invariant: weights per instrument
  sum to 1.0; the CLI warns when they don't.
- **`plan_node`** — per-user plan tree; each row carries the
  parent-relative weight. Leaf-ness is implicit (no child references).
- **`statement_import`** — one row per `(account, as_of)`. Re-imports
  replace the prior row in a single transaction.
- **`position`** — one row per `(statement_import, instrument)` in
  native currency. GBP is never stored.
- **`fx_rate`** — `(date, currency) → gbp_rate_micros`. Append-only
  history; report-time conversion walks back from the import's `as_of`
  to the most recent rate.
- **Views**: `current_import` (latest per account),
  `current_position` (positions joined to current_import for fast
  per-user listing), `category_path` (recursive ` / `-joined paths).

---

## 4. Key design decisions

- **DB authoritative.** Every working-data write goes through
  `repositories.py`. No YAML / JSON side files; the CLI's CRUD
  commands and the import flow are the only sanctioned write paths.
  Tests use `Database.connect(":memory:")` or `tmp_path`-scoped
  files.
- **`--user` is required everywhere.** There is no
  `RISKBALANCER_USER` env var fallback and no DB-stored default-user
  setting. Per-user commands refuse to run without `--user`; shared
  commands (`db init`, `fx update`) don't need it.
- **Mappings are global.** The same `(adapter, instrument)` maps to
  the same category for every household member. Per-user customisation
  happens via the *plan* tree — different users can stop the
  sub-categorisation at different depths, and the resolver
  (`resolve_category_to_plan_leaf`) walks up from the mapping's
  leaf target to find the deepest plan-leaf ancestor.
- **Native amounts on positions; GBP at read time.** The position
  table never stores GBP. The report joins `fx_rate` at the import's
  `as_of` date for each non-GBP currency. This keeps statement imports
  immutable and makes back-fills of FX rates retroactively correct.
- **Adapters are pure parsers.** A `StatementAdapter` knows how to
  pull `(instrument_id, description, market_value, currency, source)`
  out of one broker's CSV format. They do not look up categories,
  apply FX, or know about users. New broker support = new adapter +
  one entry in `KNOWN_ADAPTERS` (with a migration that inserts the
  `source` row).
- **Triggers + CHECKs are part of the contract.** Application code
  may pre-validate for friendly error messages, but every invariant
  the schema can express is also enforced by the schema. The schema
  is *locked* — see CLAUDE.md's feedback rule. Code that wants a new
  invariant goes through a deliberate schema-design conversation, not
  an add-column-and-migrate reflex.

---

## 5. Common operations

### Onboard a fresh household member

```bash
rb db init                                    # idempotent; safe to re-run
rb user create --user wife                    # DB row + private/users/wife/{statements,reports}/

# Either build a tree from scratch …
rb category add --name Equities
rb category add --parent "Equities" --name "Developed"
rb category add --parent "Equities / Developed" --name "NAM" --volatility 0.17 --adjustment 1.0
# … or clone a peer's plan
rb plan create --user wife --from emre

# Import a statement (interactive categorisation runs by default).
rb portfolio import --user wife --adapter ajbell --account isa \
                    --statement ~/Downloads/wife-isa.csv

# Run the report.
rb portfolio report --user wife --export
```

### Refresh FX after the markets close

```bash
rb fx update --currency USD --currency EUR --currency JPY  # first run picks the set
rb fx update                                                # subsequent runs refresh tracked currencies
```

### Inspect / edit mappings or categories

```bash
rb mapping list --source ibkr
rb mapping add --source ibkr --instrument EMIM --category "Equities / EM / Asia"
rb category list --user emre        # marks Emre's plan-leaves
rb instrument list --source ajbell
```

### Round-trip a plan as CSV (for hand-editing)

```bash
rb plan export --user emre --out /tmp/emre-plan.csv
# edit /tmp/emre-plan.csv …
rb plan import --user emre /tmp/emre-plan.csv
```

---

## 6. Where new behaviour goes

- A new broker → new file under `src/riskbalancer/adapters/`, new
  entry in `KNOWN_ADAPTERS`, new migration that `INSERT`s the `source`
  row, new adapter test in `tests/test_adapters.py`. **Do not** add
  any FX or category logic to the adapter.
- A new CRUD command → new `cmd_*` handler in `cli.py`, new repository
  accessors as needed in `repositories.py`, new test file under
  `tests/`. Look at `rb category add` for the canonical shape (parse
  → resolve → BEGIN / repository call / COMMIT → print outcome).
- A new schema concept → talk to the author first. Schema changes go
  through a deliberate review; see the CLAUDE.md feedback rule.
