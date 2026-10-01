#!/usr/bin/env python3
"""PreToolUse (matcher: Bash|PowerShell) - vets packages before an agent adds them to a project.

Catches `npm install|i|add <pkg>`, `pnpm/yarn/bun add`, `pip/pip3 install`, `python -m pip install`,
`uv add`, `uv pip install`, `poetry add`. For every named package it asks the public registry
(npm or PyPI) and OSV:
  not in the registry          -> deny  (likely a hallucinated name - "slopsquatting")
  OSV has a MAL-* advisory     -> deny
  one edit away from a popular package and not itself widely used -> deny with the likely name
  younger than 7 days, or very few weekly downloads -> ask
  registry or OSV unreachable, or a source we cannot vet (path, URL, git, custom index) -> ask
  otherwise -> no decision.

It never answers "allow": a hook allow would approve the whole Bash command, including whatever
else is chained after the install, and skip the permission rules and the auto-mode classifier.
Silence hands the command to the normal permission flow instead.
Installs without package names (lockfile installs such as `npm install`, `pnpm install`, `uv sync`)
are left alone and make no network calls.

The same checks run for manifest edits (Edit|Write|MultiEdit on package.json, pyproject.toml,
requirements*.txt): the edit is applied to the current file in memory, and only dependencies the new
version ADDS are vetted. Otherwise an agent could write a package into the manifest and run a bare
`npm install`, which names no package. pyproject.toml needs Python 3.11+ (tomllib); on older Pythons
pyproject edits get no decision. `workspace:` dependencies are local and skipped.

Fails open on malformed input, like the rest of the hook pack (_common.py). Network budget is kept
under the 10 s hook timeout: 3 s per request, 8 s for the whole run.
DEP_GATE_OFFLINE=1 disables the network (every lookup then counts as unavailable -> ask).
"""
import concurrent.futures
import datetime
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

NPM_REGISTRY = "https://registry.npmjs.org/"
NPM_DOWNLOADS = "https://api.npmjs.org/downloads/point/last-week/"
PYPI_JSON = "https://pypi.org/pypi/"
PYPI_STATS = "https://pypistats.org/api/packages/"
OSV_QUERY = "https://api.osv.dev/v1/query"

MIN_AGE_DAYS = 7
MIN_WEEKLY_DOWNLOADS = 100
TYPO_SAFE_DOWNLOADS = 50_000  # a package used this much is not treated as a typosquat
REQUEST_TIMEOUT = 3
TOTAL_BUDGET = 8

# Widely used packages that typosquats imitate. Short names (< 4 chars) are skipped in the
# comparison because one edit away from "pg" or "ws" is mostly noise.
POPULAR = {
    "npm": """react react-dom lodash express axios typescript vite vitest jest webpack next vue
        @angular/core @angular/common rxjs zod eslint prettier chalk commander debug dotenv moment
        dayjs uuid classnames tailwindcss postcss autoprefixer @babel/core react-router
        react-router-dom redux @reduxjs/toolkit mongoose mysql2 sequelize prisma @prisma/client
        nodemon cors body-parser jsonwebtoken bcrypt socket.io yargs inquirer glob rimraf fs-extra
        cross-env concurrently husky lint-staged ts-node tsx esbuild rollup svelte solid-js
        @types/node node-fetch semver minimist colors""".split(),
    "pypi": """requests numpy pandas django flask fastapi pydantic sqlalchemy pytest boto3 botocore
        urllib3 setuptools wheel pip six python-dateutil pyyaml jinja2 click uvicorn gunicorn celery
        redis psycopg2 psycopg2-binary psycopg alembic httpx aiohttp scipy matplotlib scikit-learn
        tensorflow torch transformers pillow beautifulsoup4 lxml selenium black ruff mypy flake8
        isort poetry rich typer attrs cryptography certifi charset-normalizer idna packaging openai
        anthropic langchain tqdm pyjwt python-dotenv marshmallow starlette orjson""".split(),
}

SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
# A shell redirection token: "2>&1", ">", ">>", "2>", "&>/dev/null", ">log.txt", "<in".
REDIRECT = re.compile(r"^(\d*|&)(>>?|<)(.*)$")


class Install:
    """One package an install command would add. `unvettable` explains why it cannot be checked."""

    def __init__(self, ecosystem, name, unvettable="", spec=None):
        self.ecosystem, self.name, self.unvettable = ecosystem, name, unvettable
        self.spec = spec or name  # as written in the command, e.g. "left-pad@1.1.0"

    def __repr__(self):
        return f"Install({self.ecosystem!r}, {self.name!r}, {self.unvettable!r})"


# ---------------------------------------------------------------- parsing

# Per tool: flags that take a value (skipped together with it) and flags that point at a source
# we cannot vet (custom registry or index, requirement files, editable installs).
NPM_FLAGS = {"value": {"--prefix", "-w", "--workspace", "--tag", "--save-prefix"},
             "unvettable": {"--registry"}}
PNPM_FLAGS = {"value": {"--filter", "-F", "--dir", "-C"}, "unvettable": {"--registry"}}
YARN_BUN_FLAGS = {"value": {"--cwd"}, "unvettable": {"--registry"}}
PIP_FLAGS = {"value": {"-c", "--constraint", "-t", "--target", "--prefix", "--root", "--python-version",
                       "--platform", "--only-binary", "--no-binary", "--python", "-p"},
             "unvettable": {"-r", "--requirement", "--requirements", "-e", "--editable", "-i", "--index-url",
                            "--extra-index-url", "-f", "--find-links", "--index", "--default-index"}}
UV_ADD_FLAGS = {"value": {"--group", "--optional", "--package", "--python", "-p"},
                "unvettable": PIP_FLAGS["unvettable"]}
POETRY_FLAGS = {"value": {"-G", "--group", "-E", "--extras", "--python", "--platform"},
                "unvettable": {"--source"}}


def _tokens(segment):
    try:
        toks = shlex.split(segment, posix=True)
    except ValueError:
        toks = segment.split()
    while toks and (re.match(r"^\w+=", toks[0]) or toks[0] in ("sudo", "command", "exec")):
        toks = toks[1:]
    return toks


def _install_args(toks):
    """(ecosystem, flag table, args) if the tokens are an install that names packages, else None."""
    if not toks:
        return None
    tool, rest = os.path.basename(toks[0]), toks[1:]
    sub = rest[0] if rest else ""
    if tool == "npm" and sub in ("install", "i", "add"):
        return "npm", NPM_FLAGS, rest[1:]
    if tool == "pnpm" and sub in ("add", "install", "i"):
        return "npm", PNPM_FLAGS, rest[1:]
    if tool in ("yarn", "bun") and sub in ("add", "install", "i"):
        return "npm", YARN_BUN_FLAGS, rest[1:]
    if tool in ("pip", "pip3") and sub == "install":
        return "pypi", PIP_FLAGS, rest[1:]
    if re.fullmatch(r"python[\d.]*", tool) and rest[:3] == ["-m", "pip", "install"]:
        return "pypi", PIP_FLAGS, rest[3:]
    if tool == "uv" and sub == "add":
        return "pypi", UV_ADD_FLAGS, rest[1:]
    if tool == "uv" and rest[:2] == ["pip", "install"]:
        return "pypi", PIP_FLAGS, rest[2:]
    if tool == "poetry" and sub == "add":
        return "pypi", POETRY_FLAGS, rest[1:]
    return None


def _npm_spec(spec):
    if spec.startswith((".", "/", "~", "file:", "git+", "git:", "http:", "https:", "github:")) or "@npm:" in spec:
        return None, "not a registry package"
    if spec.startswith("@"):
        at = spec.find("@", 1)
        name = spec if at < 0 else spec[:at]
        return (name, "") if "/" in name else (None, "not a registry package")
    if "/" in spec:
        return None, "looks like a GitHub shorthand, not a registry package"
    return spec.split("@")[0], ""


