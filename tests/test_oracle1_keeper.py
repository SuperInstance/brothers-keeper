"""
Tests for oracle1-keeper.py — Brothers Keeper fork for Oracle1 (cloud/ARM64).

Tests cover:
- GitHubQuotaTracker (check, should_throttle)
- BeachcombMonitor (state loading/saving)
- BottleWatcher (state loading/saving, check_vessel)
- MechanicDispatcher (dispatch, specialized methods)
- Oracle1Keeper (construction, start/stop)
"""

import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Load oracle1-keeper.py (hyphenated filename can't be imported normally)
_keeper_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("oracle1_keeper", os.path.join(_keeper_dir, "oracle1-keeper.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

GitHubQuotaTracker = _mod.GitHubQuotaTracker
BeachcombMonitor = _mod.BeachcombMonitor
BottleWatcher = _mod.BottleWatcher
MechanicDispatcher = _mod.MechanicDispatcher
Oracle1Keeper = _mod.Oracle1Keeper


# ============================================================
# GitHubQuotaTracker
# ============================================================

class TestGitHubQuotaTracker:
    def test_initial_state(self):
        """Tracker starts with default quota values."""
        qt = GitHubQuotaTracker("fake-token")
        assert qt.remaining == 5000
        assert qt.limit == 5000
        assert qt.reset_time is None

    def test_should_throttle_true(self):
        """Should throttle when remaining < 100."""
        qt = GitHubQuotaTracker("fake-token")
        qt.remaining = 50
        assert qt.should_throttle() is True

    def test_should_throttle_false(self):
        """Should not throttle when remaining >= 100."""
        qt = GitHubQuotaTracker("fake-token")
        qt.remaining = 500
        assert qt.should_throttle() is False

    def test_should_throttle_at_boundary(self):
        """Should throttle at exactly 99 remaining."""
        qt = GitHubQuotaTracker("fake-token")
        qt.remaining = 99
        assert qt.should_throttle() is True
        qt.remaining = 100
        assert qt.should_throttle() is False

    def test_check_updates_state(self):
        """check() updates remaining, limit, and reset_time."""
        qt = GitHubQuotaTracker("fake-token")
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "resources": {
                "core": {
                    "remaining": 4800,
                    "limit": 5000,
                    "reset": 1700000000,
                }
            }
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = qt.check()

        assert result["remaining"] == 4800
        assert result["limit"] == 5000
        assert result["status"] == "OK"
        assert qt.remaining == 4800

    def test_check_status_levels(self):
        """check() returns correct status based on remaining."""
        qt = GitHubQuotaTracker("fake-token")

        def mock_check(remaining):
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "resources": {"core": {"remaining": remaining, "limit": 5000, "reset": 1700000000}}
            }).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            with patch("urllib.request.urlopen", return_value=mock_resp):
                return qt.check()

        assert mock_check(500)["status"] == "OK"
        assert mock_check(50)["status"] == "LOW"
        assert mock_check(5)["status"] == "CRITICAL"

    def test_check_handles_error(self):
        """check() returns error dict on failure."""
        qt = GitHubQuotaTracker("fake-token")
        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            result = qt.check()
        assert "error" in result
        assert result["status"] == "UNKNOWN"


# ============================================================
# BeachcombMonitor
# ============================================================

class TestBeachcombMonitor:
    def test_initial_state(self):
        """Monitor starts with empty known lists."""
        bm = BeachcombMonitor("fake-token")
        assert isinstance(bm.known_forks, dict)
        assert isinstance(bm.known_prs, dict)

    def test_load_state_missing_file(self):
        """Loading state from missing file returns empty state."""
        bm = BeachcombMonitor("fake-token")
        bm.state_file = "/nonexistent/state.json"
        state = bm._load_state()
        assert state == {"known_forks": {}, "known_prs": {}}

    def test_save_and_load_state(self, tmp_path):
        """State persists across save/load cycles."""
        state_file = str(tmp_path / "beachcomb.json")
        bm = BeachcombMonitor("fake-token")
        bm.state_file = state_file
        bm.known_forks["repo/user1"] = {"fork_owner": "user1"}
        bm.known_prs["repo#42"] = {"number": 42}
        bm._save_state()

        bm2 = BeachcombMonitor("fake-token")
        bm2.state_file = state_file
        state = bm2._load_state()
        assert "repo/user1" in state["known_forks"]
        assert "repo#42" in state["known_prs"]

    def test_scan_returns_findings(self, tmp_path):
        """scan() returns a list (possibly empty)."""
        bm = BeachcombMonitor("fake-token")
        bm.state_file = str(tmp_path / "beachcomb.json")
        # Without valid GitHub API responses, scan returns error finding
        with patch("urllib.request.urlopen", side_effect=Exception("no network")):
            findings = bm.scan()
        assert isinstance(findings, list)
        assert len(findings) == 1
        assert findings[0]["type"] == "error"


# ============================================================
# BottleWatcher
# ============================================================

class TestBottleWatcher:
    def test_initial_state(self):
        """Watcher starts with empty known bottles."""
        bw = BottleWatcher("fake-token")
        assert isinstance(bw.known_bottles, dict)

    def test_load_state_missing_file(self):
        """Loading from missing file returns empty dict."""
        bw = BottleWatcher("fake-token")
        bw.state_file = "/nonexistent/state.json"
        assert bw._load_state() == {}

    def test_save_and_load_state(self, tmp_path):
        """Bottle state persists across save/load."""
        state_file = str(tmp_path / "bottles.json")
        bw = BottleWatcher("fake-token")
        bw.state_file = state_file
        bw.known_bottles["owner/vessel/folder/file.txt"] = {"file": "file.txt"}
        bw._save_state()

        bw2 = BottleWatcher("fake-token")
        bw2.state_file = state_file
        state = bw2._load_state()
        assert "owner/vessel/folder/file.txt" in state

    def test_check_vessel_network_error(self, tmp_path):
        """check_vessel handles network errors gracefully."""
        bw = BottleWatcher("fake-token")
        bw.state_file = str(tmp_path / "bottles.json")
        with patch("urllib.request.urlopen", side_effect=Exception("no network")):
            bottles = bw.check_vessel("SuperInstance", "some-repo")
        assert bottles == []


# ============================================================
# MechanicDispatcher
# ============================================================

class TestMechanicDispatcher:
    def test_dispatch_success(self):
        """Dispatch creates an issue and returns issue number."""
        md = MechanicDispatcher("fake-token")
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "number": 42,
            "html_url": "https://github.com/SuperInstance/fleet-mechanic/issues/42"
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = md.dispatch("fix-tests", "some-repo", "test output")

        assert result["status"] == "dispatched"
        assert result["issue"] == 42
        assert "fleet-mechanic" in result["url"]

    def test_dispatch_failure(self):
        """Dispatch returns failed status on error."""
        md = MechanicDispatcher("fake-token")
        with patch("urllib.request.urlopen", side_effect=Exception("HTTP 403")):
            result = md.dispatch("fix-tests", "some-repo")
        assert result["status"] == "failed"
        assert "error" in result

    def test_dispatch_fix_tests(self):
        """fix-tests dispatch uses correct task type."""
        md = MechanicDispatcher("fake-token")
        with patch.object(md, "dispatch", return_value={"status": "dispatched"}) as mock:
            md.dispatch_fix_tests("some-repo", "test failure output")
        mock.assert_called_once_with("fix-tests", "some-repo", "test failure output"[:500])

    def test_dispatch_gen_docs(self):
        """gen-docs dispatch uses correct task type."""
        md = MechanicDispatcher("fake-token")
        with patch.object(md, "dispatch", return_value={"status": "dispatched"}) as mock:
            md.dispatch_gen_docs("some-repo")
        mock.assert_called_once_with("gen-docs", "some-repo")

    def test_dispatch_review(self):
        """review dispatch uses correct task type."""
        md = MechanicDispatcher("fake-token")
        with patch.object(md, "dispatch", return_value={"status": "dispatched"}) as mock:
            md.dispatch_review("some-repo")
        mock.assert_called_once_with("review", "some-repo")

    def test_dispatch_health_scan(self):
        """health-scan dispatch targets entire fleet."""
        md = MechanicDispatcher("fake-token")
        with patch.object(md, "dispatch", return_value={"status": "dispatched"}) as mock:
            md.dispatch_health_scan()
        mock.assert_called_once_with("health-scan", "SuperInstance/*")


# ============================================================
# Oracle1Keeper
# ============================================================

class TestOracle1Keeper:
    def test_construction(self):
        """Oracle1Keeper constructs with all components."""
        config = {
            "flywheel": {"enabled": True, "git_repos": []},
            "watch_processes": [],
            "thresholds": {},
        }
        keeper = Oracle1Keeper(config)
        assert keeper.resource is not None
        assert keeper.process is not None
        assert keeper.flywheel is not None
        assert keeper.quota is not None
        assert keeper.beachcomb is not None
        assert keeper.bottle_watcher is not None

    def test_stop(self):
        """stop() sets running to False."""
        config = {"flywheel": {"enabled": True, "git_repos": []},
                   "watch_processes": [], "thresholds": {}}
        keeper = Oracle1Keeper(config)
        keeper.running = True
        keeper.stop()
        assert keeper.running is False

    def test_watch_vessels_populated(self):
        """Default watch vessels are configured."""
        config = {"flywheel": {"enabled": True, "git_repos": []},
                   "watch_processes": [], "thresholds": {}}
        keeper = Oracle1Keeper(config)
        assert len(keeper.watch_vessels) == 3
        assert ("SuperInstance", "superz-vessel") in keeper.watch_vessels
