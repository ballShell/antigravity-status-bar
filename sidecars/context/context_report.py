#!/usr/bin/env python3
"""Context window usage report for Antigravity 2.0 conversations.

A port of the agy CLI `/context` command (cli/model.ContextModel in agy 1.4.4).
The CLI reads the live trajectory from memory; this script reads the same
protobufs from the conversation database (`steps` and `gen_metadata` tables).
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import quote, unquote, urlparse

# Ensure Conda/Anaconda/Miniconda on Windows 10/11 can locate sqlite3.dll in <prefix>\Library\bin
# even when invoked outside an activated Conda shell.
if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    for _py_prefix in {sys.prefix, getattr(sys, "base_prefix", sys.prefix)}:
        _conda_bin = Path(_py_prefix) / "Library" / "bin"
        if _conda_bin.is_dir():
            try:
                os.add_dll_directory(str(_conda_bin))
            except OSError:
                pass

import sqlite3

# --- protobuf ----------------------------------------------------------------


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        if shift == 63 and b > 1:
            raise ValueError("Varint overflow")
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    raise ValueError("Truncated varint")


class Msg:
    """Lazily decoded protobuf message: field number -> list of raw values."""

    def __init__(self, buf: bytes | None):
        self.fields: dict[int, list] = {}
        pos, n = 0, len(buf or b"")
        while pos < n:
            try:
                tag, pos = read_varint(buf, pos)
                fn, wt = tag >> 3, tag & 7
                if fn <= 0:
                    break
                if wt == 0:
                    val, pos = read_varint(buf, pos)
                elif wt == 2:
                    ln, pos = read_varint(buf, pos)
                    if ln < 0 or pos + ln > n:
                        break
                    val = buf[pos : pos + ln]
                    pos += ln
                elif wt == 1:
                    if pos + 8 > n:
                        break
                    val, pos = buf[pos : pos + 8], pos + 8
                elif wt == 5:
                    if pos + 4 > n:
                        break
                    val, pos = buf[pos : pos + 4], pos + 4
                else:
                    break
            except ValueError:
                break
            self.fields.setdefault(fn, []).append(val)

    def has(self, fn: int) -> bool:
        return fn in self.fields

    def int(self, fn: int, signed32: bool = False) -> int:
        v = self.fields.get(fn, [0])[-1]
        v = v if isinstance(v, int) else 0
        if signed32 and v >= 1 << 63:
            v -= 1 << 64
        return v

    def bytes(self, fn: int) -> bytes:
        v = self.fields.get(fn, [b""])[-1]
        return v if isinstance(v, bytes) else b""

    def str(self, fn: int) -> str:
        return self.bytes(fn).decode("utf-8", errors="replace")

    def msg(self, fn: int) -> Msg | None:
        vals = [v for v in self.fields.get(fn, []) if isinstance(v, bytes)]
        if not vals:
            return None
        return Msg(vals[0] if len(vals) == 1 else b"".join(vals))

    def msgs(self, fn: int) -> list[Msg]:
        return [Msg(v) for v in self.fields.get(fn, []) if isinstance(v, bytes)]


# CortexStep oneof payload field numbers.
STEP_VIEW_FILE = 14
STEP_USER_INPUT = 19
STEP_PLANNER_RESPONSE = 20
STEP_CHECKPOINT = 30
STEP_CODE_ACTION = 10

STATUS_DONE = 3

# TokenType values of TopLevelTokenGroup.type.
TOKEN_SYSTEM_PROMPT, TOKEN_TOOLS, TOKEN_RULES, TOKEN_MCP_TOOLS, TOKEN_SKILLS = 1, 2, 3, 4, 5
TOKEN_SUBAGENTS, TOKEN_CHAT_MESSAGES, TOKEN_WORKFLOWS, TOKEN_PLUGINS, TOKEN_FLOWS = 6, 7, 8, 9, 10

# CortexStepType names (exa.cortex_pb), used to parse "N: TYPE" chat item names.
STEP_TYPES = {
    "UNSPECIFIED": 0, "DUMMY": 1, "FINISH": 2, "PLAN_INPUT": 3, "MQUERY": 4, "CODE_ACTION": 5,
    "GIT_COMMIT": 6, "GREP_SEARCH": 7, "VIEW_FILE": 8, "LIST_DIRECTORY": 9, "COMPILE": 10,
    "VIEW_CODE_ITEM": 13, "USER_INPUT": 14, "PLANNER_RESPONSE": 15, "ERROR_MESSAGE": 17,
    "RUN_COMMAND": 21, "CHECKPOINT": 23, "PROPOSE_CODE": 24, "FIND": 25, "SUGGESTED_RESPONSES": 27,
    "COMMAND_STATUS": 28, "MEMORY": 29, "READ_URL_CONTENT": 31, "VIEW_CONTENT_CHUNK": 32,
    "SEARCH_WEB": 33, "RETRIEVE_MEMORY": 34, "MCP_TOOL": 38, "MANAGER_FEEDBACK": 39,
    "TOOL_CALL_PROPOSAL": 40, "TOOL_CALL_CHOICE": 41, "TRAJECTORY_CHOICE": 42, "CLIPBOARD": 45,
    "VIEW_FILE_OUTLINE": 47, "POST_PR_REVIEW": 49, "LIST_RESOURCES": 51, "READ_RESOURCE": 52,
    "LINT_DIFF": 53, "FIND_ALL_REFERENCES": 54, "BRAIN_UPDATE": 55, "OPEN_BROWSER_URL": 56,
    "RUN_EXTENSION_CODE": 57, "PROPOSAL_FEEDBACK": 59, "TRAJECTORY_SEARCH": 60,
    "EXECUTE_BROWSER_JAVASCRIPT": 61, "LIST_BROWSER_PAGES": 62, "CAPTURE_BROWSER_SCREENSHOT": 63,
    "CLICK_BROWSER_PIXEL": 64, "READ_TERMINAL": 65, "CAPTURE_BROWSER_CONSOLE_LOGS": 66,
    "READ_BROWSER_PAGE": 67, "BROWSER_GET_DOM": 68, "CODE_SEARCH": 73, "BROWSER_INPUT": 74,
    "BROWSER_MOVE_MOUSE": 75, "BROWSER_SELECT_OPTION": 76, "BROWSER_SCROLL_UP": 77,
    "BROWSER_SCROLL_DOWN": 78, "BROWSER_CLICK_ELEMENT": 79, "BROWSER_PRESS_KEY": 80,
    "TASK_BOUNDARY": 81, "NOTIFY_USER": 82, "CODE_ACKNOWLEDGEMENT": 83, "INTERNAL_SEARCH": 84,
    "BROWSER_SUBAGENT": 85, "FILE_CHANGE": 86, "MOVE": 87, "BROWSER_SCROLL": 88,
    "KNOWLEDGE_GENERATION": 89, "EPHEMERAL_MESSAGE": 90, "GENERATE_IMAGE": 91,
    "DELETE_DIRECTORY": 92, "COMPILE_APPLET": 93, "INSTALL_APPLET_DEPENDENCIES": 94,
    "INSTALL_APPLET_PACKAGE": 95, "BROWSER_RESIZE_WINDOW": 96, "BROWSER_DRAG_PIXEL_TO_PIXEL": 97,
    "CONVERSATION_HISTORY": 98, "KNOWLEDGE_ARTIFACTS": 99, "SEND_COMMAND_INPUT": 100,
    "SYSTEM_MESSAGE": 101, "WAIT": 102, "AGENCY_TOOL_CALL": 103, "CIDER_AGENT_DUMMY": 104,
    "BUILD_CLEANER": 105, "BLAZE_BUILD_TARGETS": 106, "BLAZE_TEST_TARGETS": 107,
    "SET_UP_FIREBASE": 108, "MOMA": 109, "RESTART_DEV_SERVER": 110, "DEPLOY_FIREBASE": 111,
    "SHELL_EXEC": 112, "BROWSER_MOUSE_WHEEL": 113, "LINT_APPLET": 114, "KI_INSERTION": 116,
    "RETRIEVE_CONTENT": 117, "CRITIQUE": 118, "FINDINGS": 119, "BROWSER_MOUSE_UP": 120,
    "BROWSER_MOUSE_DOWN": 121, "WORKSPACE_API": 122, "BROWSER_LIST_NETWORK_REQUESTS": 123,
    "BROWSER_GET_NETWORK_REQUEST": 124, "BROWSER_REFRESH_PAGE": 125, "EDIT_NOTEBOOK": 126,
    "INVOKE_SUBAGENT": 127, "WRITE_BLOB": 128, "READ_NOTEBOOK": 129, "PROPOSE_AI_COMMENTS": 130,
    "START_CODE_REVIEW": 131, "GENERIC": 132, "SET_UP_CLOUD_SQL": 133, "EXECUTE_NOTEBOOK": 134,
    "CLOUD_SQL_UPDATE_SCHEMA": 135, "RPC_ACTION": 136, "CLOUD_SQL_EXECUTE_SQL": 137,
    "ASK_QUESTION": 138, "DIRECTORY_RULES": 139, "TOOL_SEARCH": 140,
}

CAT_USER, CAT_AGENT, CAT_TOOL, CAT_CHECKPOINT, CAT_SYSTEM = 0, 1, 2, 3, 4

# --- formatting --------------------------------------------------------------


def fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        v = n / 1_000_000.0
        return f"{v:.0f}M" if v.is_integer() else f"{v:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000.0:.1f}k"
    return str(n)


def fmt_ctx_limit(n: int) -> str:
    return fmt_tok(n)


def fmt_usd(amount: float | None) -> str:
    if amount is None:
        return "-"
    if amount <= 0:
        return "$0.00"
    if amount < 0.01:
        return f"${amount:.4f}"
    if amount < 0.10:
        return f"${amount:.3f}"
    return f"${amount:.2f}"


def go_div4(n: int) -> int:
    """Go's int division by 4 (truncates toward zero)."""
    return int(n / 4)


def _strip_win_extended_prefix(raw: str) -> str:
    """Strip Win32 extended-length path prefixes for drive and UNC paths only."""
    if not raw:
        return raw
    if raw.startswith("\\\\?\\UNC\\") or raw.startswith("//?/UNC/"):
        sep = "\\" if raw.startswith("\\") else "/"
        return sep * 2 + raw[8:]
    if (raw.startswith("\\\\?\\") or raw.startswith("//?/")) and len(raw) >= 6 and raw[4].isalpha() and raw[5] == ":":
        return raw[4:]
    return raw


def sqlite_ro_uri(path: Path, immutable: bool = False) -> str:
    """Build a percent-encoded SQLite read-only URI safe for #, ?, %, spaces, drive letters, and UNC shares."""
    raw = os.fspath(path)
    cleaned = _strip_win_extended_prefix(raw) if os.name == "nt" else raw
    abs_p = Path(os.path.abspath(cleaned))
    posix_p = (_strip_win_extended_prefix(abs_p.as_posix()) if os.name == "nt" else abs_p.as_posix())
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    if posix_p.startswith("//"):
        return "file:////" + quote(posix_p.lstrip("/"), safe="/:") + suffix
    return abs_p.as_uri() + suffix


def uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a local filesystem path."""
    if not uri or not uri.startswith("file:"):
        return _strip_win_extended_prefix(uri)
    parsed = urlparse(uri)
    netloc = unquote(parsed.netloc)
    p = unquote(parsed.path)
    # Handle non-standard two-slash Windows drive URIs like file://C:/Users/...
    if len(netloc) == 2 and netloc[1] == ":" and netloc[0].isalpha():
        return f"{netloc}{p}"
    if netloc and netloc.lower() != "localhost":
        if os.name == "nt":
            return "\\\\" + netloc + p.replace("/", "\\")
        return f"//{netloc}{p}"
    if len(p) > 2 and p[0] == "/" and p[2] == ":" and p[1].isalpha():
        p = p[1:]
    return _strip_win_extended_prefix(p)


def shorten_path(uri: str) -> str:
    p = _strip_win_extended_prefix(uri_to_path(uri))
    home = _strip_win_extended_prefix(str(Path.home()))
    p_norm = p.replace("\\", "/")
    home_norm = home.replace("\\", "/").rstrip("/")
    if home_norm and p_norm == home_norm:
        return "~"
    if home_norm and p_norm.startswith(home_norm + "/"):
        return "~" + p_norm[len(home_norm) :]
    if os.name == "nt" and home_norm and p_norm.lower().startswith(home_norm.lower() + "/"):
        return "~" + p_norm[len(home_norm) :]
    return p_norm


# --- stats (model.ContextModel.collectStats) ---------------------------------


def step_category(step_type: int) -> int:
    if step_type == 14:
        return CAT_USER
    if step_type == 15:
        return CAT_AGENT
    if step_type == 23:
        return CAT_CHECKPOINT
    if step_type in (0, 2, 101):
        return CAT_SYSTEM
    return CAT_TOOL


STEP_GENERIC = 140
GENERIC_TYPE = 132
ORIGINAL_STEP_TYPE_URL = "/gemini_coder.Step"


def packed_original_step(generic: Msg) -> bytes | None:
    """The typed step a GENERIC step wraps (rehydration.originalStepAny)."""
    result = generic.msg(2)
    if result is None:
        return None
    payload = result.msg(6)
    if payload is not None and payload.str(1).endswith(ORIGINAL_STEP_TYPE_URL):
        return payload.bytes(2)
    for entry in result.msgs(5):
        if entry.str(1) == "__original_cortex_step":
            value = entry.msg(2)
            if value is not None and value.str(1).endswith(ORIGINAL_STEP_TYPE_URL):
                return value.bytes(2)
    return None


def is_skill_file(path: str) -> bool:
    return path.replace("\\", "/").endswith("/SKILL.md")


class Step:
    def __init__(
        self,
        pos: int,
        payload: bytes,
        step_type: int,
        status: int,
        metadata: bytes | None,
        idx: int | None = None,
        task_details: bytes | None = None,
        error_details: bytes | None = None,
    ):
        self.pos = pos
        self.idx = pos if idx is None else idx
        self.pb = Msg(payload)
        self.type = self.pb.int(1) if self.pb.has(1) else step_type
        self.status = self.pb.int(4) if self.pb.has(4) else status
        self.md = self.pb.msg(5) or Msg(metadata)
        self.task_details = Msg(task_details) if task_details else None
        self.error_details = error_details
        self.view_file_override: Msg | None = None
        self.generic_pb: Msg | None = None
        if self.type == GENERIC_TYPE:
            self.rehydrate()

    def rehydrate(self):
        """Turn a GENERIC tool step back into its typed step, like the CLI's rehydration.RehydrateStep."""
        generic = self.pb.msg(STEP_GENERIC)
        if generic is None:
            return
        self.generic_pb = generic
        original = packed_original_step(generic)
        if original is not None:
            self.pb = Msg(original)
            self.type = self.pb.int(1)
            self.md = self.pb.msg(5) or Msg(b"")
            return
        tc = self.md.msg(4)
        if tc is None or tc.str(2) not in ("view_file", "read_file"):
            return
        args = _parse_json_dict(tc.str(3))
        if not args:
            return
        path = next((args[k] for k in ("AbsolutePath", "Path", "FilePath") if isinstance(args.get(k), str) and args[k]), "")
        if path:
            path_bytes = path.encode("utf-8", errors="replace")
            self.type = 8  # VIEW_FILE
            self.view_file_override = Msg(
                b"\x0a" + _varint(len(path_bytes)) + path_bytes + (b"\x88\x01\x01" if is_skill_file(path) else b"")
            )

    def payload(self, fn: int) -> Msg | None:
        if fn == STEP_VIEW_FILE and self.view_file_override is not None:
            return self.view_file_override
        return self.pb.msg(fn)


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def estimate_step_tokens(step: Step) -> int:
    ui = step.payload(STEP_USER_INPUT)
    pr = step.payload(STEP_PLANNER_RESPONSE)
    if ui is not None:
        n = len(ui.bytes(2))  # user_response
    elif pr is not None:
        n = len(pr.bytes(1))  # response
    else:
        tc = step.md.msg(4)  # tool_call
        n = len(tc.bytes(3)) if tc else 0  # arguments_json
        out = step.md.int(23, signed32=True)  # tool_call_output_tokens
        if out > 0:
            return out
    if n == 0:
        return 0
    return max(1, go_div4(n))


def step_tokens(step: Step) -> int:
    usage = step.md.msg(9)  # model_usage
    if usage is not None:
        return usage.int(3)  # output_tokens
    return estimate_step_tokens(step)


def compaction_tokens(step: Step) -> int:
    for ri in reversed(step.md.msgs(28)):  # retry_infos
        usage = ri.msg(2)
        if usage is None:
            continue
        resp = usage.int(10)
        if resp:
            return resp
        out, thinking = usage.int(3), usage.int(9)
        return out - thinking if thinking < out else 0
    return 0


def checkpoint_summary_tokens(step: Step) -> int:
    t = compaction_tokens(step)
    if t:
        return t
    ck = step.payload(STEP_CHECKPOINT) or Msg(b"")
    return go_div4(len(ck.bytes(4)) + len(ck.bytes(5)) + len(ck.bytes(6)))


def checkpoint_newer_than_generation(steps: list[Step], chat_start: Msg | None) -> tuple[int, Step | None]:
    i = len(steps) - 1
    while i >= 0:
        s = steps[i]
        if s.payload(STEP_CHECKPOINT) is not None and s.status == STATUS_DONE:
            break
        i -= 1
    if i < 0:
        return -1, None
    for s in steps[i + 1 :]:
        if s.payload(STEP_PLANNER_RESPONSE) is not None and s.status == STATUS_DONE:
            return -1, None
    has_gen_ck = chat_start is not None and chat_start.has(2)
    gen_ck = chat_start.int(2, signed32=True) if has_gen_ck else -1
    ck_idx = steps[i].payload(STEP_CHECKPOINT).int(1)
    if gen_ck >= ck_idx:
        return -1, None
    return i, steps[i]


def chat_tokens_by_category(items: list[Msg]) -> tuple[bool, list[int]]:
    out = [0, 0, 0, 0]  # user, agent, tool, system
    if not items:
        return False, out
    for it in items:
        left, sep, right = it.str(1).partition(": ")
        if not sep or not re.fullmatch(r"[+-]?[0-9]+", left) or right not in STEP_TYPES:
            return False, [0, 0, 0, 0]
        cat = step_category(STEP_TYPES[right])
        tok = it.int(3, signed32=True)
        if cat == CAT_USER:
            out[0] += tok
        elif cat in (CAT_AGENT, CAT_CHECKPOINT):
            out[1] += tok
        elif cat == CAT_TOOL:
            out[2] += tok
        else:
            out[3] += tok
    return True, out


def detect_system_files(home: str, workspace: str | None, app_dir: str, gemini_dir: str | None = None) -> list[tuple[str, int]]:
    if gemini_dir:
        gemini = Path(gemini_dir).expanduser()
        disp = "~/.gemini"
    elif home:
        gemini = Path(home) / ".gemini"
        disp = "~/" + gemini.name
    else:
        return []
    found = []
    f = gemini / "GEMINI.md"
    if f.exists() and not f.is_dir():
        found.append(disp + "/GEMINI.md")
    for sub in ("skills", "rules", "agents", "global_workflows"):
        if (gemini / sub).is_dir():
            found.append(f"{disp}/{sub}/")
    if app_dir:
        for sub in ("rules", "skills", "agents", "global_workflows"):
            if (gemini / app_dir / sub).is_dir():
                found.append(f"{disp}/{app_dir}/{sub}/")
    if workspace:
        for root in (".agents", "_agents"):
            for sub in ("rules", "skills", "agents", "workflows"):
                if (Path(workspace) / root / sub).is_dir():
                    found.append(f"{root}/{sub}/")
            hooks = Path(workspace) / root / "hooks.json"
            if hooks.exists() and not hooks.is_dir():
                found.append(f"{root}/hooks.json")
    return [(p, 0) for p in found]


def _checkpoint_request_label(ck: Msg) -> str:
    """Pick the most recent meaningful user request inside a checkpoint."""
    reqs = [re.sub(r"\s+", " ", rm.str(1)).strip() for rm in ck.msgs(30)]
    for req in reversed(reqs):
        if req and not req.startswith("/"):
            return req
    for req in reversed(reqs):
        if req and not req.lower().startswith("/compact"):
            return req
    return ""


def _scale_chat_categories(st: dict, target_tokens: int) -> None:
    """Scale user/agent/tool/system_messages proportionally to sum to target_tokens."""
    cats_keys = ("user", "agent", "tool", "system_messages")
    est = sum(st[k] for k in cats_keys)
    if est <= 0:
        st["agent"] = target_tokens
        return
    raw_scaled = [(k, st[k] * target_tokens / est) for k in cats_keys]
    floored = {k: int(v) for k, v in raw_scaled}
    rem = target_tokens - sum(floored.values())
    if rem > 0:
        for k, _ in sorted(raw_scaled, key=lambda kv: kv[1] - int(kv[1]), reverse=True):
            if rem <= 0:
                break
            if st[k] > 0:
                floored[k] += 1
                rem -= 1
    for k in cats_keys:
        st[k] = floored[k]


def collect_stats(steps: list[Step], gen: Msg | None, limit: int) -> dict:
    st = {k: 0 for k in (
        "user", "agent", "tool", "checkpoint", "system_messages", "system_prompt", "system_tools",
        "system_overhead", "rules", "skills", "subagents", "legacy_workflows", "workflows",
        "plugins", "mcp_tools",
    )}
    st["has_breakdown"] = False
    st["estimated_from_gen"] = False
    st["active_checkpoint"] = -1
    checkpoints, knowledge, artifacts, skill_files = [], [], [], []
    seen_k, seen_a, seen_s = set(), set(), set()

    chat = gen.msg(1) if gen is not None else None
    chat_start = chat.msg(9) if chat is not None else None
    cwm = chat_start.msg(10) if chat_start is not None else None
    breakdown = cwm.msg(3) if cwm is not None else None
    newer_pos, newer_step = checkpoint_newer_than_generation(steps, chat_start)
    has_newer = newer_step is not None
    st["compacted"] = has_newer

    # When Undo deletes the latest gen_metadata row, the new latest row is an older
    # generation whose detailed token breakdown (cwm.f3) was already stripped by Antigravity.
    # If that generation sat after a historical checkpoint (active_ck >= 0), restrict step
    # estimates to steps after that checkpoint so pre-compaction history is never double-counted.
    active_ck_id = chat_start.int(2, signed32=True) if (chat_start is not None and chat_start.has(2)) else -1
    active_ck_pos = -1
    active_ck_step: Step | None = None
    if breakdown is None and not has_newer and active_ck_id >= 0:
        for pos, s in enumerate(steps):
            ck = s.payload(STEP_CHECKPOINT)
            if ck is not None and s.status == STATUS_DONE and ck.int(1) == active_ck_id:
                active_ck_pos = pos
                active_ck_step = s

    cutoff_pos = newer_pos if has_newer else active_ck_pos

    for pos, step in enumerate(steps):
        cat = step_category(step.type)
        tok = step_tokens(step)
        counted = 0 if (cutoff_pos >= 0 and cutoff_pos > pos) else tok
        if cat == CAT_USER:
            st["user"] += counted
        elif cat == CAT_AGENT:
            st["agent"] += counted
        elif cat == CAT_TOOL:
            st["tool"] += counted
        elif cat == CAT_CHECKPOINT:
            st["checkpoint"] += tok
            ck = step.payload(STEP_CHECKPOINT)
            if ck is not None and step.status == STATUS_DONE:
                ts_msg = step.md.msg(1)
                created_at = ts_msg.int(1) if ts_msg is not None else 0
                checkpoints.append({
                    "index": ck.int(1),
                    "start": ck.int(11),
                    "end": ck.int(12),
                    "intent": ck.str(4),
                    "request_label": _checkpoint_request_label(ck),
                    "created_at": created_at,
                })
        else:
            st["system_messages"] += counted

        ca = step.payload(STEP_CODE_ACTION)
        result = ca.msg(2) if ca is not None else None
        edit = result.msg(1) if result is not None else None
        if edit is not None:
            uri = edit.str(8)  # absolute_uri
            short = shorten_path(uri)
            if ca.int(21) and uri not in seen_a:  # is_artifact_file
                seen_a.add(uri)
                artifacts.append((short, tok))
            if ca.int(24) and uri not in seen_k:  # is_knowledge_file
                seen_k.add(uri)
                knowledge.append((short, tok))

        vf = step.payload(STEP_VIEW_FILE)
        if vf is not None and vf.int(17):  # is_skill_file
            uri = vf.str(1)
            if uri not in seen_s:
                seen_s.add(uri)
                skill_files.append((shorten_path(uri), tok))

    if breakdown is not None:
        st["has_breakdown"] = True
        seen_prompt = seen_tools = False
        chat_tok, chat_items = 0, []
        for g in breakdown.msgs(1):
            gtype, gtok = g.int(2), g.int(4, signed32=True)
            if gtype == TOKEN_SYSTEM_PROMPT:
                extracted = 0
                for child in g.msgs(5):
                    name, ctok = child.str(1), child.int(3, signed32=True)
                    if name == "flows":
                        st["workflows"] += ctok
                    elif name == "skills":
                        st["skills"] += ctok
                    elif name == "subagents":
                        st["subagents"] += ctok
                    else:
                        continue
                    extracted += ctok
                st["system_prompt"] = gtok - extracted
                seen_prompt = True
            elif gtype == TOKEN_TOOLS:
                st["system_tools"] = gtok
                seen_tools = True
            elif gtype == TOKEN_RULES:
                st["rules"] += gtok
            elif gtype == TOKEN_MCP_TOOLS:
                st["mcp_tools"] += gtok
            elif gtype == TOKEN_SKILLS:
                st["skills"] += gtok
            elif gtype == TOKEN_SUBAGENTS:
                st["subagents"] += gtok
            elif gtype == TOKEN_CHAT_MESSAGES:
                chat_tok, chat_items = gtok, g.msgs(5)
            elif gtype == TOKEN_WORKFLOWS:
                st["legacy_workflows"] += gtok
            elif gtype == TOKEN_PLUGINS:
                st["plugins"] += gtok
            elif gtype == TOKEN_FLOWS:
                st["workflows"] += gtok

        if not seen_prompt and not seen_tools and chat_tok:
            usage = chat.msg(4)
            api_input = usage.int(2) + usage.int(5) if usage is not None else 0
            if chat_tok < api_input:
                st["system_overhead"] = api_input - chat_tok

        if chat_tok and not has_newer:
            ok, cats = chat_tokens_by_category(chat_items)
            if ok:
                st["user"], st["agent"], st["tool"], st["system_messages"] = cats
            else:
                _scale_chat_categories(st, chat_tok)
    elif not has_newer and gen is not None:
        if active_ck_step is not None:
            st["agent"] += checkpoint_summary_tokens(active_ck_step)
        cwm_total = cwm.int(1, signed32=True) if cwm is not None else 0
        usage = chat.msg(4) if chat is not None else None
        api_input = (usage.int(2) + usage.int(5)) if usage is not None else 0
        target_total = cwm_total if cwm_total > 0 else api_input
        if target_total > 0:
            st["estimated_from_gen"] = True
            chat_est = st["user"] + st["agent"] + st["tool"] + st["system_messages"]
            if chat_est < target_total:
                st["system_overhead"] = target_total - chat_est
            elif chat_est > target_total:
                _scale_chat_categories(st, target_total)

    if chat_start is not None and chat_start.has(2):
        st["active_checkpoint"] = chat_start.int(2, signed32=True)
    if has_newer:
        st["agent"] += checkpoint_summary_tokens(newer_step)
        st["active_checkpoint"] = newer_step.payload(STEP_CHECKPOINT).int(1)

    st["total"] = sum(st[k] for k in (
        "user", "agent", "tool", "system_messages", "system_prompt", "system_tools", "system_overhead",
        "rules", "skills", "subagents", "legacy_workflows", "workflows", "plugins", "mcp_tools",
    ))
    st["limit"] = limit
    st["checkpoints"] = checkpoints
    st["knowledge_files"] = knowledge
    st["artifact_files"] = artifacts
    st["skill_files"] = skill_files
    return st


