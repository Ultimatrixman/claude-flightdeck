#!/usr/bin/env python3
"""Tests for ctxmon. Run with the SAME interpreter the hooks use:

    python3 plugins/ctxmon/tests/test_ctxmon.py

Stdlib unittest on purpose: the hooks run under the bare interpreter, so a
test suite that needs a virtualenv would be testing a different environment
than the one that ships.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctxmon  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = {k: getattr(ctxmon, k) for k in
                       ("STATE_DIR", "SESSIONS_DIR", "SL_DIR", "QUOTA_HIST",
                        "PROJECTS_DIR")}
        ctxmon.STATE_DIR = root / "state"
        ctxmon.SESSIONS_DIR = ctxmon.STATE_DIR / "sessions"
        ctxmon.SL_DIR = ctxmon.STATE_DIR / "sl"
        ctxmon.QUOTA_HIST = ctxmon.STATE_DIR / "quota-history.jsonl"
        ctxmon.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.root = root

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(ctxmon, k, v)
        self.tmp.cleanup()

    def write_transcript(self, records, name="s.jsonl") -> str:
        p = self.root / name
        with p.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return str(p)


def assistant(usage=None, content=None, ts=None):
    r = {"type": "assistant", "timestamp": ts or "2026-08-10T12:00:00.000Z",
         "message": {"content": content if content is not None else []}}
    if usage:
        r["message"]["usage"] = usage
    return r


def usage(ctx, out=0):
    return {"input_tokens": 1, "cache_creation_input_tokens": ctx - 1,
            "cache_read_input_tokens": 0, "output_tokens": out}


class TestBands(Base):
    def test_boundaries_map_to_expected_names(self):
        self.assertEqual(ctxmon._band(0.0)[0], "NORMAL")
        self.assertEqual(ctxmon._band(0.49)[0], "NORMAL")
        self.assertEqual(ctxmon._band(0.50)[0], "CONSERVE")
        self.assertEqual(ctxmon._band(0.69)[0], "CONSERVE")
        self.assertEqual(ctxmon._band(0.70)[0], "HARVEST")
        self.assertEqual(ctxmon._band(0.84)[0], "HARVEST")
        self.assertEqual(ctxmon._band(0.85)[0], "HANDOFF")
        self.assertEqual(ctxmon._band(5.0)[0], "HANDOFF")

    def test_harvest_band_names_the_durable_artifacts(self):
        advice = ctxmon._band(0.75)[1]
        for word in ("memory", "task", "doc"):
            self.assertIn(word, advice.lower())


class TestBurnRate(Base):
    def test_none_when_series_too_short(self):
        self.assertIsNone(ctxmon.burn_per_hour([]))
        self.assertIsNone(ctxmon.burn_per_hour([{"t": 1000.0, "f": 10}]))

    def test_none_when_span_under_five_minutes(self):
        now = time.time()
        hist = [{"t": now - 60, "f": 10}, {"t": now, "f": 12}]
        self.assertIsNone(ctxmon.burn_per_hour(hist))

    def test_measures_points_per_hour(self):
        now = time.time()
        hist = [{"t": now - 3600, "f": 10.0}, {"t": now, "f": 30.0}]
        self.assertAlmostEqual(ctxmon.burn_per_hour(hist), 20.0, places=3)

    def test_window_reset_discards_the_previous_window(self):
        """A reset drops the percentage; measuring across it yields a negative
        burn rate, which would read as 'quota is refilling'."""
        now = time.time()
        hist = [{"t": now - 7200, "f": 80.0},
                {"t": now - 3600, "f": 5.0},    # window reset here
                {"t": now, "f": 25.0}]
        self.assertAlmostEqual(ctxmon.burn_per_hour(hist), 20.0, places=3)


class TestQuotaSampling(Base):
    def _sl(self, five, seven=1.0):
        return {"rate_limits": {"five_hour": {"used_percentage": five,
                                              "resets_at": 1786396800},
                                "seven_day": {"used_percentage": seven,
                                              "resets_at": 1786780800}}}

    def test_appends_then_dedupes_unchanged_readings(self):
        ctxmon.quota_sample(self._sl(10.0))
        ctxmon.quota_sample(self._sl(10.0))
        ctxmon.quota_sample(self._sl(10.0))
        rows = ctxmon.QUOTA_HIST.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), 1, "unchanged readings must not append")
        ctxmon.quota_sample(self._sl(11.0))
        rows = ctxmon.QUOTA_HIST.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), 2)

    def test_ignores_payload_without_rate_limits(self):
        ctxmon.quota_sample({})
        ctxmon.quota_sample({"rate_limits": {}})
        self.assertFalse(ctxmon.QUOTA_HIST.exists())


class TestStaleQuota(Base):
    """Regression, measured 2026-08-11: 26 of 28 captured statusline payloads
    were stale, several by 200+ hours, because Claude Code writes one for every
    session it LISTS, not only live ones. Sampling them injected phantom window
    resets into the burn series and put a dead window's percentage into a live
    prompt."""

    def _write_sl(self, sid8, resets_in_h, pct=50.0):
        ctxmon.SL_DIR.mkdir(parents=True, exist_ok=True)
        (ctxmon.SL_DIR / f"{sid8}.json").write_text(json.dumps({
            "model": {"display_name": "Opus 5"},
            "rate_limits": {
                "five_hour": {"used_percentage": pct,
                              "resets_at": time.time() + resets_in_h * 3600},
                "seven_day": {"used_percentage": 20.0}}}), encoding="utf-8")

    def test_fresh_payload_keeps_its_quota(self):
        self._write_sl("aaaa1111", resets_in_h=2)
        d = ctxmon._statusline_payload("aaaa1111")
        self.assertIn("rate_limits", d)
        self.assertFalse(d.get("_quota_stale"))

    def test_past_reset_drops_quota_but_keeps_the_rest(self):
        self._write_sl("bbbb2222", resets_in_h=-8)
        d = ctxmon._statusline_payload("bbbb2222")
        self.assertNotIn("rate_limits", d)
        self.assertTrue(d["_quota_stale"])
        self.assertEqual(d["model"]["display_name"], "Opus 5",
                         "only quota goes stale; the model name is still true")

    def test_stale_payload_is_never_sampled_into_the_burn_series(self):
        self._write_sl("cccc3333", resets_in_h=-8)
        ctxmon.quota_sample(ctxmon._statusline_payload("cccc3333"))
        self.assertFalse(ctxmon.QUOTA_HIST.exists())

    def test_history_discards_rows_whose_reset_had_already_passed(self):
        now = time.time()
        ctxmon.STATE_DIR.mkdir(parents=True, exist_ok=True)
        ctxmon.QUOTA_HIST.write_text("\n".join(json.dumps(r) for r in [
            {"t": now - 600, "f": 10.0, "s": 5, "fr": now + 3600},   # good
            {"t": now - 300, "f": 61.0, "s": 22, "fr": now - 28800},  # stale
            {"t": now - 60, "f": 12.0, "s": 5, "fr": now + 3600},    # good
        ]) + "\n", encoding="utf-8")
        rows = ctxmon.quota_history(hours=6)
        self.assertEqual([r["f"] for r in rows], [10.0, 12.0])

    def test_stale_dip_is_not_mistaken_for_a_window_reset(self):
        """Two live sessions tick at different moments, so a fresh 10% reading
        can be followed 11s later by an older session's 3%. Same resets_at =
        stale, not a reset."""
        now, fr = time.time(), time.time() + 4 * 3600
        rows = [{"t": now - 3600, "f": 10.0, "fr": fr},
                {"t": now - 3589, "f": 3.0, "fr": fr},   # stale, same window
                {"t": now, "f": 30.0, "fr": fr}]
        self.assertEqual([r["f"] for r in ctxmon.dedupe_quota(rows)],
                         [10.0, 30.0])
        self.assertAlmostEqual(ctxmon.burn_per_hour(rows), 20.0, places=2)

    def test_collapse_reads_as_a_reset_when_resets_at_is_missing(self):
        """Legacy rows carry no resets_at. Treating every drop as stale there
        would discard a real reset and report the OLD window's burn as current."""
        now = time.time()
        rows = [{"t": now - 7200, "f": 80.0},
                {"t": now - 3600, "f": 2.0},   # collapse = reset
                {"t": now, "f": 22.0}]
        self.assertEqual(len(ctxmon.dedupe_quota(rows)), 3)
        rows2 = [{"t": now - 3600, "f": 40.0},
                 {"t": now - 3500, "f": 37.0},  # modest dip = stale
                 {"t": now, "f": 60.0}]
        self.assertEqual([r["f"] for r in ctxmon.dedupe_quota(rows2)],
                         [40.0, 60.0])

    def test_real_reset_still_recognised_by_a_moved_resets_at(self):
        now = time.time()
        rows = [{"t": now - 7200, "f": 80.0, "fr": now - 3600},
                {"t": now - 3600, "f": 5.0, "fr": now + 5400},  # real reset
                {"t": now, "f": 25.0, "fr": now + 5400}]
        self.assertEqual(len(ctxmon.dedupe_quota(rows)), 3)
        self.assertAlmostEqual(ctxmon.burn_per_hour(rows), 20.0, places=2)

    def test_snapshot_flags_stale_quota(self):
        self._write_sl("dddd4444", resets_in_h=-1)
        p = self.write_transcript([assistant(usage(1000))])
        saved = ctxmon.HUD_DIR
        ctxmon.HUD_DIR = self.root / "no-hud"
        try:
            snap = ctxmon.build_snapshot(
                {"session_id": "dddd4444xx", "transcript_path": p, "cwd": ""})
        finally:
            ctxmon.HUD_DIR = saved
        self.assertTrue(snap["quota_stale"])
        self.assertIsNone(snap["rate_5h_pct"])
        self.assertNotIn("quota", ctxmon.status_line(snap),
                         "a dead window must not be rendered as current")


