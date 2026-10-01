---
name: harness-audit
description: Audit the AI-coding harness — global Claude Code config (~/.claude or $CLAUDE_CONFIG_DIR) and the current project (instruction files, settings, permissions, sandbox, hooks, subagents, skills, plugins, MCP, memory, evals, observability, cost) — interview the developer, and produce a prioritized, evidence-backed improvement plan. Read-only until changes are approved.
argument-hint: "[all|global|project] [quick|full]"
disable-model-invocation: true
effort: high
---

# Harness audit

You are auditing the AI-coding harness of the developer running this session: their global Claude Code configuration and the current project's. The goal is a prioritized, evidence-backed plan that makes the agent measurably better from session to session — fewer hallucinations, less rework, higher first-pass success, lower cost per successful task.

Arguments: $ARGUMENTS
- Scope: `all` (default), `global`, or `project`.
- Depth: `full` (default: all phases, 6–10 interview questions) or `quick` (phases 1–3, at most 3 questions, a one-page report).

## Ground rules

1. **Read-only until approval.** Without asking you may read files, list directories, and run inspection commands: `claude --version`, `claude --help`, `claude mcp list`, `claude plugin list`, `git status`, `git log`, `wc`, `jq`. Do not run hook scripts, eval runners, loops or project commands that have side effects. To test a hook's behavior, first read the script, then feed it a fixture JSON from a temp directory.
2. **Secrets stay secret.** Never print values of env vars, tokens, keys or credentials. Report only the key name and location, e.g. `ANTHROPIC_API_KEY in ~/.claude/settings.json:14 (value redacted)`. Do not open `.env*`, `~/.ssh`, cloud credential files or `~/.claude.json` OAuth fields; record only whether they exist and whether they are denied.
3. **Evidence for everything.** Each finding cites `file:line` or command output. Each recommendation carries an evidence grade — A measured, B consensus of ≥3 independent practitioners, C single credible source, D plausible but unverified — and says whether it depends on the model generation.
4. **Version reality.** The reference standard at the end reflects Claude Code v2.1.283 (September 2026). Run `claude --version` first. If a feature, flag, field or event from the standard is missing from `claude --help`, `/help` or the current docs (code.claude.com/docs), mark it unverified instead of recommending it. When current docs contradict this prompt, follow the docs and say so.
5. **Keep your context clean.** For sweeps larger than a handful of files, delegate read-only inventory to subagents (Explore) with a precise brief and a compact structured output format. Keep raw file dumps out of the main context.
6. **Don't duplicate built-in tools.** Use `/doctor prompt-audit` (v2.1.283+) for stale or conflicting instruction text and `/skill-doctor` for skill health; fold their findings into your report instead of re-deriving them.
7. **Language.** Talk to the developer in the language they write in; keep technical terms in English.

## Phase 1 — Environment

