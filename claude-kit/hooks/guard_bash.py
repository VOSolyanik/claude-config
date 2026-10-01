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
# rm may delete inside the project and under /tmp, judged by the real path (symlinks resolved); never the
# roots themselves, .git, ancestors of the project or home, or anything else. Owner's intent (01.10.2026):
# agents clean build artifacts and temp files without asking. Temp files live in /tmp/<name>, not $TMPDIR.
TMP_ROOTS = ("/tmp", "/private/tmp")
GLOB = re.compile(r"[*?[]")
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


def _under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _expand_home(t, home):
    if t == "~" or t.startswith("~/"):
        return home + t[1:]
    for v in ("$HOME", "${HOME}"):
        if t == v or t.startswith(v + "/"):
            return home + t[len(v):]
    return t


def _real(path):
    """Where rm acts: realpath(dirname) + basename. `x` removes a symlink x itself; for `x/` the dirname is
    x, so the link is followed."""
    return os.path.normpath(os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path)))


def rm_target_problem(t, cwd, root, home):
    """Why rm must not touch target t, or None."""
    if t in DANGEROUS_RM_TARGETS:
        return "a protected target"
    t = _expand_home(t, home)
    if "$" in t or "`" in t:
        return "an unexpanded variable; use a literal path"
    named = os.path.join(cwd, t)
    path = _real(named)
    root_r, home_r = os.path.realpath(root), os.path.realpath(home)
    tmp_r = tuple({os.path.realpath(r) for r in TMP_ROOTS} | set(TMP_ROOTS))
    parts = path.split("/")
    if ".git" in parts:
        return "inside .git"
    globbed = [k for k, part in enumerate(parts) if GLOB.search(part)]
    if globbed:  # a glob stands for everything in its directory: judge that directory
        path = "/".join(parts[:globbed[0]]) or "/"
    if path in tmp_r:
        return "the temp root itself"
    if path == root_r:
        return "the project root"
    if _under(root_r, path) or _under(home_r, path):
        return "an ancestor of the project or home"
    in_project = _under(path, root_r)
    if not (in_project or any(_under(path, r) for r in tmp_r)):
        return "outside the project and /tmp"
    lexical = os.path.normpath(named)
    if not in_project and (_under(lexical, root) or _under(lexical, root_r)):
        return "a symlink that leads out of the project"
    return None


def rm_danger(cmd, cwd, root, home):
    """First (target, why) rm must not touch, or None. Without a project root every rm is refused."""
    for seg in re.split(r"&&|\|\||;|\||\n", cmd):
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in ("sudo", "command", "exec")):
            toks = toks[1:]
        if not toks or os.path.basename(toks[0]) != "rm":
            continue
        targets, opts_done = [], False
        for t in toks[1:]:
            if not opts_done and t == "--":
                opts_done = True
            elif opts_done or not t.startswith("-"):
                targets.append(t)
        for t in targets:
            if not root:
                return t, "no project root (CLAUDE_PROJECT_DIR and the hook's cwd are both missing)"
            why = rm_target_problem(t, cwd, root, home)
            if why:
                return t, why
    return None


def main():
    data = load_input()
    cmd = (data.get("tool_input") or {}).get("command") or ""
    hits = []
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ""
    root = os.path.normpath(root) if root else ""
    cwd = os.path.normpath(data.get("cwd") or root or ".")
    found = rm_danger(cmd, cwd, root, os.path.normpath(os.path.expanduser("~")))
    if found:
        t, why = found
        hits.append(("rm-dangerous-target", "deny",
                     f"`rm` on `{t}` is blocked: {why}. rm may delete inside the project or under /tmp, "
                     "e.g. `rm -rf node_modules dist` or `rm -rf /tmp/<name>`."))
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
