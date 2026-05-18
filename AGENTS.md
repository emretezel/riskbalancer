# riskbalancer — AI Coding Agent Instructions

> **Note for agents**: `CLAUDE.md` and `AGENTS.md` are always identical.
> If you edit one, apply the same change to the other immediately.

RiskBalancer is a Python CLI that ingests broker statements for one or more household
members, maps holdings into a per-user nested category plan, converts values to GBP
when needed, and compares actual holdings against risk-parity targets.

---

## Architecture

`docs/architecture.md` is the authoritative reference for component
layout, data flow, and key design decisions. **Read it at the start of
every session before making any structural change.** If you find it
out of sync with the code, fix the doc in the same commit as the code
change.

The current top-level shape of the codebase is:

- `src/riskbalancer/` — CLI entry point, broker adapters, domain
  models, repositories (all SQL), `paths.py` (`UserPaths` filesystem
  decisions), `plan_bootstrap.py` (DB-backed catalog construction and
  the `plan create` interactive walker), `plan_adjust.py`,
  `plan_csv.py`.
- `private/` — gitignored local data:
  - `riskbalancer.db` — authoritative SQLite store for every mutable
    concept (users, accounts, categories, instruments, mappings,
    plans, FX rates, statement imports, positions).
  - `users/<user>/statements/<adapter>/<account>/<YYYY>/<MM>/...` —
    raw broker statements kept on disk so they can be re-parsed when
    an adapter changes.
  - `users/<user>/reports/<YYYY-MM-DD>.csv` — generated report exports.
- `docs/` — `architecture.md`, `database-schema.md`, and any
  per-broker notes under `docs/adapters/`.
- `tests/` — pytest suite mirroring the source tree.

**Every per-user command requires `--user <name>`.** There is no
default-user resolution — no env var, no DB-stored setting. Shared
commands (`db init`, `fx update`) take no `--user`. All filesystem
decisions flow through `UserPaths.for_user(user, root=...)` — do not
embed layout literals in command handlers; route them through that
object.

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

The authoritative store for **every** mutable working concept is SQLite
at `private/riskbalancer.db`. There are no YAML or JSON side files for
working data; the schema and the CLI's CRUD commands are the only
sanctioned write paths.

**Authoritative working store (gitignored):**

| Concern | Location | Format |
|---|---|---|
| Users, accounts, instruments, categories (+ vol/adj), plans, mappings, FX rates, statement imports, positions | `private/riskbalancer.db` | SQLite |

**Filesystem artefacts (still on disk, gitignored):**

| Concern | Location | Format |
|---|---|---|
| Raw broker statements (kept for re-parse on adapter change) | `private/users/<user>/statements/<broker>/<account>/<YYYY>/<MM>/...` | broker-native |
| Generated reports (CSV output of `rb portfolio report --export`) | `private/users/<user>/reports/<YYYY-MM-DD>.csv` | CSV |

Persistence rules:

- **Never commit anything under `private/`.** It holds real financial
  data for every household member.
- **DB is the single source of truth.** Working data lives in
  `private/riskbalancer.db` and nowhere else. The CLI's CRUD commands
  (`rb category add`, `rb instrument add`, `rb mapping add`, etc.),
  `rb portfolio import`, and one-off local scripts are the only write
  paths. Reads go through `repositories.py` — callers above the
  repository layer never write SQL.
- **Mappings are global.** The `mapping` table has no `user_id`; one
  canonical mapping per instrument shared across users. Per-user
  customisation happens through the *plan* tree
  (`plan_node.user_id`), and the resolver
  (`resolve_category_to_plan_leaf`) walks the global category tree to
  find each user's deepest plan-leaf ancestor of a mapping's target.
- **Schema discipline.** Validate every persisted structure on read
  and fail loudly on malformed data; do not silently coerce or paper
  over missing fields. The schema enforces this via `CHECK` /
  `UNIQUE` / `FOREIGN KEY` constraints and triggers — see the
  Database and SQL Design section below, and the full reference in
  `docs/database-schema.md`.
- **Stable identifiers.** `(account_id, as_of)` is the stable key for
  `statement_import`; `(source_id, instrument_id_text)` is the stable
  key for `instrument`; `(user_id, source_id, name)` is the stable
  key for `account`. Re-imports of the same `(account, as_of)`
  cascade through the old positions and insert fresh rows; different
  accounts at the same broker (e.g. AJ Bell SIPP vs Dealing) coexist
  as separate `account` rows.
- **No magic values.** Use `None` / `NULL` for missing values, never
  sentinels like `0`, `-1`, or `"N/A"`. The schema enforces this via
  `NOT NULL` defaults and `CHECK` constraints.
- **Document the schema.** Any DB change must update
  `docs/database-schema.md` and `docs/architecture.md` in the same
  commit, and ship as a new append-only migration in
  `src/riskbalancer/migrations.py`. **The schema is locked** — see the
  Database and SQL Design section for the review rule before
  proposing one.

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
