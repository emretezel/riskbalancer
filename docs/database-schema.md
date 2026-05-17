# RiskBalancer Database Schema

> The authoritative reference for the on-disk database. Read this before
> writing any code that touches `private/riskbalancer.db` or
> `src/riskbalancer/repositories.py`.

The database is SQLite. It is the single source of truth for every mutable
concept in the project: users, categories, plans, instruments, mappings,
statement imports, positions, and FX rates. Curated YAML files under
`config/` (`seed_plan.yaml`, `mappings/<adapter>.yaml`, `fx.example.yaml`)
are *seed inputs only* — loaded into the database once via `rb db seed`.

The redesign rationale lives at `/Users/emre/.claude/plans/we-need-to-have-snappy-crescent.md`.

---

## 1. Location, connection, lifecycle

| Concern | Convention |
|---|---|
| File path | `private/riskbalancer.db` (gitignored, like everything under `private/`). Tests pass `:memory:`. |
| Open path | `Database.connect(path)` in `src/riskbalancer/db.py`. **Never construct a `sqlite3.Connection` directly** — that bypasses PRAGMA setup and migrations. |
| FK enforcement | `PRAGMA foreign_keys = ON` is set on every connect. Required for any of the `REFERENCES` clauses below to do anything. |
| Journaling | File-backed: `PRAGMA journal_mode = WAL` plus `synchronous = NORMAL` for crash safety. In-memory: `journal_mode = MEMORY`. |
| Minimum SQLite | 3.37 (the `STRICT` table syntax used everywhere landed in 3.37.0). Older runtimes are rejected at connect time with a clear error. |
| Schema version | Tracked in **two places** for safety: `PRAGMA user_version` is the authoritative integer that drives the migration runner; `schema_version` is a normal table with `(version, applied_at)` rows for human inspection. |
| Migrations | Append-only list in `src/riskbalancer/migrations.py`. Each migration runs inside its own explicit transaction. A DB whose `user_version` is *higher* than this binary supports is rejected (downgrade protection). |

---

## 2. Storage conventions

### 2.1 Money

Stored as `INTEGER` ten-thousandths of a unit of currency (suffix
`_decithou`). £1.2345 is `12345`. This gives four decimal places of
precision without any floating-point error.

CLAUDE.md's rule "integer minor units or `NUMERIC` affinity for money and
prices — never `REAL`/floating point" is satisfied by the integer-minor-unit
form throughout the schema. There is no `REAL`/`FLOAT` money column anywhere.

### 2.2 Fractions

Weights, volatility, adjustments, and FX rates are stored as `INTEGER`
parts-per-million (suffix `_micros`). `0.55` is `550000`; `1.0` is
`1000000`; an adjustment of `1.35` is `1350000`. The helper
`riskbalancer.seed.fraction_to_micros(value)` rounds — so
`0.62 + 0.05 + 0.13 + 0.2` round-trips to exactly `1_000_000`, not
`999_999`.

### 2.3 Quantities

Number-of-units holdings (e.g. fractional shares) are stored as `INTEGER`
micro-units (×1e6). `NULL` is allowed — adapters that don't report
quantity (broker statements that only give value) leave it null.

### 2.4 Dates and timestamps

| Kind | Format | CHECK pattern |
|---|---|---|
| Date (no time) | ISO-8601 `TEXT` `YYYY-MM-DD` | `GLOB '????-??-??'` |
| Timestamp | ISO-8601 UTC `TEXT` `YYYY-MM-DDTHH:MM:SSZ` | `GLOB '????-??-??T*Z'` |

The `Z` suffix is required on every timestamp column. `repositories._utc_now_iso()`
is the only sanctioned way to produce one — it canonicalises Python's
default `+00:00` to `Z`.

Note: SQLite `GLOB` uses `?` for single-char and `*` for many-char.
`_` is **literal** in GLOB (it is the single-char wildcard in `LIKE`,
not here). Patterns above use `?`.

