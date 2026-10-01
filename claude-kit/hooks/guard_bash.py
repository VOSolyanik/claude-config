#!/usr/bin/env python3
"""PreToolUse (matcher: Bash|PowerShell) - destructive-command guard.
deny  -> Claude sees the reason and adapts; ask -> user confirms (forces a prompt even in auto mode).
Every block appends a lesson (see _common.append_lesson).
Why: docs example block-rm.sh + anthropics/claude-code bash_command_validator_example.py. Regex guards are
bypassable (python -c, eval, base64) - they are guidance + audit; deny rules + sandbox enforce. A timed-out
command hook does NOT block, so keep it fast. Grade B (practice) / C (security value) - 04_hooks.md C3a."""
import os, re, shlex, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import load_input, emit, append_lesson

DANGEROUS_RM_TARGETS = {"/", "/*", "~", "~/", "$HOME", "${HOME}", "*", ".", "./", "..", "../", "./*"}
# (rule_id, decision, regex on full command, hint)
RULES = [
    ("no-verify", "deny", r"\bgit\s+(commit|push)\b[^|;&]*\s(--no-verify|-n)\b",
     "Do not skip git hooks; fix the failing pre-commit/pre-push check instead."),
    ("force-push", "deny", r"\bgit\s+push\b[^|;&]*\s(--force(?!-with-lease)|-f)\b",
     "Force-push is blocked, including --force-with-lease; leave history rewrites to the owner."),
    ("force-push-main", "deny", r"\bgit\s+push\b[^|;&]*--force-with-lease[^|;&]*\b(main|master)\b",
     "Never rewrite main/master history."),
    ("curl-pipe-sh", "deny", r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b",
     "Download the script, inspect it, then run it explicitly."),
    ("chmod-777", "deny", r"\bchmod\s+(-R\s+)?0?777\b", "Use least-privilege modes (e.g. 755/644)."),
    ("disk-wipe", "deny", r"\b(mkfs(\.\w+)?|dd\s+[^|;&]*of=/dev/)", "Disk-level writes are never needed here."),
    ("fork-bomb", "deny", r":\(\)\s*\{\s*:\|:&\s*\};:", "Fork bomb."),
    ("hard-reset", "ask", r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f|checkout\s+--\s+\.|restore\s+(--worktree\s+)?\.)",
     "Discards uncommitted work. Prefer `git stash push -u` first."),
    ("sql-destructive", "ask", r"(?i)\b(drop\s+(database|schema|table)|truncate\s+table|delete\s+from\s+\w+\s*(;|$))",
     "Destructive SQL: confirm target DB is disposable (never prod)."),
    ("infra-destroy", "ask", r"\b(terraform\s+(destroy|apply\s+[^|;&]*-auto-approve)|kubectl\s+delete|aws\s+s3\s+rm\s+[^|;&]*--recursive|az\s+group\s+delete|alembic\s+downgrade\s+base)\b",
     "Infrastructure/data destruction needs human confirmation."),
]


def rm_danger(cmd):
    for seg in re.split(r"&&|\|\||;|\||\n", cmd):
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in ("sudo", "command", "exec")):
            toks = toks[1:]
        if not toks or os.path.basename(toks[0]) != "rm":
            continue
        flags = "".join(t.lstrip("-") for t in toks[1:] if t.startswith("-"))
        recursive = "r" in flags.lower() or "--recursive" in toks
        targets = [t for t in toks[1:] if not t.startswith("-")]
        for t in targets:
            if t in DANGEROUS_RM_TARGETS or (recursive and re.fullmatch(r"/[^/]*/?", t)):
                return t
    return None


def main():
    data = load_input()
    cmd = (data.get("tool_input") or {}).get("command") or ""
    hits = []
    t = rm_danger(cmd)
    if t:
        hits.append(("rm-dangerous-target", "deny", f"`rm` on `{t}` is blocked. Delete explicit sub-paths (e.g. `rm -r build/`)."))
    for rule_id, decision, rx, hint in RULES:
        if re.search(rx, cmd):
            hits.append((rule_id, decision, hint))
    if not hits:
        sys.exit(0)  # no decision -> normal permission flow (silence never approves)
    decision = "deny" if any(h[1] == "deny" for h in hits) else "ask"
    reason = " | ".join(f"[{r}] {h}" for r, _, h in hits)
    for r, d, h in hits:
        if d == "deny":
            append_lesson(data, r, cmd, h)
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                 "permissionDecision": decision,
                                 "permissionDecisionReason": f"Blocked by guard_bash: {reason}"}})


if __name__ == "__main__":
    main()
