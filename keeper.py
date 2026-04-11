#!/usr/bin/env python3
"""
brothers-keeper/keeper.py — The Lighthouse Keeper

External watchdog for agent runtimes (OpenClaw, ZeroClaw, or any git-agent).
Runs as a separate process on the same hardware.
Independent of the agent's instance — survives agent crashes.

Core responsibilities:
1. RESOURCE MONITORING — RAM, CPU, disk, network. Alert before OOM.
2. PROCESS WATCHDOG — Track agent processes. Restart if stuck/crashed.
3. RISK ASSESSMENT — Pre-flight checks before dangerous operations.
4. OPERATIONAL LOGGING — External observation of inputs/outputs/data changes.
5. SELF-HEALING — Run openclaw doctor --fix, restart gateway, clean state.
6. BEACON — Light up when the fleet needs to know.

Design principle: The keeper is NOT the ship. The keeper is the lighthouse.
It observes, warns, and assists — but does not navigate.
"""

import os
import sys
import json
import time
import signal
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_CONFIG = {
    # What to watch
    "watch_processes": [
        {"name": "openclaw-gateway", "cmd": "pgrep -f 'openclaw.*gateway'"},
        {"name": "node-agent", "cmd": "pgrep -f 'openclaw.*agent'"},
    ],

    # Resource thresholds (percentage or absolute)
    "thresholds": {
        "ram_warning": 80,      # Alert at 80% RAM
        "ram_critical": 90,     # Kill processes at 90%
        "disk_warning": 85,     # Alert at 85% disk
        "cpu_warning": 95,      # Alert at 95% CPU sustained
        "cpu_sustain_sec": 30,  # How long CPU must be high to trigger
        "swap_warning": 50,     # Alert at 50% swap usage
    },

    # Process management
    "process": {
        "check_interval_sec": 30,       # How often to check
        "restart_cooldown_sec": 300,    # Don't restart more than once per 5min
        "max_restart_attempts": 3,      # Give up after 3 tries
        "kill_after_stuck_sec": 300,    # Kill process stuck for 5 min
    },

    # Risk assessment
    "risk": {
        "max_concurrent_execs": 5,      # Max simultaneous subprocess spawns
        "max_single_process_mb": 512,   # Kill process exceeding this RSS
        "dangerous_patterns": [
            "fork bomb", "while true",
        ],
        "pre_flight_check": True,       # Check resources before heavy ops
    },

    # Logging
    "logs": {
        "dir": "/var/log/brothers-keeper",
        "operational": "operations.log",    # What happened outside
        "resource": "resources.log",         # Resource snapshots
        "alert": "alerts.log",               # Warnings and emergencies
        "process": "processes.log",          # Process lifecycle events
        "max_log_mb": 100,                   # Rotate logs at 100MB
        "retention_days": 30,                # Keep logs for 30 days
    },

    # Self-healing actions
    "healing": {
        "auto_restart_gateway": True,
        "auto_doctor_fix": True,
        "auto_clean_tmp": True,
        "tmp_max_gb": 5,
        "notify_on_heal": True,
    },

    # Beacon (alerting)
    "beacon": {
        "method": "log",           # "log", "telegram", "webhook"
        "webhook_url": "",         # For external alerting
        "telegram_chat_id": "",    # For Telegram alerts
        "coalesce_sec": 300,       # Don't alert more than once per 5min
    },
}


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class ResourceSnapshot:
    timestamp: str
    ram_total_mb: int
    ram_used_mb: int
    ram_percent: float
    swap_total_mb: int
    swap_used_mb: int
    swap_percent: float
    cpu_percent: float
    disk_total_gb: float
    disk_used_gb: float
    disk_percent: float
    load_1m: float
    load_5m: float
    load_15m: float
    openclaw_rss_mb: int = 0
    top_processes: List[Dict] = field(default_factory=list)


@dataclass
class ProcessEvent:
    timestamp: str
    event_type: str  # "started", "stopped", "restarted", "killed", "stuck"
    process_name: str
    pid: Optional[int] = None
    details: str = ""


@dataclass
class OperationalChange:
    timestamp: str
    category: str  # "input", "output", "data", "config", "network", "commit"
    description: str
    before: Optional[str] = None
    after: Optional[str] = None
    severity: str = "info"  # "info", "warning", "critical"