### 2.5 Currency codes

3-letter `TEXT` enforced by `CHECK (length(currency) = 3)`.

### 2.6 Strict mode

Every table is declared `STRICT`, so the type listed on each column is
actually enforced. Stuffing a string into an `INTEGER` column raises at
write time — type affinity is off.

### 2.7 No magic values

Per CLAUDE.md, missing values are `NULL` and meaningful states are
encoded by proper columns, never by sentinels like `0`, `-1`, or `"N/A"`.

---

## 3. Tables

Each table is described with its purpose, primary key, columns, foreign
keys, and unique constraints. `STRICT` is implied on every table —
omitted from the column lists for brevity.

### 3.1 `schema_version`

Records every applied migration with an ISO-8601 UTC timestamp. The
migration runner relies on `PRAGMA user_version` for sequencing — this
table is purely human-readable.

| Column | Type | Notes |
|---|---|---|
| `version` | `INTEGER PRIMARY KEY` | Matches `user_version` after the migration runs. |
| `applied_at` | `TEXT NOT NULL` | `GLOB '????-??-??T*Z'`. |

### 3.2 `user`

Top-level namespace for everything per-user. `name` is the on-disk
identifier the CLI accepts via `--user`.

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `name` | `TEXT NOT NULL UNIQUE` | `length(name) > 0`. |
| `created_at` | `TEXT NOT NULL` | ISO-8601 UTC. |

Deleting a row cascades through every per-user concept (plans, sources,
accounts, statement imports, positions). Categories, instruments, and
mappings survive — those are global registries.

### 3.3 `category`

The single hierarchical registry of categories. **Pure structure only —
no weight, no volatility, no adjustment.** A category is a leaf in one
user's plan and a branch in another's by virtue of which `plan_node`
rows reference it — not by anything on the category itself.

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `parent_id` | `INTEGER REFERENCES category(id) ON DELETE RESTRICT` | `NULL` for top-level. |
| `name` | `TEXT NOT NULL` | `length(name) > 0`. |

Constraints:

- `UNIQUE (parent_id, name)` — siblings under one parent cannot share a name.
- `UNIQUE INDEX idx_category_top_level_name ON category(name) WHERE parent_id IS NULL` —
  SQLite treats `NULL` as distinct in composite `UNIQUE`, so this partial
  index is needed to also enforce uniqueness for the top-level case.

`ON DELETE RESTRICT` (rather than `CASCADE`) is deliberate: a category
referenced by any `plan_node` or `mapping` cannot be deleted, because
those rows would otherwise lose their referent. The error surfaces at
write time and forces the caller to clean up references first.

A category named `Govt` can exist under `Bonds / Developed / NAM` *and*
under `Bonds / Developed / Europe` simultaneously — different parents,
different IDs. The schema deliberately supports this.

### 3.4 `category_attribute`

The single source of truth for a category's **intrinsic** volatility
and adjustment. Both columns are NOT NULL; row existence means
"this category has explicit canonical fundamentals and can serve as a
plan-leaf for any user who adopts it". Categories without a row exist
in `category` purely as structural nodes — a user who wants to hold
such a category as a plan-leaf must supply explicit vol/adj at
plan-creation time, which the walker upserts here. Plan weights live
exclusively on `plan_node` and are not represented here.

| Column | Type | Notes |
|---|---|---|
| `category_id` | `INTEGER PRIMARY KEY REFERENCES category(id) ON DELETE CASCADE` | One row per category, at most. Cascade fires only when the bare `category` row is deleted, which is blocked by `RESTRICT` on `mapping` and `plan_node` first. |
| `volatility_micros` | `INTEGER NOT NULL` | `volatility_micros >= 0`. Annualised volatility as a fraction of unit value, stored as parts-per-million. |
| `adjustment_micros` | `INTEGER NOT NULL` | `adjustment_micros >= 0`. Multiplicative risk adjustment, parts-per-million. Not clamped to ≤1.0 — the seed's `Bonds / Developed / NAM / Inflation` has `1.35`. Zero is allowed (e.g. seed `Cash`); a category with `adjustment = 0` contributes zero risk weight regardless of its `plan_node.weight_micros`. |

