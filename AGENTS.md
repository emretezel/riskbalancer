# riskbalancer — AI Coding Agent Instructions

> **Note for agents**: `CLAUDE.md` and `AGENTS.md` are always identical.
> If you edit one, apply the same change to the other immediately.

RiskBalancer is a Python CLI that ingests broker statements for one or more household
members, maps holdings into a per-user nested category plan, converts values to GBP
when needed, and compares actual holdings against risk-parity targets.

---

## Architecture

If this repository does not yet contain a `docs/architecture.md` file, **do not write any
implementation code until you have done the following**:

1. Start a conversation with the author to understand the goals, constraints, and non-goals.
2. Propose a high-level architecture: identify the key components and how they communicate.
3. Get the author's sign-off on the high-level design.
4. Break each component down into a more detailed design before touching code.
5. Implement one component at a time, in dependency order (foundations first).
6. Write `docs/architecture.md` once the design is agreed, and keep it up to date as the
   project evolves.

When the architecture file already exists, read it at the start of every session before
making any structural decisions.

The current top-level shape of the codebase is:

- `src/riskbalancer/` — CLI entry point, broker adapters, domain models, portfolio
  logic, `paths.py` (`UserPaths` filesystem decisions), `plan_bootstrap.py` (catalog
  construction and the `plan create` interactive walker).
- `config/` — committed configuration:
  - `seed_plan.yaml` — catalog floor for the very first user.
  - `riskbalancer.example.yaml` — committed template for
    `riskbalancer.yaml`. The runtime file (`config/riskbalancer.yaml`)
    is gitignored so personal defaults never leave the local clone.
  - `mappings/<adapter>.yaml` — shared adapter mappings.
  - `fx.example.yaml` — FX template.
- `private/` — gitignored local data:
  - `fx.yaml` — shared GBP FX rates.
  - `users/<user>/` — per-user `plan.yaml`, `portfolio.json`, `mappings/`,
    `statements/`, `reports/`.
- `tests/` — pytest suite mirroring the source tree.

Every per-user command takes `--user <name>`, falling back to the
`RISKBALANCER_USER` env var and then to `default_user` in
`config/riskbalancer.yaml`. All filesystem decisions flow through
`UserPaths.for_user(user, root=...)` — do not embed layout literals in
command handlers; route them through that object.

---

## Tools & Stack

### Python

| Concern | Tool / Convention |
|---|---|
| Package manager | **conda** environment named `riskbalancer`, with `pip install -e '.[dev]'` for the project itself |
| Project metadata | `pyproject.toml` (PEP 517/518), setuptools backend |
| Dependency pinning | `pyproject.toml` `[project.dependencies]` and `[project.optional-dependencies].dev` |
| Versioning | `pyproject.toml → [project] version` — follow SemVer |
| Minimum Python | 3.12 (declared in `requires-python`) |

- The project must remain installable in development mode (`pip install -e '.[dev]'`).
- All dependencies — including dev and test dependencies — must be declared in
  `pyproject.toml` under `[project.dependencies]` and `[project.optional-dependencies]`.
- Do not hard-code paths. Use `importlib.resources` or `pathlib.Path(__file__).parent` for
  package-relative paths. User-facing paths come through CLI arguments and resolve relative
  to the project root.
- Target Python 3.12 only. Do not use language features unavailable on 3.12, and do not
  introduce code that requires a newer interpreter without bumping `requires-python`
  explicitly.

### General principles (all languages)

- Use the right package manager for the language — do not mix package managers within a
  single language layer without strong justification.
- Ensure the project can be: packaged, versioned, installed by end users, and installed
  in development mode by contributors — all from `pyproject.toml` alone.
- As the project grows, revisit tool and library choices. If a better-fit alternative
  exists, propose the migration to the author with a concrete rationale before switching.

---

## Documentation

- **`README.md`** at the repo root must stay short: project purpose, installation
  instructions, and the two or three most common usage examples. Nothing else belongs here.
- All substantive documentation lives under `docs/`. Keep that folder organised; create
  subdirectories when a topic area grows beyond two or three files.
- **Architecture**: `docs/architecture.md` — high-level design, component map, key decisions.
- **Adapters**: when a new broker adapter is added, document its expected statement format
  and any quirks under `docs/adapters/<adapter>.md`.
