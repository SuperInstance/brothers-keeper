"""
Comprehensive tests for keeper.py — The Lighthouse Keeper (Brothers Keeper v2).

Tests cover:
- Data classes (ResourceSnapshot, FlywheelState, TokenAllowance, ScheduleEntry)
- ProcessWatchdog (check, should_restart, restart)
- FlywheelMonitor (check, should_nudge, nudge, time parsing)
- TokenSteward (request_tokens, report_usage, checkpoint gating, zero-trust, masked keys)
- GpuScheduler (request, release, status, preemption)
- MultiAgentCoordinator (register, agent status)
- OperationalLogger (log)
- SelfHealer (heal — gateway restart, tmp cleanup)
- BrothersKeeper (construction, stop, alert coalescing, pre_flight)
- DEFAULT_CONFIG structure
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

# Ensure keeper.py is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keeper import (
    DEFAULT_CONFIG,
    ResourceSnapshot,
    FlywheelState,
    TokenAllowance,
    ScheduleEntry,
    ResourceMonitor,
    ProcessWatchdog,
    FlywheelMonitor,
    TokenSteward,
    GpuScheduler,
    MultiAgentCoordinator,
    OperationalLogger,
    SelfHealer,
    BrothersKeeper,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def config():
    """Return a copy of the default config for isolated tests."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    return cfg


