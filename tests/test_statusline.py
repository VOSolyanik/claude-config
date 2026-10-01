"""Tests for claude-kit/statusline.sh: session JSON on stdin, two coloured lines on stdout.

Run: python3 -m unittest discover -s tests -p 'test_*.py'
"""
import json
import os
import re
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "claude-kit", "statusline.sh")

ANSI = re.compile(r"\x1b\[[0-9;]*m")
GREEN, YELLOW, RED, GRAY = "78", "220", "196", "240"

FULL = {
    "model": {"display_name": "Opus 5.5"},
    "effort": {"level": "high"},
    # 120k used: 12% of the 1M window, 30% of the 400k autoCompactWindow the status line counts against
    "context_window": {
        "used_percentage": 12,
        "context_window_size": 1_000_000,
        "current_usage": {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 19_000,
            "cache_read_input_tokens": 100_000,
            "output_tokens": 5_000,
        },
    },
    "prompt_cache": {
        "hit_ratio": 0.978,
        "warm": True,
        "ttl": "1h",
        "last_miss_cause": {"causes": ["tools_changed", "ttl_expired"]},
    },
    "cost": {"total_cost_usd": 1.234, "total_duration_ms": 3_723_000},
    "rate_limits": {"five_hour": {"used_percentage": 40.2}},
}


