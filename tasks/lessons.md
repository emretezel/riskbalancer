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