@pytest.fixture
def tmp_log_dir(tmp_path):
    """Return a config pointing logs to a temp directory."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["logs"]["dir"] = str(tmp_path)
    return cfg


@pytest.fixture
def mock_snapshot():
    """Return a typical ResourceSnapshot for testing."""
    return ResourceSnapshot(
        timestamp=datetime.utcnow().isoformat(),
        ram_total_mb=8192, ram_used_mb=4096, ram_percent=50.0,
        swap_total_mb=2048, swap_used_mb=200, swap_percent=10.0,
        cpu_percent=25.0, disk_total_gb=100.0, disk_used_gb=40.0, disk_percent=40.0,
    )


# ============================================================
# DEFAULT_CONFIG
# ============================================================

class TestDefaultConfig:
    def test_has_required_top_level_keys(self):
        for key in ("watch_processes", "thresholds", "process", "risk",
                     "logs", "healing", "beacon", "flywheel", "gpu",
                     "token_steward", "coordination"):
            assert key in DEFAULT_CONFIG, f"DEFAULT_CONFIG missing key: {key}"

    def test_thresholds_have_required_keys(self):
        for key in ("ram_warning", "ram_critical", "disk_warning", "cpu_warning",
                     "cpu_sustain_sec", "swap_warning"):
            assert key in DEFAULT_CONFIG["thresholds"]

    def test_process_config_values(self):
        p = DEFAULT_CONFIG["process"]
        assert p["check_interval_sec"] > 0
        assert p["restart_cooldown_sec"] > 0
        assert p["max_restart_attempts"] > 0


# ============================================================
# Data Classes
# ============================================================

class TestDataClasses:
    def test_resource_snapshot_defaults(self):
        snap = ResourceSnapshot(
            timestamp="2025-01-01T00:00:00",
            ram_total_mb=8000, ram_used_mb=4000, ram_percent=50.0,
            swap_total_mb=2000, swap_used_mb=100, swap_percent=5.0,
            cpu_percent=20.0, disk_total_gb=100.0, disk_used_gb=50.0, disk_percent=50.0,
        )
        assert snap.load_1m == 0
        assert snap.load_5m == 0
        assert snap.gpu_mem_used_mb == 0
        assert snap.top_processes == []

    def test_flywheel_state_defaults(self):
        state = FlywheelState(timestamp="t", agent_name="agent1", status="spinning")
        assert state.current_task == ""
        assert state.last_commit_time is None
        assert state.checkpoint_reached is None
        assert state.commits_this_hour == 0

    def test_token_allowance_defaults(self):
        ta = TokenAllowance(
            timestamp="t", agent_name="a", provider="openai", daily_limit_usd=10.0
        )
        assert ta.used_today_usd == 0
        assert ta.tokens_used == 0
        assert ta.calls_made == 0
        assert ta.status == "active"

    def test_schedule_entry_defaults(self):
        se = ScheduleEntry(
            timestamp="t", agent_name="a", resource="gpu",
            amount="80%", duration_min=60, priority=5,
        )
        assert se.reason == ""
        assert se.status == "requested"


# ============================================================
# ProcessWatchdog
# ============================================================

class TestProcessWatchdog:
    def test_check_detects_new_process(self, config):
        """When a process appears for the first time, emit 'started' event."""
        config["watch_processes"] = [
            {"name": "test-gateway", "cmd": "echo 1234"}
        ]
        wd = ProcessWatchdog(config)
        events = wd.check()
        assert len(events) == 1
        assert events[0]["event_type"] == "started"
        assert events[0]["process_name"] == "test-gateway"
        assert events[0]["pid"] == 1234

    def test_check_detects_stopped_process(self, config):
        """When a previously-seen process disappears, emit 'stopped' event."""
        config["watch_processes"] = [
            {"name": "test-gateway", "cmd": "echo 1234"}
        ]
        wd = ProcessWatchdog(config)
        wd.check()  # First check — process seen
        # Now subprocess returns empty output
        config["watch_processes"][0]["cmd"] = "echo ''"
        events = wd.check()
        assert len(events) == 1
        assert events[0]["event_type"] == "stopped"
        assert events[0]["process_name"] == "test-gateway"

    def test_check_detects_pid_change(self, config):
        """When PID changes, emit 'restarted' event."""
        config["watch_processes"] = [
            {"name": "test-gateway", "cmd": "echo 1234"}
        ]
        wd = ProcessWatchdog(config)
        wd.check()  # PID 1234
        config["watch_processes"][0]["cmd"] = "echo 5678"
        events = wd.check()
        assert len(events) == 1
        assert events[0]["event_type"] == "restarted"
        assert events[0]["pid"] == 5678

    def test_check_no_events_when_process_absent_initially(self, config):
        """No events when process never existed and still doesn't exist."""
        config["watch_processes"] = [
            {"name": "no-such-process", "cmd": "echo ''"}
        ]
        wd = ProcessWatchdog(config)
        events = wd.check()
        assert len(events) == 0

    def test_should_restart_within_cooldown(self, config):
        """Should not restart if cooldown has not elapsed."""
        config["process"]["restart_cooldown_sec"] = 300
        wd = ProcessWatchdog(config)
        wd.restart_times["test"] = datetime.utcnow()
        assert wd.should_restart("test") is False

    def test_should_restart_after_cooldown(self, config):
        """Should allow restart after cooldown elapses."""
        config["process"]["restart_cooldown_sec"] = 300
        config["process"]["max_restart_attempts"] = 5
        wd = ProcessWatchdog(config)
        wd.restart_times["test"] = datetime.utcnow() - timedelta(seconds=301)
        assert wd.should_restart("test") is True

    def test_should_restart_max_attempts_exceeded(self, config):
        """Should not restart if max attempts reached."""
        config["process"]["restart_cooldown_sec"] = 1
        config["process"]["max_restart_attempts"] = 3
        wd = ProcessWatchdog(config)
        wd.restart_counts["test"] = 3
        assert wd.should_restart("test") is False

    def test_restart_success(self, config):
        """Restart returns True on successful subprocess call."""
        config["process"]["restart_cooldown_sec"] = 1
        config["process"]["max_restart_attempts"] = 5
        wd = ProcessWatchdog(config)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = wd.restart("test-gateway", method="gateway")
        assert result is True
        assert wd.restart_counts["test-gateway"] == 1

    def test_restart_failure(self, config):
        """Restart returns False when subprocess raises."""
        config["process"]["restart_cooldown_sec"] = 1
        config["process"]["max_restart_attempts"] = 5
        wd = ProcessWatchdog(config)
        with patch("subprocess.run", side_effect=Exception("boom")):
            result = wd.restart("test-gateway", method="gateway")
        assert result is False
        assert wd.restart_counts["test-gateway"] == 1

    def test_restart_doctor_method(self, config):
        """Restart with 'doctor' method calls openclaw doctor --fix."""
        config["process"]["restart_cooldown_sec"] = 1
        config["process"]["max_restart_attempts"] = 5
        wd = ProcessWatchdog(config)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = wd.restart("test-gateway", method="doctor")
        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "doctor" in args