class TestTranscriptScan(Base):
    def test_tracks_context_output_and_agents(self):
        recs = [
            assistant(usage(1000, out=50)),
            assistant(content=[{"type": "tool_use", "id": "t1", "name": "Agent",
                                "input": {"description": "go"}}]),
            assistant(usage(2000, out=70)),
        ]
        p = self.write_transcript(recs)
        st = ctxmon._scan_transcript(p, "sid1")
        self.assertEqual(st["ctx"], 2000)
        self.assertEqual(st["out"], 120)
        self.assertEqual(st["agents_total"], 1)
        self.assertEqual(st["open_agents"], ["t1"])

    def test_agent_closes_when_its_result_arrives(self):
        recs = [
            assistant(content=[{"type": "tool_use", "id": "t1", "name": "Agent",
                                "input": {}}]),
            {"type": "user", "timestamp": "2026-08-10T12:00:01.000Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
        ]
        p = self.write_transcript(recs)
        st = ctxmon._scan_transcript(p, "sid2")
        self.assertEqual(st["open_agents"], [])
        self.assertEqual(st["agents_total"], 1)

    def test_duplicate_usage_records_do_not_double_count_output(self):
        """Streaming writes the same usage several times; counting each one
        would inflate the session output total."""
        recs = [assistant(usage(1000, out=50)), assistant(usage(1000, out=50)),
                assistant(usage(1000, out=50))]
        p = self.write_transcript(recs)
        st = ctxmon._scan_transcript(p, "sid3")
        self.assertEqual(st["out"], 50)

    def test_incremental_resume_matches_a_full_scan(self):
        recs = [assistant(usage(1000, out=50))]
        p = self.write_transcript(recs)
        first = ctxmon._scan_transcript(p, "sid4")
        self.assertEqual(first["ctx"], 1000)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(assistant(usage(3000, out=80))) + "\n")
        second = ctxmon._scan_transcript(p, "sid4")
        self.assertEqual(second["ctx"], 3000)
        self.assertEqual(second["out"], 130)

    def test_shrunk_file_forces_a_full_rescan(self):
        recs = [assistant(usage(5000, out=90)), assistant(usage(9000, out=10))]
        p = self.write_transcript(recs)
        ctxmon._scan_transcript(p, "sid5")
        self.write_transcript([assistant(usage(1200, out=5))], name="s.jsonl")
        st = ctxmon._scan_transcript(p, "sid5")
        self.assertEqual(st["ctx"], 1200, "a rewritten transcript must rescan")

    def test_missing_transcript_is_not_an_error(self):
        st = ctxmon._scan_transcript(str(self.root / "nope.jsonl"), "sid6")
        self.assertEqual(st["ctx"], 0)