# --- rendering ---------------------------------------------------------------

CATEGORIES = [
    ("◉", "User messages", "user", True),
    ("◉", "Agent responses", "agent", True),
    ("◉", "Tool calls", "tool", True),
    ("⛁", "System prompt", "system_prompt", False),
    ("⛁", "System tools", "system_tools", False),
    ("⛁", "System overhead", "system_overhead", False),
    ("⛁", "Rules", "rules", False),
    ("⛁", "Skills", "skills", False),
    ("⛁", "Subagents", "subagents", False),
    ("⛁", "Legacy workflows", "legacy_workflows", False),
    ("⛁", "Workflows", "workflows", False),
    ("⛁", "Plugins", "plugins", False),
    ("⛁", "MCP tools", "mcp_tools", False),
    ("◉", "System messages", "system_messages", False),
]

GRID_COLORS = ["\x1b[90m", "\x1b[36m", "\x1b[35m", "\x1b[33m", "\x1b[34m", "\x1b[2m"]


def pct(n: int, limit: int) -> float:
    return n / limit * 100.0 if limit else 0.0


def stats_heading(st: dict) -> str:
    if st["compacted"]:
        return "Estimated after compaction · updates next turn"
    if st["has_breakdown"]:
        return "Token usage by category"
    if st.get("estimated_from_gen"):
        return "Estimated from latest generation · updates next turn"
    return "Estimated usage (awaiting generation)"


def render_stats(st: dict, model: str) -> str:
    used, limit = st["total"], st["limit"]
    out = []
    if limit:
        out.append(f"{model} · {fmt_tok(used)}/{fmt_ctx_limit(limit)} tokens\n ({min(100.0, pct(used, limit)):.1f}%)\n")
    else:
        out.append(f"{model} · {fmt_tok(used)}/Unknown tokens\n")
    out.append(stats_heading(st) + "\n")
    for icon, name, key, always in CATEGORIES:
        tok = st[key]
        if always or tok:
            out.append(f"{icon} {name}: {fmt_tok(tok)} tokens ({pct(tok, limit):.1f}%)\n")
    free = limit - used if used < limit else 0
    if limit:
        out.append(f"□ Free space: {fmt_tok(free)} ({pct(free, limit):.1f}%)\n")
    else:
        out.append("□ Free space: Unknown\n")
    if st["checkpoint"]:
        out.append(f"⊠ Checkpoint buffer: {fmt_tok(st['checkpoint'])} tokens (not counted in usage)\n")
    return "".join(out)


def grid_segments(st: dict) -> list[int]:
    """Token totals of the grid's five colored segments, in drawing order."""
    return [
        st["system_prompt"] + st["system_tools"] + st["system_overhead"],
        st["user"],
        st["agent"],
        st["tool"],
        sum(st[k] for k in ("rules", "skills", "subagents", "legacy_workflows", "workflows", "plugins", "mcp_tools")),
    ]