# ============================================================
# FlywheelMonitor
# ============================================================

class TestFlywheelMonitor:
    def test_check_idle_no_commits(self, config):
        """With no recent commits, status should be 'idle'."""
        config["flywheel"]["git_repos"] = []
        fm = FlywheelMonitor(config)
        state = fm.check("test-agent")
        assert state.status == "idle"
        assert state.agent_name == "test-agent"

    def test_check_storing_state(self, config):
        """Check stores state internally."""
        config["flywheel"]["git_repos"] = []
        fm = FlywheelMonitor(config)
        state = fm.check("agent-x")
        assert "agent-x" in fm.state
        assert fm.state["agent-x"] is state

    def test_should_nudge_no_prior_nudge(self, config):
        """Should nudge if agent was never nudged."""
        fm = FlywheelMonitor(config)
        assert fm.should_nudge("agent1") is True

    def test_should_nudge_within_cooldown(self, config):
        """Should not nudge if nudged recently."""
        config["flywheel"]["nudge_cooldown_min"] = 10
        fm = FlywheelMonitor(config)
        fm.nudge("agent1")
        assert fm.should_nudge("agent1") is False

    def test_should_nudge_after_cooldown(self, config):
        """Should nudge after cooldown elapses."""
        config["flywheel"]["nudge_cooldown_min"] = 1
        fm = FlywheelMonitor(config)
        fm.last_nudge["agent1"] = datetime.utcnow() - timedelta(minutes=2)
        assert fm.should_nudge("agent1") is True

    def test_nudge_records_time(self, config):
        """Nudge records the current time."""
        fm = FlywheelMonitor(config)
        before = datetime.utcnow()
        fm.nudge("agent1")
        assert "agent1" in fm.last_nudge
        assert fm.last_nudge["agent1"] >= before

    def test_parse_time_iso_format(self, config):
        """Parse standard ISO time format."""
        fm = FlywheelMonitor(config)
        t = "2025-01-15T10:30:00"
        result = fm._parse_time(t)
        assert result.year == 2025
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 10

    def test_parse_time_fallback_on_invalid(self, config):
        """Parse returns current time for invalid strings."""
        fm = FlywheelMonitor(config)
        before = datetime.utcnow()
        result = fm._parse_time("not-a-date")
        assert result >= before - timedelta(seconds=2)

    def test_read_checkpoint_missing_file(self, config):
        """Read checkpoint returns None for non-existent file."""
        fm = FlywheelMonitor(config)
        assert fm._read_checkpoint("/nonexistent/path/file.txt") is None

    def test_read_checkpoint_empty_path(self, config):
        """Read checkpoint returns None for empty path."""
        fm = FlywheelMonitor(config)
        assert fm._read_checkpoint("") is None

    def test_read_checkpoint_valid_file(self, config, tmp_path):
        """Read checkpoint returns file content."""
        fm = FlywheelMonitor(config)
        cp_file = tmp_path / "checkpoint.txt"
        cp_file.write_text("step-42-complete")
        result = fm._read_checkpoint(str(cp_file))
        assert result == "step-42-complete"


# ============================================================
# TokenSteward
# ============================================================

