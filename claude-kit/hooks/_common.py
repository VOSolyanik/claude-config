"""Shared helpers for the hook pack. Each hook imports this via sys.path (same dir).

Design rules (why + source):
- Fail OPEN on malformed input: hooks are guidance + audit, the hard layer is permission deny
  rules + sandbox ("use the permission system rather than a hook to enforce a hard allow or deny"
  - code.claude.com/docs/en/hooks; 04_hooks.md C3a).
- Print exactly one JSON object to stdout (stdout is parsed as JSON only if it is `{...}`;
  shell-profile echoes corrupt it - 04_hooks.md Q1/Q2).
- Keep injected strings < 10,000 chars: longer additionalContext/systemMessage is saved to a file
  and only a 2,000-char preview reaches Claude (CHANGELOG 2.1.89; 04_hooks.md Q1).
- State lives inside .git/ so it never shows up in diffs; writes are atomic because all matching
  hooks run in parallel (04_hooks.md Q2 "All matching hooks run in parallel").
"""
import datetime, hashlib, json, os, subprocess, sys

CAP = 9000  # stay under the 10,000-char additionalContext/systemMessage cap


def load_input():
    try:
        return json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open: no decision


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.exit(0)


def project_dir(data):
    # CLAUDE_PROJECT_DIR = session start root (does not follow worktrees); cwd follows them.
    return os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()


def clip(text, n=CAP):
    text = text or ""
    return text if len(text) <= n else text[: n - 40] + f"\n...[truncated {len(text) - n + 40} chars]"


def sh(cmd, cwd=None, timeout=20, env=None):
    """Run a shell command; return (exit_code, combined_output). 124 on timeout."""
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {cmd}"


def git(args, cwd):
    code, out = sh("git " + args, cwd=cwd, timeout=10)
    return out.strip() if code == 0 else ""


def state_dir(data):
    """Per-repo state OUTSIDE the tracked tree: inside .git (never shows in diffs),
    else the session scratchpad, else /tmp."""
    cwd = data.get("cwd") or os.getcwd()
    gd = git("rev-parse --absolute-git-dir", cwd)
    base = os.path.join(gd, "claude-hooks") if gd else (data.get("scratchpad_dir") or "/tmp/claude-hooks")
    os.makedirs(base, exist_ok=True)
    return base


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)  # atomic: parallel hooks never read a half-written file


def marker_exists(root, name, subdirs=("", "backend", "frontend", "api", "web", "app")):
    """Monorepo-aware marker lookup (uv.lock in backend/, pnpm-lock.yaml in frontend/ ...)."""
    extra = [d for d in os.environ.get("HOOK_MARKER_DIRS", "").split(",") if d]
    return any(os.path.exists(os.path.join(root, d, name)) for d in list(subdirs) + extra)


def local_bin(root, name, subdirs=("", "frontend", "web")):
    """node_modules/.bin/<name> or .venv/bin/<name> in the repo root or a known package dir."""
    for d in subdirs + ("backend",):
        for rel in (("node_modules", ".bin", name), (".venv", "bin", name)):
            p = os.path.join(root, d, *rel)
            if os.path.exists(p):
                return p
    return None


def tree_fingerprint(cwd):
    """Hash of HEAD + tracked diff + untracked file list/contents. Changes whenever code changes."""
    h = hashlib.sha256()
    h.update(git("rev-parse HEAD", cwd).encode())
    code, diff = sh("git diff HEAD --no-color --binary", cwd=cwd, timeout=20)
    h.update(diff.encode(errors="ignore"))
    for rel in git("ls-files --others --exclude-standard", cwd).splitlines():
        h.update(rel.encode())
        try:
            with open(os.path.join(cwd, rel), "rb") as f:
                h.update(f.read(1_000_000))
        except OSError:
            pass
    return h.hexdigest()[:16]


def append_lesson(data, rule_id, what, hint):
    """'Lesson on block' (compounding loop, 04_hooks.md C14): append one deduplicated line per
    (rule, action) to .claude/lessons/blocked.md; SessionStart re-injects the tail so the agent
    stops repeating the same blocked action. Promotion to CLAUDE.md/rules stays a human decision
    (auto-editing instructions from model output = memory-poisoning risk, 02_memory_context.md Q7)."""
    try:
        d = os.path.join(project_dir(data), ".claude", "lessons")
        os.makedirs(d, exist_ok=True)
        idx_path = os.path.join(d, ".blocked-index.json")
        idx = read_json(idx_path, {})
        key = hashlib.sha1(f"{rule_id}|{what}".encode()).hexdigest()[:12]
        entry = idx.get(key, {"count": 0})
        entry["count"] += 1
        entry["last"] = datetime.date.today().isoformat()
        idx[key] = entry
        write_json(idx_path, idx)
        if entry["count"] == 1:  # first occurrence -> human-readable line
            who = data.get("agent_type") or "main"
            with open(os.path.join(d, "blocked.md"), "a") as f:
                f.write(f"- {entry['last']} [{rule_id}] ({who}) blocked `{what[:140]}` -> {hint}\n")
    except Exception:
        pass  # lessons are best-effort, never break the guard