def render_grid(st: dict, width: int, rows: int, color: bool) -> list[str]:
    cols = max(10, width // 2)
    total_cells = max(1, rows) * cols
    limit = st["limit"]
    counts = [0 if not tok or not limit else max(1, int(tok / limit * total_cells)) for tok in grid_segments(st)]
    used = sum(counts)
    if used > total_cells:
        f = total_cells / used
        counts = [int(c * f) for c in counts]
    counts.append(total_cells - sum(counts))

    cells = []
    for i, n in enumerate(counts):
        glyph = "□" if i == 5 else "◉"
        cells.extend([f"{GRID_COLORS[i]}{glyph}\x1b[0m" if color else glyph] * n)
    return [" ".join(cells[r * cols : (r + 1) * cols]) for r in range(len(cells) // cols)]


def _clip_utf8_ellipsis(text: str, max_bytes: int = 70) -> str:
    if len(text.encode()) <= max_bytes:
        return text
    return text.encode()[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def _checkpoint_display_text(ck: dict) -> str:
    raw = ck.get("intent") or ck.get("request_label") or ""
    if raw:
        return _clip_utf8_ellipsis(raw, 70)
    return f"steps {ck['start']}–{ck['end']}"


def checkpoint_view(st: dict) -> dict | None:
    cks = st["checkpoints"]
    if not cks or st["active_checkpoint"] < 0:
        return None
    active, historical = None, 0
    for ck in cks:
        if ck["index"] == st["active_checkpoint"]:
            active = ck
        else:
            historical += 1
    if active is None:
        return None
    text = _checkpoint_display_text(active)
    items = [
        {
            "number": ck["index"] + 1,
            "text": _checkpoint_display_text(ck),
            "start": ck["start"],
            "end": ck["end"],
            "created_at": ck.get("created_at", 0),
            "active": ck["index"] == st["active_checkpoint"],
        }
        for ck in sorted(cks, key=lambda c: c["index"], reverse=True)
    ]
    return {
        "count": len(cks),
        "active_number": active["index"] + 1,
        "active_text": text,
        "historical": historical,
        "items": items,
    }


def render_checkpoints(st: dict) -> str:
    view = checkpoint_view(st)
    if view is None:
        return ""
    out = [
        f"Checkpoints ({view['count']}) · /rewind\n",
        f"└ Checkpoint {view['active_number']} (active, in context): {view['active_text']}\n",
    ]
    if view["historical"]:
        out.append(f"  {view['historical']} historical checkpoint(s) (summarized, not in context)\n")
    return "".join(out)


def render_file_section(title: str, subtitle: str, items: list[tuple[str, int]]) -> str:
    if not items:
        return ""
    out = [f"\n{title} · {subtitle}\n"]
    for path, tok in items:
        out.append(f" └ {path}: {fmt_tok(tok)} tokens\n" if tok > 0 else f" └ {path}\n")
    return "".join(out)


def render_cost(cost_stats: dict | None) -> str:
    if not cost_stats or not cost_stats.get("total_calls"):
        return ""
    total_usd = cost_stats.get("total_usd")
    main_b = cost_stats.get("main_breakdown") or {}
    sub_b = cost_stats.get("subagent_breakdown") or {}
    unpriced = cost_stats.get("unpriced_calls", 0)
    note = " (estimated, excludes unpriced models)" if (unpriced and total_usd is not None) else " (estimated list price)"
    head = fmt_usd(total_usd) if total_usd is not None else "Unknown"
    lines = [f"\nEstimated API cost · {head}{note}\n"]
    if cost_stats.get("priced_calls"):
        in_usd = main_b.get("input_usd", 0.0) + sub_b.get("input_usd", 0.0)
        cache_usd = main_b.get("cache_read_usd", 0.0) + sub_b.get("cache_read_usd", 0.0)
        out_usd = main_b.get("output_usd", 0.0) + sub_b.get("output_usd", 0.0)
        lines.append(f" └ Breakdown: input {fmt_usd(in_usd)} · cache {fmt_usd(cache_usd)} · output {fmt_usd(out_usd)}\n")
        if sub_b.get("calls"):
            lines.append(
                f" └ Scope: main {fmt_usd(cost_stats.get('main_usd'))} ({main_b.get('calls', 0)} calls)"
                f" · subagents ({sub_b.get('count', 0)}) {fmt_usd(cost_stats.get('subagent_usd'))} ({sub_b.get('calls', 0)} calls)\n"
            )
    if unpriced:
        models_str = ", ".join(cost_stats.get("unpriced_models") or [])
        suffix = f" ({models_str})" if models_str else ""
        lines.append(f" └ Unpriced calls: {unpriced}{suffix}\n")
    return "".join(lines)


def render(
    st: dict,
    model: str,
    system_files: list[tuple[str, int]],
    term_width: int,
    color: bool,
    cost_stats: dict | None = None,
) -> str:
    stats = render_stats(st, model)
    grid_width = max(20, (2 * term_width) // 5)
    if term_width >= 70 and term_width - grid_width - 6 >= 40:
        grid = render_grid(st, grid_width, max(3, stats.count("\n")), color)
        stat_lines = stats.rstrip("\n").split("\n")
        pad = max(10, grid_width // 2) * 2 - 1
        lines = []
        for i in range(max(len(grid), len(stat_lines))):
            g = grid[i] if i < len(grid) else " " * pad
            s = stat_lines[i] if i < len(stat_lines) else ""
            lines.append(f"{g}   {s}".rstrip())
        body = "\n".join(lines) + "\n"
    else:
        grid = render_grid(st, term_width if term_width >= 20 else 40, 3, color)
        body = stats + "\n" + "\n".join(grid) + "\n"

    parts = ["└ Context Usage\n", "\n", body]
    cost_block = render_cost(cost_stats)
    if cost_block:
        parts.append(cost_block)
    ck = render_checkpoints(st)
    if ck:
        parts += ["\n", ck]
    parts.append(render_file_section("Knowledge files", "/rules", st["knowledge_files"]))
    parts.append(render_file_section("Artifact files", "/artifact", st["artifact_files"]))
    parts.append(render_file_section("Skill files", "/skill", st["skill_files"]))
    parts.append(render_file_section("System files", "auto-loaded", system_files))
    parts.append("\nRelated: /artifact · /skill · /rewind")
    return "".join(parts)


# --- background tasks --------------------------------------------------------

TASK_STATUS_NAMES = {
    2: "running",
    3: "done",
    4: "error",
    6: "cancelled",
}


def _parse_json_dict(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fallback_command_from_tool_call(tc: Msg | None) -> str:
    if tc is None:
        return ""
    args = _parse_json_dict(tc.str(3))
    if not args:
        return ""
    if isinstance(args.get("CommandLine"), str) and args["CommandLine"]:
        return args["CommandLine"]
    if "DurationSeconds" in args or "CronExpression" in args:
        parts = []
        if args.get("DurationSeconds") is not None:
            parts.append(f"Timer: {args['DurationSeconds']}s")
        if args.get("CronExpression"):
            parts.append(f"Cron: {args['CronExpression']}")
        if args.get("Prompt"):
            parts.append(f"Prompt: {args['Prompt']}")
        return ", ".join(parts)
    return ""


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _safe_str(val) -> str:
    return val.strip() if isinstance(val, str) else ""


def _safe_max_tokens(val) -> int:
    if isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return val if 0 < val < 10_000_000_000 else 0
    if isinstance(val, float):
        if not math.isfinite(val) or val <= 0 or val >= 10_000_000_000:
            return 0
        return int(val)
    return 0


def _file_sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def _wal_state(db_path: Path) -> str:
    """Return 'absent', 'empty', 'nonempty', or 'unknown' (when stat fails with non-ENOENT OSError)."""
    wal = Path(f"{db_path}-wal")
    try:
        st = wal.stat()
        return "nonempty" if st.st_size > 0 else "empty"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"


def _has_nonempty_wal(db_path: Path) -> bool:
    return _wal_state(db_path) == "nonempty"


def _copy_file_retry(src: Path, dst: Path, optional: bool = False) -> bool:
    """Copy a DB/WAL file with brief retries for transient Windows sharing locks.

    ``optional=True`` only tolerates ``FileNotFoundError``; permission or I/O errors
    always propagate so callers never query a stale snapshot missing committed WAL rows.
    """
    for attempt in range(3):
        try:
            shutil.copyfile(src, dst)
            return True
        except FileNotFoundError:
            if optional:
                return False
            raise
        except PermissionError:
            if attempt < 2:
                time.sleep(0.03 * (attempt + 1))
                continue
            raise
        except OSError:
            if attempt < 2:
                time.sleep(0.03 * (attempt + 1))
                continue
            raise
    return False


def _rmtree_retry(path: str) -> None:
    for attempt in range(3):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt < 2:
                time.sleep(0.03 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)


def _query_ro_db(db_path: Path, fn):
    """Run read-only query callback ``fn(conn)`` with busy retries and WAL no-shm fallbacks.

    Opening a WAL-mode SQLite DB with ``?mode=ro`` fails with
    ``OperationalError: unable to open database file`` (or ``disk I/O error`` /
    ``readonly database`` on Windows ``winShmMap``) when no ``-shm`` file
    exists (e.g. cleanly closed historical conversations) or when the process
    cannot create/write ``-shm``.
    """
    last_err: sqlite3.Error | None = None
    for attempt in range(3):
        conn = None
        try:
            conn = sqlite3.connect(sqlite_ro_uri(db_path), uri=True, timeout=1.5)
            return fn(conn)
        except sqlite3.OperationalError as exc:
            last_err = exc
            msg = str(exc).lower()
            if ("locked" in msg or "busy" in msg) and attempt < 2:
                time.sleep(0.05 * (attempt + 1))
                continue
            if any(k in msg for k in ("unable to open database file", "disk i/o error", "readonly database")):
                break
            raise
        finally:
            if conn is not None:
                conn.close()
    else:
        assert last_err is not None
        raise last_err

    wal_state = _wal_state(db_path)
    if wal_state in ("absent", "empty"):
        conn = sqlite3.connect(sqlite_ro_uri(db_path, immutable=True), uri=True)
        try:
            return fn(conn)
        finally:
            conn.close()

    # Copy DB + WAL into a temporary directory and verify source signatures did not
    # shift mid-copy; never silently drop a nonempty/unreadable WAL.
    wal = Path(f"{db_path}-wal")
    last_snap_err: Exception | None = None
    for attempt in range(2):
        tmpdir = tempfile.mkdtemp(prefix="ag-status-bar-")
        try:
            db_sig_before = _file_sig(db_path)
            wal_sig_before = _file_sig(wal)
            tmp_db = Path(tmpdir) / db_path.name
            _copy_file_retry(db_path, tmp_db, optional=False)
            wal_required = wal_state == "nonempty" or wal_sig_before is not None
            _copy_file_retry(wal, Path(f"{tmp_db}-wal"), optional=not wal_required)
            db_sig_after = _file_sig(db_path)
            wal_sig_after = _file_sig(wal)
            if db_sig_before != db_sig_after or wal_sig_before != wal_sig_after:
                last_snap_err = sqlite3.OperationalError("Database or WAL changed during snapshot copy")
                if attempt == 0:
                    time.sleep(0.05)
                    continue
                raise last_snap_err
            conn = sqlite3.connect(tmp_db)
            try:
                return fn(conn)
            finally:
                conn.close()
        except OSError as exc:
            last_snap_err = sqlite3.OperationalError(f"Failed to create consistent SQLite snapshot: {exc}")
            if attempt == 0:
                time.sleep(0.05)
                continue
            raise last_snap_err from exc
        finally:
            _rmtree_retry(tmpdir)
    assert last_snap_err is not None
    raise last_snap_err


def _subagent_db_status(db_path: Path | None, sub_conv_id: str) -> str:
    if db_path is None or not sub_conv_id or not re.fullmatch(r"[0-9a-fA-F-]+", sub_conv_id):
        return "unknown"
    sub_db = db_path.parent / f"{sub_conv_id}.db"
    if not sub_db.is_file():
        return "unknown"
    try:
        rows = _query_ro_db(
            sub_db,
            lambda conn: conn.execute("SELECT status FROM steps ORDER BY idx DESC LIMIT 5").fetchall(),
        )
    except sqlite3.Error:
        return "unknown"
    if not rows:
        return "running"
    statuses = [r[0] for r in rows]
    if any(s == 2 for s in statuses):
        return "running"
    latest = statuses[0]
    if latest == 4:
        return "error"
    if latest == 6:
        return "cancelled"
    return "done"


def _extract_conversation_id(text: str) -> str:
    if not text:
        return ""
    stripped = text.strip()
    if stripped.startswith("{"):
        parsed = _parse_json_dict(stripped)
        cid = parsed.get("conversationId")
        if isinstance(cid, str) and re.fullmatch(r"[0-9a-fA-F-]+", cid):
            return cid
    m_cid = re.search(r'"conversationId"\s*:\s*"([0-9a-fA-F-]+)"', text)
    return m_cid.group(1) if m_cid else ""


def _subagent_conv_id_from_step(step: Step) -> str:
    tc = step.md.msg(4)
    tool_name = tc.str(2) if tc is not None else ""
    if step.type != 127 and tool_name != "invoke_subagent":
        return ""
    gen_step = step.pb.msg(STEP_GENERIC) or step.generic_pb
    res = gen_step.msg(2) if gen_step is not None else None
    res_text = res.str(1) if res is not None else ""
    return _extract_conversation_id(res_text)


def extract_background_tasks(steps: list[Step], db_path: Path | None = None) -> list[dict]:
    """Extract background tasks (commands, timers) and subagents from steps."""
    killed_subagents: dict[str, int] = {}
    latest_kill_all_idx = -1
    for step in steps:
        if step.status != STATUS_DONE:
            continue
        tc = step.md.msg(4)
        if tc is None or tc.str(2) != "manage_subagents":
            continue
        args = _parse_json_dict(tc.str(3))
        action = args.get("Action")
        if action == "kill_all":
            latest_kill_all_idx = max(latest_kill_all_idx, step.idx)
        elif action == "kill":
            conv_ids = args.get("ConversationIds")
            if isinstance(conv_ids, list):
                for cid in conv_ids:
                    if isinstance(cid, str) and cid:
                        killed_subagents[cid] = max(killed_subagents.get(cid, -1), step.idx)

    tasks: list[dict] = []
    for step in steps:
        td = step.task_details
        tc = step.md.msg(4)
        tool_name = tc.str(2) if tc is not None else ""

        if td is not None and td.fields:
            command = _clip_text(td.str(4) or _fallback_command_from_tool_call(tc), 32768)
            log_uri = td.str(2)
            log_path = uri_to_path(log_uri) if log_uri else ""
            has_log = False
            log_mtime = 0
            log_size = 0
            if log_path:
                try:
                    if os.path.isfile(log_path):
                        st_res = os.stat(log_path)
                        has_log = True
                        log_mtime = int(st_res.st_mtime)
                        log_size = int(st_res.st_size)
                except OSError:
                    pass
            ts = td.msg(8)
            started_at = ts.int(1) if ts is not None else 0
            status_code = step.status
            status = TASK_STATUS_NAMES.get(status_code, "unknown")
            tasks.append({
                "step_idx": step.idx,
                "task_id": td.str(1),
                "title": _clip_text(td.str(7), 512),
                "command": command,
                "output_preview": _clip_text(td.str(3), 4096),
                "status": status,
                "status_code": status_code,
                "is_daemon": bool(td.int(6)),
                "tool": tool_name,
                "started_at": started_at,
                "has_log": has_log,
                "log_path": log_path,
                "log_mtime": log_mtime,
                "log_size": log_size,
            })
        elif step.type == 127 or tool_name == "invoke_subagent":
            args = _parse_json_dict(tc.str(3) if tc is not None else "")
            role = args.get("Role") if isinstance(args.get("Role"), str) else ""
            type_name = args.get("TypeName") if isinstance(args.get("TypeName"), str) else ""
            prompt = _clip_text(args.get("Prompt"), 32768) if isinstance(args.get("Prompt"), str) else ""

            sub_conv_id = _subagent_conv_id_from_step(step)

            if step.status == 2:
                status = "running"
            elif step.status == 4:
                status = "error"
            elif step.status == 6:
                status = "cancelled"
            elif latest_kill_all_idx > step.idx or (sub_conv_id and killed_subagents.get(sub_conv_id, -1) > step.idx):
                status = "cancelled"
            else:
                status = _subagent_db_status(db_path, sub_conv_id)

            ts = step.md.msg(1)
            started_at = ts.int(1) if ts is not None else 0
            tasks.append({
                "step_idx": step.idx,
                "task_id": sub_conv_id or f"subagent-{step.idx}",
                "title": role or type_name or "Subagent",
                "command": prompt,
                "output_preview": "",
                "status": status,
                "status_code": step.status,
                "is_daemon": False,
                "tool": "invoke_subagent",
                "subagent_type": type_name,
                "started_at": started_at,
                "has_log": False,
                "log_mtime": 0,
                "log_size": 0,
            })

    tasks.sort(key=lambda t: (t["status"] == "running", t["step_idx"]), reverse=True)
    return tasks


# --- cache & output stats ----------------------------------------------------


def _latest_usage_gen_idx(gens: list[Msg]) -> int:
    last_idx = -1
    for i, g in enumerate(gens):
        chat = g.msg(1)
        u = chat.msg(4) if chat is not None else None
        if u is None:
            continue
        if u.int(2) + u.int(5) + u.int(3) > 0:
            last_idx = i
    return last_idx


def collect_cache_stats(gens: list[Msg]) -> dict:
    """Compute latest-turn and session-cumulative prompt cache hit rates from gen_metadata."""
    generations = 0
    session_input = 0
    session_cache_read = 0
    latest_idx = _latest_usage_gen_idx(gens)
    last_input = 0
    last_cache_read = 0

    for i, g in enumerate(gens):
        chat = g.msg(1)
        u = chat.msg(4) if chat is not None else None
        if u is None:
            continue
        inp = u.int(2)
        cache_read = u.int(5)
        if i == latest_idx:
            last_input = inp
            last_cache_read = cache_read
        if inp + cache_read <= 0:
            continue
        generations += 1
        session_input += inp
        session_cache_read += cache_read

    last_total_input = last_input + last_cache_read
    session_total_input = session_input + session_cache_read
    return {
        "generations": generations,
        "last_hit_rate": round(last_cache_read / last_total_input * 100.0, 1) if last_total_input > 0 else None,
        "last_cache_read_tokens": last_cache_read,
        "last_input_tokens": last_input,
        "last_total_input_tokens": last_total_input,
        "session_hit_rate": round(session_cache_read / session_total_input * 100.0, 1) if session_total_input > 0 else None,
        "session_cache_read_tokens": session_cache_read,
        "session_input_tokens": session_input,
        "session_total_input_tokens": session_total_input,
    }


def collect_output_stats(gens: list[Msg]) -> dict:
    """Compute session-cumulative and latest-turn output & thinking tokens from gen_metadata."""
    session_output = 0
    session_thinking = 0
    session_response = 0
    latest_idx = _latest_usage_gen_idx(gens)
    last_output = 0
    last_thinking = 0

    for i, g in enumerate(gens):
        chat = g.msg(1)
        u = chat.msg(4) if chat is not None else None
        if u is None:
            continue
        out = u.int(3)
        thinking = u.int(9)
        if i == latest_idx:
            last_output = out
            last_thinking = thinking if out > 0 else 0
        if out <= 0:
            continue
        resp = u.int(10)
        session_output += out
        session_thinking += thinking
        session_response += resp if resp > 0 else max(0, out - thinking)

    return {
        "session_output_tokens": session_output,
        "session_thinking_tokens": session_thinking,
        "session_response_tokens": session_response,
        "thinking_pct": round(session_thinking / session_output * 100.0, 1) if session_output > 0 else None,
        "last_output_tokens": last_output,
        "last_thinking_tokens": last_thinking,
    }


# Speed estimation thresholds (matching CC Switch's agy_session / usage_stats.rs口径):
# Antigravity records `chat.msg(11)` (first-token wait) and `chat.msg(12)` (streaming),
# so total latency = first_token_ms + streaming_ms. Using total latency with >= 200 output
# tokens and >= 1000 ms avoids artificially inflated rates on buffered/thinking bursts.
SPEED_ESTIMATE_MIN_OUTPUT_TOKENS = 200
SPEED_ESTIMATE_MIN_DURATION_MS = 1000


def _duration_ms(dur: Msg | None) -> int | None:
    """Decode google.protobuf.Duration (seconds: int64, nanos: int32) to non-negative milliseconds."""
    if dur is None or not dur.fields:
        return None
    sec = dur.int(1, signed32=True)
    nanos = dur.int(2, signed32=True)
    if sec < 0 or nanos < 0 or nanos >= 1_000_000_000:
        return None
    return sec * 1000 + nanos // 1_000_000


def _gen_timing_ms(gen: Msg) -> tuple[int, int] | None:
    """Return (latency_ms, first_token_ms) from gen_metadata chat fields 11 and 12."""
    chat = gen.msg(1)
    if chat is None:
        return None
    first_token_ms = _duration_ms(chat.msg(11))
    streaming_ms = _duration_ms(chat.msg(12))
    if first_token_ms is None or streaming_ms is None:
        return None
    latency_ms = first_token_ms + streaming_ms
    if latency_ms <= 0:
        return None
    return latency_ms, first_token_ms


def _median_int(values: list[int]) -> int | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) // 2


def collect_speed_stats(gens: list[Msg]) -> dict:
    """Compute CC Switch-compatible output speed (tok/s) and TTFT stats from gen_metadata."""
    eligible_calls = 0
    timed_calls = 0
    est_output_tokens = 0
    est_duration_ms = 0
    last_tps: float | None = None
    last_output_tokens = 0
    last_latency_ms = 0
    last_first_token_ms: int | None = None
    ttft_samples: list[int] = []

    for g in gens:
        chat = g.msg(1)
        u = chat.msg(4) if chat is not None else None
        if u is None:
            continue
        out = u.int(3)
        if out <= 0 and u.int(2) + u.int(5) <= 0:
            continue
        timing = _gen_timing_ms(g)
        if timing is None:
            continue
        latency_ms, first_token_ms = timing
        timed_calls += 1
        ttft_samples.append(first_token_ms)
        if out >= SPEED_ESTIMATE_MIN_OUTPUT_TOKENS and latency_ms >= SPEED_ESTIMATE_MIN_DURATION_MS:
            eligible_calls += 1
            est_output_tokens += out
            est_duration_ms += latency_ms
            last_output_tokens = out
            last_latency_ms = latency_ms
            last_first_token_ms = first_token_ms
            last_tps = round(out / (latency_ms / 1000.0), 1)

    session_tps = round(est_output_tokens / (est_duration_ms / 1000.0), 1) if est_duration_ms > 0 else None
    return {
        "estimated": True,
        "eligible_calls": eligible_calls,
        "timed_calls": timed_calls,
        "output_tokens": est_output_tokens,
        "duration_ms": est_duration_ms,
        "session_tps": session_tps,
        "last_tps": last_tps,
        "last_output_tokens": last_output_tokens,
        "last_latency_ms": last_latency_ms,
        "last_first_token_ms": last_first_token_ms,
        "first_token_median_ms": _median_int(ttft_samples),
    }


# --- model usage & API cost estimation ---------------------------------------

# Public API list pricing in USD per 1,000,000 tokens.
# Sources:
#   - Google Gemini Developer API: https://ai.google.dev/gemini-api/docs/pricing
#   - Anthropic Claude API: https://platform.claude.com/docs/en/about-claude/pricing
# Each model maps to schedule tuples (effective_from_date_or_None, prompt_tiers)
# ordered descending by effective_from, with prompt_tiers ordered ascending by
# max_prompt_tokens (None = unbounded).
MODEL_PRICING: dict[str, tuple[tuple[date | None, tuple[tuple[int | None, dict[str, float | None]], ...]], ...]] = {
    "gemini-4-carbon": (
        (date(2026, 12, 1), ((None, {"input": 4.00, "cache_read": 0.20, "output": 20.00}),)),
        (None, ((None, {"input": 2.00, "cache_read": 0.10, "output": 10.00}),)),
    ),
    "gemini-4-argon": (
        (date(2026, 12, 1), ((None, {"input": 4.00, "cache_read": 0.20, "output": 20.00}),)),
        (None, ((None, {"input": 2.00, "cache_read": 0.10, "output": 10.00}),)),
    ),
    "gemini-4-neon": (
        (None, ((None, {"input": 3.00, "cache_read": 0.15, "output": 15.00}),)),
    ),
    "gemini-4-hydrogen": (
        (None, ((None, {"input": 1.50, "cache_read": 0.075, "output": 7.50}),)),
    ),
    "gemini-3.8-flash": (
        (date(2027, 1, 1), ((None, {"input": 1.50, "cache_read": 0.15, "output": 7.50}),)),
        (None, ((None, {"input": 0.75, "cache_read": 0.075, "output": 3.75}),)),
    ),
    "gemini-3.7-flash": (
        (None, ((None, {"input": 0.75, "cache_read": 0.075, "output": 3.75}),)),
    ),
    "gemini-3.6-flash": (
        (date(2027, 1, 1), ((None, {"input": 1.50, "cache_read": 0.15, "output": 7.50}),)),
        (None, ((None, {"input": 0.75, "cache_read": 0.075, "output": 3.75}),)),
    ),
    "gemini-flash-latest": (
        (None, ((None, {"input": 0.75, "cache_read": 0.075, "output": 3.75}),)),
    ),
    "gemini-3.5-flash": (
        (None, ((None, {"input": 1.50, "cache_read": 0.15, "output": 9.00}),)),
    ),
    "gemini-3.5-flash-lite": (
        (None, ((None, {"input": 0.30, "cache_read": 0.03, "output": 2.50}),)),
    ),
    "gemini-3.1-flash-lite": (
        (None, ((None, {"input": 0.25, "cache_read": 0.025, "output": 1.50}),)),
    ),
    "gemini-3.1-pro": (
        (
            None,
            (
                (200_000, {"input": 2.00, "cache_read": 0.20, "output": 12.00}),
                (None, {"input": 4.00, "cache_read": 0.40, "output": 18.00}),
            ),
        ),
    ),
    "gemini-3-pro": (
        (
            None,
            (
                (200_000, {"input": 2.00, "cache_read": 0.20, "output": 12.00}),
                (None, {"input": 4.00, "cache_read": 0.40, "output": 18.00}),
            ),
        ),
    ),
    "gemini-3-flash": (
        (None, ((None, {"input": 0.50, "cache_read": 0.05, "output": 3.00}),)),
    ),
    "gemini-2.5-pro": (
        # Effective Dec 1, 2026, Gemini 2.5 Pro routes to Gemini 3.8 Flash and bills at its rates.
        (date(2027, 1, 1), ((None, {"input": 1.50, "cache_read": 0.15, "output": 7.50}),)),
        (date(2026, 12, 1), ((None, {"input": 0.75, "cache_read": 0.075, "output": 3.75}),)),
        (
            None,
            (
                (200_000, {"input": 1.25, "cache_read": 0.125, "output": 10.00}),
                (None, {"input": 2.50, "cache_read": 0.25, "output": 15.00}),
            ),
        ),
    ),
    "gemini-2.5-flash": (
        (None, ((None, {"input": 0.30, "cache_read": 0.03, "output": 2.50}),)),
    ),
    "gemini-2.5-flash-lite": (
        (None, ((None, {"input": 0.10, "cache_read": 0.01, "output": 0.40}),)),
    ),
    "gemini-2.0-flash": (
        (None, ((None, {"input": 0.10, "cache_read": 0.025, "output": 0.40}),)),
    ),
    "claude-fable-5-2": (
        (None, ((None, {"input": 8.00, "cache_read": 0.20, "output": 40.00}),)),
    ),
    "claude-fable-5-1": (
        (None, ((None, {"input": 10.00, "cache_read": 0.25, "output": 50.00}),)),
    ),
    "claude-fable-5": (
        (None, ((None, {"input": 10.00, "cache_read": 1.00, "output": 50.00}),)),
    ),
    "claude-mythos-5-1": (
        (None, ((None, {"input": 10.00, "cache_read": 0.25, "output": 50.00}),)),
    ),
    "claude-mythos-5": (
        (None, ((None, {"input": 10.00, "cache_read": 1.00, "output": 50.00}),)),
    ),
    "claude-opus-5-5": (
        (None, ((None, {"input": 4.00, "cache_read": 0.20, "output": 20.00}),)),
    ),
    "claude-sonnet-5-5": (
        (None, ((None, {"input": 2.00, "cache_read": 0.10, "output": 10.00}),)),
    ),
    "claude-opus-5": (
        (None, ((None, {"input": 5.00, "cache_read": 0.50, "output": 25.00}),)),
    ),
    "claude-opus-4-8": (
        (None, ((None, {"input": 5.00, "cache_read": 0.50, "output": 25.00}),)),
    ),
    "claude-opus-4-7": (
        (None, ((None, {"input": 5.00, "cache_read": 0.50, "output": 25.00}),)),
    ),
    "claude-opus-4-6": (
        (None, ((None, {"input": 5.00, "cache_read": 0.50, "output": 25.00}),)),
    ),
    "claude-opus-4-5": (
        (None, ((None, {"input": 5.00, "cache_read": 0.50, "output": 25.00}),)),
    ),
    "claude-opus-4-1": (
        (None, ((None, {"input": 15.00, "cache_read": 1.50, "output": 75.00}),)),
    ),
    "claude-opus-4": (
        (None, ((None, {"input": 15.00, "cache_read": 1.50, "output": 75.00}),)),
    ),
    "claude-sonnet-5": (
        (None, ((None, {"input": 2.00, "cache_read": 0.20, "output": 10.00}),)),
    ),
    "claude-sonnet-4-6": (
        (None, ((None, {"input": 3.00, "cache_read": 0.30, "output": 15.00}),)),
    ),
    "claude-sonnet-4-5": (
        (None, ((None, {"input": 3.00, "cache_read": 0.30, "output": 15.00}),)),
    ),
    "claude-sonnet-4": (
        (None, ((None, {"input": 3.00, "cache_read": 0.30, "output": 15.00}),)),
    ),
    "claude-3-5-sonnet": (
        (None, ((None, {"input": 3.00, "cache_read": 0.30, "output": 15.00}),)),
    ),
    "claude-haiku-5-5": (
        (
            None,
            (
                (100_000, {"input": 0.10, "cache_read": 0.01, "output": 0.50}),
                (None, {"input": 0.50, "cache_read": 0.05, "output": 2.50}),
            ),
        ),
    ),
    "claude-haiku-4-5": (
        (None, ((None, {"input": 1.00, "cache_read": 0.10, "output": 5.00}),)),
    ),
    "claude-3-5-haiku": (
        (None, ((None, {"input": 0.80, "cache_read": 0.08, "output": 4.00}),)),
    ),
}

PRICING_SLUG_ALIASES = {
    "gemini-pro-orchestrator": "gemini-4-carbon",
    "gemini-pro-orchestrator-carbon": "gemini-4-carbon",
    "gemini-pro-orchestrator-neon": "gemini-4-neon",
    "gemini-pro-orchestrator-argon": "gemini-4-argon",
    "gemini-pro-orchestrator-g1-pro": "gemini-4-argon",
    "gemini-flash-worker-agy": "gemini-4-hydrogen",
    "gemini-4-hydrogen-sonar": "gemini-4-hydrogen",
    "gemini-4-hydrogen-orchestrator": "gemini-4-hydrogen",
    "gemini-3.8-flash-n": "gemini-3.8-flash",
    "gemini-2.5-flash-thinking": "gemini-2.5-flash",
    "gemini-3.1-pro-preview": "gemini-3.1-pro",
    "gemini-3.1-pro-preview-customtools": "gemini-3.1-pro",
    "gemini-3.1-pro-customtools": "gemini-3.1-pro",
    "gemini-3-pro-preview": "gemini-3-pro",
    "gemini-3-flash-preview": "gemini-3-flash",
}

PRICING_DISPLAY_ALIASES = {
    "orchestrator carbon": "gemini-4-carbon",
    "gemini 4 carbon": "gemini-4-carbon",
    "orchestrator argon": "gemini-4-argon",
    "gemini 4 argon": "gemini-4-argon",
    "orchestrator neon": "gemini-4-neon",
    "gemini 4 neon": "gemini-4-neon",
    "gemini 4 hydrogen": "gemini-4-hydrogen",
    "gemini 4 hydrogen orchestrator": "gemini-4-hydrogen",
    "gemini 3.8 flash": "gemini-3.8-flash",
    "gemini 3.7 flash": "gemini-3.7-flash",
    "gemini 3.6 flash": "gemini-3.6-flash",
    "gemini flash latest": "gemini-flash-latest",
    "gemini 3.5 flash": "gemini-3.5-flash",
    "gemini 3.5 flash lite": "gemini-3.5-flash-lite",
    "gemini 3.1 flash lite": "gemini-3.1-flash-lite",
    "gemini 3.1 pro": "gemini-3.1-pro",
    "gemini 3.1 pro preview": "gemini-3.1-pro",
    "gemini 3 pro": "gemini-3-pro",
    "gemini 3 pro preview": "gemini-3-pro",
    "gemini 3 flash": "gemini-3-flash",
    "gemini 3 flash preview": "gemini-3-flash",
    "gemini 2.5 pro": "gemini-2.5-pro",
    "gemini 2.5 flash": "gemini-2.5-flash",
    "gemini 2.5 flash lite": "gemini-2.5-flash-lite",
    "gemini 2.0 flash": "gemini-2.0-flash",
    "claude fable 5.2": "claude-fable-5-2",
    "claude fable 5.1": "claude-fable-5-1",
    "claude fable 5": "claude-fable-5",
    "claude mythos 5.1": "claude-mythos-5-1",
    "claude mythos 5": "claude-mythos-5",
    "claude opus 5.5": "claude-opus-5-5",
    "claude sonnet 5.5": "claude-sonnet-5-5",
    "claude opus 5": "claude-opus-5",
    "claude opus 4.8": "claude-opus-4-8",
    "claude opus 4.7": "claude-opus-4-7",
    "claude opus 4.6": "claude-opus-4-6",
    "claude opus 4.5": "claude-opus-4-5",
    "claude opus 4.1": "claude-opus-4-1",
    "claude opus 4": "claude-opus-4",
    "claude sonnet 5": "claude-sonnet-5",
    "claude sonnet 4.6": "claude-sonnet-4-6",
    "claude sonnet 4.5": "claude-sonnet-4-5",
    "claude sonnet 4": "claude-sonnet-4",
    "claude 3.5 sonnet": "claude-3-5-sonnet",
    "claude haiku 5.5": "claude-haiku-5-5",
    "claude haiku 4.5": "claude-haiku-4-5",
    "claude 3.5 haiku": "claude-3-5-haiku",
}


def base_model_id(slug: str) -> str:
    """Normalize model slug to base model family ID (strip effort & -tiered alias)."""
    parts = [p for p in slug.strip().split("-") if p]
    if len(parts) > 1 and parts[-1].lower() in EFFORT_LEVELS:
        parts.pop()
    if len(parts) > 1 and parts[-1].lower() == "tiered":
        parts.pop()
    return "-".join(parts)


def _normalize_pricing_slug(slug: str) -> str:
    s = slug.strip().lower()
    if not s:
        return ""
    if s in PRICING_SLUG_ALIASES:
        return PRICING_SLUG_ALIASES[s]
    if s in MODEL_PRICING:
        return s
    parts = [p for p in s.split("-") if p]
    while len(parts) > 1 and (
        parts[-1] in (*EFFORT_LEVELS, "tiered", "thinking", "preview", "customtools")
        or (len(parts[-1]) == 8 and parts[-1].isdigit())
    ):
        candidate = "-".join(parts)
        if candidate in PRICING_SLUG_ALIASES:
            return PRICING_SLUG_ALIASES[candidate]
        if candidate in MODEL_PRICING:
            return candidate
        parts.pop()
    joined = "-".join(parts)
    if joined in PRICING_SLUG_ALIASES:
        return PRICING_SLUG_ALIASES[joined]
    if joined in MODEL_PRICING:
        return joined
    return ""


def _normalize_pricing_display(name: str) -> str:
    if not name:
        return ""
    base = re.sub(r"\s*\((orchestrator)\)\s*$", "", split_model_effort(name)[0], flags=re.IGNORECASE)
    norm = re.sub(r"[^a-z0-9.]+", " ", base.lower()).strip()
    if norm in PRICING_DISPLAY_ALIASES:
        return PRICING_DISPLAY_ALIASES[norm]
    if norm.startswith("orchestrator ") and norm[len("orchestrator "):] in ("carbon", "argon", "neon"):
        return PRICING_DISPLAY_ALIASES[norm]
    return ""


def resolve_pricing_model_key(slugs: tuple[str, ...], display_names: tuple[str, ...]) -> str:
    for slug in slugs:
        key = _normalize_pricing_slug(slug)
        if key:
            return key
    for name in display_names:
        key = _normalize_pricing_display(name)
        if key:
            return key
    return ""


def lookup_model_rates(pricing_key: str, prompt_tokens: int, on_date: date) -> dict[str, float | None] | None:
    schedules = MODEL_PRICING.get(pricing_key)
    if not schedules:
        return None
    for effective_from, tiers in schedules:
        if effective_from is None or on_date >= effective_from:
            for max_prompt, rates in tiers:
                if max_prompt is None or prompt_tokens <= max_prompt:
                    return rates
    return None


def estimate_call_cost(
    pricing_key: str,
    inp: int,
    cache_read: int,
    out: int,
    on_date: date,
) -> dict[str, float] | None:
    if not pricing_key:
        return None
    rates = lookup_model_rates(pricing_key, inp + cache_read, on_date)
    if rates is None:
        return None
    cache_rate = rates["cache_read"]
    if cache_read > 0 and cache_rate is None:
        return None
    in_usd = (inp / 1_000_000.0) * float(rates["input"])
    cache_usd = ((cache_read / 1_000_000.0) * float(cache_rate)) if cache_read > 0 and cache_rate is not None else 0.0
    out_usd = (out / 1_000_000.0) * float(rates["output"])
    saved_usd = (
        (cache_read / 1_000_000.0) * max(0.0, float(rates["input"]) - float(cache_rate))
        if cache_read > 0 and cache_rate is not None
        else 0.0
    )
    return {
        "input_usd": in_usd,
        "cache_read_usd": cache_usd,
        "output_usd": out_usd,
        "total_usd": in_usd + cache_usd + out_usd,
        "cache_saved_usd": saved_usd,
    }


def _step_dates_from_steps(steps: list[Step]) -> dict[int, date]:
    dates: dict[int, date] = {}
    for step in steps:
        ts = step.md.msg(1)
        sec = ts.int(1) if ts is not None else 0
        if sec > 0:
            try:
                dates[step.idx] = datetime.fromtimestamp(sec).date()
            except (OverflowError, OSError, ValueError):
                pass
    return dates


def _gen_call_date(gen: Msg, step_dates: dict[int, date] | None, fallback_date: date) -> date:
    if step_dates:
        chat = gen.msg(1)
        if chat is not None:
            for kv in chat.msgs(20):
                if kv.str(1) == "last_step_index":
                    raw = kv.str(2).strip()
                    if raw.isdigit():
                        d = step_dates.get(int(raw))
                        if d is not None:
                            return d
                    break
    return fallback_date


def _new_cost_bucket() -> dict:
    return {
        "calls": 0,
        "priced_calls": 0,
        "unpriced_calls": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "input_usd": 0.0,
        "cache_read_usd": 0.0,
        "output_usd": 0.0,
        "total_usd": 0.0,
        "cache_saved_usd": 0.0,
    }


def _evaluate_gens(
    gens: list[Msg],
    catalog: dict | None = None,
    step_dates: dict[int, date] | None = None,
    override_date: date | None = None,
) -> tuple[dict, list[tuple[Msg, int, int, int, int, str, str, str, str, dict[str, float] | None]], dict[str, int]]:
    latest_step_date = max(step_dates.values()) if step_dates else None
    fallback_date = override_date or latest_step_date or date.today()
    bucket = _new_cost_bucket()
    unpriced_models: dict[str, int] = {}
    evaluated: list[tuple[Msg, int, int, int, int, str, str, str, str, dict[str, float] | None]] = []

    for g in gens:
        chat = g.msg(1)
        u = chat.msg(4) if chat is not None else None
        if u is None:
            continue
        inp = u.int(2)
        cache_read = u.int(5)
        out = u.int(3)
        thinking = u.int(9)
        if inp + cache_read + out <= 0:
            continue

        mid = model_id(g)
        resp_mid = chat.str(19) if chat is not None else ""
        raw_cat = (catalog or {}).get(mid)
        catalog_display = _safe_str(raw_cat.get("displayName")) if isinstance(raw_cat, dict) else ""
        chat_display = chat.str(21) if chat is not None else ""
        display = split_model_effort(catalog_display or model_name(g))[0]

        call_date = override_date if override_date is not None else _gen_call_date(g, step_dates, fallback_date)
        pricing_key = resolve_pricing_model_key((mid, resp_mid), (catalog_display, chat_display, display))
        call_cost = estimate_call_cost(pricing_key, inp, cache_read, out, call_date)

        bucket["calls"] += 1
        bucket["input_tokens"] += inp
        bucket["cache_read_tokens"] += cache_read
        bucket["output_tokens"] += out
        if call_cost is not None:
            bucket["priced_calls"] += 1
            bucket["input_usd"] += call_cost["input_usd"]
            bucket["cache_read_usd"] += call_cost["cache_read_usd"]
            bucket["output_usd"] += call_cost["output_usd"]
            bucket["total_usd"] += call_cost["total_usd"]
            bucket["cache_saved_usd"] += call_cost["cache_saved_usd"]
        else:
            bucket["unpriced_calls"] += 1
            unpriced_models[display] = unpriced_models.get(display, 0) + 1

        evaluated.append((g, inp, cache_read, out, thinking, mid, resp_mid, catalog_display, chat_display, call_cost))

    return bucket, evaluated, unpriced_models


def slug_effort_label(slug: str) -> str:
    """Extract normalized effort label from a model slug when present."""
    parts = [p for p in slug.strip().split("-") if p]
    if len(parts) > 1 and parts[-1].lower() in EFFORT_LEVELS:
        e = parts[-1].lower()
        return "XHigh" if e == "xhigh" else e.capitalize()
    return ""


def split_model_effort(name: str) -> tuple[str, str]:
    """Split a model display name into (base_name, effort_label).

    Only strips known thinking/reasoning effort levels so non-effort suffixes
    such as 'Orchestrator (Carbon)' remain intact.
    """
    text = name.strip()
    m = EFFORT_DISPLAY_RE.search(text)
    if m:
        effort_raw = re.sub(r"\s+", " ", m.group(1)).strip()
        effort = "XHigh" if effort_raw.lower() in ("xhigh", "extra high") else effort_raw.capitalize()
        base = text[:m.start()].strip()
        return (base or text), effort
    if " " not in text and "-" in text:
        parts = text.split("-")
        if len(parts) > 1 and parts[-1].lower() in EFFORT_LEVELS:
            return "-".join(parts[:-1]), ("XHigh" if parts[-1].lower() == "xhigh" else parts[-1].capitalize())
    return text, ""


def _model_usage_from_evaluated(evaluated: list, catalog: dict | None = None) -> list[dict]:
    """Aggregate per-model call counts, token usage, and estimated USD across evaluated gens."""
    id_buckets: dict[str, dict] = {}
    name_buckets: dict[str, dict] = {}

    def _new_bucket(model_disp: str, mid_val: str) -> dict:
        return {
            "model": model_disp,
            "model_id": mid_val,
            "calls": 0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "efforts": {},
            "priced_calls": 0,
            "unpriced_calls": 0,
            "input_cost_usd": 0.0,
            "cache_read_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "total_cost_usd": 0.0,
            "_aliases": set(),
            "_label_votes": {},
        }

    def _add_stats(
        entry: dict,
        inp: int,
        cache_read: int,
        out: int,
        thinking: int,
        effort: str,
        call_cost: dict[str, float] | None,
    ):
        entry["calls"] += 1
        entry["input_tokens"] += inp
        entry["cache_read_tokens"] += cache_read
        entry["output_tokens"] += out
        entry["thinking_tokens"] += thinking
        if effort:
            entry["efforts"][effort] = entry["efforts"].get(effort, 0) + 1
        if call_cost is not None:
            entry["priced_calls"] += 1
            entry["input_cost_usd"] += call_cost["input_usd"]
            entry["cache_read_cost_usd"] += call_cost["cache_read_usd"]
            entry["output_cost_usd"] += call_cost["output_usd"]
            entry["total_cost_usd"] += call_cost["total_usd"]
        else:
            entry["unpriced_calls"] += 1

    catalog_name_to_ids: dict[str, set[str]] = {}
    for slug, cat_val in (catalog or {}).items():
        if not isinstance(slug, str) or not isinstance(cat_val, dict):
            continue
        cat_disp = cat_val.get("displayName")
        if isinstance(cat_disp, str) and cat_disp.strip():
            cat_base = split_model_effort(cat_disp)[0].lower()
            canon_slug = base_model_id(slug)
            if cat_base and canon_slug:
                catalog_name_to_ids.setdefault(cat_base, set()).add(canon_slug)

    for g, inp, cache_read, out, thinking, mid, resp_mid, catalog_display, chat_display, call_cost in evaluated:
        chat = g.msg(1)
        canon_id = base_model_id(mid) if mid else (base_model_id(resp_mid) if resp_mid and "-" in resp_mid else "")

        raw_display = catalog_display or model_name(g)
        display, disp_effort = split_model_effort(raw_display)
        slug_effort = (
            slug_effort_label(mid)
            if mid
            else (slug_effort_label(resp_mid) if resp_mid and "-" in resp_mid else "")
        )
        effort = slug_effort or disp_effort

        base_catalog = split_model_effort(catalog_display)[0] if catalog_display else ""
        base_chat = split_model_effort(chat_display)[0] if chat_display else ""
        id_default = split_model_effort(model_label(canon_id))[0] if canon_id else ""

        if canon_id:
            entry = id_buckets.get(canon_id)
            chosen_label = base_catalog or base_chat or id_default or display
            if entry is None:
                entry = _new_bucket(chosen_label, canon_id)
                id_buckets[canon_id] = entry
            for alias in (display, base_catalog, base_chat, id_default):
                if alias:
                    entry["_aliases"].add(alias.lower())
            vote_label = base_catalog or base_chat or id_default
            if vote_label:
                entry["_label_votes"][vote_label] = entry["_label_votes"].get(vote_label, 0) + 1
            _add_stats(entry, inp, cache_read, out, thinking, effort, call_cost)
        else:
            key = display.lower()
            entry = name_buckets.get(key)
            if entry is None:
                entry = _new_bucket(display, "")
                name_buckets[key] = entry
            _add_stats(entry, inp, cache_read, out, thinking, effort, call_cost)

    # Merge historical no-ID buckets only when unambiguous across BOTH observed session IDs and the catalog
    unmatched_names: list[dict] = []
    for name_key, n_entry in name_buckets.items():
        matches = [b for b in id_buckets.values() if name_key in b["_aliases"]]
        cat_ids = catalog_name_to_ids.get(name_key)
        if len(matches) == 1 and (not cat_ids or cat_ids == {matches[0]["model_id"]}):
            target = matches[0]
            target["calls"] += n_entry["calls"]
            target["input_tokens"] += n_entry["input_tokens"]
            target["cache_read_tokens"] += n_entry["cache_read_tokens"]
            target["output_tokens"] += n_entry["output_tokens"]
            target["thinking_tokens"] += n_entry["thinking_tokens"]
            target["priced_calls"] += n_entry["priced_calls"]
            target["unpriced_calls"] += n_entry["unpriced_calls"]
            target["input_cost_usd"] += n_entry["input_cost_usd"]
            target["cache_read_cost_usd"] += n_entry["cache_read_cost_usd"]
            target["output_cost_usd"] += n_entry["output_cost_usd"]
            target["total_cost_usd"] += n_entry["total_cost_usd"]
            for eff, cnt in n_entry["efforts"].items():
                target["efforts"][eff] = target["efforts"].get(eff, 0) + cnt
            lbl = n_entry["model"]
            target["_label_votes"][lbl] = target["_label_votes"].get(lbl, 0) + n_entry["calls"]
        else:
            unmatched_names.append(n_entry)

    all_buckets: list[dict] = []
    for b in id_buckets.values():
        if b["_label_votes"]:
            b["model"] = max(b["_label_votes"].items(), key=lambda kv: kv[1])[0]
        all_buckets.append(b)
    all_buckets.extend(unmatched_names)

    results: list[dict] = []
    for entry in all_buckets:
        total_in = entry["input_tokens"] + entry["cache_read_tokens"]
        total = total_in + entry["output_tokens"]
        efforts = dict(sorted(entry["efforts"].items(), key=lambda kv: kv[1], reverse=True))
        has_cost = entry["priced_calls"] > 0
        results.append({
            "model": entry["model"],
            "model_id": entry["model_id"],
            "calls": entry["calls"],
            "input_tokens": entry["input_tokens"],
            "cache_read_tokens": entry["cache_read_tokens"],
            "output_tokens": entry["output_tokens"],
            "thinking_tokens": entry["thinking_tokens"],
            "efforts": efforts,
            "total_input_tokens": total_in,
            "total_tokens": total,
            "cache_hit_rate": round(entry["cache_read_tokens"] / total_in * 100.0, 1) if total_in > 0 else None,
            "share_pct": 0.0,
            "estimated_cost_usd": round(entry["total_cost_usd"], 6) if has_cost else None,
            "input_cost_usd": round(entry["input_cost_usd"], 6) if has_cost else None,
            "cache_read_cost_usd": round(entry["cache_read_cost_usd"], 6) if has_cost else None,
            "output_cost_usd": round(entry["output_cost_usd"], 6) if has_cost else None,
            "unpriced_calls": entry["unpriced_calls"],
        })
    results.sort(key=lambda m: (m["total_tokens"], m["calls"]), reverse=True)
    grand_total = sum(m["total_tokens"] for m in results)
    if grand_total > 0:
        for m in results:
            m["share_pct"] = round(m["total_tokens"] / grand_total * 100.0, 1)
    return results


def collect_model_usage(
    gens: list[Msg],
    catalog: dict | None = None,
    step_dates: dict[int, date] | None = None,
    override_date: date | None = None,
) -> list[dict]:
    """Aggregate per-model call counts, token usage, and estimated USD across gen_metadata."""
    _, evaluated, _ = _evaluate_gens(gens, catalog=catalog, step_dates=step_dates, override_date=override_date)
    return _model_usage_from_evaluated(evaluated, catalog=catalog)


def _read_subagent_cost_rows(sub_db: Path) -> tuple[list[Step], list[Msg]]:
    def _read(conn: sqlite3.Connection):
        conn.execute("BEGIN")
        step_cols = {r[1] for r in conn.execute("PRAGMA table_info(steps)").fetchall()}
        sub_step_rows = []
        if step_cols:
            idx_col = "idx" if "idx" in step_cols else "rowid"
            st_col = "step_type" if "step_type" in step_cols else "0"
            status_col = "status" if "status" in step_cols else "0"
            md_col = "metadata" if "metadata" in step_cols else "NULL"
            sp_col = "step_payload" if "step_payload" in step_cols else "NULL"
            if "step_type" in step_cols:
                sub_step_rows = conn.execute(
                    f"SELECT {idx_col}, {st_col}, {status_col}, {md_col}, "
                    f"CASE WHEN {st_col} IN (127, 132) THEN {sp_col} ELSE NULL END "
                    f"FROM steps ORDER BY 1"
                ).fetchall()
            else:
                sub_step_rows = conn.execute(
                    f"SELECT {idx_col}, {st_col}, {status_col}, {md_col}, {sp_col} FROM steps ORDER BY 1"
                ).fetchall()
        has_gen = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gen_metadata'"
        ).fetchone()
        gen_rows = []
        if has_gen:
            gen_cols = {r[1] for r in conn.execute("PRAGMA table_info(gen_metadata)").fetchall()}
            if "data" in gen_cols:
                order_col = "idx" if "idx" in gen_cols else "rowid"
                gen_rows = conn.execute(f"SELECT data FROM gen_metadata ORDER BY {order_col}").fetchall()
        conn.rollback()
        return sub_step_rows, gen_rows

    sub_step_rows, gen_rows = _query_ro_db(sub_db, _read)
    sub_steps = [
        Step(pos, r[4] or b"", r[1], r[2], r[3], idx=r[0])
        for pos, r in enumerate(sub_step_rows)
    ]
    sub_gens = [Msg(r[0]) for r in gen_rows]
    return sub_steps, sub_gens


def _extract_subagent_ids_from_steps(steps: list[Step]) -> list[str]:
    ids: list[str] = []
    for step in steps:
        cid = _subagent_conv_id_from_step(step)
        if cid and re.fullmatch(r"[0-9a-fA-F-]+", cid):
            ids.append(cid)
    return ids


def _bucket_summary(bucket: dict, count: int | None = None) -> dict:
    has_cost = bucket["priced_calls"] > 0
    out = {
        "calls": bucket["calls"],
        "priced_calls": bucket["priced_calls"],
        "unpriced_calls": bucket["unpriced_calls"],
        "input_tokens": bucket["input_tokens"],
        "cache_read_tokens": bucket["cache_read_tokens"],
        "output_tokens": bucket["output_tokens"],
        "input_usd": round(bucket["input_usd"], 6) if has_cost else 0.0,
        "cache_read_usd": round(bucket["cache_read_usd"], 6) if has_cost else 0.0,
        "output_usd": round(bucket["output_usd"], 6) if has_cost else 0.0,
        "total_usd": round(bucket["total_usd"], 6) if has_cost else None,
        "cache_saved_usd": round(bucket["cache_saved_usd"], 6) if has_cost else 0.0,
    }
    if count is not None:
        out["count"] = count
    return out


def _summarize_cost_by_model(
    evaluated_rows: list,
    scope_total_usd: float | None,
    catalog: dict | None,
) -> list[dict]:
    if not evaluated_rows:
        return []
    priced_total = float(scope_total_usd) if scope_total_usd is not None else 0.0
    out: list[dict] = []
    for m_item in _model_usage_from_evaluated(evaluated_rows, catalog=catalog):
        m_usd = m_item.get("estimated_cost_usd")
        out.append({
            "model": m_item["model"],
            "model_id": m_item["model_id"],
            "calls": m_item["calls"],
            "total_tokens": m_item["total_tokens"],
            "estimated_cost_usd": m_usd,
            "input_cost_usd": m_item["input_cost_usd"],
            "cache_read_cost_usd": m_item["cache_read_cost_usd"],
            "output_cost_usd": m_item["output_cost_usd"],
            "unpriced_calls": m_item["unpriced_calls"],
            "share_pct": round((m_usd / priced_total * 100.0), 1) if (m_usd is not None and priced_total > 0) else 0.0,
        })
    out.sort(
        key=lambda x: (
            x["estimated_cost_usd"] if x["estimated_cost_usd"] is not None else -1.0,
            x["total_tokens"],
            x["calls"],
        ),
        reverse=True,
    )
    return out


def collect_session_cost(
    db_path: Path,
    main_steps: list[Step],
    main_gens: list[Msg],
    main_bucket: dict,
    unpriced_models: dict[str, int],
    catalog: dict | None = None,
    override_date: date | None = None,
    main_evaluated: list | None = None,
) -> dict:
    """Aggregate public API equivalent USD estimate across the main conversation and descendant subagents."""
    sub_bucket = _new_cost_bucket()
    merged_unpriced = dict(unpriced_models)
    sub_ids_seen: set[str] = {db_path.stem.lower()}
    sub_db_ids: list[str] = []
    missing_sub_ids: list[str] = []
    failed_sub_ids: list[str] = []
    sub_evaluated: list = []
    sub_with_calls = 0

    max_depth = 4
    max_sub_dbs = 200
    queue: deque[tuple[str, int]] = deque()
    for cid in _extract_subagent_ids_from_steps(main_steps):
        if cid.lower() not in sub_ids_seen:
            sub_ids_seen.add(cid.lower())
            queue.append((cid, 1))

    while queue and (len(sub_db_ids) + len(missing_sub_ids)) < max_sub_dbs:
        cid, depth = queue.popleft()
        sub_db = db_path.parent / f"{cid}.db"
        if not sub_db.is_file():
            missing_sub_ids.append(cid)
            continue
        sub_db_ids.append(cid)
        try:
            sub_steps, sub_gens = _read_subagent_cost_rows(sub_db)
        except (sqlite3.Error, OSError):
            failed_sub_ids.append(cid)
            continue

        if depth < max_depth:
            for child_cid in _extract_subagent_ids_from_steps(sub_steps):
                if child_cid.lower() not in sub_ids_seen:
                    sub_ids_seen.add(child_cid.lower())
                    queue.append((child_cid, depth + 1))

        s_dates = _step_dates_from_steps(sub_steps)
        child_bucket, child_eval, child_unpriced = _evaluate_gens(
            sub_gens,
            catalog=catalog,
            step_dates=s_dates,
            override_date=override_date,
        )
        sub_evaluated.extend(child_eval)
        if child_bucket["calls"] > 0:
            sub_with_calls += 1
        for k in (
            "calls", "priced_calls", "unpriced_calls",
            "input_tokens", "cache_read_tokens", "output_tokens",
            "input_usd", "cache_read_usd", "output_usd", "total_usd", "cache_saved_usd",
        ):
            sub_bucket[k] += child_bucket[k]
        for mname, cnt in child_unpriced.items():
            merged_unpriced[mname] = merged_unpriced.get(mname, 0) + cnt

    total_calls = main_bucket["calls"] + sub_bucket["calls"]
    priced_calls = main_bucket["priced_calls"] + sub_bucket["priced_calls"]
    unpriced_calls = main_bucket["unpriced_calls"] + sub_bucket["unpriced_calls"]
    total_usd = round(main_bucket["total_usd"] + sub_bucket["total_usd"], 6) if priced_calls > 0 else None
    main_usd = round(main_bucket["total_usd"], 6) if main_bucket["priced_calls"] > 0 else (0.0 if priced_calls > 0 and main_bucket["calls"] == 0 else None)
    sub_usd = round(sub_bucket["total_usd"], 6) if sub_bucket["priced_calls"] > 0 else (0.0 if sub_bucket["calls"] == 0 else None)
    cache_saved_usd = round(main_bucket["cache_saved_usd"] + sub_bucket["cache_saved_usd"], 6) if priced_calls > 0 else 0.0
    unpriced_list = [k for k, _ in sorted(merged_unpriced.items(), key=lambda kv: kv[1], reverse=True)]

    all_evaluated = list(main_evaluated or []) + sub_evaluated
    by_model = _summarize_cost_by_model(all_evaluated, total_usd, catalog)
    subagent_by_model = _summarize_cost_by_model(sub_evaluated, sub_usd, catalog)

    return {
        "estimated": True,
        "currency": "USD",
        "total_usd": total_usd,
        "main_usd": main_usd,
        "subagent_usd": sub_usd,
        "cache_saved_usd": cache_saved_usd,
        "total_calls": total_calls,
        "priced_calls": priced_calls,
        "unpriced_calls": unpriced_calls,
        "unpriced_models": unpriced_list,
        "by_model": by_model,
        "subagent_by_model": subagent_by_model,
        "main_breakdown": _bucket_summary(main_bucket),
        "subagent_breakdown": _bucket_summary(sub_bucket, count=sub_with_calls),
        "subagent_conversation_ids": sub_db_ids,
        "missing_subagent_ids": missing_sub_ids,
        "incomplete": bool(failed_sub_ids),
        "failed_subagent_count": len(failed_sub_ids),
    }


# --- main --------------------------------------------------------------------


def _gen_last_step_index(gen: Msg) -> int | None:
    chat = gen.msg(1)
    if chat is None:
        return None
    for kv in chat.msgs(20):
        if kv.str(1) == "last_step_index":
            raw = kv.str(2).strip()
            if raw.isdigit():
                return int(raw)
            return None
    return None


def _select_active_gen(steps: list[Step], gens: list[Msg]) -> Msg | None:
    """Pick the latest generation that is not ahead of the current step list (e.g., after Undo)."""
    if not gens:
        return None
    if not steps:
        return gens[-1]
    max_step_idx = max(s.idx for s in steps)
    for g in reversed(gens):
        lsi = _gen_last_step_index(g)
        if lsi is None or lsi <= max_step_idx:
            return g
    return None


def _load_db_full(
    db_path: Path,
    catalog: dict | None = None,
    override_date: date | None = None,
) -> tuple[list[Step], Msg | None, dict, dict, list[dict], dict, dict]:
    def _read(conn: sqlite3.Connection):
        conn.execute("BEGIN")
        step_cols = [r[1] for r in conn.execute("PRAGMA table_info(steps)").fetchall()]
        if not step_cols:
            raise RuntimeError("Unsupported conversation database schema: missing steps table")
        col_names = set(step_cols)
        has_task_details = "task_details" in col_names
        has_error_details = "error_details" in col_names
        select_cols = [
            "idx" if "idx" in col_names else "rowid",
            "step_type" if "step_type" in col_names else "0",
            "status" if "status" in col_names else "0",
            "metadata" if "metadata" in col_names else "NULL",
            "step_payload" if "step_payload" in col_names else "NULL",
            "task_details" if has_task_details else "NULL",
            "error_details" if has_error_details else "NULL",
        ]
        rows = conn.execute(f"SELECT {', '.join(select_cols)} FROM steps ORDER BY 1").fetchall()
        has_gen = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gen_metadata'"
        ).fetchone()
        if has_gen:
            gen_cols = {r[1] for r in conn.execute("PRAGMA table_info(gen_metadata)").fetchall()}
            if "data" in gen_cols:
                order_col = "idx" if "idx" in gen_cols else "rowid"
                gen_rows = conn.execute(f"SELECT data FROM gen_metadata ORDER BY {order_col}").fetchall()
            else:
                gen_rows = []
        else:
            gen_rows = []
        conn.rollback()
        return rows, gen_rows

    rows, gen_rows = _query_ro_db(db_path, _read)
    steps = [
        Step(pos, r[4] or b"", r[1], r[2], r[3], idx=r[0], task_details=r[5], error_details=r[6])
        for pos, r in enumerate(rows)
    ]
    gens = [Msg(r[0]) for r in gen_rows]
    step_dates = _step_dates_from_steps(steps)
    main_bucket, evaluated, unpriced_models = _evaluate_gens(
        gens,
        catalog=catalog,
        step_dates=step_dates,
        override_date=override_date,
    )
    model_usage = _model_usage_from_evaluated(evaluated, catalog=catalog)
    cost_stats = collect_session_cost(
        db_path,
        steps,
        gens,
        main_bucket,
        unpriced_models,
        catalog=catalog,
        override_date=override_date,
        main_evaluated=evaluated,
    )
    return (
        steps,
        _select_active_gen(steps, gens),
        collect_cache_stats(gens),
        collect_output_stats(gens),
        model_usage,
        cost_stats,
        collect_speed_stats(gens),
    )


def load_db(db_path: Path, catalog: dict | None = None) -> tuple[list[Step], Msg | None, dict, dict, list[dict]]:
    steps, latest_gen, cache_stats, output_stats, model_usage, _, _ = _load_db_full(db_path, catalog=catalog)
    return steps, latest_gen, cache_stats, output_stats, model_usage


def load(db_path: Path) -> tuple[list[Step], Msg | None]:
    steps, latest_gen, _, _, _ = load_db(db_path)
    return steps, latest_gen


# Native context windows from the model catalog, which the CLI fetches from the server
# (ModelInfo.max_tokens) and never writes to disk. Matched by model ID prefix; the first
# match wins. Add a row when a new model family ships.
NATIVE_CONTEXT_WINDOWS = [
    ("claude-opus-4-6", 250_000),
    ("claude-sonnet-4-6", 250_000),
    ("claude-", 1_000_000),
    ("gpt-oss-", 131_072),
    ("gemini-", 1_048_576),
]

EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
EFFORT_DISPLAY_RE = re.compile(
    r"\s*\((minimal|low|medium|high|xhigh|extra\s+high|max)\)\s*$",
    re.IGNORECASE,
)
WORD_LABELS = {"gpt": "GPT", "oss": "OSS"}


def model_id(gen: Msg | None) -> str:
    planner = gen.msg(3) if gen is not None else None
    return planner.str(28) if planner is not None else ""  # planner_config.model_name


def model_label(slug: str) -> str:
    """Catalog-style label from a model ID: gemini-3.8-flash-medium -> Gemini 3.8 Flash (Medium)."""
    parts = slug.split("-")
    effort = parts.pop().capitalize() if len(parts) > 1 and parts[-1] in EFFORT_LEVELS else ""
    words: list[str] = []
    for p in parts:
        if p.isdigit() and words and words[-1][-1:].isdigit():
            words[-1] += "." + p  # claude-opus-5-5 -> 5.5
        elif p in WORD_LABELS:
            words.append(WORD_LABELS[p])
        elif p[:1].isdigit():
            words.append(p.upper())  # 120b -> 120B
        else:
            words.append(p.capitalize())
    label = " ".join(words).replace("GPT OSS", "GPT-OSS")
    return f"{label} ({effort})" if effort else label


def model_name(gen: Msg | None) -> str:
    chat = gen.msg(1) if gen is not None else None
    if chat is not None and chat.str(21):  # model_display_name
        return chat.str(21)
    if model_id(gen):
        return model_label(model_id(gen))
    if chat is not None and chat.str(19):  # response_model
        return chat.str(19)
    return "Unknown Model"


def _model_slug_candidates(gen: Msg | None) -> tuple[str, ...]:
    """Ordered model ID candidates for limit/catalog lookup, resilient to stripped planner_config."""
    if gen is None:
        return ()
    out: list[str] = []
    seen: set[str] = set()

    def _add(val: str) -> None:
        v = val.strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)

    _add(model_id(gen))
    chat = gen.msg(1)
    if chat is not None:
        resp_mid = chat.str(19).strip()
        if resp_mid:
            _add(resp_mid)
            _add(_normalize_pricing_slug(resp_mid))
        disp = chat.str(21).strip()
        if disp:
            _add(_normalize_pricing_display(disp))
    return tuple(out)


def _lookup_catalog_entry(catalog: dict | None, gen: Msg | None) -> dict:
    """Look up live catalog metadata even when planner_config was stripped (e.g., after Undo)."""
    if not catalog or gen is None:
        return {}
    candidates = _model_slug_candidates(gen)
    for slug in candidates:
        raw = catalog.get(slug)
        if isinstance(raw, dict):
            return raw

    chat = gen.msg(1)
    disp = chat.str(21).strip() if chat is not None else ""
    if disp:
        disp_lower = disp.lower()
        exact_matches = [
            v for v in catalog.values()
            if isinstance(v, dict) and _safe_str(v.get("displayName")).lower() == disp_lower
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if exact_matches:
            maxes = {_safe_max_tokens(v.get("maxTokens")) for v in exact_matches}
            maxes.discard(0)
            if len(maxes) == 1:
                return {"maxTokens": next(iter(maxes)), "displayName": disp}

    # Family-level fallback (e.g. response_model='gemini-4-neon' -> catalog keys 'gemini-4-neon-*')
    for slug in candidates:
        fam = base_model_id(slug).lower()
        if not fam:
            continue
        fam_maxes = {
            _safe_max_tokens(v.get("maxTokens"))
            for k, v in catalog.items()
            if isinstance(k, str) and isinstance(v, dict) and base_model_id(k).lower() == fam
        }
        fam_maxes.discard(0)
        if len(fam_maxes) == 1:
            return {"maxTokens": next(iter(fam_maxes)), "displayName": ""}
    return {}


def context_limit(gen: Msg | None) -> int:
    """The model's native window, like the CLI; else the request budget (max_context_tokens)."""
    for slug in _model_slug_candidates(gen):
        low = slug.lower()
        for prefix, tokens in NATIVE_CONTEXT_WINDOWS:
            if low.startswith(prefix):
                return tokens
    return request_budget(gen)


def request_budget(gen: Msg | None) -> int:
    chat = gen.msg(1) if gen is not None else None
    start = chat.msg(9) if chat is not None else None
    cwm = start.msg(10) if start is not None else None
    return cwm.int(4, signed32=True) if cwm is not None else 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Analyze Antigravity 2.0 conversation context usage.")
    parser.add_argument("--conversation-id", help="Conversation UUID (auto-detected if omitted)")
    parser.add_argument("--db", help="Path to a conversation .db file (e.g. one from ~/.gemini/antigravity-cli/conversations)")
    parser.add_argument("--workspace", help="Active workspace path", default=None)
    parser.add_argument("--limit", type=int, default=0, help="Context window token limit override")
    parser.add_argument("--app-dir", default="antigravity", help="App data folder under ~/.gemini (default: antigravity)")
    parser.add_argument("--gemini-dir", default=None, help="Explicit .gemini config root directory override")
    parser.add_argument("--width", type=int, default=100, help="Terminal width used for the grid layout")
    parser.add_argument("--color", action="store_true", help="Color the grid segments with ANSI escapes")
    parser.add_argument("--json", action="store_true", help="Output JSON data")
    parser.add_argument("--model-catalog", help='JSON file of {"<model id>": {"maxTokens": n, "displayName": "..."}} '
                        "from the app's live model catalog; overrides the built-in limits and labels")
    parser.add_argument("--price-date", default=None, help="Override pricing date (YYYY-MM-DD) for deterministic estimates")
    args = parser.parse_args()

    override_date: date | None = None
    if args.price_date:
        try:
            override_date = date.fromisoformat(args.price_date)
        except ValueError as exc:
            raise SystemExit(f"Invalid --price-date '{args.price_date}' (expected YYYY-MM-DD): {exc}") from exc

    effective_app_dir = args.app_dir
    if args.db:
        db_path = Path(args.db).expanduser()
        conv_id = db_path.stem
        effective_gemini_dir = args.gemini_dir
    else:
        effective_gemini_dir = args.gemini_dir or os.environ.get("ANTIGRAVITY_GEMINI_DIR")
        env_app_data = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", "").strip()
        if not effective_gemini_dir and env_app_data and args.app_dir == "antigravity":
            app_data_path = Path(env_app_data).expanduser()
            effective_gemini_dir = str(app_data_path.parent)
            effective_app_dir = app_data_path.name or "antigravity"
            conv_dir = app_data_path / "conversations"
        else:
            gemini_root = Path(effective_gemini_dir).expanduser() if effective_gemini_dir else (Path.home() / ".gemini")
            conv_dir = gemini_root / effective_app_dir / "conversations"
        conv_id = args.conversation_id
        if not conv_id:
            dbs = sorted(conv_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not dbs:
                raise SystemExit("No conversation databases found.")
            conv_id = dbs[0].stem
        db_path = conv_dir / f"{conv_id}.db"
    if not db_path.is_file():
        raise SystemExit(f"Conversation database not found: {db_path}")

    catalog = {}
    if args.model_catalog:
        try:
            with open(args.model_catalog, encoding="utf-8") as fh:
                loaded_catalog = json.load(fh)
            if isinstance(loaded_catalog, dict):
                catalog = loaded_catalog
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[status-bar] ignoring invalid model catalog {args.model_catalog}: {exc}", file=sys.stderr)
    steps, gen, cache_stats, output_stats, model_usage, cost_stats, speed_stats = _load_db_full(
        db_path, catalog=catalog, override_date=override_date
    )
    catalog_entry = _lookup_catalog_entry(catalog, gen)
    cat_max = _safe_max_tokens(catalog_entry.get("maxTokens"))
    limit = args.limit if args.limit > 0 else cat_max or context_limit(gen)
    st = collect_stats(steps, gen, limit)
    model = _safe_str(catalog_entry.get("displayName")) or model_name(gen)
    system_files = detect_system_files(os.path.expanduser("~"), args.workspace, effective_app_dir, gemini_dir=effective_gemini_dir)

    if args.json:
        print(json.dumps({
            "conversation_id": conv_id,
            "model": model,
            "heading": stats_heading(st),
            "grid_segments": grid_segments(st),
            "checkpoint_view": checkpoint_view(st),
            "text": render(st, model, system_files, args.width, False, cost_stats=cost_stats),
            "model_id": model_id(gen) or ((gen.msg(1).str(19) if gen and gen.msg(1) else "")),
            "request_budget_tokens": request_budget(gen),
            "used_tokens": st["total"],
            "limit_tokens": limit,
            "used_pct": round(min(100.0, pct(st["total"], limit)), 1),
            "free_tokens": limit - st["total"] if st["total"] < limit else 0,
            "has_breakdown": st["has_breakdown"],
            "compacted": st["compacted"],
            "checkpoint_buffer": st["checkpoint"],
            "categories": [
                {"icon": icon, "name": name, "key": key, "tokens": st[key], "pct": round(pct(st[key], limit), 1)}
                for icon, name, key, always in CATEGORIES
                if always or st[key]
            ],
            "active_checkpoint": st["active_checkpoint"],
            "checkpoints": st["checkpoints"],
            "knowledge_files": [{"path": p, "tokens": t} for p, t in st["knowledge_files"]],
            "artifact_files": [{"path": p, "tokens": t} for p, t in st["artifact_files"]],
            "skill_files": [{"path": p, "tokens": t} for p, t in st["skill_files"]],
            "system_files": [{"path": p} for p, _ in system_files],
            "background_tasks": extract_background_tasks(steps, db_path=db_path),
            "cache_stats": cache_stats,
            "output_stats": output_stats,
            "speed_stats": speed_stats,
            "model_usage": model_usage,
            "cost_stats": cost_stats,
        }, indent=2))
        return

    print(render(st, model, system_files, args.width, args.color, cost_stats=cost_stats))


if __name__ == "__main__":
    main()