class TestTokenSteward:
    def test_request_tokens_approved(self, config):
        """Token request approved when under limit."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["zero_trust"] = False
        config["token_steward"]["checkpoint_gated"] = False
        config["token_steward"]["vault_path"] = ""
        config["token_steward"]["allowances"] = {
            "agent1": {"provider": "openai", "daily_limit_usd": 10.0}
        }
        ts = TokenSteward(config)
        approved, msg = ts.request_tokens("agent1", "openai", 1.0)
        assert approved is True

    def test_request_tokens_limit_exceeded(self, config):
        """Token request denied when daily limit exceeded."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["allowances"] = {
            "agent1": {"provider": "openai", "daily_limit_usd": 5.0, "used_today_usd": 4.5}
        }
        ts = TokenSteward(config)
        approved, msg = ts.request_tokens("agent1", "openai", 1.0)
        assert approved is False
        assert "Daily limit" in msg

    def test_request_tokens_auto_register(self, config):
        """Unknown agent gets auto-registered."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["vault_path"] = ""
        ts = TokenSteward(config)
        assert "new_agent" not in ts.allowances
        approved, _ = ts.request_tokens("new_agent", "openai", 0.5)
        assert approved is True
        assert "new_agent" in ts.allowances

    def test_request_tokens_checkpoint_gated(self, config):
        """Token request denied when checkpoint not approved."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["checkpoint_gated"] = True
        config["token_steward"]["allowances"] = {
            "agent1": {"provider": "openai", "daily_limit_usd": 10.0}
        }
        ts = TokenSteward(config)
        approved, msg = ts.request_tokens("agent1", "openai", 1.0)
        assert approved is False
        assert "Checkpoint" in msg

    def test_request_tokens_zero_trust_unverified(self, config):
        """Zero-trust denies unregistered agents."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["zero_trust"] = True
        config["token_steward"]["checkpoint_gated"] = False
        config["token_steward"]["allowances"] = {
            "spy": {"provider": "openai", "daily_limit_usd": 10.0}
        }
        ts = TokenSteward(config)
        approved, msg = ts.request_tokens("spy", "openai", 1.0)
        assert approved is False
        assert "not verified" in msg

    def test_request_tokens_disabled_steward(self, config):
        """When steward disabled, auto-approve without tracking."""
        config["token_steward"]["enabled"] = False
        ts = TokenSteward(config)
        approved, _ = ts.request_tokens("anyone", "openai", 100.0)
        assert approved is True

    def test_approve_checkpoint_enables_access(self, config):
        """After checkpoint approval, gated requests succeed."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["checkpoint_gated"] = True
        config["token_steward"]["zero_trust"] = False
        config["token_steward"]["allowances"] = {
            "agent1": {"provider": "openai", "daily_limit_usd": 10.0}
        }
        ts = TokenSteward(config)
        # Before approval: denied
        ok, _ = ts.request_tokens("agent1", "openai", 1.0)
        assert ok is False
        # Approve checkpoint
        ts.approve_checkpoint("agent1", "step-5-done")
        # After approval: approved
        ok, _ = ts.request_tokens("agent1", "openai", 1.0)
        assert ok is True

    def test_report_usage(self, config):
        """Usage reporting updates token and cost counters."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["allowances"] = {
            "agent1": {"provider": "openai", "daily_limit_usd": 10.0}
        }
        ts = TokenSteward(config)
        ts.report_usage("agent1", tokens_used=500, actual_cost_usd=0.05)
        a = ts.allowances["agent1"]
        assert a.tokens_used == 500
        assert a.used_today_usd == 0.05

    def test_report_usage_takes_max_cost(self, config):
        """Report usage with higher actual cost overrides estimated."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["allowances"] = {
            "agent1": {"provider": "openai", "daily_limit_usd": 10.0, "used_today_usd": 1.0}
        }
        ts = TokenSteward(config)
        ts.report_usage("agent1", tokens_used=100, actual_cost_usd=0.5)
        # max(1.0, 0.5) = 1.0
        assert ts.allowances["agent1"].used_today_usd == 1.0
        ts.report_usage("agent1", tokens_used=100, actual_cost_usd=2.0)
        # max(1.0, 2.0) = 2.0
        assert ts.allowances["agent1"].used_today_usd == 2.0

    def test_get_usage_report(self, config):
        """Usage report includes all registered agents."""
        config["token_steward"]["enabled"] = True
        config["token_steward"]["allowances"] = {
            "agent1": {"provider": "openai", "daily_limit_usd": 10.0, "used_today_usd": 2.0, "tokens_used": 500, "calls_made": 3},
            "agent2": {"provider": "anthropic", "daily_limit_usd": 5.0},
        }
        ts = TokenSteward(config)
        report = ts.get_usage_report()
        assert "agent1" in report
        assert "agent2" in report
        assert report["agent1"]["provider"] == "openai"
        assert report["agent1"]["used"] == 2.0
        assert report["agent1"]["calls"] == 3

    def test_get_masked_key(self, config):
        """Masked key hides middle portion."""
        config["token_steward"]["vault_path"] = ""
        ts = TokenSteward(config)
        with patch.object(ts, "_get_raw_key", return_value="sk-1234567890abcdef"):
            masked = ts._get_masked_key("openai")
        assert masked == "sk-1...cdef"

    def test_get_masked_key_short(self, config):
        """Short keys are fully masked."""
        config["token_steward"]["vault_path"] = ""
        ts = TokenSteward(config)
        with patch.object(ts, "_get_raw_key", return_value="short"):
            masked = ts._get_masked_key("openai")
        assert masked == "***"


