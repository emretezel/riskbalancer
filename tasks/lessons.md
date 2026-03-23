# Lessons

This file records repeatable Codex mistake patterns for this repo.
Append new lessons instead of rewriting old ones so the history stays useful.

## Entry Template
- Date:
- Trigger:
- Pattern:
- Preventive rule:
- Follow-up:

## Lessons

### 2026-03-23: Verify repo-specific workflow assumptions first
- Trigger: Repo guidance referenced `src/pyvalue`, missing tooling, and a nonexistent `tasks/lessons.md` path before those assumptions were checked against the actual repository.
- Pattern: Reusing workflow rules from another project without confirming local paths, environments, or tool availability.
- Preventive rule: Before adding or tightening AGENTS instructions, verify the repo paths, environment names, CLI commands, and installed tools in the current project.
- Follow-up: Prefer validated repo-local paths such as `src/riskbalancer`, and create missing workflow files deliberately instead of assuming they already exist.

### 2026-03-23: Tighten manual CLI inputs when the user wants explicit data entry
- Trigger: Manual `portfolio add` still allowed omitted categories and implicit mapping reuse after the user clarified that category should be required.
- Pattern: Preserving convenience fallbacks when the user has explicitly asked for stricter input requirements.
- Preventive rule: When the user tightens a CLI contract, remove or disable conflicting fallback paths instead of leaving them available behind optional arguments.
- Follow-up: Treat manual portfolio additions as explicit user-entered data and require `--category` at the parser and command layers.

### 2026-03-23: Keep reversible UX changes easy when the user is still shaping the workflow
- Trigger: After tightening `portfolio add` to require `--category`, the user asked to restore the earlier manual mapping reuse flow.
- Pattern: Making a UX contract stricter in a way that throws away a useful fallback path while the user is still iterating on the desired workflow.
- Preventive rule: When a workflow preference is still moving, keep the implementation easy to relax again and avoid deleting proven convenience paths unless the user clearly wants them gone for good.
- Follow-up: Restore optional manual mappings for `portfolio add` and treat explicit categories plus reusable saved mappings as compatible modes.
