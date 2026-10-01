"""Tests for claude-kit/statusline.sh: session JSON on stdin, one status line on stdout.

Run: python3 -m unittest discover -s tests -p 'test_*.py'
"""
import json
import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude-kit", "statusline.sh")

FULL = {
    "model": {"display_name": "Opus 5.5"},
    "effort": {"level": "high"},
    "context_window": {"used_percentage": 12.7, "remaining_percentage": 87.3},
    "prompt_cache": {
        "hit_ratio": 0.978,
        "warm": True,
        "ttl": "1h",
        "last_miss_cause": {"causes": ["tools_changed", "ttl_expired"]},
    },
    "cost": {"total_cost_usd": 1.234},
    "rate_limits": {"five_hour": {"used_percentage": 40.2}},
}


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.repo = os.path.join(self.home, "dev", "proj")
        os.makedirs(self.repo)
        git("init", "-q", cwd=self.repo)
        git("checkout", "-q", "-b", "feat/x", cwd=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def run_line(self, payload):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        out = subprocess.run(
            ["/bin/bash", SCRIPT], input=stdin, capture_output=True, text=True,
            env={**os.environ, "HOME": self.home}, timeout=10,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.count("\n"), 0, repr(out.stdout))
        return out.stdout

    def with_dir(self, d, **extra):
        return {**FULL, "workspace": {"current_dir": d}, **extra}

    def test_full_payload_shows_every_segment_in_order(self):
        line = self.run_line(self.with_dir(self.repo))
        self.assertEqual(
            line,
            "~/dev/proj (feat/x) | [Opus 5.5] | effort high | ctx 12% | "
            "cache 97% warm/1h miss:tools_changed,ttl_expired | $1.23 | 5h 40%",
        )

    def test_outside_git_shows_dir_without_branch(self):
        plain = os.path.join(self.home, "notes")
        os.makedirs(plain)
        line = self.run_line(self.with_dir(plain))
        self.assertTrue(line.startswith("~/notes | [Opus 5.5] | "), line)

    def test_cwd_field_is_the_fallback_for_workspace(self):
        payload = {**FULL, "cwd": self.repo}
        self.assertTrue(self.run_line(payload).startswith("~/dev/proj (feat/x) | "))

    def test_cold_cache_without_ttl_or_misses(self):
        payload = self.with_dir(self.repo, prompt_cache={"hit_ratio": 0, "warm": False})
        self.assertIn(" | cache 0% COLD | ", self.run_line(payload))

    def test_minimal_payload_skips_optional_segments(self):
        line = self.run_line({"model": {"display_name": "Sonnet 5.5"}, "workspace": {"current_dir": self.repo}})
        self.assertEqual(line, "~/dev/proj (feat/x) | [Sonnet 5.5] | ctx -")

    def test_invalid_json_prints_a_fallback(self):
        self.assertEqual(self.run_line("not json"), "[claude]")


if __name__ == "__main__":
    unittest.main()