@dataclass
class Alert:
    timestamp: str
    level: str  # "warning", "critical", "resolved"
    category: str  # "resource", "process", "risk", "healing"
    message: str
    action_taken: str = ""
    resolved_at: Optional[str] = None


# ============================================================
# RESOURCE MONITOR
# ============================================================

class ResourceMonitor:
    """Read system vitals from /proc (Linux)."""

    def snapshot(self) -> ResourceSnapshot:
        ram = self._read_meminfo()
        swap = self._read_swap()
        cpu = self._read_cpu()
        disk = self._read_disk()
        load = self._read_load()
        oc_rss = self._read_openclaw_rss()
        top = self._read_top_processes(5)

        ram_pct = (ram['used'] / ram['total'] * 100) if ram['total'] > 0 else 0
        swap_pct = (swap['used'] / swap['total'] * 100) if swap['total'] > 0 else 0

        return ResourceSnapshot(
            timestamp=datetime.utcnow().isoformat(),
            ram_total_mb=ram['total'],
            ram_used_mb=ram['used'],
            ram_percent=ram_pct,
            swap_total_mb=swap['total'],
            swap_used_mb=swap['used'],
            swap_percent=swap_pct,
            cpu_percent=cpu,
            disk_total_gb=disk['total'],
            disk_used_gb=disk['used'],
            disk_percent=disk['percent'],
            load_1m=load[0],
            load_5m=load[1],
            load_15m=load[2],
            openclaw_rss_mb=oc_rss,
            top_processes=top,
        )

    def _read_meminfo(self):
        info = {'total': 0, 'used': 0}
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        info['total'] = int(line.split()[1]) // 1024
                    elif line.startswith('MemAvailable:'):
                        available = int(line.split()[1]) // 1024
                        info['used'] = info['total'] - available
        except: pass
        return info

    def _read_swap(self):
        info = {'total': 0, 'used': 0}
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('SwapTotal:'):
                        info['total'] = int(line.split()[1]) // 1024
                    elif line.startswith('SwapFree:'):
                        free = int(line.split()[1]) // 1024
                        info['used'] = info['total'] - free
        except: pass
        return info

    def _read_cpu(self):
        try:
            with open('/proc/stat') as f:
                line = f.readline()
            vals = [int(x) for x in line.split()[1:]]
            idle = vals[3]
            total = sum(vals)
            time.sleep(0.1)
            with open('/proc/stat') as f:
                line = f.readline()
            vals2 = [int(x) for x in line.split()[1:]]
            idle2 = vals2[3]
            total2 = sum(vals2)
            d_idle = idle2 - idle
            d_total = total2 - total
            return (1.0 - d_idle / d_total) * 100 if d_total > 0 else 0
        except:
            return 0

    def _read_disk(self):
        try:
            r = subprocess.run(['df', '/'], capture_output=True, text=True, timeout=5)
            line = r.stdout.strip().split('\n')[1]
            parts = line.split()
            total = int(parts[1]) // (1024*1024)
            used = int(parts[2]) // (1024*1024)
            pct = float(parts[4].rstrip('%'))
            return {'total': total, 'used': used, 'percent': pct}
        except:
            return {'total': 0, 'used': 0, 'percent': 0}

    def _read_load(self):
        try:
            with open('/proc/loadavg') as f:
                parts = f.read().split()
            return (float(parts[0]), float(parts[1]), float(parts[2]))
        except:
            return (0, 0, 0)

    def _read_openclaw_rss(self):
        try:
            r = subprocess.run(['pgrep', '-f', 'openclaw'], capture_output=True, text=True, timeout=5)
            if r.returncode != 0 or not r.stdout.strip():
                return 0
            total_rss = 0
            for pid in r.stdout.strip().split('\n'):
                try:
                    with open(f'/proc/{pid}/status') as f:
                        for line in f:
                            if line.startswith('VmRSS:'):
                                total_rss += int(line.split()[1])
                except: pass
            return total_rss // 1024  # KB to MB
        except:
            return 0

    def _read_top_processes(self, n=5):
        try:
            r = subprocess.run(
                ['ps', 'aux', '--sort=-%mem'],
                capture_output=True, text=True, timeout=5
            )
            lines = r.stdout.strip().split('\n')[1:n+1]
            procs = []
            for line in lines:
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    procs.append({
                        'user': parts[0], 'pid': int(parts[1]),
                        'cpu': float(parts[2]), 'mem': float(parts[3]),
                        'rss_mb': int(parts[5]) // 1024,
                        'command': parts[10][:80]
                    })
            return procs
        except:
            return []