def colored(code, text):
    return f"\x1b[38;5;{code}m{text}\x1b[0m"


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
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
        self.settings(autoCompactWindow=400_000)

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, **values):
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f:
            json.dump(values, f)

    def run_raw(self, payload):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        out = subprocess.run(
            ["/bin/bash", SCRIPT], input=stdin, capture_output=True, text=True,
            env={**env, "HOME": self.home}, timeout=10,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")
        return out.stdout.rstrip("\n")

    def lines(self, payload):
        return ANSI.sub("", self.run_raw(payload)).split("\n")

    def with_dir(self, d=None, **extra):
        return {**FULL, "workspace": {"current_dir": d or self.repo}, **extra}

    def ctx(self, used, size=1_000_000):
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 990, "cache_read_input_tokens": used - 1_000}
        return self.with_dir(context_window={
            "used_percentage": round(used * 100 / size), "context_window_size": size, "current_usage": usage,
        })

    # ---- layout

    def test_full_payload_gives_two_lines_with_every_segment(self):
        l1, l2 = self.lines(self.with_dir())
        self.assertEqual(l1, "📁 ~/dev/proj │ 🌿 feat/x │ Opus 5.5 · effort high")
        self.assertEqual(
            l2,
            "██⣿⣀⣀⣀⣀⣀⣀⣀ 30% │ cache 97% miss:tools_changed,ttl_expired │ $1.23 │ 5h 40% │ 1h 2m",
        )

    def test_cwd_field_is_the_fallback_for_workspace(self):
        l1, _ = self.lines({**FULL, "cwd": self.repo})
        self.assertTrue(l1.startswith("📁 ~/dev/proj │ 🌿 feat/x │ "), l1)

    def test_path_outside_home_is_shown_as_is(self):
        l1, _ = self.lines(self.with_dir("/"))
        self.assertTrue(l1.startswith("📁 / │ "), l1)

    def test_duration_formats(self):
        for ms, text in [(0, "0s"), (59_000, "59s"), (125_000, "2m 5s"), (3_723_000, "1h 2m")]:
            payload = self.with_dir(cost={"total_cost_usd": 1, "total_duration_ms": ms})
            self.assertTrue(self.lines(payload)[1].endswith(" │ " + text), (ms, text))

    def test_bar_is_full_at_100_and_empty_at_0(self):
        self.assertTrue(self.lines(self.ctx(400_000))[1].startswith("██⣿⣿⣿⣿⣿⣿⣿⣿ 100%"))
        self.assertTrue(self.lines(self.ctx(1_000))[1].startswith("⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀ 0%"))

    # ---- git status

    def test_git_counts_staged_modified_untracked(self):
        for name in ("a", "b"):
            with open(os.path.join(self.repo, name), "w") as f:
                f.write("1\n")
        git("add", "a", "b", cwd=self.repo)
        git("commit", "-qm", "init", cwd=self.repo)
        with open(os.path.join(self.repo, "a"), "w") as f:
            f.write("2\n")
        with open(os.path.join(self.repo, "new"), "w") as f:
            f.write("x\n")
        with open(os.path.join(self.repo, "staged"), "w") as f:
            f.write("x\n")
        git("add", "staged", cwd=self.repo)
        raw = self.run_raw(self.with_dir())
        self.assertIn("🌿 feat/x", ANSI.sub("", raw))
        self.assertIn(colored("141", "+1 !1 ?1"), raw)

    def test_outside_git_branch_is_a_gray_dash(self):
        plain = os.path.join(self.home, "notes")
        os.makedirs(plain)
        raw = self.run_raw(self.with_dir(plain))
        self.assertEqual(ANSI.sub("", raw).split("\n")[0], "📁 ~/notes │ 🌿 - │ Opus 5.5 · effort high")
        self.assertIn(colored(GRAY, "-"), raw.split("\n")[0])

    # ---- context: used tokens against autoCompactWindow (the compaction point), not the model window

    def assert_ctx(self, used, size, text, color):
        raw = self.run_raw(self.ctx(used, size))
        self.assertIn(colored(color, text), raw.split("\n")[1], (used, size, text, color))

    def test_ctx_is_counted_against_auto_compact_window(self):
        self.assert_ctx(200_000, 1_000_000, "50%", GREEN)

    def test_without_auto_compact_window_ctx_falls_back_to_the_model_window(self):
        self.settings()
        self.assert_ctx(200_000, 1_000_000, "20%", GREEN)

    def test_missing_settings_file_falls_back_to_the_model_window(self):
        os.remove(os.path.join(self.home, ".claude", "settings.json"))
        self.assert_ctx(200_000, 1_000_000, "20%", GREEN)

    def test_used_above_the_compaction_point_is_capped_at_100(self):
        self.assert_ctx(500_000, 1_000_000, "100%", RED)
        self.assertTrue(self.lines(self.ctx(500_000))[1].startswith("██⣿⣿⣿⣿⣿⣿⣿⣿ 100%"))

    def test_thresholds_are_60_and_80_percent_of_the_compaction_point(self):
        self.assert_ctx(239_999, 1_000_000, "59%", GREEN)
        self.assert_ctx(240_000, 1_000_000, "60%", YELLOW)
        self.assert_ctx(319_999, 1_000_000, "79%", YELLOW)
        self.assert_ctx(320_000, 1_000_000, "80%", RED)

    def test_thresholds_without_auto_compact_window(self):
        self.settings()
        self.assert_ctx(599_999, 1_000_000, "59%", GREEN)
        self.assert_ctx(600_000, 1_000_000, "60%", YELLOW)
        self.assert_ctx(800_000, 1_000_000, "80%", RED)

    def test_compact_window_larger_than_the_model_window_uses_the_window(self):
        self.settings(autoCompactWindow=1_000_000)
        self.assert_ctx(100_000, 200_000, "50%", GREEN)
        self.assert_ctx(120_000, 200_000, "60%", YELLOW)

    def test_without_current_usage_ctx_is_a_gray_dash(self):
        payload = self.with_dir(context_window={"used_percentage": 12, "context_window_size": 1_000_000})
        raw = self.run_raw(payload)
        self.assertTrue(ANSI.sub("", raw).split("\n")[1].startswith("⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀ - │ "))
        self.assertIn(colored(GRAY, "-"), raw.split("\n")[1].split("│")[0])

    def test_bar_takes_the_context_color(self):
        raw = self.run_raw(self.ctx(320_000))
        self.assertTrue(raw.split("\n")[1].startswith(f"\x1b[38;5;{RED}m█"), repr(raw))

    def test_cost_and_5h_colors(self):
        for usd, color in [(1.99, GREEN), (2.5, YELLOW), (10.5, RED)]:
            raw = self.run_raw(self.with_dir(cost={"total_cost_usd": usd, "total_duration_ms": 1000}))
            self.assertIn(colored(color, f"${usd:.2f}"), raw, usd)
        for pct, color in [(49, GREEN), (50, YELLOW), (80, RED)]:
            raw = self.run_raw(self.with_dir(rate_limits={"five_hour": {"used_percentage": pct}}))
            self.assertIn(colored(color, f"{pct}%"), raw.split("\n")[1].split("5h ")[1], pct)

    # ---- cache

    def test_cold_cache_without_misses(self):
        payload = self.with_dir(prompt_cache={"hit_ratio": 0, "warm": False})
        self.assertIn(" │ cache 0% COLD │ ", self.lines(payload)[1])

    def test_warm_cache_without_misses_shows_only_the_ratio(self):
        payload = self.with_dir(prompt_cache={"hit_ratio": 0.5, "warm": True, "last_miss_cause": None})
        self.assertIn(" │ cache 50% │ ", self.lines(payload)[1])

    # ---- missing data

    def test_minimal_payload_shows_gray_dashes(self):
        raw = self.run_raw({"model": {"display_name": "Sonnet 5.5"}, "workspace": {"current_dir": self.repo}})
        l1, l2 = ANSI.sub("", raw).split("\n")
        self.assertEqual(l1, "📁 ~/dev/proj │ 🌿 feat/x │ Sonnet 5.5 · effort -")
        self.assertEqual(l2, "⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀ - │ cache - │ $- │ 5h - │ -")
        self.assertEqual(raw.split("\n")[1].count(colored(GRAY, "-")), 5)

    def test_empty_object_still_renders(self):
        l1, l2 = self.lines({})
        self.assertEqual(l1, "📁 - │ 🌿 - │ Claude · effort -")
        self.assertTrue(l2.startswith("⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀ - │ "))

    def test_invalid_json_prints_a_fallback(self):
        self.assertEqual(self.run_raw("not json"), "[claude]")


if __name__ == "__main__":
    unittest.main()