# ============================================================
# GpuScheduler
# ============================================================

class TestGpuScheduler:
    def test_request_gpu_available(self, config):
        """Request GPU when available — granted immediately."""
        config["gpu"]["current_holder"] = ""
        config["coordination"]["schedule_path"] = ""
        gs = GpuScheduler(config)
        approved, msg = gs.request_gpu("agent1", duration_min=30)
        assert approved is True
        assert "Granted" in msg
        assert gs.current_holder == "agent1"

    def test_request_gpu_held_by_another(self, config):
        """Request GPU held by another agent — denied."""
        future = (datetime.utcnow() + timedelta(minutes=20)).isoformat()
        config["gpu"]["current_holder"] = "agent0"
        config["gpu"]["holder_expires"] = future
        config["coordination"]["schedule_path"] = ""
        gs = GpuScheduler(config)
        approved, msg = gs.request_gpu("agent1", duration_min=30)
        assert approved is False
        assert "held by" in msg

    def test_request_gpu_high_priority_preempts(self, config):
        """High priority (priority > 5) evicts current holder."""
        future = (datetime.utcnow() + timedelta(minutes=20)).isoformat()
        config["gpu"]["current_holder"] = "agent0"
        config["gpu"]["holder_expires"] = future
        config["coordination"]["schedule_path"] = ""
        gs = GpuScheduler(config)
        approved, msg = gs.request_gpu("agent1", duration_min=30, priority=8)
        assert approved is True
        assert gs.current_holder == "agent1"

    def test_release_gpu(self, config):
        """Releasing GPU clears holder."""
        config["coordination"]["schedule_path"] = ""
        gs = GpuScheduler(config)
        gs.request_gpu("agent1", duration_min=30)
        assert gs.current_holder == "agent1"
        gs.release_gpu("agent1")
        assert gs.current_holder == ""

    def test_release_gpu_wrong_agent(self, config):
        """Releasing from wrong agent does nothing."""
        config["coordination"]["schedule_path"] = ""
        gs = GpuScheduler(config)
        gs.request_gpu("agent1", duration_min=30)
        gs.release_gpu("imposter")
        assert gs.current_holder == "agent1"

    def test_get_status_available(self, config):
        """Status shows available when no holder."""
        config["gpu"]["current_holder"] = ""
        config["gpu"]["holder_expires"] = ""
        gs = GpuScheduler(config)
        status = gs.get_status()
        assert status["is_available"] is True
        assert status["current_holder"] == ""

    def test_get_status_held(self, config):
        """Status shows unavailable when holder has time remaining."""
        future = (datetime.utcnow() + timedelta(minutes=20)).isoformat()
        config["gpu"]["current_holder"] = "agent1"
        config["gpu"]["holder_expires"] = future
        gs = GpuScheduler(config)
        status = gs.get_status()
        assert status["is_available"] is False

    def test_find_best_window_available(self, config):
        """Best window is now when GPU is free."""
        config["gpu"]["current_holder"] = ""
        gs = GpuScheduler(config)
        window = gs.find_best_window(60)
        # Should be close to now
        now_iso = datetime.utcnow().isoformat()
        assert window[:10] == now_iso[:10]

    def test_find_best_window_held(self, config):
        """Best window is after current holder expires."""
        future = (datetime.utcnow() + timedelta(minutes=20)).isoformat()
        config["gpu"]["current_holder"] = "agent0"
        config["gpu"]["holder_expires"] = future
        gs = GpuScheduler(config)
        window = gs.find_best_window(60)
        assert window == future


