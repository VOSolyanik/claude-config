"""Offline tests for claude-kit/hooks/dep_audit.py. Auditor runs are replaced by a fake runner that
replays real `pnpm audit --json` / `npm audit --json` output captured on 2026-10-01 (tests/fixtures).

Run: python3 -m unittest discover -s tests -p 'test_*.py'
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(HERE, "..", "claude-kit", "hooks")
sys.path.insert(0, HOOKS)

import dep_audit  # noqa: E402


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name)) as f:
        return f.read()


class FakeRunner:
    def __init__(self, rc=1, stdout="", raises=None):
        self.rc, self.stdout, self.raises, self.calls = rc, stdout, raises, []

    def __call__(self, argv, cwd):
        self.calls.append((argv, cwd))
        if self.raises:
            raise self.raises
        return self.rc, self.stdout


def which_all(name):
    return "/usr/bin/" + name


def which_js_only(name):
    return "/usr/bin/" + name if name in ("npm", "pnpm") else None


class Project:
    """A temp directory with the given lockfiles; `sub` is a nested directory inside it."""

    def __init__(self, *lockfiles):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.sub = os.path.join(self.root, "packages", "web")
        os.makedirs(self.sub)
        for name in lockfiles:
            open(os.path.join(self.root, name), "w").close()

    def close(self):
        self.tmp.cleanup()


def event(cmd, cwd, **response):
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": cwd,
            "tool_input": {"command": cmd}, "tool_response": response or {"stdout": "", "stderr": ""}}


class WhenToAudit(unittest.TestCase):
    def setUp(self):
        self.p = Project("pnpm-lock.yaml")
        self.runner = FakeRunner(stdout=fixture("pnpm-audit.json"))

    def tearDown(self):
        self.p.close()

    def test_commands_that_add_no_packages_are_not_audited(self):
        for cmd in ["ls", "pnpm install", "npm ci", "uv sync", "pnpm test"]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(dep_audit.audit(event(cmd, self.p.root), self.runner, which_all))
        self.assertEqual(self.runner.calls, [])

    def test_interrupted_install_is_not_audited(self):
        ctx = dep_audit.audit(event("pnpm add lodash", self.p.root, interrupted=True), self.runner, which_all)
        self.assertIsNone(ctx)
        self.assertEqual(self.runner.calls, [])


class JavaScript(unittest.TestCase):
    def tearDown(self):
        self.p.close()

    def test_pnpm_lockfile_runs_pnpm_audit_and_reports_findings(self):
        self.p = Project("pnpm-lock.yaml")
        run = FakeRunner(stdout=fixture("pnpm-audit.json"))
        ctx = dep_audit.audit(event("pnpm add lodash", self.p.root), run, which_all)
        self.assertEqual(run.calls[0][0][:2], ["pnpm", "audit"])
        self.assertEqual(run.calls[0][1], self.p.root)
        self.assertIn("lodash", ctx)
        self.assertIn("high", ctx)
        self.assertIn("GHSA-35jh-r3h4-6jhm", ctx)
        self.assertIn(">=4.17.21", ctx)      # patched versions from the advisory
        self.assertIn("3 high", ctx)         # totals from metadata

    def test_npm_lockfile_runs_npm_audit_and_reports_findings(self):
        self.p = Project("package-lock.json")
        run = FakeRunner(stdout=fixture("npm-audit.json"))
        ctx = dep_audit.audit(event("npm install lodash@4.17.15", self.p.root), run, which_all)
        self.assertEqual(run.calls[0][0][:2], ["npm", "audit"])
        self.assertIn("lodash", ctx)
        self.assertIn("Command Injection in lodash", ctx)
        self.assertIn("4.18.1", ctx)         # fixAvailable version

    def test_lockfile_is_found_above_the_working_directory(self):
        self.p = Project("pnpm-lock.yaml")
        run = FakeRunner(stdout=fixture("pnpm-audit.json"))
        ctx = dep_audit.audit(event("pnpm add lodash", self.p.sub), run, which_all)
        self.assertIsNotNone(ctx)
        self.assertEqual(run.calls[0][1], self.p.root)

    def test_cd_prefix_moves_the_working_directory(self):
        self.p = Project()
        open(os.path.join(self.p.sub, "package-lock.json"), "w").close()
        run = FakeRunner(stdout=fixture("npm-audit.json"))
        dep_audit.audit(event("cd packages/web && npm i lodash", self.p.root), run, which_all)
        self.assertEqual(run.calls[0][1], self.p.sub)

    def test_cd_after_other_commands_still_moves_the_working_directory(self):
        # found live: `mkdir -p x && cd x && ... && pnpm add ...` audited the session's cwd instead of x
        self.p = Project()
        open(os.path.join(self.p.sub, "package-lock.json"), "w").close()
        for cmd in ["mkdir -p packages/web && cd packages/web && npm i lodash",
                    "cd packages && cd web && printf x > notes.txt && npm i lodash 2>&1 | tail -3; ls"]:
            with self.subTest(cmd=cmd):
                run = FakeRunner(stdout=fixture("npm-audit.json"))
                dep_audit.audit(event(cmd, self.p.root), run, which_all)
                self.assertEqual(run.calls[0][1], self.p.sub)

    def test_cd_after_the_install_does_not_count(self):
        self.p = Project("package-lock.json")
        open(os.path.join(self.p.sub, "package-lock.json"), "w").close()  # a later cd would find this one
        run = FakeRunner(stdout=fixture("npm-audit.json"))
        dep_audit.audit(event("npm i lodash && cd packages/web", self.p.root), run, which_all)
        self.assertEqual(run.calls[0][1], self.p.root)

    def test_clean_audit_says_nothing(self):
        self.p = Project("pnpm-lock.yaml")
        clean = json.dumps({"advisories": {}, "metadata": {"vulnerabilities":
                            {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0}}})
        self.assertIsNone(dep_audit.audit(event("pnpm add zod", self.p.root), FakeRunner(rc=0, stdout=clean), which_all))

    def test_auditor_failures_fail_open(self):
        self.p = Project("pnpm-lock.yaml")
        for run in [FakeRunner(rc=2, stdout="not json"), FakeRunner(raises=OSError("boom")),
                    FakeRunner(raises=subprocess.TimeoutExpired("pnpm", 25))]:
            with self.subTest(run=run.stdout or run.raises):
                self.assertIsNone(dep_audit.audit(event("pnpm add lodash", self.p.root), run, which_all))


class NoAuditor(unittest.TestCase):
    def tearDown(self):
        self.p.close()

    def test_python_project_without_an_auditor_gets_a_one_line_note(self):
        self.p = Project("uv.lock")
        run = FakeRunner()
        ctx = dep_audit.audit(event("uv add httpx", self.p.root), run, which_js_only)
        self.assertEqual(run.calls, [])
        self.assertIn("no auditor", ctx)
        self.assertIn("uv.lock", ctx)

    def test_no_lockfile_says_nothing(self):
        self.p = Project()
        self.assertIsNone(dep_audit.audit(event("npm i lodash", self.p.root), FakeRunner(), which_all))


class FakeNpmRegistry:
    """Abbreviated npm documents: name -> {version: deprecated message or None}, plus the latest tag."""

    def __init__(self, packages=None, broken=False):
        self.packages, self.broken, self.calls = packages or {}, broken, []

    def __call__(self, url):
        self.calls.append(url)
        if self.broken:
            raise OSError("registry down")
        name = dep_audit.unquote(url.rsplit("/", 1)[-1])
        if name not in self.packages:
            return 404, None
        versions = self.packages[name]
        latest = sorted(versions)[-1]
        return 200, {"name": name, "dist-tags": {"latest": latest},
                     "versions": {v: ({"deprecated": msg} if msg else {}) for v, msg in versions.items()}}


CLEAN_PNPM = json.dumps({"advisories": {}, "metadata": {"vulnerabilities": {"high": 0}}})


class Deprecated(unittest.TestCase):
    def setUp(self):
        self.p = Project("pnpm-lock.yaml")
        self.reg = FakeNpmRegistry({"left-pad": {"1.1.0": None, "1.3.0": "use String.prototype.padStart()"},
                                    "zod": {"3.23.8": None}})

    def tearDown(self):
        self.p.close()

    def audit(self, cmd, runner=None, which=which_all, cwd=None):
        return dep_audit.audit(event(cmd, cwd or self.p.root), runner or FakeRunner(rc=0, stdout=CLEAN_PNPM),
                               which, fetch=self.reg)

    def test_deprecated_latest_version_is_reported_with_the_registry_message(self):
        ctx = self.audit("pnpm add left-pad")
        self.assertIn("left-pad@1.3.0", ctx)
        self.assertIn("deprecated", ctx)
        self.assertIn("use String.prototype.padStart()", ctx)

    def test_explicit_version_is_the_one_checked(self):
        self.assertIsNone(self.audit("pnpm add left-pad@1.1.0"))

    def test_package_that_is_not_deprecated_adds_nothing(self):
        self.assertIsNone(self.audit("pnpm add zod"))

    def test_deprecation_and_cves_are_reported_together(self):
        ctx = self.audit("pnpm add left-pad", runner=FakeRunner(stdout=fixture("pnpm-audit.json")))
        self.assertIn("GHSA-35jh-r3h4-6jhm", ctx)
        self.assertIn("use String.prototype.padStart()", ctx)

    def test_deprecation_is_reported_without_a_lockfile(self):
        bare = Project()
        try:
            ctx = self.audit("npm i left-pad", cwd=bare.root)
        finally:
            bare.close()
        self.assertIn("use String.prototype.padStart()", ctx)

    def test_registry_failure_is_silent(self):
        self.reg.broken = True
        self.assertIsNone(self.audit("pnpm add left-pad"))

    def test_python_packages_are_not_looked_up_in_npm(self):
        dep_audit.audit(event("uv add httpx", self.p.root), FakeRunner(), which_js_only, fetch=self.reg)
        self.assertEqual(self.reg.calls, [])

    def test_scoped_package_url_is_encoded(self):
        self.reg.packages["@types/node"] = {"20.0.0": None}
        self.audit("pnpm add @types/node")
        self.assertTrue(self.reg.calls[0].endswith("/@types%2Fnode"))


def which_with_osv(name):
    return "/opt/homebrew/bin/" + name if name in ("npm", "pnpm", "osv-scanner") else None


class OsvScanner(unittest.TestCase):
    """uv.lock, poetry.lock, requirements.txt, yarn.lock, bun.lock go through osv-scanner 2.6.0.
    Fixtures are real `osv-scanner scan source -L <lockfile> --format json` runs (2026-10-01) on
    lockfiles pinning requests==2.19.0 or lodash@4.17.15, trimmed of details/references."""

    CASES = [
        ("uv.lock", "uv add requests", "osv-uv.json", "requests@2.19.0"),
        ("poetry.lock", "poetry add requests", "osv-poetry.json", "requests@2.19.0"),
        ("requirements.txt", "pip install requests==2.19.0", "osv-requirements.json", "requests@2.19.0"),
        ("yarn.lock", "yarn add lodash", "osv-yarn.json", "lodash@4.17.15"),
        ("bun.lock", "bun add lodash", "osv-bun.json", "lodash@4.17.15"),
    ]

    def test_each_lockfile_is_scanned_and_reported(self):
        for lockfile, cmd, fx, pkg in self.CASES:
            with self.subTest(lockfile=lockfile):
                p = Project(lockfile)
                try:
                    run = FakeRunner(stdout=fixture(fx))
                    ctx = dep_audit.audit(event(cmd, p.root), run, which_with_osv)
                    self.assertEqual(run.calls[0][0], ["osv-scanner", "scan", "source", "-L", lockfile,
                                                       "--format", "json"])
                    self.assertEqual(run.calls[0][1], p.root)
                    self.assertIn(pkg, ctx)
                    self.assertIn("GHSA-", ctx)
                    self.assertIn("fixed in", ctx)
                finally:
                    p.close()

    def test_groups_are_counted_once_with_cve_and_fix_version(self):
        p = Project("poetry.lock")
        try:
            ctx = dep_audit.audit(event("poetry add requests", p.root),
                                  FakeRunner(stdout=fixture("osv-poetry.json")), which_with_osv)
        finally:
            p.close()
        self.assertIn("GHSA-x84v-xcm2-53pg", ctx)
        self.assertIn("CVE-2018-18074", ctx)
        self.assertIn("fixed in 2.20.0", ctx)     # the version event, not the git commit hash
        self.assertNotIn("c45d7c49ea75", ctx)
        groups = sum(len(pk["groups"]) for r in json.loads(fixture("osv-poetry.json"))["results"] for pk in r["packages"])
        totals = ctx.split("(", 1)[1].split(")", 1)[0]
        self.assertEqual(sum(int(part.split()[0]) for part in totals.split(", ")), groups)

    def test_transitive_packages_are_reported_too(self):
        p = Project("uv.lock")
        try:
            ctx = dep_audit.audit(event("uv add requests", p.root),
                                  FakeRunner(stdout=fixture("osv-uv.json")), which_with_osv)
        finally:
            p.close()
        self.assertIn("urllib3@", ctx)

    def test_commit_hashes_that_start_with_a_digit_are_not_versions(self):
        # found in a real run: idna's GHSA-jjg7-2v4v-x38h lists a fix commit 1d365e17... next to 3.7
        p = Project("uv.lock")
        try:
            ctx = dep_audit.audit(event("uv add requests", p.root),
                                  FakeRunner(stdout=fixture("osv-uv.json")), which_with_osv)
        finally:
            p.close()
        self.assertNotIn("1d365e17", ctx)
        self.assertIn("GHSA-jjg7-2v4v-x38h (CVE-2024-3651); fixed in 3.7", ctx)

    def test_unknown_exit_code_fails_open(self):
        p = Project("uv.lock")
        try:
            ctx = dep_audit.audit(event("uv add requests", p.root), FakeRunner(rc=128, stdout=""), which_with_osv)
        finally:
            p.close()
        self.assertIsNone(ctx)

    def test_without_osv_scanner_yarn_gets_the_no_auditor_note(self):
        p = Project("yarn.lock")
        try:
            ctx = dep_audit.audit(event("yarn add lodash", p.root), FakeRunner(), which_js_only)
        finally:
            p.close()
        self.assertIn("no auditor", ctx)


class HookProtocol(unittest.TestCase):
    def run_hook(self, stdin_text):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        return subprocess.run([sys.executable, os.path.join(HOOKS, "dep_audit.py")], input=stdin_text,
                              capture_output=True, text=True, env=env, timeout=20)

    def test_malformed_stdin_fails_open(self):
        p = self.run_hook("not json")
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_unrelated_command_prints_nothing(self):
        p = self.run_hook(json.dumps(event("ls", "/")))
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_context_is_wrapped_as_post_tool_use_output(self):
        out = dep_audit.hook_output("dep_audit: something")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], "dep_audit: something")


if __name__ == "__main__":
    unittest.main()
