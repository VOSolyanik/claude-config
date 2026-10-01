"""Tests for claude-kit/hooks/guard_bash.py: the rm policy (project and /tmp only) and its fail-closed root.

Each case runs the hook as Claude Code does: hook JSON on stdin, a fake HOME, CLAUDE_PROJECT_DIR set to a
temporary project. Lessons the hook appends land in that temporary project.

Run: python3 -m unittest discover -s tests -p 'test_*.py'
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude-kit", "hooks", "guard_bash.py")

ALLOWED = [
    "rm -rf node_modules",
    "rm -rf ./dist",
    "rm -rf /tmp/x",
    "rm -rf /tmp/x /tmp/y",
    'rm -rf /tmp/dep-test /tmp/trailer-test; echo "exit $?"',
    "rm -rf node_modules dist build .cache coverage .next .turbo",
    "rm -rf dist/*",
    "rm -rf -- -weird",
    "rm -f build/out.log",
]

DENIED = [
    "rm -rf ~",
    "rm -rf /",
    "rm -rf ..",
    "rm -rf .git",
    "rm -rf /tmp",
    "rm -rf /Users/someone/dev",
    "rm -rf $HOME/x",
    "rm -rf *",
    "rm -rf /tmp/../etc",
    "rm -rf /tmp/*",
    "rm -rf /tmp/x ~/dev",
    "rm -fr /Users/someone/dev",
    "rm -r -f $HOME",
    "rm -rf node_modules /etc",
    "rm -rf sub/../../other-repo",
    "rm -rf ${HOME}/x",
    "rm -rf $TMPDIR/x",
    "rm -rf /var/folders/xx/T/tmp.abc",
    "rm .git/index.lock",
    "rm -rf .",
    "cd dist && rm -rf ../..",
]


class GuardRmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.project = os.path.join(self.home, "dev", "proj")
        os.makedirs(os.path.join(self.project, "dist"))

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, command, project_env=True, cwd_in_input=True):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        if cwd_in_input:
            payload["cwd"] = self.project
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        env["HOME"] = self.home
        if project_env:
            env["CLAUDE_PROJECT_DIR"] = self.project
        out = subprocess.run(
            ["python3", HOOK], input=json.dumps(payload), capture_output=True, text=True,
            env=env, cwd=self.tmp.name, timeout=10,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        if not out.stdout.strip():
            return None
        return json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"]

    def test_allowed_matrix(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_denied_matrix(self):
        for command in DENIED:
            with self.subTest(command=command):
                self.assertEqual(self.decide(command), "deny")

    def test_reason_names_the_target_and_the_rule(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": self.project,
                   "tool_input": {"command": "rm -rf /tmp/../etc"}}
        out = subprocess.run(
            ["python3", HOOK], input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "HOME": self.home, "CLAUDE_PROJECT_DIR": self.project}, cwd=self.tmp.name,
        )
        reason = json.loads(out.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("`/tmp/../etc`", reason)
        self.assertIn("outside the project and /tmp", reason)

    def test_project_root_by_absolute_path(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": self.project,
                   "tool_input": {"command": f"rm -rf {self.project}"}}
        out = subprocess.run(
            ["python3", HOOK], input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "HOME": self.home, "CLAUDE_PROJECT_DIR": self.project}, cwd=self.tmp.name,
        )
        self.assertIn("the project root", json.loads(out.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    # ---- symlinks: membership is checked on the real path

    def test_symlink_out_of_the_project(self):
        outside = tempfile.mkdtemp(dir="/tmp", prefix="outside-target-")
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(outside, os.path.join(self.project, "link"))
        self.assertEqual(self.decide("rm -rf link/"), "deny")  # follows the link out of the project
        self.assertIsNone(self.decide("rm -rf link"))  # removes only the link

    def test_symlink_into_git(self):
        os.makedirs(os.path.join(self.project, ".git"))
        os.symlink(os.path.join(self.project, ".git"), os.path.join(self.project, "g"))
        self.assertEqual(self.decide("rm -rf g/"), "deny")
        self.assertIsNone(self.decide("rm -f g"))

    def test_project_reached_through_a_symlinked_parent(self):
        alias = os.path.join(self.tmp.name, "alias")
        os.symlink(self.project, alias)
        self.assertIsNone(self.decide(f"rm -rf {alias}/dist"))

    def test_project_under_tmp_keeps_its_ancestors(self):
        base = tempfile.mkdtemp(dir="/tmp", prefix="guard-proj-")
        self.addCleanup(shutil.rmtree, base, True)
        self.project = os.path.join(base, "proj")
        os.makedirs(os.path.join(self.project, "dist"))
        self.assertIsNone(self.decide("rm -rf dist"))
        self.assertEqual(self.decide(f"rm -rf {base}"), "deny")
        self.assertEqual(self.decide(f"rm -rf {base}/*"), "deny")
        self.assertIsNone(self.decide(f"rm -rf {base}/other"))

    # ---- project root: CLAUDE_PROJECT_DIR, else cwd from the hook input, else fail closed

    def test_root_falls_back_to_cwd_from_input(self):
        self.assertIsNone(self.decide("rm -rf dist", project_env=False))
        self.assertEqual(self.decide("rm -rf /Users/someone/dev", project_env=False), "deny")

    def test_without_project_dir_and_cwd_rm_is_denied(self):
        self.assertEqual(self.decide("rm -rf dist", project_env=False, cwd_in_input=False), "deny")
        self.assertEqual(self.decide("rm -rf /tmp/x", project_env=False, cwd_in_input=False), "deny")

    def test_without_project_dir_and_cwd_other_commands_pass(self):
        self.assertIsNone(self.decide("ls -la", project_env=False, cwd_in_input=False))

    # ---- the rest of the guard is unchanged

    def test_other_rules_still_fire(self):
        self.assertEqual(self.decide("git commit --no-verify -m x"), "deny")
        self.assertEqual(self.decide("git reset --hard"), "ask")

    def test_malformed_input_fails_open(self):
        out = subprocess.run(["python3", HOOK], input="not json", capture_output=True, text=True, timeout=10)
        self.assertEqual((out.returncode, out.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