- Keep documentation in sync with the code. When you change behaviour, update the relevant
  doc file in the same commit.

---

## Coding Standards

### Design & Elegance

- Before implementing, identify the most appropriate design pattern and apply sound
  object-oriented (or functional, where idiomatic) principles.
- Prefer the simplest solution that is correct and maintainable. Do not over-engineer
  straightforward tasks.
- Refactoring is not optional — it is part of every feature. When adding or changing
  code, check whether the surrounding design is still the most elegant solution. If not,
  refactor it before moving on.
- **File length**: If a code file grows too long, think carefully about how best to
  split it into multiple files for ease of maintenance and readability. There is no
  fixed line-count rule — use judgement based on how many distinct responsibilities
  the file has accumulated.
- **Ongoing design review**: As the project grows, whenever you are working on a part
  of the code, review whether that part should be refactored to better adhere to
  established design patterns and object-oriented principles. Long-term maintainability,
  ease of change, readability, and the ability to add new features without friction are
  paramount. Do not defer this review — if a structural improvement is warranted, propose
  it to the author before moving on.

### Comments & Documentation in Code

- All new code must be **heavily commented** — explain the *why*, not just the *what*.
- Every new module/file must open with a brief docstring describing its purpose and the
  author name.
- Use type annotations everywhere. The codebase is fully typed under strict mypy settings
  and must stay that way.
- Every public function, method, and class must have a docstring.

### Project Structure

- Source code lives under `src/riskbalancer/`. New modules go there.
- Tests live under `tests/` and mirror the source tree (e.g. `tests/test_portfolio.py`
  exercises `src/riskbalancer/portfolio.py`). Fixture files live under `tests/fixtures/`.
- Re-evaluate structure after significant changes. If a better layout has become clear,
  reorganise — a clean structure is worth the churn.

---

## Persistence

The authoritative store for all mutable working data is SQLite at
`private/riskbalancer.db`. Human-readable YAML/JSON files still play two
distinct roles: committed seed/config inputs that are loaded into the DB, and
gitignored per-user side files that the import/report flow still reads from
on disk.

**Seed / config inputs (loaded into the DB by `seed.py`, or read at startup):**

| Concern | Location | Format | Committed? |
|---|---|---|---|
| Catalog floor (default plan) | `config/seed_plan.yaml` | YAML | yes |
| Default-user template | `config/riskbalancer.example.yaml` | YAML | yes |
| Default-user pointer | `config/riskbalancer.yaml` | YAML | **gitignored** |
| Shared adapter mappings | `config/mappings/<adapter>.yaml` | YAML | yes |
| FX template | `config/fx.example.yaml` | YAML | yes |
| Shared FX rates | `private/fx.yaml` | YAML | **gitignored** |

**Authoritative working store (gitignored):**

| Concern | Location | Format |
|---|---|---|
| Users, accounts, instruments, plan tree, category attributes, seeded mappings, FX rates, statement imports, positions | `private/riskbalancer.db` | SQLite |

**Per-user side files (gitignored, still file-based):**

| Concern | Location | Format |
|---|---|---|
| Per-user category plan (on-disk artifact) | `private/users/<user>/plan.yaml` | YAML |
| Per-user portfolio snapshot (on-disk artifact) | `private/users/<user>/portfolio.json` | JSON |
| Per-user adapter mapping overrides | `private/users/<user>/mappings/<adapter>.yaml` | YAML |
| Per-user manual mappings | `private/users/<user>/mappings/manual.yaml` | YAML |
| Per-user raw statements (broker exports) | `private/users/<user>/statements/<broker>/...` | broker-native |
| Per-user reports (output) | `private/users/<user>/reports/<YYYY-MM-DD>.csv` | CSV |

Several concerns currently live in both worlds (e.g. plans in `plan.yaml` *and*
in the `plan_node` table; mappings in per-user YAML overrides *and* in the
global `mapping` table). This is a transitional hybrid — the DB is becoming
the source of truth, but a full migration of the side files is still
outstanding.

Persistence rules:

- **Never commit anything under `private/`.** It holds real financial data for
  every household member.
