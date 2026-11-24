"""End-to-end tests for the panel's parser (sidecars/context/context_report.py) on synthetic conversation databases.

Run with: python -m unittest discover tests
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent.parent / "sidecars" / "context" / "context_report.py"

spec = importlib.util.spec_from_file_location("context_report", SCRIPT)
context_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_report)

# --- protobuf encoding -------------------------------------------------------


def varint(n: int) -> bytes:
    n &= 0xFFFFFFFFFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def f(fn: int, val) -> bytes:
    if isinstance(val, int):
        return varint(fn << 3) + varint(val)
    if isinstance(val, str):
        val = val.encode()
    return varint(fn << 3 | 2) + varint(len(val)) + val


def m(*parts: bytes) -> bytes:
    return b"".join(parts)


# --- database builder --------------------------------------------------------


class ConversationDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE steps (idx integer, step_type integer NOT NULL DEFAULT 0, status integer NOT NULL DEFAULT 0, "
            "has_subtrajectory numeric NOT NULL DEFAULT false, metadata blob, error_details blob, permissions blob, "
            "task_details blob, render_info blob, step_payload blob, step_format integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))"
        )
        self.conn.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
        self.n = 0

    def step(self, step_type: int, metadata: bytes = b"", payload_field: int | None = None, payload: bytes = b"",
             status: int = 3, task_details: bytes = b""):
        pb = m(f(1, step_type), f(4, status), f(5, metadata), f(payload_field, payload) if payload_field else b"")
        self.conn.execute(
            "INSERT INTO steps (idx, step_type, status, metadata, task_details, step_payload) VALUES (?, ?, ?, ?, ?, ?)",
            (self.n, step_type, status, metadata, task_details or None, pb),
        )
        self.n += 1

    def generation(self, groups: list[bytes], max_context: int = 1_000_000, usage: bytes = b"", checkpoint_index: int = -1,
                   model: str = "test-model", display_name: str = "Test Model"):
        cwm = m(f(1, 0), f(3, m(*[f(1, g) for g in groups])), f(4, max_context))
        chat = m(f(4, usage), f(9, m(f(2, checkpoint_index), f(10, cwm))), f(21, display_name) if display_name else b"")
        data = m(f(1, chat), f(3, f(28, model)))
        self.conn.execute("INSERT INTO gen_metadata (idx, data) VALUES ((SELECT COUNT(*) FROM gen_metadata), ?)", (data,))

    def close(self):
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None


def group(token_type: int, tokens: int, *children: tuple[str, int], name: str = "group") -> bytes:
    return m(f(1, name), f(2, token_type), f(4, tokens), *[f(5, m(f(1, n), f(3, t))) for n, t in children])


def user_input(text: str) -> bytes:
    return f(2, text)


def planner(text: str) -> bytes:
    return f(1, text)


def usage(input_tokens: int = 0, output: int = 0, cache_read: int = 0, thinking: int = 0, response: int = 0) -> bytes:
    parts = [f(2, input_tokens), f(3, output), f(5, cache_read)]
    if thinking:
        parts.append(f(9, thinking))
    if response:
        parts.append(f(10, response))
    return m(*parts)


def tool_call(args: str) -> bytes:
    return f(4, m(f(2, "tool"), f(3, args)))


# Step types and payload fields used below.
USER_INPUT, PLANNER_RESPONSE, CHECKPOINT, FINISH, VIEW_FILE, CODE_ACTION, GENERIC = 14, 15, 23, 2, 8, 5, 132
P_USER, P_PLANNER, P_CHECKPOINT, P_VIEW_FILE, P_CODE_ACTION = 19, 20, 30, 14, 10


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db_path = self.home / "conv.db"
        self.db = ConversationDB(self.db_path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: getattr(self, "db", None) and self.db.close())

    def tearDown(self):
        self.db.close()

    def run_report(self, *args: str) -> str:
        self.db.close()
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8")
        res = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(self.db_path), *args],
            capture_output=True, env=env, text=True, encoding="utf-8", check=True,
        )
        return res.stdout

    def report(self, *args: str) -> dict:
        data = json.loads(self.run_report("--json", *args))
        data["by_name"] = {c["name"]: c["tokens"] for c in data["categories"]}
        return data

    def basic_steps(self):
        self.db.step(USER_INPUT, payload_field=P_USER, payload=user_input("hello world!!"))  # 13 bytes -> 3
        self.db.step(PLANNER_RESPONSE, metadata=f(9, usage(900, 50)), payload_field=P_PLANNER, payload=planner("r" * 400))
        self.db.step(GENERIC, metadata=tool_call("a" * 40))  # 40 bytes -> 10

    # --- breakdown from the latest generation ---

    def test_exact_breakdown_from_chat_items(self):
        self.basic_steps()
        self.db.generation([
            group(1, 1000, ("skills", 200), ("user_rules", 50), ("subagents", 30), ("flows", 20)),
            group(2, 500),
            group(3, 30),
            group(4, 40),
            group(7, 370, ("0: USER_INPUT", 20), ("1: PLANNER_RESPONSE", 200), ("2: GENERIC", 80),
                  ("3: FINISH", 30), ("4: CHECKPOINT", 40)),
            group(8, 11),
            group(9, 12),
            group(10, 13),
        ])
        r = self.report()
        c = r["by_name"]
        self.assertEqual(c["User messages"], 20)
        self.assertEqual(c["Agent responses"], 240)  # planner + checkpoint
        self.assertEqual(c["Tool calls"], 80)
        self.assertEqual(c["System messages"], 30)  # FINISH
        self.assertEqual(c["System prompt"], 750)  # user_rules stays in the system prompt
        self.assertEqual(c["Skills"], 200)
        self.assertEqual(c["Subagents"], 30)
        self.assertEqual(c["Workflows"], 33)  # flows item + FLOWS group
        self.assertEqual(c["Legacy workflows"], 11)
        self.assertEqual(c["Plugins"], 12)
        self.assertEqual(c["Rules"], 30)
        self.assertEqual(c["MCP tools"], 40)
        self.assertNotIn("System overhead", c)
        self.assertEqual(r["used_tokens"], sum(v for v in c.values()))
        self.assertEqual(r["limit_tokens"], 1_000_000)
        self.assertTrue(r["has_breakdown"])

    def test_latest_generation_wins_over_larger_one(self):
        self.basic_steps()
        self.db.generation([group(1, 90000), group(7, 10, ("0: USER_INPUT", 10))])
        self.db.generation([group(1, 100), group(7, 10, ("0: USER_INPUT", 10))])
        self.assertEqual(self.report()["by_name"]["System prompt"], 100)

    def test_unknown_item_scales_step_estimates(self):
        self.basic_steps()
        self.db.generation([group(7, 315, ("0: USER_INPUT", 20), ("1: NOT_A_STEP_TYPE", 1))])
        c = self.report()["by_name"]
        # Step estimates 3 / 50 / 10 scaled by 315 / 63 = 5.
        self.assertEqual((c["User messages"], c["Agent responses"], c["Tool calls"]), (15, 250, 50))

    def test_overhead_only_without_prompt_or_tools(self):
        self.basic_steps()
        self.db.generation([group(7, 300, ("0: USER_INPUT", 300))], usage=usage(1000, 0, 500))
        self.assertEqual(self.report()["by_name"]["System overhead"], 1200)

    def test_no_overhead_with_system_prompt(self):
        self.basic_steps()
        self.db.generation([group(1, 100), group(7, 300, ("0: USER_INPUT", 300))], usage=usage(1000, 0, 500))
        self.assertNotIn("System overhead", self.report()["by_name"])

    # --- estimates without a breakdown ---

    def test_awaiting_generation_uses_step_estimates(self):
        self.basic_steps()
        self.db.step(FINISH, metadata=tool_call("b" * 8))
        r = self.report()
        c = r["by_name"]
        self.assertFalse(r["has_breakdown"])
        self.assertEqual((c["User messages"], c["Agent responses"], c["Tool calls"], c["System messages"]), (3, 50, 10, 2))
        self.assertEqual(r["limit_tokens"], 0)
        self.assertIn("Estimated usage (awaiting generation)", self.run_report())

    def test_tool_call_output_tokens_take_precedence(self):
        self.db.step(GENERIC, metadata=m(tool_call("a" * 400), f(23, 7)))
        self.assertEqual(self.report()["by_name"]["Tool calls"], 7)

    # --- compaction ---

    def test_compaction_zeroes_summarized_steps(self):
        self.basic_steps()
        self.db.step(CHECKPOINT, payload_field=P_CHECKPOINT,
                     payload=m(f(1, 0), f(11, 0), f(12, 2), f(4, "Fix the login bug"), f(5, "s" * 383)))
        self.db.step(USER_INPUT, payload_field=P_USER, payload=user_input("next question please"))  # 20 -> 5
        self.db.generation([group(1, 800), group(7, 370, ("0: USER_INPUT", 370))])
        r = self.report()
        c = r["by_name"]
        self.assertTrue(r["compacted"])
        self.assertEqual(c["User messages"], 5)
        self.assertEqual(c["Agent responses"], 100)  # (17 + 383) / 4 summary estimate
        self.assertEqual(c["Tool calls"], 0)
        self.assertEqual(c["System prompt"], 800)
        self.assertEqual(r["active_checkpoint"], 0)
        out = self.run_report()
        self.assertIn("Estimated after compaction · updates next turn", out)
        self.assertIn("Checkpoints (1) · /rewind", out)
        self.assertIn("└ Checkpoint 1 (active, in context): Fix the login bug", out)

    def test_checkpoint_already_in_generation_is_not_newer(self):
        self.basic_steps()
        self.db.step(CHECKPOINT, payload_field=P_CHECKPOINT, payload=m(f(1, 0), f(11, 0), f(12, 2)))
        self.db.generation([group(7, 10, ("0: USER_INPUT", 10))], checkpoint_index=0)
        r = self.report()
        self.assertFalse(r["compacted"])
        self.assertIn("steps 0–2", self.run_report())

    def test_checkpoint_view_uses_request_label_and_history_items(self):
        self.db.step(USER_INPUT, metadata=f(1, f(1, 1795262000)), payload_field=P_USER, payload=user_input("first"))
        self.db.step(
            CHECKPOINT,
            metadata=f(1, f(1, 1795262100)),
            payload_field=P_CHECKPOINT,
            payload=m(f(1, 0), f(11, 0), f(12, 1), f(30, f(1, "Setup repo")), f(30, f(1, "/compact"))),
        )
        self.db.step(
            CHECKPOINT,
            metadata=f(1, f(1, 1795262500)),
            payload_field=P_CHECKPOINT,
            payload=m(f(1, 1), f(11, 1), f(12, 5), f(30, f(1, "Fix navbar")), f(30, f(1, "/compact"))),
        )
        self.db.generation([group(7, 10, ("0: USER_INPUT", 10))], checkpoint_index=1)
        r = self.report()
        ck_view = r["checkpoint_view"]
        self.assertEqual(ck_view["active_number"], 2)
        self.assertEqual(ck_view["active_text"], "Fix navbar")
        self.assertEqual(ck_view["historical"], 1)
        self.assertEqual([it["number"] for it in ck_view["items"]], [2, 1])
        self.assertEqual([it["text"] for it in ck_view["items"]], ["Fix navbar", "Setup repo"])
        self.assertEqual(ck_view["items"][0]["active"], True)
        self.assertEqual(ck_view["items"][1]["active"], False)
        self.assertEqual(ck_view["items"][1]["created_at"], 1795262100)

    def test_checkpoint_buffer_counts_checkpoint_steps(self):
        self.db.step(CHECKPOINT, metadata=tool_call("c" * 80), payload_field=P_CHECKPOINT, payload=f(1, 0))
        self.db.step(PLANNER_RESPONSE, metadata=f(9, usage(0, 5)), payload_field=P_PLANNER, payload=planner("x"))
        r = self.report()
        self.assertEqual(r["checkpoint_buffer"], 20)
        self.assertNotIn(20, [v for k, v in r["by_name"].items()])

    # --- files ---

    def test_file_sections(self):
        home_uri = "file:///" + str(self.home).replace("\\", "/").lstrip("/")
        self.db.step(VIEW_FILE, metadata=tool_call("v" * 40), payload_field=P_VIEW_FILE,
                     payload=m(f(1, home_uri + "/.gemini/skills/foo/SKILL.md"), f(17, 1)))
        edit = lambda path: f(2, f(1, f(8, home_uri + path)))  # noqa: E731
        self.db.step(CODE_ACTION, metadata=tool_call("w" * 80), payload_field=P_CODE_ACTION, payload=m(edit("/brain/task.md"), f(21, 1)))
        self.db.step(CODE_ACTION, metadata=tool_call("w" * 80), payload_field=P_CODE_ACTION, payload=m(edit("/brain/task.md"), f(21, 1)))
        self.db.step(CODE_ACTION, metadata=tool_call("k" * 40), payload_field=P_CODE_ACTION, payload=m(edit("/knowledge/a.md"), f(24, 1)))
        (self.home / ".gemini" / "skills").mkdir(parents=True)
        (self.home / ".gemini" / "GEMINI.md").write_text("x")
        (self.home / ".gemini" / "antigravity" / "skills").mkdir(parents=True)
        (self.home / ".gemini" / "config" / "skills").mkdir(parents=True)  # not an auto-loaded folder
        (self.home / "ws" / ".agents" / "rules").mkdir(parents=True)
        (self.home / "ws" / ".agents" / "hooks.json").write_text("{}")
        r = self.report("--workspace", str(self.home / "ws"))
        self.assertEqual(r["skill_files"], [{"path": "~/.gemini/skills/foo/SKILL.md", "tokens": 10}])
        self.assertEqual(r["artifact_files"], [{"path": "~/brain/task.md", "tokens": 20}])
        self.assertEqual(r["knowledge_files"], [{"path": "~/knowledge/a.md", "tokens": 10}])
        self.assertEqual([s["path"] for s in r["system_files"]],
                         ["~/.gemini/GEMINI.md", "~/.gemini/skills/", "~/.gemini/antigravity/skills/",
                          ".agents/rules/", ".agents/hooks.json"])
        out = self.run_report("--workspace", str(self.home / "ws"))
        self.assertIn("Knowledge files · /rules\n └ ~/knowledge/a.md: 10 tokens", out)
        self.assertIn("System files · auto-loaded\n └ ~/.gemini/GEMINI.md\n", out)

    def test_generic_steps_are_rehydrated(self):
        home_uri = "file:///" + str(self.home).replace("\\", "/").lstrip("/")
        # Desktop app: a write wrapped in a GENERIC step that packs the original code_action step.
        original = m(f(1, CODE_ACTION), f(4, 3), f(5, tool_call("w" * 80)),
                     f(P_CODE_ACTION, m(f(2, f(1, f(8, home_uri + "/brain/plan.md"))), f(21, 1))))
        packed = m(f(1, "type.googleapis.com/gemini_coder.Step"), f(2, original))
        self.db.step(GENERIC, metadata=tool_call("ignored"), payload_field=140, payload=f(2, f(6, packed)))
        # A GENERIC view_file with no packed original: rebuilt from the tool call arguments.
        args = json.dumps({"AbsolutePath": str(self.home / "skills" / "x" / "SKILL.md")})
        self.db.step(GENERIC, metadata=f(4, m(f(2, "view_file"), f(3, args))), payload_field=140, payload=f(2, b""))
        r = self.report()
        self.assertEqual(r["artifact_files"], [{"path": "~/brain/plan.md", "tokens": 20}])
        self.assertEqual(r["skill_files"], [{"path": "~/skills/x/SKILL.md", "tokens": len(args) // 4}])

    # --- model label and context limit ---

    def test_native_window_and_label_from_model_id(self):
        self.basic_steps()
        self.db.generation([group(7, 300, ("0: USER_INPUT", 300))], max_context=256_000,
                           model="gemini-3.8-flash-medium", display_name="")
        r = self.report()
        self.assertEqual(r["model"], "Gemini 3.8 Flash (Medium)")
        self.assertEqual(r["limit_tokens"], 1_048_576)
        self.assertEqual(r["request_budget_tokens"], 256_000)
        self.assertIn("Gemini 3.8 Flash (Medium) · 300/1.0M tokens", self.run_report())

    def test_model_catalog_overrides_table(self):
        self.basic_steps()
        self.db.generation([group(1, 100), group(7, 300, ("0: USER_INPUT", 300))],
                           model="gemini-9-test-high", display_name="")
        catalog = self.home / "catalog.json"
        catalog.write_text(json.dumps({"gemini-9-test-high": {"maxTokens": 2_000_000, "displayName": "Gemini 9 Test (High)"}}))
        r = self.report("--model-catalog", str(catalog))
        self.assertEqual((r["model"], r["limit_tokens"]), ("Gemini 9 Test (High)", 2_000_000))
        self.assertEqual(r["heading"], "Token usage by category")
        self.assertEqual(r["grid_segments"], [100, 300, 0, 0, 0])
        self.assertTrue(r["text"].startswith("└ Context Usage"))
        self.assertIn("Gemini 9 Test (High) · 400/2M tokens", r["text"])

    def test_unknown_model_falls_back_to_request_budget(self):
        self.basic_steps()
        self.db.generation([group(7, 300, ("0: USER_INPUT", 300))], max_context=256_000, model="new-model-x", display_name="")
        r = self.report()
        self.assertEqual((r["model"], r["limit_tokens"]), ("New Model X", 256_000))

    # --- rendering ---

    def test_text_layout(self):
        self.basic_steps()
        self.db.generation([group(1, 1000), group(7, 300, ("0: USER_INPUT", 300))], max_context=256_000)
        out = self.run_report()
        lines = out.splitlines()
        self.assertEqual(lines[0], "└ Context Usage")
        self.assertTrue(lines[2].endswith("Test Model · 1.3k/256.0k tokens"))
        self.assertIn("◉ Agent responses: 0 tokens (0.0%)", out)  # always shown
        self.assertNotIn("Plugins", out)  # zero, hidden
        self.assertTrue(out.rstrip().endswith("Related: /artifact · /skill · /rewind"))
        grid_rows = [l for l in lines if l.startswith(("◉", "□"))]
        self.assertEqual(len(grid_rows[0].split("   ")[0].split(" ")), 20)  # 20 columns at width 100

    def test_limit_override_and_narrow_layout(self):
        self.basic_steps()
        self.db.generation([group(7, 300, ("0: USER_INPUT", 300))])
        out = self.run_report("--limit", "200000", "--width", "60")
        lines = out.splitlines()
        self.assertEqual(lines[2], "Test Model · 300/200.0k tokens")  # stats above the grid
        self.assertTrue(lines[-3].startswith("□"))

    # --- background tasks ---

    def test_background_tasks_extraction(self):
        log_file = self.home / "task-0.log"
        log_file.write_text("line 1\nline 2\n", encoding="utf-8")
        log_uri = log_file.as_uri()

        def td(task_id: str, uri: str = "", preview: str = "", cmd: str = "", is_daemon: int = 0,
               title: str = "", ts: int = 1795262200) -> bytes:
            parts = [f(1, task_id), f(8, f(1, ts))]
            if uri:
                parts.append(f(2, uri))
            if preview:
                parts.append(f(3, preview))
            if cmd:
                parts.append(f(4, cmd))
            if is_daemon:
                parts.append(f(6, is_daemon))
            if title:
                parts.append(f(7, title))
            return m(*parts)

        # idx 0: cancelled command with log file
        self.db.step(
            GENERIC,
            metadata=f(4, m(f(2, "run_command"), f(3, json.dumps({"CommandLine": "npm run build"})))),
            status=6,
            task_details=td("conv/task-0", uri=log_uri, preview="building...", cmd="npm run build", title="Build app"),
        )
        # idx 1: schedule timer with empty td.str(4), falls back to tool call JSON
        self.db.step(
            GENERIC,
            metadata=f(4, m(f(2, "schedule"), f(3, json.dumps({"DurationSeconds": 30, "Prompt": "Check workers"})))),
            status=3,
            task_details=td("conv/task-1", title="30s timer", ts=1795262210),
        )
        # idx 2: running daemon command
        self.db.step(
            GENERIC,
            metadata=f(4, m(f(2, "run_command"), f(3, json.dumps({"CommandLine": "python3 server.py", "IsDaemon": True})))),
            status=2,
            task_details=td("conv/task-2", preview="listening", cmd="python3 server.py", is_daemon=1, title="Dev server", ts=1795262220),
        )
        # idx 3: running normal command (newer running task should sort first)
        self.db.step(
            GENERIC,
            metadata=f(4, m(f(2, "run_command"), f(3, json.dumps({"CommandLine": "pytest -q"})))),
            status=2,
            task_details=td("conv/task-3", cmd="pytest -q", title="Run tests", ts=1795262230),
        )

        tasks = self.report()["background_tasks"]
        self.assertEqual([t["step_idx"] for t in tasks], [3, 2, 1, 0])

        self.assertEqual(tasks[0]["status"], "running")
        self.assertEqual(tasks[0]["command"], "pytest -q")
        self.assertFalse(tasks[0]["is_daemon"])

        self.assertEqual(tasks[1]["status"], "running")
        self.assertTrue(tasks[1]["is_daemon"])
        self.assertEqual(tasks[1]["tool"], "run_command")

        self.assertEqual(tasks[2]["status"], "done")
        self.assertEqual(tasks[2]["tool"], "schedule")
        self.assertEqual(tasks[2]["command"], "Timer: 30s, Prompt: Check workers")

        self.assertEqual(tasks[3]["status"], "cancelled")
        self.assertTrue(tasks[3]["has_log"])
        self.assertGreater(tasks[3]["log_size"], 0)
        self.assertGreater(tasks[3]["log_mtime"], 0)

    # --- prompt cache hit rate ---

    def test_cache_hit_rate_stats(self):
        self.basic_steps()
        empty = self.report()["cache_stats"]
        self.assertEqual(empty["generations"], 0)
        self.assertIsNone(empty["last_hit_rate"])
        self.assertIsNone(empty["session_hit_rate"])

        # Re-open DB and add 3 generations with explicit uncached input + cache_read usage.
        self.db = ConversationDB.__new__(ConversationDB)
        self.db.conn = sqlite3.connect(self.db_path)
        self.db.generation([group(7, 1000, ("0: USER_INPUT", 1000))], usage=usage(input_tokens=1000, cache_read=0))
        self.db.generation([group(7, 1000, ("0: USER_INPUT", 1000))], usage=usage(input_tokens=200, cache_read=800))
        self.db.generation([group(7, 1000, ("0: USER_INPUT", 1000))], usage=usage(input_tokens=100, cache_read=900))

        cs = self.report()["cache_stats"]
        self.assertEqual(cs["generations"], 3)
        self.assertEqual(cs["last_hit_rate"], 90.0)
        self.assertEqual(cs["last_cache_read_tokens"], 900)
        self.assertEqual(cs["last_input_tokens"], 100)
        self.assertEqual(cs["last_total_input_tokens"], 1000)
        self.assertEqual(cs["session_hit_rate"], 56.7)
        self.assertEqual(cs["session_cache_read_tokens"], 1700)
        self.assertEqual(cs["session_input_tokens"], 1300)
        self.assertEqual(cs["session_total_input_tokens"], 3000)

    def test_output_and_thinking_stats(self):
        self.basic_steps()
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=100, output=1000, cache_read=900, thinking=600, response=400),
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=100, output=500, cache_read=900, thinking=150, response=350),
        )
        os_stats = self.report()["output_stats"]
        self.assertEqual(os_stats["session_output_tokens"], 1500)
        self.assertEqual(os_stats["session_thinking_tokens"], 750)
        self.assertEqual(os_stats["session_response_tokens"], 750)
        self.assertEqual(os_stats["thinking_pct"], 50.0)
        self.assertEqual(os_stats["last_output_tokens"], 500)
        self.assertEqual(os_stats["last_thinking_tokens"], 150)

    def test_subagent_tasks_extraction(self):
        sub_running_id = "11111111-1111-1111-1111-111111111111"
        sub_done_id = "22222222-2222-2222-2222-222222222222"
        sub_killed_id = "33333333-3333-3333-3333-333333333333"

        # Sibling DB for running subagent (has a step with status=2)
        sub_running_db = ConversationDB(self.home / f"{sub_running_id}.db")
        sub_running_db.step(GENERIC, status=2)
        sub_running_db.close()

        # Sibling DB for finished subagent (steps all status=3)
        sub_done_db = ConversationDB(self.home / f"{sub_done_id}.db")
        sub_done_db.step(GENERIC, status=3)
        sub_done_db.close()

        def sub_step(cid: str, role: str, type_name: str, prompt: str, ts: int):
            md = m(
                f(1, f(1, ts)),
                f(4, m(f(2, "invoke_subagent"), f(3, json.dumps({"Role": role, "TypeName": type_name, "Prompt": prompt})))),
            )
            res = f(1, f'Created the following subagent:\n{{\n  "conversationId": "{cid}"\n}}')
            self.db.step(GENERIC, metadata=md, payload_field=140, payload=f(2, res), status=3)

        sub_step(sub_done_id, "Codebase Researcher", "research", "Inspect parser", 1795262000)
        sub_step(sub_killed_id, "Temp Explorer", "research", "Explore logs", 1795262010)
        sub_step(sub_running_id, "Live Worker", "self", "Watch files", 1795262020)

        # Later manage_subagents kill for sub_killed_id
        self.db.step(
            GENERIC,
            metadata=f(4, m(f(2, "manage_subagents"), f(3, json.dumps({"Action": "kill", "ConversationIds": [sub_killed_id]})))),
            status=3,
        )

        tasks = self.report()["background_tasks"]
        # Running subagent sorted first, then newer finished/cancelled subagents by step_idx desc
        self.assertEqual([t["task_id"] for t in tasks], [sub_running_id, sub_killed_id, sub_done_id])
        self.assertEqual(tasks[0]["status"], "running")
        self.assertEqual(tasks[0]["tool"], "invoke_subagent")
        self.assertEqual(tasks[0]["title"], "Live Worker")
        self.assertEqual(tasks[0]["subagent_type"], "self")
        self.assertEqual(tasks[0]["command"], "Watch files")

        self.assertEqual(tasks[1]["status"], "cancelled")
        self.assertEqual(tasks[2]["status"], "done")

    # --- multi-model usage ---

    def test_multi_model_usage_breakdown(self):
        self.basic_steps()
        # 2 calls from planning/flagship model (one without model_id slug, matching display_name)
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=1000, output=500, cache_read=4000, thinking=200),
            model="",
            display_name="Gemini 4 Hydrogen (XHigh)",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=500, output=500, cache_read=2000, thinking=100),
            model="gemini-4-hydrogen-xhigh",
            display_name="",
        )
        # 1 call from lighter execution model
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=200, output=300, cache_read=1500, thinking=50),
            model="gemini-4-neon-medium",
            display_name="",
        )

        mu = self.report()["model_usage"]
        self.assertEqual(len(mu), 2)

        self.assertEqual(mu[0]["model"], "Gemini 4 Hydrogen")
        self.assertEqual(mu[0]["model_id"], "gemini-4-hydrogen")
        self.assertEqual(mu[0]["calls"], 2)
        self.assertEqual(mu[0]["efforts"], {"XHigh": 2})
        self.assertEqual(mu[0]["input_tokens"], 1500)
        self.assertEqual(mu[0]["cache_read_tokens"], 6000)
        self.assertEqual(mu[0]["total_input_tokens"], 7500)
        self.assertEqual(mu[0]["output_tokens"], 1000)
        self.assertEqual(mu[0]["thinking_tokens"], 300)
        self.assertEqual(mu[0]["total_tokens"], 8500)
        self.assertEqual(mu[0]["cache_hit_rate"], 80.0)
        self.assertEqual(mu[0]["share_pct"], 81.0)

        self.assertEqual(mu[1]["model"], "Gemini 4 Neon")
        self.assertEqual(mu[1]["model_id"], "gemini-4-neon")
        self.assertEqual(mu[1]["calls"], 1)
        self.assertEqual(mu[1]["efforts"], {"Medium": 1})
        self.assertEqual(mu[1]["total_tokens"], 2000)
        self.assertEqual(mu[1]["cache_hit_rate"], 88.2)
        self.assertEqual(mu[1]["share_pct"], 19.0)

    def test_same_model_different_efforts_grouped_together(self):
        self.basic_steps()
        catalog = self.home / "catalog.json"
        catalog.write_text(json.dumps({
            "gemini-4-hydrogen-tiered-high": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Hydrogen (High)"},
            "gemini-pro-orchestrator": {"maxTokens": 1_048_576, "displayName": "Orchestrator (Carbon)"},
            "gemini-pro-orchestrator-neon": {"maxTokens": 1_048_576, "displayName": "Orchestrator (Neon)"},
        }))
        # Hydrogen across XHigh, High (catalog), Medium (slug)
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=1000, output=200, cache_read=3000, thinking=50),
            model="",
            display_name="Gemini 4 Hydrogen (XHigh)",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=500, output=100, cache_read=1500, thinking=25),
            model="gemini-4-hydrogen-tiered-high",
            display_name="",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=300, output=100, cache_read=1000, thinking=20),
            model="gemini-4-hydrogen-medium",
            display_name="",
        )
        # Neon across XHigh and Medium
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=400, output=100, cache_read=1000, thinking=10),
            model="",
            display_name="Gemini 4 Neon (XHigh)",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=200, output=50, cache_read=500, thinking=5),
            model="gemini-4-neon-medium",
            display_name="",
        )
        # Non-effort parenthetical variants must stay separate
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=100, output=50, cache_read=200),
            model="gemini-pro-orchestrator",
            display_name="",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=100, output=50, cache_read=100),
            model="gemini-pro-orchestrator-neon",
            display_name="",
        )

        mu = self.report("--model-catalog", str(catalog))["model_usage"]
        by_name = {m["model"]: m for m in mu}
        self.assertEqual(set(by_name.keys()), {
            "Gemini 4 Hydrogen",
            "Gemini 4 Neon",
            "Orchestrator (Carbon)",
            "Orchestrator (Neon)",
        })
        self.assertEqual(by_name["Gemini 4 Hydrogen"]["calls"], 3)
        self.assertEqual(by_name["Gemini 4 Hydrogen"]["total_tokens"], 7700)
        self.assertEqual(by_name["Gemini 4 Hydrogen"]["efforts"], {"XHigh": 1, "High": 1, "Medium": 1})
        self.assertEqual(by_name["Gemini 4 Neon"]["calls"], 2)
        self.assertEqual(by_name["Gemini 4 Neon"]["total_tokens"], 2250)
        self.assertEqual(by_name["Gemini 4 Neon"]["efforts"], {"XHigh": 1, "Medium": 1})
        self.assertEqual(by_name["Orchestrator (Carbon)"]["efforts"], {})
        self.assertEqual(by_name["Orchestrator (Neon)"]["efforts"], {})

    def test_catalog_partial_rename_and_shared_display_name(self):
        self.basic_steps()
        catalog = self.home / "catalog_edge.json"
        catalog.write_text(json.dumps({
            # Only High variant has a custom rename without effort label; Medium uses default name
            "gemini-4-hydrogen-high": {"maxTokens": 1_048_576, "displayName": "Hydrogen Custom"},
            "gemini-4-hydrogen-medium": {"maxTokens": 1_048_576, "displayName": "Gemini 4 Hydrogen (Medium)"},
            # Two distinct model IDs sharing an identical catalog displayName must NOT merge
            "gemini-2.5-flash": {"maxTokens": 1_048_576, "displayName": "Gemini 3.5 Flash Lite"},
            "gemini-3.5-flash-lite": {"maxTokens": 1_048_576, "displayName": "Gemini 3.5 Flash Lite"},
        }))
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=100, output=50),
            model="gemini-4-hydrogen-high",
            display_name="",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=100, output=50),
            model="gemini-4-hydrogen-medium",
            display_name="",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=100, output=50),
            model="gemini-4-hydrogen-medium",
            display_name="",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=80, output=20),
            model="gemini-2.5-flash",
            display_name="",
        )
        self.db.generation(
            [group(7, 1000, ("0: USER_INPUT", 1000))],
            usage=usage(input_tokens=70, output=20),
            model="gemini-3.5-flash-lite",
            display_name="",
        )
        mu = self.report("--model-catalog", str(catalog))["model_usage"]
        by_id = {m["model_id"]: m for m in mu}
        self.assertEqual(set(by_id.keys()), {"gemini-4-hydrogen", "gemini-2.5-flash", "gemini-3.5-flash-lite"})
        self.assertEqual(by_id["gemini-4-hydrogen"]["model"], "Gemini 4 Hydrogen")
        self.assertEqual(by_id["gemini-4-hydrogen"]["efforts"], {"Medium": 2, "High": 1})
        self.assertEqual(by_id["gemini-2.5-flash"]["calls"], 1)
        self.assertEqual(by_id["gemini-3.5-flash-lite"]["calls"], 1)

    def test_json_type_guards_and_missing_subagent_db(self):
        # Null and array JSON in view_file rehydrate + non-list ConversationIds + pending kill must not crash or cancel
        for bad_args in ("null", "[1, 2]", '"string"'):
            self.db.step(GENERIC, metadata=f(4, m(f(2, "view_file"), f(3, bad_args))), payload_field=140, payload=f(2, b""))

        missing_sub_id = "44444444-4444-4444-4444-444444444444"
        md_sub = m(f(1, f(1, 1795262000)), f(4, m(f(2, "invoke_subagent"), f(3, json.dumps({"Role": "Explorer", "TypeName": "research", "Prompt": "Scan"})))))
        res_sub = f(1, f'{{"conversationId": "{missing_sub_id}"}}')
        self.db.step(GENERIC, metadata=md_sub, payload_field=140, payload=f(2, res_sub), status=3)

        # Failed manage_subagents kill (status=4) and scalar ConversationIds
        self.db.step(GENERIC, metadata=f(4, m(f(2, "manage_subagents"), f(3, json.dumps({"Action": "kill", "ConversationIds": missing_sub_id})))), status=4)

        r = self.report()
        self.assertEqual(len(r["background_tasks"]), 1)
        self.assertEqual(r["background_tasks"][0]["status"], "unknown")

    def test_inactive_checkpoint_not_promoted_and_reserved_path_wal(self):
        # Reserved URI chars (#, %, space) valid on both POSIX and Windows + active WAL writer
        special_path = self.home / "conv #1 %20 test.db"
        wal_db = ConversationDB(special_path)
        self.addCleanup(wal_db.close)
        wal_db.conn.execute("PRAGMA journal_mode=WAL")
        wal_db.step(USER_INPUT, payload_field=P_USER, payload=user_input("hi"))
        wal_db.step(CHECKPOINT, payload_field=P_CHECKPOINT, payload=m(f(1, 0), f(11, 0), f(12, 1), f(4, "old")), status=3)
        wal_db.step(CHECKPOINT, payload_field=P_CHECKPOINT, payload=m(f(1, 1), f(11, 1), f(12, 2), f(4, "pending")), status=2)
        wal_db.step(PLANNER_RESPONSE, metadata=f(9, usage(100, 10)), payload_field=P_PLANNER, payload=planner("ok"))
        # Generation explicitly omits field 2 (no active checkpoint)
        cwm = m(f(1, 0), f(3, f(1, group(7, 10, ("0: USER_INPUT", 10)))), f(4, 1_000_000))
        wal_db.conn.execute("INSERT INTO gen_metadata (idx, data) VALUES (0, ?)", (m(f(1, m(f(9, f(10, cwm))))),))
        wal_db.conn.commit()

        old_db_path = self.db_path
        self.db_path = special_path
        try:
            r = self.report()
        finally:
            self.db_path = old_db_path
        self.assertEqual(r["active_checkpoint"], -1)
        self.assertIsNone(r["checkpoint_view"])
        self.assertEqual(len(r["checkpoints"]), 1)

    def test_latest_stats_alignment_and_old_schema_tolerated(self):
        self.basic_steps()
        # Gen 1 completed with output; Gen 2 has input/cache only; Gen 3 output-only
        self.db.generation([group(7, 1000, ("0: USER_INPUT", 1000))], usage=usage(input_tokens=100, output=400, cache_read=900))
        self.db.generation([group(7, 1000, ("0: USER_INPUT", 1000))], usage=usage(input_tokens=0, output=120, cache_read=0))
        r = self.report()
        self.assertEqual(r["cache_stats"]["generations"], 1)
        self.assertIsNone(r["cache_stats"]["last_hit_rate"])
        self.assertEqual(r["cache_stats"]["last_input_tokens"], 0)
        self.assertEqual(r["output_stats"]["last_output_tokens"], 120)
        self.assertEqual(r["output_stats"]["session_output_tokens"], 520)

        # Older DB missing task_details / error_details and gen_metadata missing idx
        legacy_path = self.home / "legacy.db"
        conn = sqlite3.connect(legacy_path)
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer, status integer, metadata blob, step_payload blob)")
        conn.execute("INSERT INTO steps VALUES (0, 14, 3, X'', ?)", (m(f(1, 14), f(4, 3), f(19, user_input("hello!!!"))),))
        conn.execute("CREATE TABLE gen_metadata (data blob)")
        cwm = m(f(1, 0), f(3, f(1, group(7, 10, ("0: USER_INPUT", 10)))), f(4, 250_000))
        conn.execute("INSERT INTO gen_metadata VALUES (?)", (m(f(1, m(f(9, f(10, cwm))))),))
        conn.commit()
        old_db_path = self.db_path
        self.db_path = legacy_path
        try:
            legacy_r = self.report()
        finally:
            self.db_path = old_db_path
        self.assertEqual(legacy_r["used_tokens"], 10)
        self.assertEqual(legacy_r["limit_tokens"], 250_000)

    def test_round2_hardening_edges(self):
        # 1. Unpaired UTF-16 surrogate in view_file JSON path + stale nonnegative gen_ck + exact 3-token scaling
        bad_json = '{"AbsolutePath": "\\ud800/tmp/SKILL.md"}'
        self.db.step(GENERIC, metadata=f(4, m(f(2, "view_file"), f(3, bad_json))), payload_field=140, payload=f(2, b""))
        self.db.step(USER_INPUT, payload_field=P_USER, payload=user_input("first prompt!!"))
        self.db.step(CHECKPOINT, payload_field=P_CHECKPOINT, payload=m(f(1, 1), f(11, 0), f(12, 1), f(4, "cp1")), status=3)
        self.db.step(PLANNER_RESPONSE, metadata=f(9, usage(10, 5)), payload_field=P_PLANNER, payload=planner("reply!!"))
        self.db.generation([group(7, 3, ("0: UNKNOWN_ITEM", 3))], usage=usage(input_tokens=100, output=10), checkpoint_index=0, model="gemini-2.5-flash", display_name="")

        r_single = self.report()
        self.assertFalse(r_single["compacted"])
        cat_sum = sum(c["tokens"] for c in r_single["categories"] if c["key"] in ("user", "agent", "tool", "system_messages"))
        self.assertEqual(cat_sum, 3)

        # Re-open DB to verify catalog edge cases without invalidating the single-gen check above
        self.db = ConversationDB.__new__(ConversationDB)
        self.db.conn = sqlite3.connect(self.db_path)
        catalog = self.home / "bad_or_ambig.json"
        catalog.write_text(json.dumps({
            "gemini-2.5-flash": {"maxTokens": 1_048_576, "displayName": "Shared Flash"},
            "gemini-3.5-flash-lite": {"maxTokens": 1_048_576, "displayName": "Shared Flash"},
            "gemini-4-hydrogen-high": {"maxTokens": 10**309, "displayName": 12345},
        }))
        # Historical no-ID row should NOT merge when catalog maps Shared Flash to multiple IDs
        self.db.generation([group(7, 3, ("0: USER_INPUT", 3))], usage=usage(input_tokens=100, output=10), model="", display_name="Shared Flash")
        # Row with empty planner model_id but response_model slug carrying -high effort
        resp_slug_chat = m(f(4, usage(input_tokens=100, output=10)), f(19, "gemini-4-hydrogen-high"), f(21, "Gemini 4 Hydrogen (Low)"))
        self.db.conn.execute("INSERT INTO gen_metadata (idx, data) VALUES ((SELECT COUNT(*) FROM gen_metadata), ?)", (m(f(1, resp_slug_chat)),))
        # Row with numeric displayName and overflowing maxTokens
        self.db.generation([group(7, 3, ("0: USER_INPUT", 3))], usage=usage(input_tokens=100, output=10), model="gemini-4-hydrogen-high", display_name="")

        r = self.report("--model-catalog", str(catalog))
        by_key = {(m["model_id"], m["model"]): m for m in r["model_usage"]}
        self.assertIn(("gemini-2.5-flash", "Shared Flash"), by_key)
        self.assertIn(("", "Shared Flash"), by_key)
        self.assertEqual(by_key[("gemini-4-hydrogen", "Gemini 4 Hydrogen")]["efforts"], {"High": 2})
        self.assertEqual(r["limit_tokens"], 1_048_576)

        # Corrupt catalog file degrades gracefully instead of aborting
        corrupt = self.home / "corrupt.json"
        corrupt.write_text("{not valid json")
        r_fallback = self.report("--model-catalog", str(corrupt))
        self.assertEqual(r_fallback["limit_tokens"], 1_048_576)

    def test_closed_wal_db_without_shm(self):
        # Cleanly closed WAL databases keep WAL header bytes (2, 2) but have no
        # -wal/-shm sidecar files, which causes plain `?mode=ro` to fail.
        self.db.conn.commit()
        self.db.conn.execute("PRAGMA journal_mode=WAL")
        self.db.step(14, payload_field=19, payload=user_input("hello from closed WAL"))
        self.db.generation([group(7, 1234, ("0: USER_INPUT", 1234))])
        self.db.conn.commit()
        self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()

        for suf in ("-wal", "-shm"):
            p = Path(f"{self.db_path}{suf}")
            if p.exists():
                p.unlink()

        with open(self.db_path, "rb") as fh:
            self.assertEqual(fh.read(20)[18:20], b"\x02\x02")
        self.assertFalse(Path(f"{self.db_path}-wal").exists())
        self.assertFalse(Path(f"{self.db_path}-shm").exists())

        rep = self.report()
        self.assertEqual(rep["used_tokens"], 1234)

    def test_wal_db_with_uncheckpointed_wal_and_no_shm(self):
        # Active WAL DB copied without -shm (or in a dir where -shm cannot be
        # created): must preserve uncheckpointed frames in the -wal file.
        self.db.conn.execute("PRAGMA journal_mode=WAL")
        self.db.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.db.step(14, payload_field=19, payload=user_input("initial"))
        self.db.generation([group(7, 500, ("0: USER_INPUT", 500))])
        self.db.conn.commit()
        self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Write a second generation strictly into -wal without checkpointing.
        self.db.step(14, payload_field=19, payload=user_input("in wal only"))
        self.db.generation([group(7, 4321, ("0: USER_INPUT", 4321))])
        self.db.conn.commit()

        wal_only_dir = Path(self.tmp.name) / "wal_only"
        wal_only_dir.mkdir()
        copied_db = wal_only_dir / self.db_path.name
        shutil.copyfile(self.db_path, copied_db)
        shutil.copyfile(Path(f"{self.db_path}-wal"), Path(f"{copied_db}-wal"))
        self.db.close()

        self.assertTrue(Path(f"{copied_db}-wal").stat().st_size > 0)
        self.assertFalse(Path(f"{copied_db}-shm").exists())

        steps, gen, _, _, _ = context_report.load_db(copied_db)
        self.assertEqual(len(steps), 2)
        self.assertIsNotNone(gen)
        st = context_report.collect_stats(steps, gen, 1_000_000)
        self.assertEqual(st["total"], 4321)


class FormatTest(unittest.TestCase):
    def test_model_label(self):
        cases = {
            "gemini-4-argon-medium": "Gemini 4 Argon (Medium)",
            "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
            "claude-opus-5-5-high": "Claude Opus 5.5 (High)",
            "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
        }
        for slug, label in cases.items():
            self.assertEqual(context_report.model_label(slug), label)

    def test_uri_to_path_and_varint_bounds(self):
        self.assertEqual(context_report.uri_to_path("file:///Users/a/b%20c.txt"), "/Users/a/b c.txt")
        self.assertEqual(context_report.uri_to_path("file://localhost/Users/a/b.txt"), "/Users/a/b.txt")
        self.assertEqual(context_report.uri_to_path("file:///C:/Users/a/b.txt"), "C:/Users/a/b.txt")
        self.assertIn("server/share/dir", context_report.uri_to_path("file://server/share/dir").replace("\\", "/"))
        unc_uri = context_report.sqlite_ro_uri(Path("//server/share/dir/conv #1.db"))
        self.assertTrue(unc_uri.startswith("file:////server/share/"))
        self.assertIn("%231", unc_uri)

        # 10th byte > 1 overflows uint64 and must raise ValueError cleanly inside Msg
        overflow_varint = b"\x80" * 9 + b"\x02"
        with self.assertRaises(ValueError):
            context_report.read_varint(overflow_varint, 0)
        self.assertEqual(context_report.Msg(b"\x08" + overflow_varint).int(1), 0)

    def test_native_windows(self):
        for slug, tokens in {"claude-sonnet-5-5-low": 1_000_000, "claude-opus-4-6-thinking": 250_000,
                             "gpt-oss-120b-medium": 131_072, "gemini-4-argon-high": 1_048_576}.items():
            gen = context_report.Msg(f(3, f(28, slug)))
            self.assertEqual(context_report.context_limit(gen), tokens)

    def test_fmt_tok(self):
        self.assertEqual(context_report.fmt_tok(999), "999")
        self.assertEqual(context_report.fmt_tok(1500), "1.5k")
        self.assertEqual(context_report.fmt_tok(1_000_000), "1M")
        self.assertEqual(context_report.fmt_tok(1_250_000), "1.2M")
        self.assertEqual(context_report.fmt_usd(None), "-")
        self.assertEqual(context_report.fmt_usd(0.0), "$0.00")
        self.assertEqual(context_report.fmt_usd(0.00424), "$0.0042")
        self.assertEqual(context_report.fmt_usd(0.0456), "$0.046")
        self.assertEqual(context_report.fmt_usd(3.846), "$3.85")


class CostEstimateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _run_json(self, db_path: Path, *args: str) -> dict:
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), PYTHONIOENCODING="utf-8")
        res = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db_path), "--json", *args],
            capture_output=True, env=env, text=True, encoding="utf-8", check=True,
        )
        return json.loads(res.stdout)

    def _subagent_step(self, db: ConversationDB, cid: str, ts: int | None = None):
        md_parts = [f(4, m(f(2, "invoke_subagent"), f(3, json.dumps({"Role": "Worker", "TypeName": "self", "Prompt": "work"}))))]
        if ts is not None:
            md_parts.insert(0, f(1, f(1, ts)))
        res = f(1, json.dumps({"conversationId": cid}))
        db.step(GENERIC, metadata=m(*md_parts), payload_field=140, payload=f(2, res), status=3)

    def test_session_api_cost_and_recursive_subagent_rollup(self):
        main_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        grandchild_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        missing_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"

        main_db_path = self.home / f"{main_id}.db"
        child_db_path = self.home / f"{child_id}.db"
        grand_db_path = self.home / f"{grandchild_id}.db"

        # Grandchild: 1M output on Claude Opus 5.5 ($20.00) + circular reference back to main_id
        grand = ConversationDB(grand_db_path)
        self._subagent_step(grand, main_id)
        grand.generation(
            [group(7, 100, ("0: USER_INPUT", 100))],
            usage=usage(input_tokens=0, output=1_000_000, cache_read=0),
            model="claude-opus-5-5-high",
            display_name="",
        )
        grand.close()

        # Child: Gemini 4 Neon (1M new in = $3.00) + spawns grandchild_id
        child = ConversationDB(child_db_path)
        self._subagent_step(child, grandchild_id)
        child.generation(
            [group(7, 100, ("0: USER_INPUT", 100))],
            usage=usage(input_tokens=1_000_000, output=0, cache_read=0),
            model="gemini-4-neon-medium",
            display_name="",
        )
        child.close()

        # Main:
        # 1) Gemini 4 Hydrogen: 1M in ($1.50) + 2M cache ($0.15) + 1M out ($7.50) = $9.15, cache saved = $2.85
        # 2) Orchestrator (Carbon) via legacy response slug gemini-pro-orchestrator before Dec 1: 500k in ($1.00) + 100k out ($1.00) = $2.00
        # 3) Gemini 4 Hydrogen (Orchestrator) via gemini-flash-worker-agy: 1M in ($1.50)
        main = ConversationDB(main_db_path)
        self._subagent_step(main, child_id)
        self._subagent_step(main, missing_id)
        main.generation(
            [group(7, 100, ("0: USER_INPUT", 100))],
            usage=usage(input_tokens=1_000_000, output=1_000_000, cache_read=2_000_000, thinking=400_000),
            model="gemini-4-hydrogen-xhigh",
            display_name="",
        )
        orch_chat = m(f(4, usage(input_tokens=500_000, output=100_000)), f(19, "gemini-pro-orchestrator"), f(21, "Orchestrator (Carbon)"))
        main.conn.execute("INSERT INTO gen_metadata (idx, data) VALUES ((SELECT COUNT(*) FROM gen_metadata), ?)", (m(f(1, orch_chat)),))
        worker_chat = m(f(4, usage(input_tokens=1_000_000, output=0)), f(19, "gemini-flash-worker-agy"), f(21, "Gemini 4 Hydrogen (Orchestrator)"))
        main.conn.execute("INSERT INTO gen_metadata (idx, data) VALUES ((SELECT COUNT(*) FROM gen_metadata), ?)", (m(f(1, worker_chat)),))
        main.close()

        r = self._run_json(main_db_path, "--price-date", "2026-11-24")
        cs = r["cost_stats"]
        self.assertTrue(cs["estimated"])
        self.assertEqual(cs["currency"], "USD")
        self.assertAlmostEqual(cs["main_usd"], 12.65, places=6)
        self.assertAlmostEqual(cs["subagent_usd"], 23.00, places=6)
        self.assertAlmostEqual(cs["total_usd"], 35.65, places=6)
        self.assertAlmostEqual(cs["cache_saved_usd"], 2.85, places=6)
        self.assertEqual(cs["total_calls"], 5)
        self.assertEqual(cs["priced_calls"], 5)
        self.assertEqual(cs["unpriced_calls"], 0)
        self.assertEqual(cs["subagent_breakdown"]["count"], 2)
        self.assertEqual(set(cs["subagent_conversation_ids"]), {child_id, grandchild_id})

        by_model = {m_row["model"]: m_row for m_row in r["model_usage"]}
        self.assertAlmostEqual(by_model["Gemini 4 Hydrogen"]["estimated_cost_usd"], 9.15, places=6)
        self.assertAlmostEqual(by_model["Orchestrator (Carbon)"]["estimated_cost_usd"], 2.0, places=6)
        self.assertAlmostEqual(by_model["Gemini 4 Hydrogen (Orchestrator)"]["estimated_cost_usd"], 1.5, places=6)

        cost_by_model = {m_row["model"]: m_row for m_row in cs["by_model"]}
        self.assertAlmostEqual(cost_by_model["Claude Opus 5.5"]["estimated_cost_usd"], 20.0, places=6)
        self.assertAlmostEqual(cost_by_model["Gemini 4 Hydrogen"]["estimated_cost_usd"], 9.15, places=6)
        self.assertAlmostEqual(cost_by_model["Gemini 4 Neon"]["estimated_cost_usd"], 3.0, places=6)
        self.assertAlmostEqual(cost_by_model["Orchestrator (Carbon)"]["estimated_cost_usd"], 2.0, places=6)
        self.assertAlmostEqual(cost_by_model["Gemini 4 Hydrogen (Orchestrator)"]["estimated_cost_usd"], 1.5, places=6)
        self.assertAlmostEqual(
            sum(m_row["estimated_cost_usd"] for m_row in cs["by_model"] if m_row["estimated_cost_usd"] is not None),
            cs["total_usd"],
            places=6,
        )
        sub_by_model = {m_row["model"]: m_row for m_row in cs["subagent_by_model"]}
        self.assertEqual(set(sub_by_model.keys()), {"Claude Opus 5.5", "Gemini 4 Neon"})
        self.assertAlmostEqual(sub_by_model["Claude Opus 5.5"]["estimated_cost_usd"], 20.0, places=6)
        self.assertAlmostEqual(sub_by_model["Gemini 4 Neon"]["estimated_cost_usd"], 3.0, places=6)
        self.assertAlmostEqual(
            sum(m_row["estimated_cost_usd"] for m_row in cs["subagent_by_model"] if m_row["estimated_cost_usd"] is not None),
            cs["subagent_usd"],
            places=6,
        )
        self.assertIn("Estimated API cost · $35.65", r["text"])
        self.assertIn("Scope: main $12.65 (3 calls) · subagents (2) $23.00 (2 calls)", r["text"])

    def test_pricing_schedules_and_prompt_tiers(self):
        # Verify Dec 1 / Jan 1 rate transitions via per-step timestamps and prompt length thresholds
        db_path = self.home / "tiered.db"
        db = ConversationDB(db_path)

        nov30 = 1796040000  # 2026-11-30 UTC (~12:00 UTC)
        dec02 = 1796212800  # 2026-12-02 UTC (~12:00 UTC)

        # Step 0 (Nov 30) -> Carbon @ $2/$10: 1M in ($2) + 1M out ($10) = $12
        db.step(USER_INPUT, metadata=f(1, f(1, nov30)), payload_field=P_USER, payload=user_input("nov"))
        c_nov = m(
            f(4, usage(input_tokens=1_000_000, output=1_000_000)),
            f(20, m(f(1, "last_step_index"), f(2, "0"))),
            f(21, "Gemini 4 Carbon (High)"),
        )
        db.conn.execute(
            "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
            (0, m(f(1, c_nov), f(3, f(28, "gemini-4-carbon-high")))),
        )

        # Step 1 (Dec 2) -> Carbon @ $4/$20: 1M in ($4) + 1M out ($20) = $24
        db.step(USER_INPUT, metadata=f(1, f(1, dec02)), payload_field=P_USER, payload=user_input("dec"))
        c_dec = m(
            f(4, usage(input_tokens=1_000_000, output=1_000_000)),
            f(20, m(f(1, "last_step_index"), f(2, "1"))),
            f(21, "Gemini 4 Carbon (High)"),
        )
        db.conn.execute(
            "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
            (1, m(f(1, c_dec), f(3, f(28, "gemini-4-carbon-high")))),
        )

        # Gemini 3.1 Pro prompt <= 200k vs > 200k
        # 200k in = $0.40, 1M out <=200k = $12 -> $12.40
        db.generation([group(7, 100, ("0: USER_INPUT", 100))], usage=usage(input_tokens=200_000, output=1_000_000), model="gemini-3.1-pro-preview", display_name="")
        # 10k in + 200k cache (> 200k total prompt): 10k@$4 ($0.04) + 200k@$0.40 ($0.08) + 1M out@$18 ($18.00) = $18.12
        db.generation([group(7, 100, ("0: USER_INPUT", 100))], usage=usage(input_tokens=10_000, cache_read=200_000, output=1_000_000), model="gemini-3.1-pro-preview", display_name="")

        # Claude Haiku 5.5 prompt <= 100k vs > 100k
        # 100k in@$0.10 ($0.01) + 1M out@$0.50 ($0.50) = $0.51
        db.generation([group(7, 100, ("0: USER_INPUT", 100))], usage=usage(input_tokens=100_000, output=1_000_000), model="claude-haiku-5-5", display_name="")
        # 200k in@$0.50 ($0.10) + 1M out@$2.50 ($2.50) = $2.60
        db.generation([group(7, 100, ("0: USER_INPUT", 100))], usage=usage(input_tokens=200_000, output=1_000_000), model="claude-haiku-5-5", display_name="")
        db.close()

        r = self._run_json(db_path)
        by_id = {m_row["model_id"]: m_row for m_row in r["model_usage"]}
        self.assertAlmostEqual(by_id["gemini-4-carbon"]["estimated_cost_usd"], 36.0, places=6)
        self.assertAlmostEqual(by_id["gemini-3.1-pro-preview"]["estimated_cost_usd"], 30.52, places=6)
        self.assertAlmostEqual(by_id["claude-haiku-5-5"]["estimated_cost_usd"], 3.11, places=6)

        # Gemini 3.8 Flash explicit date transition: 2026-12-31 ($0.75/$3.75) vs 2027-01-01 ($1.50/$7.50)
        flash_db_path = self.home / "flash38.db"
        fdb = ConversationDB(flash_db_path)
        fdb.generation([group(7, 100, ("0: USER_INPUT", 100))], usage=usage(input_tokens=1_000_000, output=1_000_000), model="gemini-3.8-flash-medium", display_name="")
        fdb.close()
        r_2026 = self._run_json(flash_db_path, "--price-date", "2026-12-31")
        r_2027 = self._run_json(flash_db_path, "--price-date", "2027-01-01")
        self.assertAlmostEqual(r_2026["cost_stats"]["total_usd"], 4.50, places=6)
        self.assertAlmostEqual(r_2027["cost_stats"]["total_usd"], 9.00, places=6)

    def test_unpriced_models_handling(self):
        db_only = self.home / "unpriced_only.db"
        u1 = ConversationDB(db_only)
        u1.generation([group(7, 100, ("0: USER_INPUT", 100))], usage=usage(input_tokens=500_000, output=100_000), model="gpt-oss-120b-medium", display_name="")
        u1.close()

        r1 = self._run_json(db_only, "--price-date", "2026-11-24")
        self.assertIsNone(r1["cost_stats"]["total_usd"])
        self.assertEqual(r1["cost_stats"]["priced_calls"], 0)
        self.assertEqual(r1["cost_stats"]["unpriced_calls"], 1)
        self.assertEqual(r1["cost_stats"]["unpriced_models"], ["GPT-OSS 120B"])
        self.assertIsNone(r1["model_usage"][0]["estimated_cost_usd"])
        self.assertIn("Unpriced calls: 1 (GPT-OSS 120B)", r1["text"])

        db_mix = self.home / "mixed.db"
        u2 = ConversationDB(db_mix)
        u2.generation([group(7, 100, ("0: USER_INPUT", 100))], usage=usage(input_tokens=1_000_000, output=0), model="gemini-4-hydrogen-high", display_name="")
        u2.generation([group(7, 100, ("0: USER_INPUT", 100))], usage=usage(input_tokens=500_000, output=0), model="gpt-oss-120b-medium", display_name="")
        u2.close()

        r2 = self._run_json(db_mix, "--price-date", "2026-11-24")
        self.assertAlmostEqual(r2["cost_stats"]["total_usd"], 1.50, places=6)
        self.assertEqual(r2["cost_stats"]["priced_calls"], 1)
        self.assertEqual(r2["cost_stats"]["unpriced_calls"], 1)

        bad_res = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db_mix), "--price-date", "not-a-date"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertNotEqual(bad_res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