# ============================================================
# PROCESS WATCHDOG
# ============================================================

class ProcessWatchdog:
    """Track agent processes, detect hangs, restart when needed."""

    def __init__(self, config):
        self.config = config
        self.restart_times: Dict[str, datetime] = {}
        self.restart_counts: Dict[str, int] = {}
        self.last_seen: Dict[str, int] = {}  # name -> PID

    def check(self) -> List[ProcessEvent]:
        events = []
        for proc in self.config['watch_processes']:
            name = proc['name']
            try:
                r = subprocess.run(proc['cmd'], shell=True, capture_output=True, text=True, timeout=5)
                current_pid = int(r.stdout.strip()) if r.stdout.strip() else None
            except:
                current_pid = None

            if current_pid:
                if name not in self.last_seen:
                    events.append(ProcessEvent(
                        timestamp=datetime.utcnow().isoformat(),
                        event_type="started", process_name=name, pid=current_pid
                    ))
                    self.last_seen[name] = current_pid
                elif self.last_seen[name] != current_pid:
                    events.append(ProcessEvent(
                        timestamp=datetime.utcnow().isoformat(),
                        event_type="restarted", process_name=name,
                        pid=current_pid,
                        details=f"PID changed from {self.last_seen[name]}"
                    ))
                    self.last_seen[name] = current_pid
            else:
                if name in self.last_seen:
                    events.append(ProcessEvent(
                        timestamp=datetime.utcnow().isoformat(),
                        event_type="stopped", process_name=name,
                        pid=self.last_seen.get(name),
                        details="Process no longer running"
                    ))
                    del self.last_seen[name]

        return events

    def should_restart(self, name: str) -> bool:
        now = datetime.utcnow()
        cooldown = timedelta(seconds=self.config['process']['restart_cooldown_sec'])
        if name in self.restart_times and (now - self.restart_times[name]) < cooldown:
            return False
        if self.restart_counts.get(name, 0) >= self.config['process']['max_restart_attempts']:
            return False
        return True

    def restart(self, name: str, method: str = "gateway") -> bool:
        if not self.should_restart(name):
            return False

        self.restart_times[name] = datetime.utcnow()
        self.restart_counts[name] = self.restart_counts.get(name, 0) + 1

        try:
            if method == "gateway":
                subprocess.run(['openclaw', 'gateway', 'restart'],
                             capture_output=True, timeout=30)
            elif method == "doctor":
                subprocess.run(['openclaw', 'doctor', '--fix'],
                             capture_output=True, timeout=60)
            return True
        except Exception as e:
            return False


# ============================================================
# RISK ASSESSOR
# ============================================================

class RiskAssessor:
    """Pre-flight checks and dangerous operation detection."""

    def __init__(self, config):
        self.config = config
        self.active_execs: Dict[int, Dict] = {}

    def pre_flight(self, snapshot: ResourceSnapshot) -> tuple:
        """Returns (approved: bool, warnings: list)."""
        warnings = []
        approved = True

        thresholds = self.config['thresholds']
        if snapshot.ram_percent > thresholds['ram_critical']:
            warnings.append(f"RAM critical: {snapshot.ram_percent:.1f}%")
            approved = False
        elif snapshot.ram_percent > thresholds['ram_warning']:
            warnings.append(f"RAM warning: {snapshot.ram_percent:.1f}%")

        if snapshot.swap_percent > thresholds['swap_warning']:
            warnings.append(f"Swap warning: {snapshot.swap_percent:.1f}%")

        if snapshot.openclaw_rss_mb > self.config['risk']['max_single_process_mb']:
            warnings.append(f"OpenClaw RSS high: {snapshot.openclaw_rss_mb}MB")
            approved = False

        if len(self.active_execs) >= self.config['risk']['max_concurrent_execs']:
            warnings.append(f"Too many concurrent execs: {len(self.active_execs)}")
            approved = False

        return approved, warnings

    def check_process_risk(self, pid: int, snapshot: ResourceSnapshot) -> str:
        """Check if a specific process is dangerous. Returns risk level."""
        for proc in snapshot.top_processes:
            if proc['pid'] == pid:
                if proc['rss_mb'] > self.config['risk']['max_single_process_mb']:
                    return "critical"
                if proc['cpu'] > 95:
                    return "warning"
        return "ok"

    def register_exec(self, exec_id: int, info: Dict):
        self.active_execs[exec_id] = {**info, 'started': datetime.utcnow().isoformat()}

    def unregister_exec(self, exec_id: int):
        self.active_execs.pop(exec_id, None)


