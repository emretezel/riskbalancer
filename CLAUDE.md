# AGENTS.md

## Purpose
This file gives Codex repo-specific operating instructions for RiskBalancer.
Keep it concise and practical. Use `README.md` for end-user workflow details instead of repeating them here.

## Repo Shape
- Package code lives under `src/riskbalancer/`.
- Tests live under `tests/`.
- Configuration lives under `config/`.
- The CLI entry point is `riskbalancer`, exposed from `riskbalancer.cli:main`.

## Agent Operating Rules
- Enter plan mode before taking on non-trivial implementation, review, or verification work.
- For non-trivial tasks, write a concrete spec up front so the implementation path is clear before editing starts.
- Inspect the relevant code and tests before changing behavior; do not guess about module boundaries, adapter flow, or CLI behavior.
- Keep changes focused and consistent with the existing structure unless the task explicitly calls for a larger refactor.
- Preserve current CLI and configuration behavior unless the task explicitly asks for a behavior change.
- Update tests when behavior changes or when parsing, reporting, or configuration logic moves.
- Never overwrite, revert, or reformat unrelated user changes in a dirty worktree.
- Treat local edits you did not make as user-owned. In particular, do not touch `config/categories.yaml` unless the task is explicitly about portfolio configuration.
- When delegation is allowed by the runtime, use focused subagents freely for research, exploration, and parallel analysis. Give each subagent one tightly scoped task.
- When a mistake pattern repeats, capture it in `tasks/lessons.md` and strengthen the preventive rule until the pattern stops recurring.
- Do not mark work complete without evidence that it works.
- For non-trivial changes, pause for an elegance pass. If the current fix is a patchy workaround and a cleaner design is now obvious, replace it with the cleaner design. Skip that pass for simple fixes.
- Challenge your own work before handing it back.
- After any user correction, append the lesson to `tasks/lessons.md`.

## File Safety
- Do not modify `private/`, `portfolios/`, or `reports/` unless the user explicitly asks. These paths hold private inputs or generated artifacts.
- Pause before changing `config/categories.yaml`, `config/fx.example.yaml`, or files under `config/mappings/` unless the task is specifically about portfolio or category configuration.
- Documentation, package code, tests, and repo metadata are safe to edit when needed for the task.

## Build, Test, and Development Commands
- Run project commands in the `riskbalancer` conda environment.
- Install the editable package and dev tools with `conda run -n riskbalancer python -m pip install -e '.[dev]'`.
- Run the full test suite with `conda run -n riskbalancer pytest`.
- Check the CLI entry point with `conda run -n riskbalancer riskbalancer --help`.
- Use the command examples in `README.md` when you need end-to-end workflow references.

## Coding Style and Naming
- Use four-space indentation and standard Python naming: `snake_case` for functions and modules, `CapWords` for classes, and `UPPER_SNAKE_CASE` for constants.
- Keep type hints and docstrings where they already exist, and follow the style already used in `src/riskbalancer/cli.py`.
- Format Python files with `ruff format` and keep edits aligned with the surrounding style.
- New Python modules should begin with a short triple-quoted module docstring that explains the module and names the author.
- Add comments for non-obvious business rules or parsing logic, but avoid commentary that only repeats the code.

## Testing Guidelines
- The test framework is `pytest`, configured in `pyproject.toml`.
- Use the `riskbalancer` conda environment when running tests.
- Test files should be named `tests/test_*.py`, and test functions should be named `test_*`.
- New features and behavior changes should ship with new or updated unit tests.
- Prefer targeted tests while iterating, then run the full suite before handoff when shared logic changes.

## Formatting and Static Checks
- After each Codex change, run `conda run -n riskbalancer ruff format .`.
- Then run `conda run -n riskbalancer ruff check .`.
- Then run `conda run -n riskbalancer mypy src/riskbalancer`.
- If formatting, lint, or typing checks fail, fix the issues before returning the work.

## Commit and PR Guidance
- Keep commit subjects short, imperative, and optionally scoped. Do not end the subject with a period.
- For every change, make an explicit documentation decision. Update docs when behavior, workflow, or contributor guidance changes; otherwise state why no doc change was needed.

## Response Expectations
- Summarize what changed and call out assumptions.
- Report the exact validation performed, including tests, CLI checks, formatting, lint, and typing runs.
- If any check was skipped, say so explicitly and explain why.
