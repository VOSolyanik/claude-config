"""Offline tests for claude-kit/hooks/dep_gate.py: registries and OSV are replaced by a fake fetcher.

Run: python3 -m unittest discover -s tests -p 'test_*.py'
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HOOKS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude-kit", "hooks")
sys.path.insert(0, HOOKS)

import dep_gate  # noqa: E402

NOW = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
OLD = "2015-01-01T00:00:00.000Z"
FRESH = "2026-09-29T00:00:00.000Z"


class FakeRegistry:
    """Answers the URLs dep_gate asks for. Unknown URLs are 404s; every call is recorded."""

    def __init__(self):
        self.npm = {}      # name -> (created_iso, weekly_downloads)
        self.pypi = {}     # name -> (first_upload_iso, weekly_downloads or None)
        self.malicious = set()  # (ecosystem, name)
        self.broken = False       # every request raises
        self.osv_broken = False   # only OSV raises
        self.registry_status = None  # e.g. 503 for every registry request
        self.delay = 0            # seconds to sleep per request
        self.calls = []

    def add_npm(self, name, created=OLD, downloads=1_000_000):
        self.npm[name] = (created, downloads)

    def add_pypi(self, name, first_upload=OLD, downloads=1_000_000):
        self.pypi[name] = (first_upload, downloads)

    def __call__(self, url, payload=None):
        self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if self.broken:
            raise OSError("network down")
        if url.startswith("https://api.osv.dev/"):
            if self.osv_broken:
                raise OSError("osv down")
            pkg = (payload or {})["package"]
            vulns = [{"id": "MAL-2026-1"}] if (pkg["ecosystem"], pkg["name"]) in self.malicious else []
            return 200, {"vulns": vulns} if vulns else {}
        if self.registry_status is not None:
            return self.registry_status, None
        if url.startswith(dep_gate.NPM_REGISTRY):
            name = dep_gate.unquote_name(url[len(dep_gate.NPM_REGISTRY):])
            if name not in self.npm:
                return 404, None
            return 200, {"name": name, "time": {"created": self.npm[name][0]}}
        if url.startswith(dep_gate.NPM_DOWNLOADS):
            name = dep_gate.unquote_name(url[len(dep_gate.NPM_DOWNLOADS):])
            if name not in self.npm:
                return 404, None
            return 200, {"downloads": self.npm[name][1]}
        if url.startswith(dep_gate.PYPI_JSON):
            name = url[len(dep_gate.PYPI_JSON):].split("/")[0]
            if name not in self.pypi:
                return 404, None
            first = self.pypi[name][0].replace(".000Z", "")
            return 200, {"info": {"name": name}, "releases": {"1.0": [{"upload_time_iso_8601": first + "Z"}]}}
        if url.startswith(dep_gate.PYPI_STATS):
            name = url[len(dep_gate.PYPI_STATS):].split("/")[0]
            if name not in self.pypi or self.pypi[name][1] is None:
                return 404, None
            return 200, {"data": {"last_week": self.pypi[name][1]}}
        return 404, None


def decide(cmd, reg):
    return dep_gate.decide(cmd, fetch=reg, now=NOW)


class NoPackagesNoDecision(unittest.TestCase):
    def test_bare_lockfile_installs_are_left_alone_without_network(self):
        reg = FakeRegistry()
        for cmd in ["npm install", "npm ci", "pnpm install", "pnpm i --frozen-lockfile",
                    "yarn install", "bun install", "uv sync", "poetry install", "pip install"]:
            with self.subTest(cmd=cmd):
                self.assertEqual(decide(cmd, reg), (None, ""))
        self.assertEqual(reg.calls, [])

    def test_unrelated_commands_are_ignored(self):
        reg = FakeRegistry()
        for cmd in ["ls -la", "git status", "npm test", "pnpm run build", "echo npm install foo"]:
            with self.subTest(cmd=cmd):
                self.assertEqual(decide(cmd, reg), (None, ""))
        self.assertEqual(reg.calls, [])


class ParsesPackageSpecs(unittest.TestCase):
    def test_extracts_names_from_every_supported_tool(self):
        cases = {
            "npm install left-pad": [("npm", "left-pad")],
            "npm i -D @types/node@20 typescript": [("npm", "@types/node"), ("npm", "typescript")],
            "npm add lodash@^4.17.0": [("npm", "lodash")],
            "pnpm add -w zod": [("npm", "zod")],
            "pnpm add --filter web react": [("npm", "react")],
            "yarn add axios": [("npm", "axios")],
            "bun add hono": [("npm", "hono")],
            "pip install requests": [("pypi", "requests")],
            "pip3 install 'requests[security]>=2.31' flask==3.0": [("pypi", "requests"), ("pypi", "flask")],
            "python3 -m pip install httpx": [("pypi", "httpx")],
            "uv pip install pydantic": [("pypi", "pydantic")],
            "uv add 'fastapi>=0.110' --dev pytest": [("pypi", "fastapi"), ("pypi", "pytest")],
            "poetry add -G dev ruff": [("pypi", "ruff")],
            "cd app && pnpm add lodash": [("npm", "lodash")],
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                got = [(i.ecosystem, i.name) for i in dep_gate.parse_installs(cmd)]
                self.assertEqual(got, want)


class ShellNoise(unittest.TestCase):
    """Found live on 2026-10-01: `bun install --help 2>&1` was denied because "2>&1" looked like a package."""

    def test_redirections_and_their_targets_are_not_packages(self):
        cases = {
            "bun install --help 2>&1": [],
            "npm i -h": [],
            "npm install lodash 2>&1 | tail -5": [("npm", "lodash")],
            "pip install requests > /tmp/pip.log": [("pypi", "requests")],
            "pip install requests >> log.txt 2> err.txt": [("pypi", "requests")],
            "pnpm add zod &>/dev/null": [("npm", "zod")],
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual([(i.ecosystem, i.name) for i in dep_gate.parse_installs(cmd)], want)


class ExecutedPartsOnly(unittest.TestCase):
    """Found live on 2026-10-02: a commit message that quoted an install command was denied, its words
    taken for packages. Only what a shell will run is parsed (claude-kit/hooks/_shell.py)."""

    def names(self, cmd):
        return [(i.ecosystem, i.name) for i in dep_gate.parse_installs(cmd)]

    def test_quoted_data_is_not_an_install(self):
        for cmd in [
            'git commit -m "fix: when cd came after a mkdir, pnpm add x was audited from cwd"',
            'echo "npm install fake-pkg-xyz"',
            "cat > notes.md <<'EOF'\npnpm add fake-pkg-xyz\nEOF",
            'grep -n "uv add" file',
            "printf '%s' 'pip install requests'",
            "echo done  # npm install fake-pkg-xyz",
        ]:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.names(cmd), [])

    def test_executed_installs_are_found(self):
        cases = {
            "true && pnpm add zod": [("npm", "zod")],
            'bash -c "npm i lodash"': [("npm", "lodash")],
            "sh -c 'cd web && pnpm add zod'": [("npm", "zod")],
            '"pnpm" add zod': [("npm", "zod")],
            "env npm install left-pad": [("npm", "left-pad")],
            "sudo -u me pip install requests": [("pypi", "requests")],
            "find . -name x | xargs npm install left-pad": [("npm", "left-pad"), ("npm", "<xargs input>")],
            "xargs -I{} npm install {} < list": [("npm", "<xargs input>")],
            'echo "pnpm add zod" | sh': [("npm", "zod")],
            "bash <<EOF\npip install requests\nEOF": [("pypi", "requests")],
            "echo $(pip install httpx)": [("pypi", "httpx")],
            'eval "uv add fastapi"': [("pypi", "fastapi")],
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.names(cmd), want)

    def test_xargs_install_without_named_packages_asks_without_network(self):
        reg = FakeRegistry()
        for cmd in ["cat deps.txt | xargs npm install", "xargs -n1 pip install < requirements.in"]:
            with self.subTest(cmd=cmd):
                installs = dep_gate.parse_installs(cmd)
                self.assertEqual(len(installs), 1)
                self.assertIn("xargs", installs[0].unvettable)
                decision, reason = decide(cmd, reg)
                self.assertEqual(decision, "ask")
                self.assertIn("xargs", reason)
        self.assertEqual(reg.calls, [])

    def test_bare_lockfile_install_without_xargs_stays_silent(self):
        self.assertEqual(dep_gate.parse_installs("true && npm install"), [])

    def test_unbalanced_quotes_fall_back_to_the_whole_text(self):
        self.assertEqual(self.names('echo "x; npm install fake-pkg-xyz'), [("npm", "fake-pkg-xyz")])


class Decisions(unittest.TestCase):
    def setUp(self):
        self.reg = FakeRegistry()

    def test_known_old_popular_package_is_left_alone(self):
        self.reg.add_npm("left-pad")
        self.assertEqual(decide("npm install left-pad", self.reg), (None, ""))

    def test_missing_package_is_denied_as_hallucinated(self):
        decision, reason = decide("npm install fastapi-react-magic-helper", self.reg)
        self.assertEqual(decision, "deny")
        self.assertIn("fastapi-react-magic-helper", reason)
        self.assertIn("hallucinated", reason)

    def test_missing_pypi_package_is_denied(self):
        decision, _ = decide("pip install totally-not-a-real-pkg-xyz", self.reg)
        self.assertEqual(decision, "deny")

    def test_osv_malicious_entry_is_denied(self):
        self.reg.add_npm("evil-pkg")
        self.reg.malicious.add(("npm", "evil-pkg"))
        decision, reason = decide("pnpm add evil-pkg", self.reg)
        self.assertEqual(decision, "deny")
        self.assertIn("MAL-", reason)

    def test_typosquat_of_a_popular_package_is_denied_with_a_hint(self):
        self.reg.add_pypi("reqeusts", downloads=40)
        decision, reason = decide("pip install reqeusts", self.reg)
        self.assertEqual(decision, "deny")
        self.assertIn("requests", reason)

    def test_npm_separator_swap_counts_as_typosquat(self):
        self.reg.add_npm("cross_env", downloads=12)
        decision, reason = decide("npm i cross_env", self.reg)
        self.assertEqual(decision, "deny")
        self.assertIn("cross-env", reason)

    def test_popular_packages_and_well_used_neighbours_are_not_typosquats(self):
        for name in ["react", "react-dom", "vitest", "vite"]:
            self.reg.add_npm(name)
        self.assertEqual(decide("pnpm add react react-dom vite vitest", self.reg), (None, ""))

    def test_fresh_package_asks(self):
        self.reg.add_npm("brand-new-lib", created=FRESH, downloads=5000)
        decision, reason = decide("npm install brand-new-lib", self.reg)
        self.assertEqual(decision, "ask")
        self.assertIn("days", reason)

    def test_barely_downloaded_package_asks(self):
        self.reg.add_npm("lonely-lib", downloads=3)
        decision, reason = decide("npm install lonely-lib", self.reg)
        self.assertEqual(decision, "ask")
        self.assertIn("downloads", reason)

    def test_unreachable_registry_asks(self):
        self.reg.broken = True
        decision, reason = decide("pip install requests", self.reg)
        self.assertEqual(decision, "ask")
        self.assertIn("unavailable", reason)

    def test_non_registry_sources_ask(self):
        for cmd in ["npm install ./local-pkg", "pip install -r requirements.txt",
                    "pip install git+https://github.com/x/y.git", "pip install -e .",
                    "npm install --registry https://example.com foo",
                    "pip install --extra-index-url https://example.com bar"]:
            with self.subTest(cmd=cmd):
                decision, _ = decide(cmd, self.reg)
                self.assertEqual(decision, "ask")

    def test_osv_malicious_pypi_entry_is_denied(self):
        self.reg.add_pypi("evil-pypi-pkg")
        self.reg.malicious.add(("PyPI", "evil-pypi-pkg"))
        decision, reason = decide("uv add evil-pypi-pkg", self.reg)
        self.assertEqual(decision, "deny")
        self.assertIn("MAL-", reason)

    def test_widely_used_neighbour_of_a_popular_package_is_not_a_typosquat(self):
        # preact is one edit away from react, but has millions of weekly downloads
        self.reg.add_npm("preact", downloads=3_000_000)
        self.assertEqual(decide("pnpm add preact", self.reg), (None, ""))

    def test_registry_server_error_asks(self):
        self.reg.registry_status = 503
        decision, reason = decide("npm install lodash", self.reg)
        self.assertEqual(decision, "ask")
        self.assertIn("HTTP 503", reason)

    def test_osv_unavailable_asks_even_when_the_registry_answers(self):
        self.reg.add_npm("lodash")
        self.reg.osv_broken = True
        decision, reason = decide("npm install lodash", self.reg)
        self.assertEqual(decision, "ask")
        self.assertIn("osv", reason.lower())

    def test_lookups_past_the_time_budget_ask(self):
        self.reg.add_npm("lodash")
        self.reg.delay = 0.5
        saved = dep_gate.TOTAL_BUDGET
        dep_gate.TOTAL_BUDGET = 0.2
        try:
            decision, reason = decide("npm install lodash", self.reg)
        finally:
            dep_gate.TOTAL_BUDGET = saved
        self.assertEqual(decision, "ask")
        self.assertIn("timed out", reason)

    def test_one_bad_package_denies_the_whole_command_and_names_it(self):
        self.reg.add_npm("lodash")
        decision, reason = decide("npm install lodash no-such-pkg-qq", self.reg)
        self.assertEqual(decision, "deny")
        self.assertIn("no-such-pkg-qq", reason)
        self.assertNotIn("lodash:", reason)


class ManifestEdits(unittest.TestCase):
    """PreToolUse on Edit|Write|MultiEdit: only dependencies that the edit ADDS are vetted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = FakeRegistry()
        for name in ["react", "left-pad", "lodash", "zod"]:
            self.reg.add_npm(name)
        for name in ["requests", "httpx", "fastapi", "pydantic"]:
            self.reg.add_pypi(name)

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name, content=None):
        p = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if content is not None:
            with open(p, "w") as f:
                f.write(content)
        return p

    def decide(self, tool, tool_input):
        return dep_gate.decide_manifest(tool, tool_input, fetch=self.reg, now=NOW)

    def pkg(self, deps, dev=None):
        doc = {"name": "app", "dependencies": deps}
        if dev is not None:
            doc["devDependencies"] = dev
        return json.dumps(doc, indent=2)

    def test_edit_that_adds_a_known_package_is_left_alone_and_vets_only_that_package(self):
        p = self.path("package.json", self.pkg({"react": "^18.0.0"}))
        old = '"react": "^18.0.0"'
        new = '"react": "^18.0.0",\n    "left-pad": "^1.3.0"'
        self.assertEqual(self.decide("Edit", {"file_path": p, "old_string": old, "new_string": new}), (None, ""))
        self.assertTrue(all("react" not in url for url in self.reg.calls), self.reg.calls)

    def test_edit_that_adds_a_missing_package_is_denied(self):
        p = self.path("package.json", self.pkg({"react": "^18.0.0"}))
        decision, reason = self.decide("Edit", {"file_path": p, "old_string": '"react": "^18.0.0"',
                                                "new_string": '"react": "^18.0.0", "react-magic-helperz": "1.0.0"'})
        self.assertEqual(decision, "deny")
        self.assertIn("react-magic-helperz", reason)

    def test_write_vets_only_dependencies_that_were_not_there_before(self):
        p = self.path("package.json", self.pkg({"react": "^18.0.0"}))
        content = self.pkg({"react": "^18.2.0", "zod": "^3.0.0"}, dev={"no-such-dev-pkg-qq": "1.0.0"})
        decision, reason = self.decide("Write", {"file_path": p, "content": content})
        self.assertEqual(decision, "deny")
        self.assertIn("no-such-dev-pkg-qq", reason)
        self.assertTrue(all("react" not in url for url in self.reg.calls), self.reg.calls)

    def test_version_bump_of_an_existing_dependency_makes_no_lookups(self):
        p = self.path("package.json", self.pkg({"react": "^18.0.0"}))
        self.assertEqual(self.decide("Edit", {"file_path": p, "old_string": "^18.0.0", "new_string": "^19.0.0"}),
                         (None, ""))
        self.assertEqual(self.reg.calls, [])

    def test_local_and_git_sources_in_package_json(self):
        p = self.path("package.json", self.pkg({}))
        content = self.pkg({"shared": "workspace:*"})
        self.assertEqual(self.decide("Write", {"file_path": p, "content": content}), (None, ""))
        for spec in ["file:../lib", "github:user/repo", "git+https://example.com/x.git", "npm:other@1"]:
            with self.subTest(spec=spec):
                decision, _ = self.decide("Write", {"file_path": p, "content": self.pkg({"x": spec})})
                self.assertEqual(decision, "ask")

    def test_requirements_typosquat_is_denied_with_a_hint(self):
        self.reg.add_pypi("reqeusts", downloads=10)
        p = self.path("requirements-dev.txt", "httpx==0.27\n")
        decision, reason = self.decide("Edit", {"file_path": p, "old_string": "httpx==0.27\n",
                                                "new_string": "httpx==0.27\nreqeusts==2.0  # http\n"})
        self.assertEqual(decision, "deny")
        self.assertIn("requests", reason)

    def test_requirements_new_index_or_editable_line_asks(self):
        p = self.path("requirements.txt", "httpx\n")
        for line in ["--extra-index-url https://example.com/simple", "-e ./libs/core"]:
            with self.subTest(line=line):
                decision, _ = self.decide("Write", {"file_path": p, "content": f"httpx\n{line}\n"})
                self.assertEqual(decision, "ask")

    @unittest.skipUnless(dep_gate.tomllib, "needs Python 3.11+ (tomllib)")
    def test_pyproject_pep621_and_poetry_tables(self):
        p = self.path("pyproject.toml", '[project]\nname = "app"\ndependencies = ["fastapi>=0.110"]\n')
        content = ('[project]\nname = "app"\ndependencies = ["fastapi>=0.110", "pydantic>=2", "no-such-pypi-qq"]\n'
                   '[project.optional-dependencies]\ndev = ["httpx"]\n'
                   '[tool.poetry.dependencies]\npython = "^3.12"\nrequests = "^2.31"\n')
        decision, reason = self.decide("Write", {"file_path": p, "content": content})
        self.assertEqual(decision, "deny")
        self.assertIn("no-such-pypi-qq", reason)
        self.assertNotIn("python (", reason)
        looked_up = " ".join(self.reg.calls)
        self.assertNotIn("/fastapi/", looked_up)
        for name in ["pydantic", "httpx", "requests"]:
            self.assertIn(f"/{name}/", looked_up)

    def test_multiedit_applies_every_edit(self):
        p = self.path("package.json", self.pkg({"react": "^18.0.0"}, dev={"lodash": "^4.17.21"}))
        edits = [{"old_string": '"react": "^18.0.0"', "new_string": '"react": "^18.0.0", "zod": "^3.0.0"'},
                 {"old_string": '"lodash": "^4.17.21"', "new_string": '"lodash": "^4.17.21", "nope-not-real-qq": "1"'}]
        decision, reason = self.decide("MultiEdit", {"file_path": p, "edits": edits})
        self.assertEqual(decision, "deny")
        self.assertIn("nope-not-real-qq", reason)

    def test_other_files_and_unusable_edits_are_ignored(self):
        lock = self.path("package-lock.json", "{}")
        readme = self.path("README.md", "x")
        p = self.path("package.json", self.pkg({"react": "^18.0.0"}))
        cases = [
            ("Write", {"file_path": lock, "content": self.pkg({"no-such": "1"})}),
            ("Write", {"file_path": readme, "content": "npm install no-such"}),
            ("Edit", {"file_path": p, "old_string": "not in the file", "new_string": '"no-such": "1"'}),
            ("Write", {"file_path": p, "content": "{ broken json"}),
        ]
        for tool, tool_input in cases:
            with self.subTest(tool=tool, path=tool_input["file_path"]):
                self.assertEqual(self.decide(tool, tool_input), (None, ""))
        self.assertEqual(self.reg.calls, [])


class HookProtocol(unittest.TestCase):
    """The script itself: stdin JSON in, one JSON object or nothing out, always exit 0."""

    def run_hook(self, stdin_text, env_extra=None):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env["DEP_GATE_OFFLINE"] = "1"  # never touch the network from tests
        env.update(env_extra or {})
        return subprocess.run([sys.executable, os.path.join(HOOKS, "dep_gate.py")], input=stdin_text,
                              capture_output=True, text=True, env=env, timeout=20)

    def test_malformed_stdin_fails_open(self):
        p = self.run_hook("not json")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def test_unrelated_command_prints_nothing(self):
        p = self.run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_manifest_write_goes_through_the_same_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "package.json")
            p = self.run_hook(json.dumps({"tool_name": "Write", "cwd": tmp, "tool_input": {
                "file_path": target, "content": json.dumps({"dependencies": {"left-pad": "^1.3.0"}})}}))
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")  # offline: the registry is unavailable

    def test_offline_mode_asks_for_a_new_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self.run_hook(json.dumps({"tool_name": "Bash", "cwd": tmp,
                                          "tool_input": {"command": "npm install left-pad"}}))
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "ask")


if __name__ == "__main__":
    unittest.main()