- **Single-concept storage.** Whether the storage is a YAML file, a JSON file,
  or a database table, it should model exactly one concept. Do not mix
  unrelated state in the same document or table.
- **Mapping resolution is currently hybrid.** Shared adapter mappings flow
  from `config/mappings/<adapter>.yaml` into the global `mapping` table via
  `seed.py` (the `mapping` table has no `user_id`). Per-user overrides and
  import-time learnings still live in `private/users/<user>/mappings/` YAML
  files, merged by `load_layered_mappings` at import time. Until per-user
  overrides also move into the DB, both paths are real and must stay in sync.
- **Schema discipline applies everywhere.** Validate every persisted structure
  on read (YAML or SQL) and fail loudly on malformed data; do not silently
  coerce or paper over missing fields. The DB enforces this for the working
  store via `CHECK` / `UNIQUE` / `FOREIGN KEY` constraints (see the Database
  and SQL Design section below).
- **Stable identifiers.** `(adapter, account)` is the stable key for a
  statement import — now enforced by `UNIQUE(account_id, as_of)` on
  `statement_import`, with `account` itself keyed by
  `UNIQUE(user_id, source_id, name)`. `instrument_id` is the stable key for a
  holding — now enforced by `UNIQUE(source_id, instrument_id_text)` on
  `instrument`. Re-imports of the same `(account, as_of)` cascade through the
  old positions and insert fresh rows; different accounts at the same broker
  (e.g. AJ Bell SIPP vs Dealing) coexist as separate `account` rows.
- **No magic values.** Use `None` / `NULL` / absent keys for missing values,
  never sentinels like `0`, `-1`, or `"N/A"`. The schema enforces this via
  `NOT NULL` defaults and `CHECK` constraints.
- **Document the on-disk format.** When the shape of any persisted file *or*
  any DB table changes, update `docs/architecture.md` (or
  `docs/database-schema.md` for schema changes) in the same commit; add a
  migration in `src/riskbalancer/migrations.py` for any DB change and consider
  whether a one-shot migration of existing local files is also needed.

---

## Database and SQL Design

The authoritative working store is SQLite (`private/riskbalancer.db`). All
rules in this section are binding. Migrations live in
`src/riskbalancer/migrations.py` (append-only, versioned via
`PRAGMA user_version` and the `schema_version` table). The current schema is
documented in `docs/database-schema.md`; keep that file in sync with every
migration.

### Schema Design Principles

- **Single source of truth.** Every fact lives in exactly one place. Never
  replicate a value across tables to avoid a join.
- **One table, one thing.** A table models exactly one entity, event, or
  relationship. Mixing concerns is a design smell.
- **Normalise to at least 3NF by default.** Deviate only when a clear, justified
  performance need exists — and document the deviation explicitly.
- **Primary keys: meaningful and minimal.** Prefer natural keys when they are
  genuinely stable and unique; use surrogate keys only when no natural key exists
  or the natural key is composite and unwieldy.
- **Always declare foreign keys.** Referential integrity is enforced at the schema
  level, not in application code. Ensure SQLite is opened with
  `PRAGMA foreign_keys = ON` so the constraints are actually enforced.
- **Always declare `UNIQUE` constraints** on every column or combination that is
  semantically unique, regardless of whether it is also the primary key.
- **Default to `NOT NULL`.** A column is nullable only when the absence of a value
  is a meaningful, valid state.
- **Use the most precise data type** that correctly represents the domain
  (`DATE` / ISO-8601 `TEXT` for dates, integer minor units or `NUMERIC` affinity
  for money and prices — never `REAL`/floating point for monetary or price values,
  since floating-point error is unacceptable for money).
- **No magic values.** Never use sentinel values (`0`, `-1`, `"N/A"`) to represent
  absence or special states — use `NULL` or a proper status column with a `CHECK`
  constraint.
- **`CHECK` constraints encode invariants.** Domain rules (e.g. `quantity > 0`,
  `side IN ('BUY','SELL')`, valid enum values, currency code length) must be
  `CHECK` constraints so the database enforces them.

### Indexes

- **Add indexes after the schema is correct.** Never let a performance desire
  drive a denormalisation decision.