**Branches do not appear here.** The seed loader writes a row only for
seed leaves. A user's plan that terminates above a seed leaf — e.g.
holding `Equities / EM` as a single plan-leaf rather than splitting it
into Asia / Americas / EMEA — requires the walker to collect explicit
vol/adj for `Equities / EM` and upsert a row for it. There is no
derived-value path: no weighted average over children, no fallback to
the seed's reference figures. Every plan-leaf names its own fundamentals.

**Why this shape.** Plans differ in how they weight a branch's
children, so deriving a branch's effective vol/adj from any single
"seed" weighting would silently impose one plan's choices on another.
Splitting weights (per-plan, in `plan_node`) from fundamentals
(per-category, here) keeps both facts in exactly one place.

### 3.5 `source`

A broker. **One row per adapter globally** — `ibkr` is `ibkr` regardless
of which user holds an account there, because the adapter alone
determines how statements are parsed. Account ownership lives on
`account.user_id`, not here.

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `adapter` | `TEXT NOT NULL UNIQUE` | `IN ('ibkr','ajbell','citi','ms401k','schwab','aegon','manual')`. |

`source` is **pre-populated reference data**: migration 1 inserts one
row per entry of `migrations.KNOWN_ADAPTERS` immediately after creating
the table, so `instrument.source_id` and `account.source_id` always
have a target. Adding a new broker means: new entry in
`KNOWN_ADAPTERS`, new migration that `INSERT`s the row, new adapter
module. The `_ADAPTERS_LIST` SQL literal is derived from
`KNOWN_ADAPTERS` to keep the CHECK clause in lockstep.

### 3.6 `account`

A named account at a broker, owned by a user. AJ Bell users typically
have `isa` and `sipp`; IBKR users typically have one `taxable`. Two
users with accounts at the same broker share the same `source` row but
each have their own `account` rows.

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `user_id` | `INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE` | |
| `source_id` | `INTEGER NOT NULL REFERENCES source(id) ON DELETE RESTRICT` | A broker cannot be removed while any account still references it. |
| `name` | `TEXT NOT NULL` | `length(name) > 0`. |

`UNIQUE (user_id, source_id, name)`. The same account name can be used
by different users — Emre and Tani can each have a `taxable` account at
IBKR without collision.

### 3.7 `instrument`

Global registry of broker tickers / fund identifiers. The same ticker
at two different brokers is two separate rows — the leading natural
key is `(source_id, instrument_id_text)`. The broker is reached via
the `source` FK rather than carrying its own `adapter` column (the
adapter string lives on `source.adapter`, single source of truth).

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `source_id` | `INTEGER NOT NULL REFERENCES source(id) ON DELETE RESTRICT` | The broker. A `source` row cannot be deleted while instruments still reference it. |
| `instrument_id_text` | `TEXT NOT NULL` | The broker's identifier, as it appears in the CSV. `length(instrument_id_text) > 0`. |
| `description` | `TEXT NULL` | Human-readable description. The seed loader populates this from the YAML and refuses to overwrite a non-empty existing description. |

`UNIQUE (source_id, instrument_id_text)`.

### 3.8 `mapping`

Instrument-to-category mappings, split-aware (multiple rows per
instrument when the holding maps across several categories with
weights). **Mappings are global** — one canonical row set per
instrument, shared across all users. The CLI is the only sanctioned
edit surface; whoever runs the tool can change any mapping.

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `instrument_id` | `INTEGER NOT NULL REFERENCES instrument(id) ON DELETE RESTRICT` | |
| `category_id` | `INTEGER NOT NULL REFERENCES category(id) ON DELETE RESTRICT` | Must point at a **leaf** category — enforced by trigger. |
| `weight_micros` | `INTEGER NOT NULL` | `weight_micros > 0 AND weight_micros <= 1000000`. |

