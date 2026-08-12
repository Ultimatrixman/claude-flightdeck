#!/usr/bin/env python3
"""Tests for the ipc claim matcher. Run with the SAME interpreter the hooks use:

    python3 plugins/ipc/tests/test_ipc.py

Stdlib unittest on purpose, for the same reason the ctxmon suite is: the hooks
run under the bare interpreter, so a suite that needs a virtualenv would be
testing a different environment than the one that ships.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
import ipc  # noqa: E402
import relay  # noqa: E402


FLIGHT = "C:/claude-flightdeck"
RIDE = "C:/RideGuide"


class TestClaimPlacement(unittest.TestCase):
    """A claim pattern is relative to the cwd of the session that made it. The
    old check took the pattern's literal prefix and asked whether that string
    appeared ANYWHERE in the target path, with no cwd involved, so a session
    claiming 'tests/**' in one repo warned on every other repo's tests/."""

    def test_a_relative_claim_does_not_reach_into_another_repo(self):
        self.assertFalse(ipc._claim_covers(
            f"{FLIGHT}/tests/integration.sh", "tests/**", RIDE))

    def test_a_relative_claim_still_covers_its_own_repo(self):
        self.assertTrue(ipc._claim_covers(
            f"{FLIGHT}/tests/integration.sh", "tests/**", FLIGHT))

    def test_a_claim_with_no_known_cwd_matches_nothing(self):
        """Unplaceable, so it cannot be judged. A guard that cries wolf across
        repositories is one people learn to ignore."""
        self.assertFalse(ipc._claim_covers(f"{FLIGHT}/tests/x.sh", "tests/**", ""))

    def test_an_absolute_claim_needs_no_cwd(self):
        self.assertTrue(ipc._claim_covers(
            f"{FLIGHT}/tests/x.sh", f"{FLIGHT}/tests/**", ""))

    def test_posix_absolute_claim(self):
        self.assertTrue(ipc._claim_covers(
            "/home/u/proj/src/a.py", "/home/u/proj/src/**", ""))
        self.assertFalse(ipc._claim_covers(
            "/home/u/other/src/a.py", "/home/u/proj/src/**", ""))


class TestClaimMatching(unittest.TestCase):
    def test_separator_and_case_differences_do_not_matter(self):
        self.assertTrue(ipc._claim_covers(
            r"C:\claude-flightdeck\Tests\x.sh", r"tests\**", FLIGHT))

    def test_a_bare_directory_covers_everything_beneath_it(self):
        self.assertTrue(ipc._claim_covers(f"{FLIGHT}/src/deep/a.py", "src", FLIGHT))
        self.assertTrue(ipc._claim_covers(f"{FLIGHT}/src/a.py", "./src/", FLIGHT))

    def test_a_directory_prefix_stops_at_a_path_boundary(self):
        """A bare startswith made 'src/tests' a child of 'src/test', and
        'mysrcfile.py' a member of 'src'."""
        self.assertFalse(ipc._claim_covers(f"{FLIGHT}/srcfile.py", "src", FLIGHT))
        self.assertFalse(ipc._claim_covers(f"{FLIGHT}/mysrc/a.py", "src", FLIGHT))

    def test_a_glob_is_matched_as_a_glob_not_as_a_prefix(self):
        self.assertTrue(ipc._claim_covers(
            f"{FLIGHT}/plugins/ctxmon/ctxmon.py", "plugins/*/*.py", FLIGHT))
        self.assertFalse(ipc._claim_covers(
            f"{FLIGHT}/plugins/ctxmon/notes.md", "plugins/*/*.py", FLIGHT))

    def test_an_exact_file_claim_covers_only_that_file(self):
        self.assertTrue(ipc._claim_covers(
            f"{FLIGHT}/README.md", "README.md", FLIGHT))
        self.assertFalse(ipc._claim_covers(
            f"{FLIGHT}/README.md.bak", "README.md", FLIGHT))

    def test_an_empty_pattern_matches_nothing(self):
        for pat in ("", "   ", "/"):
            self.assertFalse(ipc._claim_covers(f"{FLIGHT}/a.py", pat, FLIGHT), pat)