# ============================================================
# OPERATIONAL LOGGER
# ============================================================

class OperationalLogger:
    """Logs external changes, not internal diaries.

    The keeper records:
    - Changes to data the agent taps (API responses, file changes)
    - Network traffic patterns and latency
    - Commit activity and repo state changes
    - Resource consumption trends
    - Process lifecycle events

    The keeper does NOT record:
    - The agent's internal reasoning
    - Conversation content
    - Skill execution details
    """

    def __init__(self, config):
        self.config = config
        log_dir = Path(config['logs']['dir'])
        log_dir.mkdir(parents=True, exist_ok=True)

    def log_operation(self, change: OperationalChange):
        self._append('operational', json.dumps(asdict(change)))

    def log_resources(self, snapshot: ResourceSnapshot):
        self._append('resource', json.dumps(asdict(snapshot)))

    def log_alert(self, alert: Alert):
        self._append('alert', json.dumps(asdict(alert)))

    def log_process(self, event: ProcessEvent):
        self._append('process', json.dumps(asdict(event)))

    def _append(self, log_type: str, line: str):
        filename = self.config['logs'].get(log_type, f'{log_type}.log')
        path = Path(self.config['logs']['dir']) / filename
        try:
            with open(path, 'a') as f:
                f.write(line + '\n')
        except Exception as e:
            print(f"[keeper] log write error: {e}", file=sys.stderr)

    def snapshot_state(self) -> Dict:
        """Capture current external state for comparison."""
        state = {}
        # Git status of key repos
        workspace = Path(os.environ.get('OPENCLAW_WORKSPACE', '/home/lucineer/.openclaw/workspace'))
        if workspace.exists():
            try:
                r = subprocess.run(['git', 'status', '--porcelain'],
                                 capture_output=True, text=True, cwd=workspace, timeout=10)
                state['workspace_dirty_files'] = len(r.stdout.strip().split('\n')) if r.stdout.strip() else 0
            except: pass

            try:
                r = subprocess.run(['git', 'log', '-1', '--format=%H %s'],
                                 capture_output=True, text=True, cwd=workspace, timeout=10)
                state['workspace_last_commit'] = r.stdout.strip()
            except: pass

        # Network connectivity check
        state['network_up'] = os.system('ping -c1 -W2 8.8.8.8 >/dev/null 2>&1') == 0

        # OpenClaw gateway status
        try:
            r = subprocess.run(['openclaw', 'gateway', 'status'],
                             capture_output=True, text=True, timeout=10)
            state['gateway_status'] = r.stdout.strip()[:200]
        except:
            state['gateway_status'] = 'unreachable'

        return state


# ============================================================
# SELF-HEALER
# ============================================================

class SelfHealer:
    """Automatic recovery actions when the agent is in trouble."""

    def __init__(self, config, logger: OperationalLogger):
        self.config = config
        self.logger = logger

    def heal(self, snapshot: ResourceSnapshot, events: List[ProcessEvent]) -> List[str]:
        actions = []
        healing = self.config['healing']

        # Check if gateway is down
        gateway_stopped = any(
            e.event_type == "stopped" and "gateway" in e.process_name
            for e in events
        )

        if gateway_stopped and healing['auto_restart_gateway']:
            actions.append("Restarting openclaw gateway...")
            try:
                subprocess.run(['openclaw', 'gateway', 'restart'],
                             capture_output=True, timeout=30)
                self.logger.log_operation(OperationalChange(
                    timestamp=datetime.utcnow().isoformat(),
                    category="process", description="Gateway auto-restarted by keeper",
                    severity="warning"
                ))
            except Exception as e:
                actions.append(f"Gateway restart failed: {e}")

        # Clean /tmp if disk is getting full
        if healing['auto_clean_tmp'] and snapshot.disk_percent > self.config['thresholds']['disk_warning']:
            actions.append("Cleaning /tmp...")
            try:
                cleaned = subprocess.run(
                    ['find', '/tmp', '-type', 'f', '-mtime', '+1', '-delete'],
                    capture_output=True, timeout=30
                )
                actions.append(f"Cleaned old /tmp files")
            except Exception as e:
                actions.append(f"Tmp clean failed: {e}")

        return actions

    def kill_risky_process(self, pid: int, reason: str) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            os.kill(pid, signal.SIGKILL)
            self.logger.log_operation(OperationalChange(
                timestamp=datetime.utcnow().isoformat(),
                category="process", description=f"Killed PID {pid}: {reason}",
                severity="critical"
            ))
            return True
        except:
            return False