class TestUncounted(Base):
    def test_bytes_after_last_usage_become_a_token_estimate(self):
        p = self.root / "t.jsonl"
        p.write_bytes(b"x" * 4700)
        est = ctxmon._uncounted(str(p), 0)
        self.assertEqual(est, 0, "no usage record yet means no baseline")
        est = ctxmon._uncounted(str(p), 700)
        self.assertEqual(est, int(4000 / ctxmon.BYTES_PER_TOKEN))

    def test_estimate_is_an_upper_bound_by_construction(self):
        self.assertLess(ctxmon.BYTES_PER_TOKEN, 7.47,
                        "constant must sit below the measured median so the "
                        "estimate over-states tokens and warns early")


class TestTrail(Base):
    def test_extracts_files_commands_and_agents(self):
        recs = [
            {"type": "user", "timestamp": "2026-08-10T12:00:00.000Z",
             "message": {"content": "find the bug in adapters"}},
            assistant(content=[
                {"type": "tool_use", "id": "a", "name": "Read",
                 "input": {"file_path": "/proj/x.py"}},
                {"type": "tool_use", "id": "b", "name": "Bash",
                 "input": {"command": "pytest -q", "description": "run tests"}},
                {"type": "tool_use", "id": "c", "name": "Agent",
                 "input": {"description": "audit adapters",
                           "subagent_type": "Explore"}},
            ]),
        ]
        trail = ctxmon.build_trail(self.write_transcript(recs))
        self.assertIn("find the bug in adapters", trail)
        self.assertIn("/proj/x.py", trail)
        self.assertIn("pytest -q", trail)
        self.assertIn("audit adapters", trail)
        self.assertIn("Explore", trail)

    def test_harness_traffic_is_not_recorded_as_a_prompt(self):
        """Task notifications and slash-command echoes arrive as user-role
        string content. 20 of 47 entries in the first real harvest were those."""
        def user(text, ts="2026-08-10T12:00:00.000Z"):
            return {"type": "user", "timestamp": ts, "message": {"content": text}}
        trail = ctxmon.build_trail(self.write_transcript([
            user("fix the adapter please"),
            user("<task-notification>\n<task-id>abc</task-id>\n"),
            user("<local-command-stdout>Set model to Opus</local-command-stdout>"),
            user("<system-reminder>do a thing</system-reminder>"),
            user("now run the tests"),
        ]))
        self.assertIn("fix the adapter please", trail)
        self.assertIn("now run the tests", trail)
        for noise in ("task-notification", "local-command", "system-reminder"):
            self.assertNotIn(noise, trail)
        self.assertIn("2 prompts", trail)

    def test_repeated_identical_prompt_recorded_once(self):
        def user(text):
            return {"type": "user", "timestamp": "2026-08-10T12:00:00.000Z",
                    "message": {"content": text}}
        trail = ctxmon.build_trail(self.write_transcript(
            [user("same text"), user("same text"), user("same text")]))
        self.assertIn("1 prompts", trail)

    def test_truncation_is_declared_never_silent(self):
        """A cap that hides its own effect turns a partial record into a
        confident-looking complete one."""
        recs = [assistant(content=[
            {"type": "tool_use", "id": f"c{i}", "name": "Bash",
             "input": {"command": f"echo {i}"}}]) for i in range(30)]
        trail = ctxmon.build_trail(self.write_transcript(recs), limit=10)
        self.assertIn("20 of 30 entries omitted", trail)
        self.assertIn("30 commands", trail, "totals must state the real count")

    def test_prompt_truncation_keeps_both_ends(self):
        """Intent lives at the START of a session. The first version kept only
        the tail and dropped the 7 earliest prompts of 47."""
        recs = [{"type": "user", "timestamp": "2026-08-10T12:00:00.000Z",
                 "message": {"content": f"prompt number {i}"}} for i in range(40)]
        trail = ctxmon.build_trail(self.write_transcript(recs), limit=9)
        self.assertIn("prompt number 0", trail, "the opening intent must survive")
        self.assertIn("prompt number 39", trail, "the latest state must survive")
        self.assertIn("omitted from the middle", trail)

    def test_no_truncation_note_when_everything_fits(self):
        recs = [assistant(content=[
            {"type": "tool_use", "id": "c1", "name": "Bash",
             "input": {"command": "echo hi"}}])]
        trail = ctxmon.build_trail(self.write_transcript(recs), limit=400)
        self.assertNotIn("omitted", trail)

    def test_empty_for_a_missing_transcript(self):
        self.assertEqual(ctxmon.build_trail(str(self.root / "gone.jsonl")), "")