# ============================================================
# MultiAgentCoordinator
# ============================================================

class TestMultiAgentCoordinator:
    def test_register_agent(self, config):
        """Registering an agent stores its info."""
        config["coordination"]["agents"] = {}
        coord = MultiAgentCoordinator(config)
        coord.register_agent("agent1", pid=1234, rss_limit_mb=1024)
        assert "agent1" in coord.registered
        assert coord.registered["agent1"]["pid"] == 1234
        assert coord.registered["agent1"]["rss_limit_mb"] == 1024

    def test_register_agent_defaults(self, config):
        """Registering uses default quota values."""
        config["coordination"]["agents"] = {}
        coord = MultiAgentCoordinator(config)
        coord.register_agent("agent1", pid=1234)
        assert coord.registered["agent1"]["gpu_quota_pct"] == 50
        assert coord.registered["agent1"]["priority"] == 5

    def test_get_agent_status(self, config):
        """Agent status includes RSS info."""
        snap = ResourceSnapshot(
            timestamp="t", ram_total_mb=8000, ram_used_mb=4000, ram_percent=50.0,
            swap_total_mb=2000, swap_used_mb=100, swap_percent=5.0,
            cpu_percent=20.0, disk_total_gb=100.0, disk_used_gb=50.0, disk_percent=50.0,
        )
        config["coordination"]["agents"] = {}
        coord = MultiAgentCoordinator(config)
        coord.register_agent("agent1", pid=1, rss_limit_mb=1024)
        # PID 1 won't be readable in /proc in test, so RSS = 0
        status = coord.get_agent_status(snap)
        assert "agent1" in status
        assert status["agent1"]["pid"] == 1
        assert status["agent1"]["rss_limit_mb"] == 1024

    def test_get_agent_status_empty(self, config):
        """No agents registered → empty status."""
        snap = ResourceSnapshot(
            timestamp="t", ram_total_mb=8000, ram_used_mb=4000, ram_percent=50.0,
            swap_total_mb=2000, swap_used_mb=100, swap_percent=5.0,
            cpu_percent=20.0, disk_total_gb=100.0, disk_used_gb=50.0, disk_percent=50.0,
        )
        config["coordination"]["agents"] = {}
        coord = MultiAgentCoordinator(config)
        assert coord.get_agent_status(snap) == {}


# ============================================================
# OperationalLogger
# ============================================================

class TestOperationalLogger:
    def test_log_creates_file(self, tmp_log_dir):
        """Logging creates the log file."""
        logger = OperationalLogger(tmp_log_dir)
        logger.log("operational", {"msg": "test"})
        log_file = Path(tmp_log_dir["logs"]["dir"]) / "operations.log"
        assert log_file.exists()

    def test_log_json_format(self, tmp_log_dir):
        """Logged dict is written as JSON line."""
        logger = OperationalLogger(tmp_log_dir)
        data = {"timestamp": "2025-01-01", "msg": "hello"}
        logger.log("operational", data)
        log_file = Path(tmp_log_dir["logs"]["dir"]) / "operations.log"
        content = log_file.read_text().strip()
        parsed = json.loads(content)
        assert parsed["msg"] == "hello"

    def test_log_string_passthrough(self, tmp_log_dir):
        """String data is written as-is."""
        logger = OperationalLogger(tmp_log_dir)
        logger.log("operational", "plain text line")
        log_file = Path(tmp_log_dir["logs"]["dir"]) / "operations.log"
        assert log_file.read_text().strip() == "plain text line"

    def test_log_different_types(self, tmp_log_dir):
        """Different log types go to different files."""
        logger = OperationalLogger(tmp_log_dir)
        logger.log("alert", {"level": "warning"})
        logger.log("process", {"event": "started"})
        log_dir = Path(tmp_log_dir["logs"]["dir"])
        assert (log_dir / "alerts.log").exists()
        assert (log_dir / "processes.log").exists()


# ============================================================
# SelfHealer
# ============================================================