Establish:
- Claude Code version, OS, shell. Active config dir (`$CLAUDE_CONFIG_DIR` or `~/.claude`), other profiles (`~/.claude-*`), managed settings (macOS `/Library/Application Support/ClaudeCode/`, Linux/WSL `/etc/claude-code/`, Windows `C:\Program Files\ClaudeCode\`).
- Access mode only: subscription, API key, cloud provider, or a gateway via `ANTHROPIC_BASE_URL`. A non-Anthropic base URL auto-disables MCP tool search, which loads all MCP schemas upfront.
- Project: git root, languages and frameworks, package managers, the real test, lint, typecheck and format commands (from package.json, pyproject.toml, Makefile, justfile, CI workflows), CI provider, monorepo layout, approximate size.
- Other agents' config: AGENTS.md, `.cursor/rules`, `.github/copilot-instructions.md`, GEMINI.md, `.codex/`, `.clinerules`, `.kiro/`, `.windsurf`.

## Phase 2 — Inventory

Build one table: path | kind | lines/bytes | approx tokens (chars ÷ 3.5) | always-loaded or on-demand | last change (`git log -1` or mtime).

Global (config dir):
- CLAUDE.md and its `@imports`, `rules/`, settings.json, `agents/`, `skills/`, legacy `commands/`, `output-styles/`.
- Hook scripts referenced from settings, statusline command, keybindings.
- Plugins (installed, enabled, marketplaces) and user-scope MCP servers.
- Auto-memory size: `projects/<project>/memory/MEMORY.md` (the first 200 lines / 25 KB load every session).

Project:
- Instruction files: CLAUDE.md, `.claude/CLAUDE.md`, CLAUDE.local.md, AGENTS.md, nested CLAUDE.md/AGENTS.md.
- Settings: `.claude/settings.json`, `.claude/settings.local.json` and whether local files are git-ignored.
- Extensions: `.claude/rules/`, `.claude/agents/`, `.claude/skills/`, `.claude/commands/`, `.claude/hooks/`, `.mcp.json`.
- Agent-facing docs: progress, handoff, lessons, specs, ADRs.
- Evals: `evals/`, promptfoo, plugin eval cases.
- Automation and environment: CI jobs running `claude -p` or claude-code-action, devcontainer, observability (OTel env, Langfuse hooks).

Live views: ask the developer to run these and paste the output, in one batch: `/context`, `/doctor`, `/doctor prompt-audit`, `/usage` (the prompt-cache line), `/hooks`, `/permissions`, `/skill-doctor`. Skip any they decline.

## Phase 3 — Analysis

Score each area 0–4 with evidence. Check at least:

**A. Instructions** (CLAUDE.md, AGENTS.md, rules)
- Always-loaded volume across global, project and unscoped rules. Aim for ≤ ~200 lines per file; flag bloat.
- Keep only what can't be inferred from the code and can be verified: real commands, non-default conventions, boundaries (always / ask first / never), and "read X when Y" pointers.
- Flag:
  - repo overviews and generic advice;
  - rules the linter or formatter already enforces;
  - commands that no longer exist (check them against package scripts).
- Conflicts between global, project and rules. Files are concatenated and nothing overrides anything, so conflicts resolve arbitrarily.
- Old-model style: ALL-CAPS/MUST, "think carefully", "step by step", "double-check", "use a subagent to verify", temperature/top_p advice. Current models over-trigger on these, and effort is now the control.
- AGENTS.md interplay: Claude Code reads AGENTS.md natively only when no CLAUDE.md, `.claude/CLAUDE.md` or CLAUDE.local.md is on the path, so a CLAUDE.local.md silently disables it. For multi-tool repos, recommend AGENTS.md as the source of truth plus a CLAUDE.md that is `@AGENTS.md` and a few Claude-specific lines.
- Path-scoped rules are dropped by compaction. Anything that must survive goes unscoped or is re-injected by a SessionStart hook with the `compact` matcher.
- Block HTML comments are stripped before injection, so annotations cost nothing.

**B. Enforcement and security**
- Every must-never rule is enforced deterministically (permissions deny, sandbox, PreToolUse hook), not only in prose.
- Permissions:
  - deny rules for secrets (`~/.ssh`, cloud credentials, `.env*`, `.npmrc`, `.pypirc`, `.netrc`);
  - risky allows (`Bash(*)`, `Bash(npx *)`, `Bash(docker exec *)`, broad WebFetch);
  - `defaultMode` and any use of bypassPermissions.
- Sandbox is enabled (Linux needs bubblewrap and socat). `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` keeps secrets out of subprocess environments.
- Headless: `claude -p` in an untrusted checkout runs repo hooks and `.mcp.json` servers. CI should use `--bare`, pinned config and least-privilege tokens.
- Provenance of third-party plugins, skills, hooks and MCP servers; MCP servers with write access to production; the lethal trifecta (private data + untrusted content + an exfiltration channel).
- Dead config: keys that project files can no longer set (defaultMode `auto`/`bypassPermissions`, telemetry switches, `CLAUDE_CONFIG_DIR`).

**C. Verification gates** (highest leverage)
- A deterministic "done = green" gate: a Stop hook running fast tests, typecheck and lint, with loop protection (`stop_hook_active`, a retry counter, the built-in block cap).
- An independent verifier or reviewer in a fresh context, scheduled by the harness (hook, `/goal`, orchestrator), not by the worker. It should be read-only and end with an explicit verdict.
- Post-edit format and lint, targeted related tests, LSP diagnostics.
- Hook correctness:
  - blocking uses exit 2, never exit 1 (fail-open);
  - JSON fields are current (`hookSpecificOutput.permissionDecision`, not top-level `decision` on PreToolUse);
  - script paths exist and are executable (exit 127 silently disables a gate);
  - timeouts are set, latency is low, and state files are safe under parallel execution;
  - output stays ≤ 10k chars;
  - security hooks fail closed.

**D. Context and memory**
- Compaction window (`autoCompactWindow`).
- SessionStart re-hydration for `startup|resume|clear|compact`, a PreCompact handoff, progress and handoff files.
- Subagents used as context firewalls for heavy reading.
- Auto memory: size, stale or suspicious entries (memory poisoning is a documented attack), duplication with CLAUDE.md.
- Lessons loop: repeated mistakes should land in the cheapest correct place (hook > rule > skill > CLAUDE.md line), each with a retirement condition. Flag append-only growth.
- MCP tool-definition cost and tool-search status.

**E. Extensions**
- Skills:
  - descriptions say what and when (description + `when_to_use` ≤ 1,536 chars);
  - progressive disclosure, with scripts for deterministic steps;
  - `disable-model-invocation` for manual workflows;
  - overlapping triggers across local skills and plugins (for example superpowers vs local skills): keep one mechanism per function;
  - tested with trigger sets, `claude plugin eval` or `/skill-doctor`.
- Subagents:
  - explicit `model` and `effort` per role;
  - least-privilege tools (verifier and reviewer read-only);
  - a description that says when to use it;
  - `omitClaudeMd` where appropriate;
  - no duplicates of built-ins.
- Legacy `.claude/commands/` still work; commands were merged into skills in v2.1.3.
- Plugins: count, per-turn context cost, duplicate functionality, provenance.
- MCP: which servers are actually used; CLI plus a skill is often cheaper; project-scoped `.mcp.json`; read-only database users.

**F. Evals and observability**
- A task suite built from real tickets or fix commits (fail before the fix, pass after); pass@k and pass^k; a recorded baseline.
- A regression run on every config or model change.
- Headless eval runs assert that the config actually loaded, since `--bare` skips CLAUDE.md, hooks and skills.
- OTel enabled in user or managed scope (project settings are ignored for telemetry since v2.1.282), Langfuse or LangSmith, cost per successful task, prompt-cache hit rate (healthy is 85–98%).

**G. Workflow and autonomy**
- Task specs with runnable acceptance criteria for non-trivial work.
- Small commits, worktrees for parallel sessions, checkpoints and `/rewind`.
- `/goal` or a fresh-context loop for long runs: proof-bearing completion conditions, budget caps, drift checks.
- PR review integration.
- The autonomy mode matches the risk.

**H. Cost**
- Default model and effort, and per-role routing.
- Cache breakers: model switches (including skills that set `model:`), effort changes, MCP toggles, fast mode, idle gaps longer than the cache TTL.
- Fan-out only for long, independent work.
- `--max-budget-usd` on headless runs.

## Phase 4 — Interview

Ask after the analysis, so questions are informed; never ask what the files already answer. Use AskUserQuestion when available: at most 4 questions per batch, 2–3 batches, recommended option first. Cover:
- Goals and pain points: hallucinated APIs, ignored rules, rework, context lost after compaction, slowness, cost.
- Work mode: solo or team, how critical the repo is, autonomy tolerance (manual / auto / sandboxed bypass), unattended runs.
- Plan and budget: subscription tier or API, cost sensitivity.
- Constraints: client perimeter and compliance, data that must not leave, multiple profiles or accounts.
- Other agents in use (Codex, Cursor, Copilot…), which decides the AGENTS.md strategy.
- Time available for improvements this week and this month, and which metrics they already track.
- Confirm your assumptions: "I see X — is it intentional?"

## Phase 5 — Report

Write the report in the developer's language to `.claude/audits/harness-audit-<YYYY-MM-DD>.md` in the project (ask first if `.claude/` is tracked), or to `~/claude-audits/` for a global-only audit. Sections:
1. **Summary:** maturity level 0–4, the top 5 problems, and the top 5 wins ranked by impact ÷ effort.
2. **Scorecard A–H:** score, key evidence, one-line verdict.
3. **Findings table:** # | area | severity (critical/high/medium/low) | finding | evidence | recommendation | grade | effort S/M/L | expected effect.
4. **Roadmap:** now (≤1 h), week 1, month 1, quarter.
5. **Proposed changes** as concrete diffs or snippets (not applied), each with a rollback note.
6. **Metrics plan:** what to baseline now (first-pass success, corrections per session, rework rate, cost per successful task, cache hit %, gate blocks, verifier FAIL rate, escaped defects) and how to collect it.
7. **Unverified items and open questions.**

Maturity levels:
- **0 Ad hoc:** no instruction files, or only generated ones.
- **1 Documented:** curated CLAUDE.md or AGENTS.md.
- **2 Deterministically constrained:** deny rules, sandbox, guard hooks.
- **3 Verified and observable:** done = green gate, an independent verifier, state files, usage and cost telemetry.
- **4 Measured and self-improving:** an eval suite gates config and model changes, a lessons loop with pruning, A/B comparisons.

## Phase 6 — Apply (only on approval)

Apply the approved items one at a time:
1. Back up each file you touch (`<file>.bak-<date>`).
2. Make the smallest change that works.
3. Validate: JSON with `jq`, hooks with fixture input, flags with `claude --help`.
4. Show the diff and record it in the report.

Never modify managed settings, weaken existing deny rules, or commit or push unless the developer explicitly asks.

## Reference standard (Claude Code v2.1.283, September 2026 — verify before relying)

**Highest-leverage practices:**
1. **"Done = green" Stop gate** (B). Measured: a harness-only change took one model from 52.8% to 66.5% on Terminal-Bench 2.0, driven mainly by a self-verification loop.
2. **Deterministic must-nevers** via deny rules, sandbox and PreToolUse hooks (B).
3. **Independent verifier in a fresh context,** scheduled by the harness (B). Vendor-measured: Anthropic Code Review raised the share of PRs with substantive comments from 16% to 54%.
4. **Evals from your own fix commits** gate config and model changes; pass^k measures reliability (B, method A).
5. **Instructions carry only non-inferable information** (A). ETH 2026: human-written context files +2.4% (not significant); LLM-generated ones −0.5 to −2% success and +20–23% cost. 62% of files duplicate linter rules.
6. **Prompt cache as infrastructure:** a stable prefix and fixed model and effort per session. Uncached, a typical feature costs about 8× more.
7. **Context hygiene:** one task per session, `/clear` with a fresh brief after two failed corrections, `/rewind` instead of stacking corrections (B).
8. **State lives in files** (plan, progress, handoff, feature list) and is re-hydrated by SessionStart (B).
9. **Targeted test context instead of a bare "do TDD"** (A, one study). A bare TDD instruction raised regressions from 6.08% to 9.94%; naming the relevant tests cut them to 1.82%.
10. **Explicit model and effort per role;** fan-out only for long, independent work (B/C).
11. **Subagents as context firewalls** (B).
12. **Post-edit format and lint** (B).
13. **Observability:** OTel in user scope, cost per successful task (B).
14. **Lessons loop** with a value bar and retirement conditions (B).
15. **Long loops:** fresh context per iteration, proof-bearing completion, budget caps, drift checks (B).

**Outdated or harmful in 2026:**
- ALL-CAPS/MUST, "think carefully", "double-check", and plan mode by default on Opus 5.x.
- temperature/top_p, which return 400 on current Claude models except Haiku 4.5.
- exit 1 in blocking hooks, and top-level `decision` on PreToolUse.
- expecting project settings to control OTel or `defaultMode: auto`.
- treating "handoff instead of compaction" as settled; Amp reversed it in May 2026.
- The `/agents` wizard has been removed. `Task(...)` is now `Agent(...)`; the alias still works.

**Facts:**
- **Hooks:**
  - 33 events and 5 handler types: command, http, mcp_tool, prompt, and agent (experimental).
  - Stop and SubagentStop blocks are capped at 8 in a row (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`).
  - `permissionDecision` is `allow|deny|ask|defer`.
  - UserPromptSubmit can block a prompt or add context but cannot rewrite it.
- **Sessions and subagents:**
  - Subagent nesting defaults to 3 levels.
  - Auto mode is the default for interactive sessions; `-p` starts in Manual.
- **Built-in commands and limits:**
  - `/goal` is a session-scoped, prompt-based Stop hook with a Haiku judge, so the condition must make Claude print its own proof.
  - `claude plugin eval` has 6 grader types, defaults to 3 runs, threshold 1.0 and a no-plugin baseline.
  - `/doctor prompt-audit` flags instructions written for older models.
  - The skill listing gets about 1% of the context window.
  - `--safe-mode` starts with all customizations off, which helps isolate a broken config.