class TestHudSource(Base):
    def test_matches_by_plaintext_path_not_hash(self):
        (ctxmon.STATE_DIR / "x").mkdir(exist_ok=True)
        hud = self.root / "hud"
        (hud / "transcript-cache").mkdir(parents=True)
        (hud / "context-cache").mkdir(parents=True)
        saved = ctxmon.HUD_DIR
        ctxmon.HUD_DIR = hud
        try:
            tpath = r"C:\Users\p\.claude\projects\X\abc.jsonl"
            (hud / "transcript-cache" / "deadbeef.json").write_text(
                json.dumps({"transcriptPath": tpath}), encoding="utf-8")
            (hud / "context-cache" / "deadbeef.json").write_text(
                json.dumps({"used_percentage": 42,
                            "context_window_size": 1000000,
                            "current_usage": {"cache_read_input_tokens": 420000}}),
                encoding="utf-8")
            got = ctxmon._hud_cache_for(tpath.replace("\\", "/"))
            self.assertIsNotNone(got, "forward-slash spelling must still match")
            self.assertEqual(got["used_percentage"], 42)
            self.assertIsNone(ctxmon._hud_cache_for("C:/other/none.jsonl"))
        finally:
            ctxmon.HUD_DIR = saved


class TestSnapshot(Base):
    def test_falls_back_to_transcript_when_hud_absent(self):
        p = self.write_transcript([assistant(usage(50000, out=100))])
        saved = ctxmon.HUD_DIR
        ctxmon.HUD_DIR = self.root / "no-hud"
        try:
            snap = ctxmon.build_snapshot(
                {"session_id": "abcdef1234", "transcript_path": p, "cwd": "C:/x"},
                phase="busy")
        finally:
            ctxmon.HUD_DIR = saved
        self.assertEqual(snap["source"], "transcript")
        self.assertEqual(snap["ctx_tokens"], 50000)
        self.assertEqual(snap["phase"], "busy")
        self.assertEqual(snap["sid8"], "abcdef12")

    def test_headroom_is_measured_against_autocompact_not_the_window(self):
        p = self.write_transcript([assistant(usage(100000))])
        saved = ctxmon.HUD_DIR
        ctxmon.HUD_DIR = self.root / "no-hud"
        try:
            snap = ctxmon.build_snapshot(
                {"session_id": "s", "transcript_path": p, "cwd": ""})
        finally:
            ctxmon.HUD_DIR = saved
        window = snap["ctx_window"]
        self.assertEqual(snap["usable_tokens"],
                         int(window * (1 - ctxmon.AUTOCOMPACT_BUFFER)))
        self.assertLess(snap["usable_tokens"], window)

    def test_status_line_is_compact(self):
        snap = {"ctx_tokens": 181000, "ctx_window": 1000000, "ctx_pct": 18.1,
                "headroom_tokens": 653000, "out_total": 58000, "band": "NORMAL",
                "advice": "", "uncounted_est": 0, "agents_running": 0}
        line = ctxmon.status_line(snap)
        self.assertLess(len(line), 200, "the per-turn line is paid every turn")
        self.assertIn("181k", line)
        self.assertIn("NORMAL", line)