def _pypi_spec(spec):
    if spec.startswith((".", "/", "~", "git+")) or "://" in spec or "@" in spec \
            or spec.endswith((".whl", ".tar.gz", ".zip")):
        return None, "not a registry package"
    m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec)
    return (m.group(0), "") if m else (None, "cannot read a package name")


def parse_installs(cmd):
    """Every package that the command would add, in order; empty for lockfile installs."""
    found = []
    for segment in SEGMENT_SPLIT.split(cmd or ""):
        parsed = _install_args(_tokens(segment))
        if not parsed:
            continue
        ecosystem, flags, args = parsed
        if "--help" in args or "-h" in args:
            continue  # prints usage, installs nothing
        start, custom_source = len(found), ""
        i = 0
        while i < len(args):
            arg = args[i]
            i += 1
            redirect = REDIRECT.match(arg)
            if redirect:
                if redirect.group(3) == "":
                    i += 1  # the target is the next token
                continue
            if arg.startswith("-"):
                flag = arg.split("=", 1)[0]
                if flag in flags["unvettable"]:
                    value = arg.split("=", 1)[1] if "=" in arg else (args[i] if i < len(args) else "")
                    if "=" not in arg:
                        i += 1
                    found.append(Install(ecosystem, value or flag, f"{flag} points at a source dep_gate cannot vet"))
                    custom_source = custom_source or flag
                elif flag in flags["value"] and "=" not in arg:
                    i += 1
                continue
            name, why = (_npm_spec if ecosystem == "npm" else _pypi_spec)(arg)
            found.append(Install(ecosystem, name or arg, why, spec=arg))
        if custom_source:
            # packages may come from that other registry or index; the public one says nothing about them
            for inst in found[start:]:
                inst.unvettable = inst.unvettable or f"installed with {custom_source}; the public registry cannot vouch for it"
    return found


# ---------------------------------------------------------------- checks

def unquote_name(text):
    return urllib.parse.unquote(text.split("?")[0])


def _norm(ecosystem, name):
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "pypi" else name.lower()


