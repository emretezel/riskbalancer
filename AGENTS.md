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
  - `inbox/` — shared landing zone for unfiled statements.
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

## Persistence (file-based)

This project has no database. All persistent state is stored in human-readable files:

| Concern | Location | Format |
|---|---|---|
| Catalog floor (committed default plan) | `config/seed_plan.yaml` | YAML, committed |
| Default-user template (committed) | `config/riskbalancer.example.yaml` | YAML, committed |
| Default-user pointer (local) | `config/riskbalancer.yaml` | YAML, **gitignored** |
| Shared adapter mappings | `config/mappings/<adapter>.yaml` | YAML, committed |
| FX template | `config/fx.example.yaml` | YAML, committed |
| Per-user category plan | `private/users/<user>/plan.yaml` | YAML, **gitignored** |
| Per-user portfolio snapshot | `private/users/<user>/portfolio.json` | JSON, **gitignored** |
| Per-user mapping overrides | `private/users/<user>/mappings/<adapter>.yaml` | YAML, **gitignored** |
| Per-user manual mappings | `private/users/<user>/mappings/manual.yaml` | YAML, **gitignored** |
| Per-user statements | `private/users/<user>/statements/<broker>/...` | broker-native, **gitignored** |
| Per-user reports | `private/users/<user>/reports/<YYYY-MM-DD>.csv` | CSV, **gitignored** |
| Shared FX rates | `private/fx.yaml` | YAML, **gitignored** |
| Statements awaiting triage | `private/inbox/` | broker-native, **gitignored** |

Persistence rules:

- **Never commit anything under `private/`.** It holds real financial data for
  every household member.
- **Mapping resolution is layered.** The shared file is read first; the per-user
  override file replaces individual entries. New mappings learned at import time
  are written only to the per-user override file — the shared catalog stays curated.
- **Schema discipline still applies.** Validate every persisted structure on
  read and fail loudly on malformed data; do not silently coerce or paper over
  missing fields.
- **One concept per file.** Do not mix unrelated state in the same YAML/JSON
  document.
- **Stable identifiers.** `source_id` for broker imports and `instrument_id`
  for holdings must remain stable across runs; re-imports replace by `source_id`
  and must not duplicate data.
- **No magic values.** Use `None`/absent keys for missing values, not sentinels
  like `0`, `-1`, or `"N/A"`.
- **Document the on-disk format.** When the shape of any persisted file
  changes, update `docs/architecture.md` (or the relevant doc) in the same
  commit and consider whether a one-shot migration of existing local files is
  needed.
- **If a real database is ever introduced**, replace this section with the
  full schema design rules (single source of truth, normalisation,
  FK/UNIQUE/CHECK constraints, indexed query patterns, etc.) before any
  persistence code is written against it.

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