- **Justify each index:** name the query pattern it serves.
- **Avoid redundant indexes** (e.g. an index whose leading columns are already
  covered by another).
- **Use views** to pre-compose common joins or projections without duplicating
  data.

### Schema Evolution

Whenever features are added or code is refactored, re-evaluate the schema. If the
design can be improved, plan and apply the necessary migrations — do not silently
preserve a bad design because it already exists. Migrations for this project will
live in `src/riskbalancer/migrations.py`; add new migrations there in order, and
call out any impact on `private/riskbalancer.db`.

### SQL Style

- Write correct SQL first; optimise second.
- **Never use `SELECT *`** in production code — name every column explicitly.
- Do not repeat logic that belongs in the schema (e.g. filtering soft-deleted
  rows in every query instead of defining a view).
- Check whether each important query can use an index efficiently; use
  `EXPLAIN QUERY PLAN` for non-trivial queries.

### Review Expectations

Flag — and propose corrections for — any schema that:

- Duplicates a fact or violates normal form without justification
- Uses an imprecise data type (especially `REAL`/floating point for money or
  prices)
- Omits a constraint that should exist (`NOT NULL`, `UNIQUE`, `FOREIGN KEY`,
  `CHECK`)
- Conflates multiple entities in one table
- Uses magic values instead of proper nullability or `CHECK` constraints

When proposing schema changes, always include:

- Recommended schema with all constraints stated explicitly
- Normalisation rationale (target normal form and why)
- Index recommendations with the query patterns they serve
- Justification for any deliberate deviation from normal form

---

## Testing

- Every project must have a test suite covering unit tests and regression tests.
- Tests live under `tests/`, mirroring the source tree. Shared fixtures go in
  `tests/conftest.py`; static fixture data goes in `tests/fixtures/`.
- **No feature or behaviour change lands without tests.** New functionality → new unit
  tests. Changed behaviour → updated tests. No exceptions.
- Regression tests are added whenever a bug is fixed — the test must fail on the
  buggy code and pass on the fix.
- **Run the full test suite before every commit.** A green suite is a prerequisite for
  pushing, opening a PR, or declaring a task done.

### Python

- Use **pytest** for all tests. Run with `pytest` from the repo root.
- Organise tests by concern (`test_adapters.py`, `test_portfolio.py`, `test_cli_*.py`,
  `test_configuration.py`).
- Use `pytest.mark` to tag slow, integration, or regression tests so they can be run
  selectively during development and fully in CI.
- Aim for high coverage on business logic (category validation, FX conversion, mapping
  resolution, report aggregation). Use `pytest-cov` if a coverage figure is needed.
- Prefer fixtures over setup/teardown boilerplate — keep tests readable.
- Tests must not touch the user's real `private/`, `portfolios/`, or `reports/`
  directories. Use `tmp_path` and explicit fixture data.

---

## Static Analysis & Quality Gate

### Python

- **mypy** for static type checking — run as `mypy src/ tests/`.
- **ruff** for formatting, linting, and import sorting — run as
  `ruff format . && ruff check .`.
- The quality gate is: `ruff format`, `ruff check`, `mypy`, `pytest` — all four must pass
  with zero errors before any commit is pushed.

**mypy rules (non-negotiable):**

- `mypy src/ tests/` must report zero errors at the end of every change.
- Fix mypy errors by correcting the design — no `# type: ignore`, no `Any` / `object`
  widening to silence the checker, no removing or loosening annotations. If mypy is
  unhappy, the type model is telling you something — fix it.
- Pre-existing errors do not justify introducing new ones.

**ruff rules:**

- All code must pass `ruff format` (formatting) and `ruff check` (linting) with no
  suppressions except where a suppression has an explicit, documented reason.
- Import order is enforced by ruff — do not reorder manually.

**Recommended `pyproject.toml` quality-tool config (current project already aligns with
this; tighten over time):**

```toml
[tool.mypy]
strict = true
ignore_missing_imports = false
python_version = "3.12"
files = ["src/riskbalancer", "tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "ANN"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--strict-markers -q"
```

### Universal rule

**Every commit must pass all quality-gate tools with zero errors before being pushed.**
Pre-existing errors are not an excuse to introduce more. "The file I touched is clean"
is not enough — the whole codebase must be clean.