def _osa(a, b):
    """Optimal string alignment distance: insertions, deletions, substitutions, adjacent swaps."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def typosquat_target(ecosystem, name):
    """The popular package this name imitates, or None."""
    me = _norm(ecosystem, name)
    popular = {_norm(ecosystem, p) for p in POPULAR[ecosystem]}
    if me in popular:
        return None
    for p in sorted(popular):
        if len(p) >= 4 and _osa(me, p) <= 1:
            return p
    return None


def _parse_time(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


class Unavailable(Exception):
    pass


def _get(fetch, url, payload=None):
    try:
        status, body = fetch(url, payload)
    except Exception as e:  # network errors, timeouts, bad JSON
        raise Unavailable(f"{url.split('/')[2]} unavailable ({type(e).__name__})")
    if status == 404:
        return None
    if status != 200:
        raise Unavailable(f"{url.split('/')[2]} unavailable (HTTP {status})")
    return body


def _registry_facts(fetch, ecosystem, name):
    """(exists, created, weekly_downloads or None)."""
    if ecosystem == "npm":
        meta = _get(fetch, NPM_REGISTRY + urllib.parse.quote(name, safe="@"))
        if meta is None:
            return False, None, None
        created = (meta.get("time") or {}).get("created")
        try:
            dl = _get(fetch, NPM_DOWNLOADS + name)
            downloads = (dl or {}).get("downloads")
        except Unavailable:
            downloads = None
        return True, _parse_time(created) if created else None, downloads
    canonical = _norm("pypi", name)
    meta = _get(fetch, PYPI_JSON + canonical + "/json")
    if meta is None:
        return False, None, None
    uploads = [f.get("upload_time_iso_8601") for files in (meta.get("releases") or {}).values() for f in files]
    uploads = [u for u in uploads if u]
    created = min(_parse_time(u) for u in uploads) if uploads else None
    try:
        stats = _get(fetch, PYPI_STATS + canonical + "/recent")
        downloads = ((stats or {}).get("data") or {}).get("last_week")
    except Unavailable:
        downloads = None
    return True, created, downloads


def _malicious(fetch, ecosystem, name):
    body = _get(fetch, OSV_QUERY, {"package": {"name": name, "ecosystem": "npm" if ecosystem == "npm" else "PyPI"}})
    return [v["id"] for v in (body or {}).get("vulns", []) if str(v.get("id", "")).startswith("MAL-")]


def vet(install, fetch, now):
    """(decision or None, finding) for one package."""
    label = f"{install.name} ({install.ecosystem})"
    if install.unvettable:
        return "ask", f"{label}: {install.unvettable}"
    try:
        exists, created, downloads = _registry_facts(fetch, install.ecosystem, install.name)
        if not exists:
            hint = typosquat_target(install.ecosystem, install.name)
            extra = f"; did you mean {hint}?" if hint else ""
            return "deny", f"{label}: not in the registry - likely a hallucinated name{extra}"
        mal = _malicious(fetch, install.ecosystem, install.name)
    except Unavailable as e:
        return "ask", f"{label}: {e}; cannot vet it"
    if mal:
        return "deny", f"{label}: OSV marks it malicious ({', '.join(mal)})"
    target = typosquat_target(install.ecosystem, install.name)
    if target and (downloads is None or downloads < TYPO_SAFE_DOWNLOADS):
        return "deny", f"{label}: one edit away from the popular package '{target}' - likely a typosquat"
    if created is not None and (now - created).days < MIN_AGE_DAYS:
        return "ask", f"{label}: first published {(now - created).days} days ago"
    if downloads is not None and downloads < MIN_WEEKLY_DOWNLOADS:
        return "ask", f"{label}: only {downloads} downloads last week"
    return None, ""


def decide(cmd, fetch, now=None):
    """(decision, reason) for a whole Bash command: deny beats ask beats no decision."""
    return decide_installs(parse_installs(cmd), fetch, now)


def decide_installs(installs, fetch, now=None):
    """(decision, reason) for a list of packages: deny beats ask beats no decision."""
    if not installs:
        return None, ""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    results = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(installs)))
    futures = {pool.submit(vet, inst, fetch, now): inst for inst in installs}
    done, _ = concurrent.futures.wait(futures, timeout=TOTAL_BUDGET)
    for fut in futures:
        inst = futures[fut]
        if fut in done:
            results.append(fut.result())
        else:
            results.append(("ask", f"{inst.name} ({inst.ecosystem}): lookup timed out; cannot vet it"))
    pool.shutdown(wait=False, cancel_futures=True)
    flagged = [r for r in results if r[0]]
    if not flagged:
        return None, ""
    decision = "deny" if any(d == "deny" for d, _ in flagged) else "ask"
    return decision, "; ".join(reason for _, reason in flagged)


# ---------------------------------------------------------------- manifest edits

MANIFEST_TOOLS = ("Edit", "Write", "MultiEdit")
NPM_DEP_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _manifest_kind(path):
    base = os.path.basename(path or "")
    if base == "package.json":
        return "package.json"
    if base == "pyproject.toml":
        return "pyproject.toml"
    if re.fullmatch(r"requirements.*\.txt", base):
        return "requirements"
    return None


def _apply_edit(tool, tool_input, old):
    """The file text after the tool runs, or None if the edit cannot apply (it would fail anyway)."""
    if tool == "Write":
        return tool_input.get("content")
    edits = [tool_input] if tool == "Edit" else (tool_input.get("edits") or [])
    text = old
    for e in edits:
        before, after = e.get("old_string") or "", e.get("new_string") or ""
        if not before or before not in text:
            return None
        text = text.replace(before, after) if e.get("replace_all") else text.replace(before, after, 1)
    return text


def _key(inst):
    return _norm(inst.ecosystem, inst.name) if not inst.unvettable else inst.ecosystem + ":" + inst.spec


def _npm_manifest(text):
    doc = json.loads(text)
    found = []
    for section in NPM_DEP_SECTIONS:
        for name, spec in (doc.get(section) or {}).items():
            spec = str(spec)
            if spec.startswith("workspace:"):
                continue  # a package from the same monorepo
            local = spec.startswith(("file:", "link:", "portal:", "git", "github:", "http:", "https:", "npm:"))
            shorthand = "/" in spec and not spec[:1].isdigit() and not spec.startswith(("^", "~", ">", "<", "=", "*"))
            why = "not a registry version" if (local or shorthand) else ""
            found.append(Install("npm", name, why, spec=f"{name}@{spec}"))
    return found


def _requirement(line):
    name, why = _pypi_spec(line)
    return Install("pypi", name or line, why, spec=line)


def _requirements_manifest(text):
    found = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            flag = line.split()[0].split("=", 1)[0]
            if flag in PIP_FLAGS["unvettable"]:
                found.append(Install("pypi", line, f"{flag} points at a source dep_gate cannot vet", spec=line))
            continue
        found.append(_requirement(line))
    return found


def _pyproject_manifest(text):
    doc = tomllib.loads(text)
    project, tool = doc.get("project") or {}, doc.get("tool") or {}
    strings = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        strings += group
    for group in (doc.get("dependency-groups") or {}).values():
        strings += [g for g in group if isinstance(g, str)]  # tables are {include-group = ...}
    strings += (tool.get("uv") or {}).get("dev-dependencies") or []
    found = [_requirement(s) for s in strings if isinstance(s, str)]
    poetry = tool.get("poetry") or {}
    tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
    tables += [(g or {}).get("dependencies") or {} for g in (poetry.get("group") or {}).values()]
    for table in tables:
        for name, spec in table.items():
            if name.lower() == "python":
                continue
            local = isinstance(spec, dict) and any(k in spec for k in ("path", "git", "url", "file"))
            found.append(Install("pypi", name, "not a registry version" if local else "", spec=name))
    return found


MANIFEST_PARSERS = {"package.json": _npm_manifest, "requirements": _requirements_manifest,
                    "pyproject.toml": _pyproject_manifest}


def manifest_installs(tool, tool_input, base_dir=""):
    """Dependencies that the edit adds to a manifest; empty when nothing is added or it cannot tell."""
    path = tool_input.get("file_path") or ""
    kind = _manifest_kind(path)
    if tool not in MANIFEST_TOOLS or not kind or (kind == "pyproject.toml" and tomllib is None):
        return []
    path = os.path.join(base_dir, path) if base_dir and not os.path.isabs(path) else path
    try:
        with open(path) as f:
            old_text = f.read()
    except OSError:
        old_text = ""
    new_text = _apply_edit(tool, tool_input, old_text)
    if new_text is None:
        return []
    parse = MANIFEST_PARSERS[kind]
    try:
        after = {_key(i): i for i in parse(new_text)}
    except Exception:
        return []  # mid-edit or broken file: nothing reliable to compare
    try:
        before = {_key(i) for i in parse(old_text)} if old_text.strip() else set()
    except Exception:
        before = set()
    return [inst for key, inst in after.items() if key not in before]


def decide_manifest(tool, tool_input, fetch, now=None, base_dir=""):
    return decide_installs(manifest_installs(tool, tool_input, base_dir), fetch, now)


# ---------------------------------------------------------------- hook entry point

def real_fetch(url, payload=None):
    if os.environ.get("DEP_GATE_OFFLINE") == "1":
        raise OSError("network disabled by DEP_GATE_OFFLINE")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "claude-config-dep_gate", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _common import load_input, emit, append_lesson
    data = load_input()
    tool, tool_input = data.get("tool_name") or "", data.get("tool_input") or {}
    if tool in MANIFEST_TOOLS:
        what = tool_input.get("file_path") or ""
        decision, reason = decide_manifest(tool, tool_input, real_fetch, base_dir=data.get("cwd") or "")
    else:
        what = tool_input.get("command") or ""
        decision, reason = decide(what, real_fetch)
    if not decision:
        sys.exit(0)
    if decision == "deny":
        append_lesson(data, "dep-gate", what, reason[:200])
        reason += ". Do not work around this check; if the package is really needed, tell the user."
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                 "permissionDecisionReason": f"dep_gate: {reason}"}})


if __name__ == "__main__":
    main()