Constraints:

- `UNIQUE (instrument_id, category_id)` — a given instrument can only
  reference any one category at most once. Multiple rows per
  instrument with different `category_id` values encode a split.

Triggers (leaf-only invariant):

- `mapping_target_must_be_leaf_insert` — `BEFORE INSERT` aborts when
  the target category has children. A mapping must point at a leaf in
  the global category tree (i.e. no other `category` row has
  `parent_id = NEW.category_id`).
- `mapping_target_must_be_leaf_update` — `BEFORE UPDATE OF category_id`
  applies the same check.

Why leaf-only: the resolver (see [§7](#7-mapping-resolution)) walks up
the category tree from a mapping target to find the deepest plan-leaf
ancestor in the user's plan, so mappings target the most-specific
category they can. Allowing a mapping to point at a branch would let
the seed silently outvote a per-user split — bad, because it would
inject category weight a user didn't sign off on. By forcing every
mapping to be at a leaf, the resolver is the *only* code path that
ever rolls up.

Application-level invariant (not enforced by SQL): the weights for
each `instrument_id` group sum to `1_000_000`. The
`fraction_to_micros` helper rounds so common multi-allocation splits
(e.g. AJ Bell `SPAG` = 0.62 + 0.05 + 0.13 + 0.2) sum exactly.

### 3.9 `fx_rate`

Historical FX rates, keyed by date and currency. Rate is GBP per
native unit (e.g. `760000` for USD on a day when 1 USD = 0.76 GBP).

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `rate_date` | `TEXT NOT NULL` | `GLOB '????-??-??'`. |
| `currency` | `TEXT NOT NULL` | `length(currency) = 3`. |
| `gbp_rate_micros` | `INTEGER NOT NULL` | `gbp_rate_micros > 0`. |

`UNIQUE (rate_date, currency)`.

ECB publishes historical reference rates back to 1999; `rb fx update`
will fetch and upsert for any date as needed.

### 3.10 `plan_node`

A user's target tree. One row per node in the plan. **The leaf/branch
distinction is implicit and per-user**: a `plan_node` is a leaf iff no
other `plan_node` row has it as `parent_id`. The same `category_id` can
be a leaf in one user's plan and a branch in another's.

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `user_id` | `INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE` | |
| `parent_id` | `INTEGER NULL REFERENCES plan_node(id) ON DELETE CASCADE` | `NULL` for top-level. |
| `category_id` | `INTEGER NOT NULL REFERENCES category(id) ON DELETE RESTRICT` | |
| `weight_micros` | `INTEGER NOT NULL` | `0 <= weight_micros <= 1000000`. Zero is permitted for "category I want to keep visible but currently hold 0%" (e.g. `Cash` in the seed). |

`UNIQUE (user_id, parent_id, category_id)`.

`weight_micros` is **the sole source of plan weight** — at compute time
no other table is consulted for the parent-relative weight of a user's
plan node. Volatility and adjustment are not stored here; they live on
`category_attribute` and are looked up per plan-leaf at read time. For
a plan-leaf that sits above any seed leaf (e.g. a user holding
`Equities / EM` as a leaf without splitting it further), the walker
must have collected explicit vol/adj for that category and written a
`category_attribute` row before the plan was persisted. There is no
weighted-average derivation: every plan-leaf names its own fundamentals.

Application-level invariant (enforced in `repositories.write_plan_tree`):
sibling weights at every level sum to `1_000_000` (within the YAML
loader's tolerance of `1e-6` when expressed as fractions).

### 3.11 `statement_import`

An import event. One row per `(account_id, as_of)` — re-importing the
same statement for the same as-of replaces the previous import in a
single transaction (the runner deletes the old row, cascading through
`position`, then inserts the new one). Different `as_of` dates for the
same account coexist and form the historical timeline.

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `account_id` | `INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE` | The owning user is reached via `account.user_id` — not denormalised here. |
| `as_of` | `TEXT NOT NULL` | `GLOB '????-??-??'`. The "as-of date" the user supplied with `--as-of`. |
| `statement_path` | `TEXT NULL` | Relative path to the filed CSV under `private/users/<user>/statements/…`. `NULL` for `adapter='manual'` imports. |
| `imported_at` | `TEXT NOT NULL` | Wall-clock UTC timestamp of the import action itself. |

`UNIQUE (account_id, as_of)`.

### 3.12 `position`

One holding inside an import. Native amounts only — the GBP-equivalent
is **never stored**, only computed at query time by joining `fx_rate`
on `currency` and taking the most recent row with `rate_date <=
statement_import.as_of`. `fx_rate` is treated as append-only
authoritative history, so no per-import FX snapshot is needed.

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY` | |
| `statement_import_id` | `INTEGER NOT NULL REFERENCES statement_import(id) ON DELETE CASCADE` | |
| `instrument_id` | `INTEGER NOT NULL REFERENCES instrument(id) ON DELETE RESTRICT` | |
| `description` | `TEXT NULL` | Free-form, taken from the statement. |
| `quantity_micro_units` | `INTEGER NULL` | `NULL` when the broker doesn't report it. |
| `market_value_native_decithou` | `INTEGER NOT NULL` | `>= 0`. In the position's `currency`. |
| `currency` | `TEXT NOT NULL` | `length(currency) = 3`. |

`UNIQUE (statement_import_id, instrument_id)` — one row per
`(import, instrument)`. To represent split allocations of a single
holding, the application splits at *mapping resolution* time; the
position is a single row with the native value, and the mapping
distributes it across categories on the fly.

---

## 4. Indexes

| Index | Columns | Purpose |
|---|---|---|
| `idx_mapping_instrument` | `mapping(instrument_id)` | Drives per-import mapping lookup — the resolver fetches every `mapping` row for one instrument at a time. |
| `idx_position_instrument` | `position(instrument_id)` | Cross-import queries — "every position ever held in EMIM". Used by the interactive walker to suggest categories from history. |
| `idx_category_top_level_name` | `category(name) WHERE parent_id IS NULL` | Partial unique index that closes the gap left by `UNIQUE (parent_id, name)` when `parent_id IS NULL` — see §3.3. |

`UNIQUE` constraints implicitly create indexes. The
`statement_import(account_id, as_of)`, `fx_rate(rate_date, currency)`,
`account(user_id, source_id, name)`, and
`instrument(source_id, instrument_id_text)` uniques are already indexed
and need no explicit `CREATE INDEX`. "Portfolio as of date X" queries
scan `statement_import` by `(account_id, as_of DESC)` directly off the
unique index; per-user queries reach `statement_import` via
`account.user_id`, covered by the leading column of the `account`
unique.

---

## 5. Views

### 5.1 `current_import`

The latest `statement_import` per `account`. The window-style "max
per group" is expressed as a correlated `WHERE as_of = (SELECT MAX...)`
because SQLite doesn't have a `DISTINCT ON`.

### 5.2 `current_position`

Joins `position` to `current_import` (and `account`, to recover
`user_id`) so callers can see every position in the user's current
portfolio with a single `SELECT * FROM current_position WHERE user_id =
?`. The view surfaces `user_id`, `account_id`, and `as_of` so reports
don't need a second join. `statement_import` itself does not store
`user_id`; the view reaches it through the `account` join, which keeps
the underlying schema normalised.

### 5.3 `category_path`

A recursive CTE that materialises the full ` / `-joined path for every
category. Used wherever we need to render or match a path string
(seed loading, mapping lookups, interactive prompts). The recursion
walks down from `parent_id IS NULL`.

---

## 6. Adding a new migration

1. Append a new `_migration_N` function to `MIGRATIONS` in
   `src/riskbalancer/migrations.py`. **Do not edit existing
   migrations** — once they have shipped, they are frozen.
2. The function takes a `sqlite3.Connection` and runs whatever DDL the
   step needs. Use `connection.execute(...)` for each statement rather
   than `executescript`, so the migration runner's `BEGIN/COMMIT` wrap
   isn't broken by an implicit commit.
3. Tests for the migration go in `tests/test_db_schema.py` (for purely
   schema-level checks) or a new `tests/test_migration_N.py` (for
   data-shape tests).

The runner reads `PRAGMA user_version`, applies each pending migration
in order under its own transaction, inserts the corresponding row into
`schema_version`, and bumps `user_version`. A migration that raises
rolls back cleanly and leaves the DB at the previous version.

A DB whose `user_version` is *higher* than `len(MIGRATIONS)` (i.e. the
user downgraded the binary) is rejected at connect time with
`RuntimeError("Refusing to downgrade")`.

---

## 7. Mapping resolution

Mappings target **leaf** categories in the global category tree (enforced
by trigger — see §3.8). A user's plan, however, may stop the
sub-categorisation earlier than the mapping does. For example, the
seed maps EMIM into `Equities / EM / Asia`, but Tani's plan holds
`Equities / EM` as a single leaf — there is no `Asia` plan node to
attribute the value to.

The resolver bridges the two trees:

1. Look up every `mapping` row for the instrument (one or more rows; the
   weights sum to 1.0 across them).
2. For each mapping's `category_id`, walk up the `category.parent_id`
   chain using a recursive CTE.
3. The first ancestor (closest to the mapping leaf) that **is also a
   leaf in the user's plan** wins. "Leaf in this user's plan" means a
   `plan_node` row exists for this `(user_id, category_id)` and no
   other `plan_node` row references it as `parent_id`.
4. If no ancestor matches, the position is **uncategorised** for that
   user — surface as a warning, do not silently drop the value.

This is implemented in `repositories.resolve_category_to_plan_leaf`.
Note that the resolver only re-targets a position to the user's
plan-leaf; it does not borrow vol/adj from the mapping's deeper leaf.
The plan-leaf's own `category_attribute` row supplies the
fundamentals, as for any other leaf.

Why this design:

- The seed can ship maximally specific mappings without forcing every
  user to adopt the deepest sub-tree.
- Users can extend their plan over time (split `EM` into `Asia / EMEA /
  Americas`) and existing imports automatically attribute at the new
  granularity — no re-import required.
- The reverse path is also clean: when a user splits a leaf into
  children, the interactive walker prompts them to re-allocate any
  affected mappings inline in the same transaction, so the leaf-only
  invariant holds at every point in time.

## 8. Things this schema deliberately does **not** do

- **No `REAL` columns for money or prices.** All monetary values are
  integer ten-thousandths.
- **No denormalised GBP-converted columns** on `position`. The native
  amount + import-time FX is the canonical pair; GBP is derived.
- **No "current" flag** on `position`, `statement_import`, etc.
  "Current" is computed via the `current_import` view, not stored.
- **No magic-string sentinels.** `NULL` plus a proper status column is
  always preferred when a state needs to be modelled.
- **No global UNIQUE on `category(name)`.** Two `Govt` leaves with
  different parents are legitimately distinct rows.
- **No "is_seed" flag on user or plan_node.** The seed plan is loaded
  into `category` + `category_attribute` only; it never appears as a
  fake "_seed" user.
- **No derived vol/adj.** A category's volatility and adjustment are
  either explicit in `category_attribute` or absent. There is no
  weighted-average computation over children, no fallback to a parent's
  fundamentals, no `default_leaf_volatility` baked into the schema.
  Every plan-leaf names its own fundamentals; the walker collects them
  at plan-creation time when no row exists yet.
- **No plan-weight lookup outside `plan_node`.** Per-plan weights live
  exclusively on `plan_node.weight_micros`. The seed plan's reference
  weights are an input to plan creation (read directly from
  `config/seed_plan.yaml` by the walker) and are not persisted in any
  table.