class TestClaimOverlap(unittest.TestCase):
    def test_same_pattern_in_two_repos_does_not_overlap(self):
        self.assertEqual(
            relay._claims_overlap(["tests/**"], FLIGHT, ["tests/**"], RIDE), [])

    def test_same_pattern_in_the_same_repo_overlaps(self):
        self.assertEqual(
            relay._claims_overlap(["tests/**"], FLIGHT, ["tests/**"], FLIGHT),
            ["tests/** ~ tests/**"])

    def test_a_parent_claim_overlaps_a_child_claim(self):
        self.assertTrue(
            relay._claims_overlap(["plugins/**"], FLIGHT,
                                  ["plugins/ipc/*.py"], FLIGHT))

    def test_sibling_directories_sharing_a_prefix_do_not_overlap(self):
        self.assertEqual(
            relay._claims_overlap(["src/test/**"], FLIGHT,
                                  ["src/tests/**"], FLIGHT), [])

    def test_an_unplaceable_claim_overlaps_nothing(self):
        self.assertEqual(
            relay._claims_overlap(["tests/**"], "", ["tests/**"], FLIGHT), [])


class TestClaimCarriesItsCwd(unittest.TestCase):
    """The base travels register -> /claim -> /peers -> claims-cache -> guard.
    A break at any hop leaves the guard with no cwd, and a guard with no cwd
    now matches nothing, so a silent break would disable it entirely."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (relay.STATE_DIR, relay.SNAPSHOT, dict(relay._peers))
        relay.STATE_DIR = Path(self.tmp.name)
        relay.SNAPSHOT = relay.STATE_DIR / "peers.json"
        relay._peers.clear()

    def tearDown(self):
        relay.STATE_DIR, relay.SNAPSHOT, peers = self._saved
        relay._peers.clear()
        relay._peers.update(peers)
        self.tmp.cleanup()

    def _peer(self, name, cwd):
        relay._register({"name": name, "session_id": name, "cwd": cwd})

    def test_claim_snapshots_the_cwd_and_peers_reports_it(self):
        self._peer("s-aaa", RIDE)
        relay._claim({"name": "s-aaa", "desc": "d", "paths": ["tests/**"],
                      "cwd": RIDE})
        peer = relay._summaries()[0]
        self.assertEqual(peer["claim"]["cwd"], RIDE)

    def test_two_repos_claiming_the_same_pattern_do_not_notify_each_other(self):
        self._peer("s-aaa", RIDE)
        self._peer("s-bbb", FLIGHT)
        relay._claim({"name": "s-aaa", "desc": "a", "paths": ["tests/**"],
                      "cwd": RIDE})
        _st, body = relay._claim({"name": "s-bbb", "desc": "b",
                                  "paths": ["tests/**"], "cwd": FLIGHT})
        self.assertEqual(body["overlaps"], [])

    def test_two_sessions_in_one_repo_still_notify_each_other(self):
        self._peer("s-aaa", FLIGHT)
        self._peer("s-bbb", FLIGHT)
        relay._claim({"name": "s-aaa", "desc": "a", "paths": ["tests/**"],
                      "cwd": FLIGHT})
        _st, body = relay._claim({"name": "s-bbb", "desc": "b",
                                  "paths": ["tests/**"], "cwd": FLIGHT})
        self.assertEqual([o["name"] for o in body["overlaps"]], ["s-aaa"])

    def test_a_claim_without_an_explicit_cwd_falls_back_to_the_peer(self):
        """The CLI sends no cwd of its own; register supplied one."""
        self._peer("s-aaa", FLIGHT)
        relay._claim({"name": "s-aaa", "desc": "d", "paths": ["tests/**"]})
        self.assertEqual(relay._summaries()[0]["claim"]["cwd"], FLIGHT)


class TestMatcherStaysInStep(unittest.TestCase):
    """relay.py runs as its own detached process, so ipc.py cannot import it
    and the matcher is duplicated. Two copies that drift mean the relay
    reports an overlap the guard will not warn about, or the reverse."""

    BLOCK = re.compile(
        r"# --- claim matcher \(keep identical\) ---\n(.*?)"
        r"# --- end claim matcher ---", re.S)

    def _block(self, name):
        src = (_HERE / name).read_text(encoding="utf-8")
        m = self.BLOCK.search(src)
        self.assertIsNotNone(m, f"{name} lost its claim-matcher markers")
        return m.group(1).strip()

    def test_claim_matcher_is_identical_in_both_modules(self):
        self.assertEqual(self._block("ipc.py"), self._block("relay.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