# ============================================================
# THE KEEPER — Main Loop
# ============================================================

class BrothersKeeper:
    """The Lighthouse Keeper.

    Sits on the shore. Watches the waters. Lights the beacon when needed.
    """

    def __init__(self, config: Dict = None):
        self.config = config or DEFAULT_CONFIG
        self.monitor = ResourceMonitor()
        self.watchdog = ProcessWatchdog(self.config)
        self.risk = RiskAssessor(self.config)
        self.logger = OperationalLogger(self.config)
        self.healer = SelfHealer(self.config, self.logger)
        self.running = True
        self.last_alert_time: Dict[str, datetime] = {}
        self.cpu_history: List[float] = []

    def start(self, check_interval: int = 30):
        """Main watch loop."""
        print(f"[keeper] Brothers Keeper starting. Check interval: {check_interval}s", flush=True)
        self.logger.log_operation(OperationalChange(
            timestamp=datetime.utcnow().isoformat(),
            category="config", description="Brothers Keeper started",
            severity="info"
        ))

        while self.running:
            try:
                self._tick()
            except Exception as e:
                print(f"[keeper] tick error: {e}", file=sys.stderr)
            time.sleep(check_interval)

    def _tick(self):
        # 1. Take resource snapshot
        snapshot = self.monitor.snapshot()
        self.logger.log_resources(snapshot)
        self.cpu_history.append(snapshot.cpu_percent)
        if len(self.cpu_history) > 100:
            self.cpu_history = self.cpu_history[-100:]

        # 2. Check processes
        events = self.watchdog.check()
        for event in events:
            self.logger.log_process(event)
            if event.event_type in ("stopped", "stuck"):
                self._alert("process", f"{event.process_name}: {event.event_type}",
                           f"PID {event.pid}: {event.details}")

        # 3. Check resource thresholds
        thresholds = self.config['thresholds']
        if snapshot.ram_percent > thresholds['ram_critical']:
            self._alert("resource", "RAM critical",
                       f"{snapshot.ram_percent:.1f}% ({snapshot.ram_used_mb}/{snapshot.ram_total_mb}MB)")
            # Kill highest memory process if not the agent itself
            self._emergency_ram_cleanup(snapshot)
        elif snapshot.ram_percent > thresholds['ram_warning']:
            self._alert("resource", "RAM warning",
                       f"{snapshot.ram_percent:.1f}% ({snapshot.ram_used_mb}/{snapshot.ram_total_mb}MB)")

        if snapshot.swap_percent > thresholds['swap_warning']:
            self._alert("resource", "Swap pressure",
                       f"{snapshot.swap_percent:.1f}% ({snapshot.swap_used_mb}MB)")

        if snapshot.disk_percent > thresholds['disk_warning']:
            self._alert("resource", "Disk filling up",
                       f"{snapshot.disk_percent:.1f}% ({snapshot.disk_used_gb:.1f}GB)")

        # Sustained high CPU
        if len(self.cpu_history) >= 10:
            recent = self.cpu_history[-10:]
            if all(c > thresholds['cpu_warning'] for c in recent):
                self._alert("resource", "Sustained high CPU",
                           f"10/10 samples above {thresholds['cpu_warning']}%")

        # 4. Self-heal if needed
        actions = self.healer.heal(snapshot, events)
        for action in actions:
            print(f"[keeper] heal: {action}", flush=True)

        # 5. Periodically snapshot external state
        state = self.logger.snapshot_state()
        if not state.get('network_up'):
            self._alert("resource", "Network down", "8.8.8.8 unreachable")

    def _alert(self, category: str, title: str, detail: str):
        """Send alert with cooldown."""
        key = f"{category}:{title}"
        now = datetime.utcnow()
        cooldown = timedelta(seconds=self.config['beacon']['coalesce_sec'])

        if key in self.last_alert_time and (now - self.last_alert_time[key]) < cooldown:
            return
        self.last_alert_time[key] = now

        alert = Alert(
            timestamp=now.isoformat(),
            level="warning" if "critical" not in title.lower() else "critical",
            category=category,
            message=f"{title}: {detail}"
        )
        self.logger.log_alert(alert)

        method = self.config['beacon']['method']
        if method == "telegram" and self.config['beacon']['telegram_chat_id']:
            self._send_telegram(alert)
        elif method == "webhook" and self.config['beacon']['webhook_url']:
            self._send_webhook(alert)
        else:
            print(f"[keeper] ALERT [{alert.level}] {category}: {title} — {detail}", flush=True)

    def _emergency_ram_cleanup(self, snapshot: ResourceSnapshot):
        """Kill non-essential high-memory processes."""
        killed = []
        for proc in snapshot.top_processes:
            if proc['rss_mb'] > self.config['risk']['max_single_process_mb']:
                cmd = proc['command'].lower()
                # Don't kill the agent itself
                if any(skip in cmd for skip in ['openclaw', 'keeper', 'systemd', 'init']):
                    continue
                if self.healer.kill_risky_process(proc['pid'], f"RSS {proc['rss_mb']}MB exceeds limit"):
                    killed.append(f"{proc['command'][:40]} (PID {proc['pid']}, {proc['rss_mb']}MB)")
        if killed:
            self.logger.log_operation(OperationalChange(
                timestamp=datetime.utcnow().isoformat(),
                category="process",
                description=f"Emergency RAM cleanup: killed {len(killed)} processes",
                before=", ".join(killed),
                severity="critical"
            ))

    def _send_telegram(self, alert: Alert):
        try:
            import requests
            url = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN')}/sendMessage"
            requests.post(url, json={
                'chat_id': self.config['beacon']['telegram_chat_id'],
                'text': f"🪨 [{alert.level.upper()}] {alert.category}\n{alert.message}",
                'parse_mode': 'HTML'
            }, timeout=10)
        except: pass

    def _send_webhook(self, alert: Alert):
        try:
            import requests
            requests.post(self.config['beacon']['webhook_url'],
                        json=asdict(alert), timeout=10)
        except: pass

    def pre_flight_check(self) -> tuple:
        """Public API for agents to check before heavy operations."""
        snapshot = self.monitor.snapshot()
        return self.risk.pre_flight(snapshot)

    def stop(self):
        self.running = False
        self.logger.log_operation(OperationalChange(
            timestamp=datetime.utcnow().isoformat(),
            category="config", description="Brothers Keeper stopped",
            severity="info"
        ))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Brothers Keeper — The Lighthouse Keeper")
    parser.add_argument('--config', '-c', help='Path to config JSON')
    parser.add_argument('--interval', '-i', type=int, default=30, help='Check interval in seconds')
    parser.add_argument('--once', action='store_true', help='Run one check and exit')
    parser.add_argument('--status', action='store_true', help='Print current status and exit')
    parser.add_argument('--preflight', action='store_true', help='Pre-flight resource check')
    args = parser.parse_args()

    config = DEFAULT_CONFIG.copy()
    if args.config and os.path.exists(args.config):
        with open(args.config) as f:
            config.update(json.load(f))

    keeper = BrothersKeeper(config)

    if args.status:
        snapshot = keeper.monitor.snapshot()
        print(json.dumps(asdict(snapshot), indent=2))
        return

    if args.preflight:
        approved, warnings = keeper.pre_flight_check()
        if approved:
            print("CLEAR — resources available")
        else:
            print(f"HOLD — {'; '.join(warnings)}")
        return

    if args.once:
        keeper._tick()
        return

    signal.signal(signal.SIGINT, lambda s, f: keeper.stop())
    signal.signal(signal.SIGTERM, lambda s, f: keeper.stop())
    keeper.start(args.interval)


if __name__ == '__main__':
    main()
