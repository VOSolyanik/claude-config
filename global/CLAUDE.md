# Global preferences

## Engineering defaults

- Prefer composition over inheritance.
- Prefer explicit over clever. Readable beats terse.
- Root-cause fixes, never suppression. If I ask you to silence an error, ask why first.
- Small, reversible steps. One concern per commit.
- KISS before DRY before YAGNI. Abstract on the third occurrence, not the second.

## Workflow

- For tasks with 3+ steps or architectural impact, **enter plan mode first** (`Shift+Tab`).
- Before marking any task complete, run the project's tests and linter.
- Before committing, show me the diff summary and the commands you ran to verify.
- If stuck or uncertain, ask **one** clarifying question rather than guessing.
- Save plans to `tasks/todo.md` with checkable items.
- After corrections from me, update `tasks/lessons.md` so we don't repeat mistakes.
- At session start, check `tasks/lessons.md` for patterns to avoid.

## Commits and PRs

- Conventional commit format: `type(scope): subject`. Types: feat, fix, refactor, test, docs, chore, perf.
- Subject under 72 chars, imperative mood ("add X" not "added X").
- One concern per commit. Long PR descriptions are fine; long commit subjects aren't.
- Never push to main/master directly. Never `git push --force` without asking.

## Context hygiene

- Use `/clear` between unrelated tasks.
- Delegate exploration of >5 files to subagents so main context stays lean.
- If you've tried the same approach twice and it's not working, stop and re-plan.
- Use `/compact` before context hits 80%.

## Never do

- Don't `rm -rf` anything outside the project scope.
- Don't write secrets, API keys, or passwords into files.
- Don't modify `.env` files or anything under `secrets/`.
- Don't create commits with "Co-Authored-By: Claude" unless I've explicitly enabled it for a project.
- Don't add features or abstractions beyond what the task requires.
