#!/usr/bin/env python3
"""PostToolUse (matcher: Bash|PowerShell) - audits the dependency tree right after an agent adds packages.

Runs only when the command named packages to add (the same parser as dep_gate.py); lockfile installs
such as `npm install` or `uv sync` do not change the dependency set and are not audited.
Finds the nearest lockfile at or above the command's working directory (a leading `cd <dir> &&` counts):
  pnpm-lock.yaml     -> pnpm audit --json
  package-lock.json  -> npm audit --json
  uv.lock, poetry.lock, requirements.txt, yarn.lock, bun.lock
                     -> osv-scanner scan source -L <lockfile> --format json   (osv-scanner 2.6.0)
  no auditor on PATH, or another lockfile (bun.lockb) -> a one-line note that nothing was checked.
osv-scanner results are grouped per package (ids + aliases + max CVSS score); each group is one finding.
For npm packages it also reads the registry's abbreviated document (versions[<v>].deprecated) and reports
deprecated packages with the maintainers' message - the version named in the command, else dist-tags.latest.
This runs even without a lockfile. PyPI has no deprecation field and is not checked.
Known vulnerabilities and deprecations go back to the agent as additionalContext; a clean result says nothing.
Every failure (auditor missing, timeout, bad JSON) fails open with no output.
Output formats were taken from real `pnpm audit --json`, `npm audit --json` (pnpm 12.6, npm with node 22)
and `osv-scanner scan source` (2.6.0) runs on 2026-10-01; tests replay them from tests/fixtures.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dep_gate import parse_installs, SEGMENT_SPLIT, _tokens, NPM_REGISTRY  # noqa: E402

AUDIT_TIMEOUT = 20
REGISTRY_TIMEOUT = 3
MAX_DEPRECATION_LOOKUPS = 5
MAX_ITEMS = 8
SEVERITY_ORDER = ["critical", "high", "moderate", "low", "info", "unknown"]
JS_LOCKFILES = ["pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb"]
PY_LOCKFILES = ["uv.lock", "poetry.lock", "requirements.txt"]
AUDITORS = {"pnpm-lock.yaml": ["pnpm", "audit", "--json"], "package-lock.json": ["npm", "audit", "--json"]}
OSV_LOCKFILES = {"uv.lock", "poetry.lock", "requirements.txt", "yarn.lock", "bun.lock"}
CVSS_LEVELS = [(9.0, "critical"), (7.0, "high"), (4.0, "moderate"), (0.1, "low")]
VERSIONISH = re.compile(r"^\d+(\.\d+)+[0-9A-Za-z.+-]*$")  # needs a dot: rules out commit hashes


def _start_dir(cmd, cwd):
    """The directory the first install ran in: cwd, moved by every `cd <dir>` segment before it."""
    where = cwd
    for segment in SEGMENT_SPLIT.split(cmd or ""):
        if parse_installs(segment):
            break
        toks = _tokens(segment)
        if len(toks) == 2 and toks[0] == "cd":
            where = os.path.normpath(os.path.join(where, os.path.expanduser(toks[1])))
    return where


def _find_lockfile(start, names):
    d = os.path.abspath(start)
    for _ in range(12):
        for name in names:
            if os.path.isfile(os.path.join(d, name)):
                return d, name
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None, None


def _totals(meta):
    counts = (meta or {}).get("vulnerabilities") or {}
    parts = [f"{counts[s]} {s}" for s in SEVERITY_ORDER if counts.get(s)]
    return ", ".join(parts)


def _rank(severity):
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def parse_pnpm(report):
    items = []
    for adv in (report.get("advisories") or {}).values():
        ident = adv.get("github_advisory_id") or (adv.get("url") or "").rsplit("/", 1)[-1]
        fix = f"patched {adv['patched_versions']}" if adv.get("patched_versions") else "no patched version"
        items.append((adv.get("severity", ""), f"{adv.get('module_name')} ({adv.get('severity')}): "
                                               f"{adv.get('title')} - {ident}; {fix}"))
    return items, _totals(report.get("metadata"))


def parse_npm(report):
    items = []
    for vuln in (report.get("vulnerabilities") or {}).values():
        advisories = [v for v in vuln.get("via", []) if isinstance(v, dict)]
        if not advisories:
            continue  # vulnerable only through another package, which is reported on its own
        fix = vuln.get("fixAvailable")
        fix_text = (f"fix: {fix.get('name')}@{fix.get('version')}" if isinstance(fix, dict)
                    else "fix available" if fix else "no fix available")
        titles = "; ".join(f"{a.get('title')} - {(a.get('url') or '').rsplit('/', 1)[-1]}" for a in advisories[:2])
        items.append((vuln.get("severity", ""), f"{vuln.get('name')} ({vuln.get('severity')}): {titles}; {fix_text}"))
    return items, _totals(report.get("metadata"))


def _cvss_level(score):
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "unknown"
    return next((level for threshold, level in CVSS_LEVELS if value >= threshold), "unknown")


def _version_key(v):
    return [int(n) for n in re.findall(r"\d+", v)]


def parse_osv(report):
    """One finding per osv-scanner group: the same issue under PYSEC, GHSA and CVE ids counts once."""
    items, counts = [], {}
    for result in report.get("results") or []:
        for pkg in result.get("packages") or []:
            meta = pkg.get("package") or {}
            label = f"{meta.get('name')}@{meta.get('version')}"
            by_id = {v.get("id"): v for v in pkg.get("vulnerabilities") or []}
            for group in pkg.get("groups") or []:
                ids = group.get("ids") or []
                ident = next((i for i in ids if i.startswith("GHSA-")), ids[0] if ids else "?")
                cve = next((a for a in group.get("aliases") or [] if a.startswith("CVE-")), None)
                summary = next((by_id[i]["summary"] for i in ids if (by_id.get(i) or {}).get("summary")), None)
                fixed = {ev["fixed"] for i in ids for aff in (by_id.get(i) or {}).get("affected") or []
                         for rng in aff.get("ranges") or [] for ev in rng.get("events") or []
                         if ev.get("fixed") and VERSIONISH.match(ev["fixed"])}
                level = _cvss_level(group.get("max_severity"))
                counts[level] = counts.get(level, 0) + 1
                score = f" {group['max_severity']}" if group.get("max_severity") else ""
                fix = ("fixed in " + ", ".join(sorted(fixed, key=_version_key)[:2])) if fixed else "no fixed version listed"
                items.append((level, f"{label} ({level}{score}): {summary or 'no summary'} - {ident}"
                                     f"{f' ({cve})' if cve else ''}; {fix}"))
    totals = ", ".join(f"{counts[s]} {s}" for s in SEVERITY_ORDER if counts.get(s))
    return items, totals


PARSERS = {"pnpm-lock.yaml": parse_pnpm, "package-lock.json": parse_npm}


def _auditor(lockfile):
    """(argv, parser) for a lockfile, or (None, None)."""
    if lockfile in AUDITORS:
        return AUDITORS[lockfile], PARSERS[lockfile]
    if lockfile in OSV_LOCKFILES:
        return ["osv-scanner", "scan", "source", "-L", lockfile, "--format", "json"], parse_osv
    return None, None


def _exact_npm_version(spec):
    at = spec.find("@", 1) if spec.startswith("@") else spec.find("@")
    version = spec[at + 1:] if at > 0 else ""
    return version if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version) else None


def deprecations(installs, fetch):
    """'- name@version: message' for every npm package just added whose version is deprecated."""
    lines = []
    npm = [i for i in installs if i.ecosystem == "npm" and not i.unvettable][:MAX_DEPRECATION_LOOKUPS]
    for inst in npm:
        try:
            status, doc = fetch(NPM_REGISTRY + quote(inst.name, safe="@"))
            if status != 200 or not doc:
                continue
            version = _exact_npm_version(inst.spec) or (doc.get("dist-tags") or {}).get("latest")
            message = ((doc.get("versions") or {}).get(version) or {}).get("deprecated")
        except Exception:
            continue
        if message:
            lines.append(f"- {inst.name}@{version}: {message}")
    return lines


def audit(data, run, which, fetch=None):
    """additionalContext text for the agent, or None. Without `fetch`, deprecations are not checked."""
    response = data.get("tool_response") or {}
    if isinstance(response, dict) and response.get("interrupted"):
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    installs = parse_installs(cmd)
    if not installs:
        return None
    sections = []
    deprecated = deprecations(installs, fetch) if fetch else []
    if deprecated:
        sections.append("dep_audit: deprecated packages were just added:\n" + "\n".join(deprecated) +
                        "\nPrefer the replacement the maintainers name, or tell the user why you keep it.")
    vulns = vulnerabilities(cmd, installs, data, run, which)
    if vulns:
        sections.append(vulns)
    return "\n\n".join(sections) or None


def vulnerabilities(cmd, installs, data, run, which):
    """The vulnerability section from the project's audit tool, a no-auditor note, or None."""
    ecosystems = {i.ecosystem for i in installs}
    start = _start_dir(cmd, data.get("cwd") or os.getcwd())
    names = (JS_LOCKFILES if "npm" in ecosystems else []) + (PY_LOCKFILES if "pypi" in ecosystems else [])
    root, lockfile = _find_lockfile(start, names)
    if not lockfile:
        return None
    argv, parser = _auditor(lockfile)
    if not argv or not which(argv[0]):
        return (f"dep_audit: no auditor configured for {lockfile} in {root}; the packages just added were not "
                f"checked for known vulnerabilities.")
    try:
        rc, stdout = run(argv, root)
        if rc not in (0, 1):  # all three tools exit 1 when they find vulnerabilities
            return None
        items, totals = parser(json.loads(stdout))
    except Exception:
        return None
    if not items:
        return None
    items.sort(key=lambda item: _rank(item[0]))
    tool = "osv-scanner" if argv[0] == "osv-scanner" else " ".join(argv[:2])
    lines = [f"- {text}" for _, text in items[:MAX_ITEMS]]
    if len(items) > MAX_ITEMS:
        lines.append(f"- ... and {len(items) - MAX_ITEMS} more (run `{' '.join(argv)}` to see all)")
    return (f"dep_audit: `{tool}` after this install found known vulnerabilities in the dependency "
            f"tree ({totals}):\n" + "\n".join(lines) +
            "\nPrefer a patched version, and tell the user about anything you leave in place.")


def hook_output(context):
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}


def npm_fetch(url):
    """The registry's abbreviated document: small, and it still carries versions[<v>].deprecated."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.npm.install-v1+json",
                                               "User-Agent": "claude-config-dep_audit"})
    try:
        with urllib.request.urlopen(req, timeout=REGISTRY_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None


def real_run(argv, cwd):
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=AUDIT_TIMEOUT)
    return p.returncode, p.stdout


def main():
    from _common import load_input, emit, clip
    data = load_input()
    context = audit(data, real_run, shutil.which, fetch=npm_fetch)
    if not context:
        sys.exit(0)
    emit(hook_output(clip(context)))


if __name__ == "__main__":
    main()