class TestSelfHealer:
    def test_heal_restarts_gateway(self, tmp_log_dir):
        """Healer restarts gateway when it stops."""
        tmp_log_dir["healing"]["auto_restart_gateway"] = True
        logger = OperationalLogger(tmp_log_dir)
        healer = SelfHealer(tmp_log_dir, logger)

        snap = ResourceSnapshot(
            timestamp="t", ram_total_mb=8000, ram_used_mb=4000, ram_percent=50.0,
            swap_total_mb=2000, swap_used_mb=100, swap_percent=5.0,
            cpu_percent=20.0, disk_total_gb=100.0, disk_used_gb=50.0, disk_percent=50.0,
        )
        events = [{"event_type": "stopped", "process_name": "openclaw-gateway", "pid": 1234}]

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            actions = healer.heal(snap, events)

        assert any("Restarted gateway" in a for a in actions)
        mock_run.assert_called_once()

    def test_heal_does_not_restart_non_gateway(self, tmp_log_dir):
        """Healer only restarts gateway processes, not other stopped processes."""
        tmp_log_dir["healing"]["auto_restart_gateway"] = True
        logger = OperationalLogger(tmp_log_dir)
        healer = SelfHealer(tmp_log_dir, logger)

        snap = ResourceSnapshot(
            timestamp="t", ram_total_mb=8000, ram_used_mb=4000, ram_percent=50.0,
            swap_total_mb=2000, swap_used_mb=100, swap_percent=5.0,
            cpu_percent=20.0, disk_total_gb=100.0, disk_used_gb=50.0, disk_percent=50.0,
        )
        events = [{"event_type": "stopped", "process_name": "some-other-service", "pid": 5678}]

        with patch("subprocess.run") as mock_run:
            actions = healer.heal(snap, events)

        mock_run.assert_not_called()

    def test_heal_cleans_tmp_on_high_disk(self, tmp_log_dir):
        """Healer cleans /tmp when disk usage is high."""
        tmp_log_dir["healing"]["auto_clean_tmp"] = True
        tmp_log_dir["thresholds"]["disk_warning"] = 85
        logger = OperationalLogger(tmp_log_dir)
        healer = SelfHealer(tmp_log_dir, logger)

        snap = ResourceSnapshot(
            timestamp="t", ram_total_mb=8000, ram_used_mb=4000, ram_percent=50.0,
            swap_total_mb=2000, swap_used_mb=100, swap_percent=5.0,
            cpu_percent=20.0, disk_total_gb=100.0, disk_used_gb=90.0, disk_percent=90.0,
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            actions = healer.heal(snap, [])

        assert any("Cleaned" in a for a in actions)

    def test_heal_no_action_on_healthy_system(self, tmp_log_dir):
        """No healing actions when system is healthy."""
        logger = OperationalLogger(tmp_log_dir)
        healer = SelfHealer(tmp_log_dir, logger)

        snap = ResourceSnapshot(
            timestamp="t", ram_total_mb=8000, ram_used_mb=4000, ram_percent=50.0,
            swap_total_mb=2000, swap_used_mb=100, swap_percent=5.0,
            cpu_percent=20.0, disk_total_gb=100.0, disk_used_gb=40.0, disk_percent=40.0,
        )

        with patch("subprocess.run") as mock_run:
            actions = healer.heal(snap, [])

        mock_run.assert_not_called()
        assert actions == []


# ============================================================
# BrothersKeeper (Main Orchestration)
# ============================================================

class TestBrothersKeeper:
    def _make_keeper(self, tmp_path):
        """Create a BrothersKeeper with log dir in tmp_path."""
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["logs"]["dir"] = str(tmp_path)
        return BrothersKeeper(cfg)

    def test_construction_default_config(self, tmp_path):
        """Keeper constructs with default config and all components."""
        keeper = self._make_keeper(tmp_path)
        assert keeper.monitor is not None
        assert keeper.watchdog is not None
        assert keeper.logger is not None
        assert keeper.healer is not None
        assert keeper.flywheel is not None
        assert keeper.token_steward is not None
        assert keeper.gpu_scheduler is not None
        assert keeper.coordinator is not None
        assert keeper.running is True

    def test_construction_disabled_components(self, tmp_path):
        """Components can be disabled via config."""
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["logs"]["dir"] = str(tmp_path)
        cfg["flywheel"]["enabled"] = False
        cfg["gpu"]["enabled"] = False
        cfg["token_steward"]["enabled"] = False
        cfg["coordination"]["enabled"] = False
        keeper = BrothersKeeper(cfg)
        assert keeper.flywheel is None
        assert keeper.gpu_scheduler is None
        assert keeper.token_steward is None
        assert keeper.coordinator is None

    def test_stop_sets_running_false(self, tmp_path):
        keeper = self._make_keeper(tmp_path)
        assert keeper.running is True
        keeper.stop()
        assert keeper.running is False

    def test_request_gpu_passthrough(self, tmp_path):
        """request_gpu delegates to GpuScheduler."""
        keeper = self._make_keeper(tmp_path)
        with patch.object(keeper.gpu_scheduler, "request_gpu", return_value=(True, "OK")):
            ok, msg = keeper.request_gpu("agent1", 30)
        assert ok is True

    def test_request_gpu_disabled(self, tmp_path):
        """request_gpu returns True when scheduler is disabled."""
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["logs"]["dir"] = str(tmp_path)
        cfg["gpu"]["enabled"] = False
        keeper = BrothersKeeper(cfg)
        ok, msg = keeper.request_gpu("agent1", 30)
        assert ok is True

    def test_release_gpu_passthrough(self, tmp_path):
        """release_gpu delegates to GpuScheduler."""
        keeper = self._make_keeper(tmp_path)
        with patch.object(keeper.gpu_scheduler, "release_gpu") as mock_release:
            keeper.release_gpu("agent1")
        mock_release.assert_called_once_with("agent1")

    def test_request_tokens_passthrough(self, tmp_path):
        """request_tokens delegates to TokenSteward."""
        keeper = self._make_keeper(tmp_path)
        with patch.object(keeper.token_steward, "request_tokens", return_value=(True, "key")):
            ok, key = keeper.request_tokens("agent1", "openai", 0.5)
        assert ok is True

    def test_alert_coalescing(self, tmp_path):
        """Alerts with same key are coalesced within cooldown period."""
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["logs"]["dir"] = str(tmp_path)
        keeper = BrothersKeeper(cfg)
        keeper._alert("resource", "RAM warning", "85%")
        keeper._alert("resource", "RAM warning", "86%")  # Should be suppressed
        keeper._alert("resource", "CPU warning", "90%")   # Different key, should fire
        # Check alert log — should have 2 entries, not 3
        alert_log = tmp_path / "alerts.log"
        lines = alert_log.read_text().strip().split("\n") if alert_log.exists() else []
        # At least RAM warning and CPU warning, but not duplicate RAM
        assert len(lines) == 2


# ============================================================
# ResourceMonitor edge cases
# ============================================================

class TestResourceMonitor:
    def test_snapshot_returns_resource_snapshot(self):
        """snapshot() always returns a ResourceSnapshot."""
        rm = ResourceMonitor()
        snap = rm.snapshot()
        assert isinstance(snap, ResourceSnapshot)
        assert snap.timestamp != ""
        assert snap.ram_total_mb >= 0

    def test_read_meminfo_handles_missing_proc(self):
        """_read_meminfo handles missing /proc/meminfo gracefully."""
        rm = ResourceMonitor()
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = rm._read_meminfo()
        assert result == {"total": 0, "used": 0}

    def test_read_disk_handles_df_failure(self):
        """_read_disk handles subprocess failure gracefully."""
        rm = ResourceMonitor()
        with patch("subprocess.run", side_effect=Exception("no df")):
            result = rm._read_disk()
        assert result == {"total": 0, "used": 0, "percent": 0}

    def test_read_gpu_disabled(self):
        """_read_gpu returns zeros when disabled."""
        rm = ResourceMonitor()
        result = rm._read_gpu({"gpu": {"enabled": False}})
        assert result == {"mem_used": 0, "mem_total": 0, "util_pct": 0}

    def test_read_gpu_no_config(self):
        """_read_gpu handles None config gracefully."""
        rm = ResourceMonitor()
        result = rm._read_gpu(None)
        assert result == {"mem_used": 0, "mem_total": 0, "util_pct": 0}