class TestAgentDuration(Base):
    """Regression: a streaming response rewrites the same assistant record
    several times, so one tool_use block appears repeatedly. Keeping the LAST
    occurrence as the start time collapsed every measured agent duration to a
    couple of seconds and made the planner's verdict meaningless."""

    def _project_transcript(self, records):
        proj = self.root / "projects" / "P"
        proj.mkdir(parents=True, exist_ok=True)
        p = proj / "sess.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        ctxmon.PROJECTS_DIR = self.root / "projects"
        return p

    def test_duration_measured_from_first_occurrence(self):
        blk = [{"type": "tool_use", "id": "t1", "name": "Agent", "input": {}}]
        self._project_transcript([
            assistant(content=blk, ts="2026-08-10T12:00:00.000Z"),
            assistant(content=blk, ts="2026-08-10T12:04:00.000Z"),  # duplicate
            assistant(content=blk, ts="2026-08-10T12:04:50.000Z"),  # duplicate
            {"type": "user", "timestamp": "2026-08-10T12:05:00.000Z",
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": "t1"}]}},
        ])
        w = ctxmon.scan_window(0)
        self.assertEqual(w["agents"], 1, "duplicates must not inflate the count")
        self.assertEqual(len(w["agent_durations"]), 1)
        self.assertAlmostEqual(w["agent_durations"][0], 300.0, places=1)

    def test_background_agent_measured_from_task_notification(self):
        """A background agent's tool_result returns in seconds carrying only
        the agent's id. Real runtime is only in the later task-notification."""
        self._project_transcript([
            assistant(content=[{"type": "tool_use", "id": "bg1", "name": "Agent",
                                "input": {}}], ts="2026-08-10T12:00:00.000Z"),
            {"type": "user", "timestamp": "2026-08-10T12:00:02.000Z",
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": "bg1"}]}},
            {"type": "queue-operation", "operation": "enqueue",
             "timestamp": "2026-08-10T12:14:00.000Z",
             "content": "<task-notification>\n<task-id>abc</task-id>\n"
                        "<tool-use-id>bg1</tool-use-id>\n"
                        "<status>completed</status>\n"},
        ])
        w = ctxmon.scan_window(0)
        self.assertEqual(len(w["agent_durations"]), 1)
        self.assertAlmostEqual(w["agent_durations"][0], 840.0, places=1)
        self.assertEqual(w["open_agents"], 0)

    def test_synchronous_agent_still_measured_from_tool_result(self):
        """With no notification, tool_result IS the true end."""
        self._project_transcript([
            assistant(content=[{"type": "tool_use", "id": "sy1", "name": "Agent",
                                "input": {}}], ts="2026-08-10T12:00:00.000Z"),
            {"type": "user", "timestamp": "2026-08-10T12:03:00.000Z",
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": "sy1"}]}},
        ])
        w = ctxmon.scan_window(0)
        self.assertAlmostEqual(w["agent_durations"][0], 180.0, places=1)

    def test_unfinished_agent_counts_as_open_not_as_a_duration(self):
        self._project_transcript([
            assistant(content=[{"type": "tool_use", "id": "t9", "name": "Agent",
                                "input": {}}], ts="2026-08-10T12:00:00.000Z"),
        ])
        w = ctxmon.scan_window(0)
        self.assertEqual(w["open_agents"], 1)
        self.assertEqual(w["agent_durations"], [])

    def test_scan_transcript_does_not_double_count_duplicate_agents(self):
        blk = [{"type": "tool_use", "id": "d1", "name": "Agent", "input": {}}]
        p = self.write_transcript([assistant(content=blk),
                                   assistant(content=blk),
                                   assistant(content=blk)])
        st = ctxmon._scan_transcript(p, "dup1")
        self.assertEqual(st["agents_total"], 1)
        self.assertEqual(st["open_agents"], ["d1"])

    def test_duplicate_after_result_does_not_reopen_a_closed_agent(self):
        blk = [{"type": "tool_use", "id": "d2", "name": "Agent", "input": {}}]
        p = self.write_transcript([
            assistant(content=blk),
            {"type": "user", "timestamp": "2026-08-10T12:00:01.000Z",
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": "d2"}]}},
            assistant(content=blk),  # late duplicate of the original record
        ])
        st = ctxmon._scan_transcript(p, "dup2")
        self.assertEqual(st["open_agents"], [],
                         "a finished agent must stay finished")


class TestHooksFailOpen(Base):
    """A hook that raises must never wedge a session. These drive the real
    entry point in a subprocess, which is the only honest test of that."""

    def _run(self, cmd, stdin_text):
        # A subprocess does NOT inherit the monkeypatched module globals, so it
        # would write into the real ~/.claude/ctxmon/state and leave junk
        # sessions behind. The env var is the only isolation that crosses a
        # process boundary.
        env = dict(os.environ, RG_CTXMON_STATE_DIR=str(self.root / "sub-state"))
        return subprocess.run(
            [sys.executable, str(Path(ctxmon.__file__)), cmd],
            input=stdin_text, capture_output=True, text=True, timeout=60,
            env=env)

    def test_malformed_stdin_exits_zero(self):
        for cmd in ("prompt", "tick", "stop", "precompact", "sessionstart",
                    "sessionend"):
            r = self._run(cmd, "this is not json{{{")
            self.assertEqual(r.returncode, 0, f"{cmd} must exit 0: {r.stderr}")

    def test_empty_stdin_exits_zero(self):
        for cmd in ("prompt", "tick", "stop"):
            r = self._run(cmd, "")
            self.assertEqual(r.returncode, 0, f"{cmd} must exit 0: {r.stderr}")

    def test_nonexistent_transcript_exits_zero_and_says_nothing(self):
        payload = json.dumps({"session_id": "zz", "cwd": "C:/x",
                              "transcript_path": "C:/nope/missing.jsonl"})
        r = self._run("prompt", payload)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "",
                         "with no readable source it must stay silent, not guess")

    def test_disable_switch_silences_every_hook(self):
        env = dict(os.environ, RG_CTXMON_DISABLE="1",
                   RG_CTXMON_STATE_DIR=str(self.root / "sub-state"))
        r = subprocess.run([sys.executable, str(Path(ctxmon.__file__)), "prompt"],
                           input="{}", capture_output=True, text=True, env=env,
                           timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_subprocess_tests_do_not_touch_the_real_state_dir(self):
        """Guards the isolation above: without the env var these tests wrote
        'zz' and 'anon' sessions into the live ~/.claude/ctxmon/state."""
        self._run("prompt", json.dumps({"session_id": "zzguard", "cwd": "x"}))
        leaked = Path(os.path.expanduser("~")) / ".claude" / "ctxmon" / \
            "state" / "sessions" / "zzguard1.json"
        self.assertFalse(leaked.exists())
        self.assertTrue((self.root / "sub-state").exists())

    def test_tick_is_silent_inside_a_subagent(self):
        payload = json.dumps({"session_id": "zz", "agent_id": "sub-1",
                              "transcript_path": "x"})
        r = self._run("tick", payload)
        self.assertEqual(r.stdout.strip(), "",
                         "a subagent's context is not the main session's budget")




class TestSlashCommandsAreIntent(Base):
    """Regression: filtering the whole <command-name> family as harness noise
    reduced a real 219-command session to 2 recorded prompts, because that
    session was driven by slash commands."""

    def _user(self, text):
        return {"type": "user", "timestamp": "2026-08-10T12:00:00.000Z",
                "message": {"content": text}}

    def test_slash_command_is_kept_and_folded_to_name_and_args(self):
        trail = ctxmon.build_trail(self.write_transcript([
            self._user("<command-name>/code-review</command-name>\n"
                       "<command-message>code-review</command-message>\n"
                       "<command-args>high</command-args>"),
        ]))
        self.assertIn("/code-review high", trail)
        self.assertNotIn("command-name", trail)
        self.assertIn("1 prompts", trail)

    def test_slash_command_without_args(self):
        self.assertEqual(
            ctxmon._prompt_text("<command-name>/compact</command-name>"),
            "/compact")

    def test_command_message_and_args_alone_are_still_noise(self):
        self.assertIsNone(ctxmon._prompt_text("<command-args>opus</command-args>"))
        self.assertIsNone(ctxmon._prompt_text("<task-notification>\nx\n"))
        self.assertIsNone(ctxmon._prompt_text("   "))


class TestRedaction(Base):
    """A trail is a plaintext file holding commands verbatim. One curl with an
    Authorization header would persist a live credential."""

    def test_bearer_token_in_a_command_is_redacted(self):
        trail = ctxmon.build_trail(self.write_transcript([
            assistant(content=[{"type": "tool_use", "id": "x", "name": "Bash",
                                "input": {"command":
                                          'curl -H "Authorization: Bearer '
                                          'sk-abc123def456ghi789" https://x'}}]),
        ]))
        self.assertNotIn("sk-abc123def456ghi789", trail)
        self.assertIn("redacted", trail)

    def test_common_credential_shapes(self):
        for raw, leaked in (
            ("export GITHUB_TOKEN=ghp_aaaaaaaaaaaaaaaaaaaa", "ghp_aaaaaaaaaaaaaaaaaaaa"),
            ("psql --password hunter2000", "hunter2000"),
            ("aws --key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
            ("curl -d 'api_key=abcdef123456'", "abcdef123456"),
            ("MY_SECRET=swordfishing ./run.sh", "swordfishing"),
        ):
            self.assertNotIn(leaked, ctxmon.redact(raw), f"leaked from: {raw}")

    def test_ordinary_commands_survive_unharmed(self):
        for safe in ("git status", "pytest tests/test_router.py -k horizon",
                     "ls -la /var/log", "python -m mypackage --smoke"):
            self.assertEqual(ctxmon.redact(safe), safe)

    def test_prompts_are_redacted_too(self):
        out = ctxmon._prompt_text("deploy with token=abcdef123456 please")
        self.assertNotIn("abcdef123456", out)


class TestPathNormalisation(Base):
    def test_separator_and_case_differences_compare_equal(self):
        a = ctxmon._norm_path(r"C:\Users\p\.claude\projects\X\a.jsonl")
        b = ctxmon._norm_path("c:/users/p/.claude/projects/X/a.jsonl")
        self.assertEqual(a, b)

    def test_trailing_separator_ignored(self):
        self.assertEqual(ctxmon._norm_path("/home/u/proj/"),
                         ctxmon._norm_path("/home/u/proj"))

    def test_empty_is_safe(self):
        self.assertEqual(ctxmon._norm_path(""), "")


class TestStateLocation(Base):
    def test_state_dir_is_not_inside_the_plugin_directory(self):
        """CLAUDE_PLUGIN_ROOT changes on every plugin update and its old tree is
        cleaned up within weeks, so state written there would vanish."""
        import importlib, os as _os
        env = dict(_os.environ)
        for k in ("CTXMON_STATE_DIR", "RG_CTXMON_STATE_DIR", "FLIGHTDECK_DIR"):
            env.pop(k, None)
        _os.environ.clear()
        _os.environ.update(env)
        try:
            mod = importlib.reload(ctxmon)
            self.assertNotIn(str(Path(mod.__file__).parent).lower(),
                             str(mod.STATE_DIR).lower())
            self.assertIn("flightdeck", str(mod.STATE_DIR).lower())
        finally:
            _os.environ.clear()
            _os.environ.update(dict(env))
            importlib.reload(ctxmon)


class TestWindowSpend(Base):
    """Regression: a streaming assistant message is written to the transcript
    many times. Measured 449 usage records across 152 distinct message ids, one
    repeated 10 times; summing every copy inflated spend by 3.38x."""

    def _project(self, records):
        proj = self.root / "projects" / "P"
        proj.mkdir(parents=True, exist_ok=True)
        with (proj / "s.jsonl").open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        ctxmon.PROJECTS_DIR = self.root / "projects"

    def _rec(self, mid, out_tok):
        return {"type": "assistant", "timestamp": "2026-08-10T12:00:00.000Z",
                "message": {"id": mid, "content": [],
                            "usage": {"output_tokens": out_tok,
                                      "cache_creation_input_tokens": 0,
                                      "input_tokens": 0}}}

    def test_repeated_message_counted_once(self):
        self._project([self._rec("msg_a", 100), self._rec("msg_a", 100),
                       self._rec("msg_a", 100), self._rec("msg_b", 50)])
        self.assertEqual(ctxmon.scan_window(0)["proxy_tokens"], 150)

    def test_distinct_messages_all_counted(self):
        self._project([self._rec(f"msg_{i}", 10) for i in range(7)])
        self.assertEqual(ctxmon.scan_window(0)["proxy_tokens"], 70)

    def test_records_without_a_message_id_fall_back_to_uuid(self):
        a = self._rec(None, 30); a["message"].pop("id"); a["uuid"] = "u1"
        b = self._rec(None, 30); b["message"].pop("id"); b["uuid"] = "u1"
        c = self._rec(None, 40); c["message"].pop("id"); c["uuid"] = "u2"
        self._project([a, b, c])
        self.assertEqual(ctxmon.scan_window(0)["proxy_tokens"], 70)


class TestWindowSize(Base):
    """Regression (P0): window size was read only from claude-hud's cache, so a
    user with just ctxmon installed fell back to DEFAULT_WINDOW. On a 1M
    account that reported 130% used and forced HANDOFF at real usage of 26%."""

    def _sl(self, sid8, window, ctx, age_s=0.0, quota=True):
        ctxmon.SL_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"context_window": {"context_window_size": window,
                                      "current_usage": {"cache_read_input_tokens": ctx}}}
        if quota:
            payload["rate_limits"] = {"five_hour": {"used_percentage": 20.0,
                                                    "resets_at": time.time() + 3600}}
        p = ctxmon.SL_DIR / f"{sid8}.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        if age_s:
            old = time.time() - age_s
            os.utime(p, (old, old))
        return p

    def _snap(self, sid8, transcript=None):
        saved = ctxmon.HUD_DIR
        ctxmon.HUD_DIR = self.root / "no-hud"          # claude-hud NOT installed
        try:
            return ctxmon.build_snapshot({"session_id": sid8 + "xxxx",
                                          "transcript_path": transcript or "",
                                          "cwd": ""})
        finally:
            ctxmon.HUD_DIR = saved

    def test_window_and_usage_come_from_the_statusline_without_hud(self):
        self._sl("aaaa0001", window=1_000_000, ctx=260_000)
        snap = self._snap("aaaa0001")
        self.assertEqual(snap["ctx_window"], 1_000_000)
        self.assertEqual(snap["ctx_tokens"], 260_000)
        self.assertEqual(snap["source"], "statusline")

    def test_a_1m_session_at_26_percent_is_not_pushed_to_handoff(self):
        # The transcript must carry the tokens too. Without it the pre-fix code
        # reported ctx=0 and NORMAL for the wrong reason, and the test would
        # have passed against the bug it exists to catch.
        p = self.write_transcript([assistant(usage(260_000))])
        self._sl("aaaa0002", window=1_000_000, ctx=260_000)
        snap = self._snap("aaaa0002", transcript=p)
        self.assertEqual(snap["ctx_window"], 1_000_000)
        self.assertEqual(snap["band"], "NORMAL",
                         "pre-fix this was 260k/200k = 130% and HANDOFF")

    def test_stale_payload_still_supplies_window_size_but_not_tokens(self):
        """Window size is a property of the account and does not expire. Token
        counts from a session that stopped rendering are not current."""
        p = self.write_transcript([assistant(usage(120_000))])
        self._sl("aaaa0003", window=1_000_000, ctx=999_999, age_s=600)
        snap = self._snap("aaaa0003", transcript=p)
        self.assertEqual(snap["ctx_window"], 1_000_000)
        self.assertEqual(snap["source"], "transcript")
        self.assertEqual(snap["ctx_tokens"], 120_000)

    def test_default_window_only_when_nothing_knows(self):
        p = self.write_transcript([assistant(usage(5_000))])
        snap = self._snap("aaaa0004", transcript=p)
        self.assertEqual(snap["ctx_window"], ctxmon.DEFAULT_WINDOW)
        self.assertEqual(snap["source"], "transcript")

    def test_hud_still_wins_when_present(self):
        hud = self.root / "hud"
        (hud / "transcript-cache").mkdir(parents=True)
        (hud / "context-cache").mkdir(parents=True)
        tpath = str(self.root / "t.jsonl")
        (hud / "transcript-cache" / "h.json").write_text(
            json.dumps({"transcriptPath": tpath}), encoding="utf-8")
        (hud / "context-cache" / "h.json").write_text(json.dumps(
            {"context_window_size": 1_000_000,
             "current_usage": {"cache_read_input_tokens": 300_000}}), encoding="utf-8")
        self._sl("aaaa0005", window=1_000_000, ctx=111_111)
        saved = ctxmon.HUD_DIR
        ctxmon.HUD_DIR = hud
        try:
            snap = ctxmon.build_snapshot({"session_id": "aaaa0005xxxx",
                                          "transcript_path": tpath, "cwd": ""})
        finally:
            ctxmon.HUD_DIR = saved
        self.assertEqual(snap["source"], "hud")
        self.assertEqual(snap["ctx_tokens"], 300_000)

class TestAgentDurationLog(Base):
    """Durations used to come only from the current 5-hour window, so n was
    routinely 1 or 2 and every verdict read LOW CONFIDENCE. A run that finished
    20 minutes before the window opened is still the best evidence available."""

    def _blk(self, bid):
        return [{"type": "tool_use", "id": bid, "name": "Agent", "input": {}}]

    def _result(self, bid, ts):
        return {"type": "user", "timestamp": ts,
                "message": {"content": [{"type": "tool_result",
                                         "tool_use_id": bid}]}}

    def _notify(self, bid, ts):
        return {"type": "queue-operation", "operation": "enqueue",
                "timestamp": ts,
                "content": ("<task-notification>\n<task-id>x</task-id>\n"
                            f"<tool-use-id>{bid}</tool-use-id>\n"
                            "<status>completed</status>\n")}

    def test_background_agent_true_duration_supersedes_dispatch(self):
        """tool_result is dispatch (~2s) for a background agent; the later
        notification is the real end. Reads take the max per id."""
        p = self.write_transcript([
            assistant(content=self._blk("b1"), ts="2026-08-10T12:00:00.000Z"),
            self._result("b1", "2026-08-10T12:00:02.000Z"),
            self._notify("b1", "2026-08-10T12:45:00.000Z"),
        ])
        ctxmon._scan_transcript(p, "agt1")
        hist = ctxmon.agent_history()
        self.assertEqual(len(hist), 1, "one run, not two rows")
        self.assertAlmostEqual(hist[0]["s"], 2700.0, places=1)

    def test_synchronous_agent_recorded_from_its_result(self):
        p = self.write_transcript([
            assistant(content=self._blk("s1"), ts="2026-08-10T12:00:00.000Z"),
            self._result("s1", "2026-08-10T12:03:00.000Z"),
        ])
        ctxmon._scan_transcript(p, "agt2")
        self.assertAlmostEqual(ctxmon.agent_history()[0]["s"], 180.0, places=1)

    def test_history_accumulates_across_separate_sessions(self):
        for i, sid in enumerate(("agt3", "agt4")):
            p = self.write_transcript([
                assistant(content=self._blk(f"m{i}"), ts="2026-08-10T12:00:00.000Z"),
                self._notify(f"m{i}", "2026-08-10T12:10:00.000Z"),
            ], name=f"s{i}.jsonl")
            ctxmon._scan_transcript(p, sid)
        self.assertEqual(len(ctxmon.agent_history()), 2)

    def test_incremental_scan_still_measures_a_run_it_did_not_start(self):
        """The start timestamp must survive between scans, or a run spanning
        two hook invocations is never measured at all."""
        p = self.write_transcript([
            assistant(content=self._blk("i1"), ts="2026-08-10T12:00:00.000Z"),
        ])
        ctxmon._scan_transcript(p, "agt5")
        self.assertEqual(ctxmon.agent_history(), [])
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(self._notify("i1", "2026-08-10T12:30:00.000Z")) + "\n")
        ctxmon._scan_transcript(p, "agt5")
        hist = ctxmon.agent_history()
        self.assertEqual(len(hist), 1)
        self.assertAlmostEqual(hist[0]["s"], 1800.0, places=1)

    def test_entries_older_than_the_window_are_excluded(self):
        ctxmon.record_agent_duration("old", time.time() - 40 * 86400, 600.0)
        ctxmon.record_agent_duration("new", time.time() - 3600, 900.0)
        ids = [r["id"] for r in ctxmon.agent_history(days=30)]
        self.assertEqual(ids, ["new"])

    def test_zero_and_negative_durations_are_not_recorded(self):
        ctxmon.record_agent_duration("z", time.time(), 0.0)
        ctxmon.record_agent_duration("n", time.time(), -5.0)
        self.assertEqual(ctxmon.agent_history(), [])

    def test_missing_log_is_not_an_error(self):
        self.assertEqual(ctxmon.agent_history(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
