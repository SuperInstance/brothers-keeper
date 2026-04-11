#!/usr/bin/env python3
"""
brothers-keeper/keeper.py v2 — The Lighthouse Keeper

External watchdog for agent runtimes (OpenClaw, ZeroClaw, any git-agent).
Runs as a separate process on the same hardware.
Survives agent crashes, OOMs, deadlocks.

v1: Resource monitoring, process watchdog, self-healing, operational logging
v2: Flywheel monitoring, GPU scheduling, token stewardship, multi-agent coordination

Design: The keeper is NOT the ship. The keeper is the lighthouse.
"""

import os
import sys
import json
import time
import signal
import subprocess
import threading
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Tuple


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_CONFIG = {
    # What to watch
    "watch_processes": [
        {"name": "openclaw-gateway", "cmd": "pgrep -f 'openclaw.*gateway'"},
        {"name": "node-agent", "cmd": "pgrep -f 'openclaw.*agent'"},
    ],

    # Resource thresholds
    "thresholds": {
        "ram_warning": 80, "ram_critical": 90,
        "disk_warning": 85, "cpu_warning": 95,
        "cpu_sustain_sec": 30, "swap_warning": 50,
    },

    # Process management
    "process": {
        "check_interval_sec": 60,
        "restart_cooldown_sec": 300,
        "max_restart_attempts": 3,
    },

    # Risk assessment
    "risk": {
        "max_concurrent_execs": 5,
        "max_single_process_mb": 2048,
        "pre_flight_check": True,
    },

    # Logging
    "logs": {
        "dir": "/var/log/brothers-keeper",
        "operational": "operations.log",
        "resource": "resources.log",
        "alert": "alerts.log",
        "process": "processes.log",
        "flywheel": "flywheel.log",
        "token": "token_usage.log",
        "schedule": "schedule.log",
    },

    # Self-healing
    "healing": {
        "auto_restart_gateway": True,
        "auto_doctor_fix": True,
        "auto_clean_tmp": True,
        "tmp_max_gb": 5,
    },

    # Alerting
    "beacon": {
        "method": "log",
        "telegram_chat_id": "",
        "webhook_url": "",
        "coalesce_sec": 300,
    },

    # v2: FLYWHEEL MONITORING
    "flywheel": {
        "enabled": True,
        "idle_timeout_min": 15,        # Alert if no commits in 15 min during active session
        "stuck_timeout_min": 30,       # Alert if same checkpoint for 30 min
        "checkpoint_file": "",         # Path to file agent writes progress to
        "nudge_cooldown_min": 10,      # Don't nudge more than once per 10 min
        "git_repos": [],               # Repos to watch for commit activity
        "commit_check_interval_sec": 300,
    },

    # v2: GPU SCHEDULING
    "gpu": {
        "enabled": True,
        "slots": [                   # Named GPU time slots
            {"name": "default", "priority": 0, "max_pct": 100},
        ],
        "current_holder": "",        # Which agent currently has GPU priority
        "holder_expires": "",        # ISO timestamp when slot expires
        "monitor_cmd": "nvidia-smi",
        "max_gpu_mem_pct": 90,
    },

    # v2: TOKEN STEWARDSHIP
    "token_steward": {
        "enabled": True,
        "vault_path": "",            # Path to vault JSON (secrets keeper holds)
        "allowances": {},            # agent_name -> {provider, daily_limit, used_today, reset_at}
        "zero_trust": False,         # Strict mode: agents never see raw keys
        "checkpoint_gated": False,   # Release tokens only at approved checkpoints
    },

    # v2: MULTI-AGENT COORDINATION
    "coordination": {
        "enabled": True,
        "agents": {},                # Registered agents: {name -> {pid, rss_limit, gpu_quota, priority}}
        "max_agents": 4,             # Max concurrent agents on this hardware
        "resource_sharing": True,    # Allow agents to negotiate shared resources
        "schedule_path": "",         # Path to shared schedule file
    },
}


# ============================================================
# DATA CLASSES (v1 + v2)
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
    load_1m: float = 0
    load_5m: float = 0
    load_15m: float = 0
    openclaw_rss_mb: int = 0
    gpu_mem_used_mb: int = 0
    gpu_mem_total_mb: int = 0
    gpu_util_pct: float = 0
    top_processes: List[Dict] = field(default_factory=list)


@dataclass
class FlywheelState:
    timestamp: str
    agent_name: str
    status: str              # "spinning", "idle", "stuck", "blocked", "completed"
    current_task: str = ""
    last_commit_time: Optional[str] = None
    last_commit_repo: str = ""
    commits_this_hour: int = 0
    checkpoint_reached: Optional[str] = None
    estimated_completion: Optional[str] = None
    reason: str = ""


@dataclass
class TokenAllowance:
    timestamp: str
    agent_name: str
    provider: str
    daily_limit_usd: float
    used_today_usd: float = 0
    tokens_used: int = 0
    calls_made: int = 0
    checkpoint: Optional[str] = None
    checkpoint_approved: bool = False
    status: str = "active"    # "active", "paused", "exhausted", "revoked"


@dataclass
class ScheduleEntry:
    timestamp: str
    agent_name: str
    resource: str            # "gpu", "ram", "disk_io"
    amount: str              # "80%", "4GB", "exclusive"
    duration_min: int
    priority: int            # 0=low, 5=normal, 10=critical
    reason: str = ""
    status: str = "requested"  # "requested", "approved", "active", "completed", "denied"


# ============================================================
# RESOURCE MONITOR (v1 + GPU)
# ============================================================

class ResourceMonitor:
    def snapshot(self, config: Dict = None) -> ResourceSnapshot:
        ram = self._read_meminfo()
        swap = self._read_swap()
        cpu = self._read_cpu()
        disk = self._read_disk()
        load = self._read_load()
        oc_rss = self._read_process_rss("openclaw")
        top = self._read_top_processes(5)
        gpu = self._read_gpu(config)

        ram_pct = (ram['used'] / ram['total'] * 100) if ram['total'] > 0 else 0
        swap_pct = (swap['used'] / swap['total'] * 100) if swap['total'] > 0 else 0

        return ResourceSnapshot(
            timestamp=datetime.utcnow().isoformat(),
            ram_total_mb=ram['total'], ram_used_mb=ram['used'], ram_percent=ram_pct,
            swap_total_mb=swap['total'], swap_used_mb=swap['used'], swap_percent=swap_pct,
            cpu_percent=cpu, disk_total_gb=disk['total'], disk_used_gb=disk['used'],
            disk_percent=disk['percent'], load_1m=load[0], load_5m=load[1], load_15m=load[2],
            openclaw_rss_mb=oc_rss, gpu_mem_used_mb=gpu['mem_used'], gpu_mem_total_mb=gpu['mem_total'],
            gpu_util_pct=gpu['util_pct'], top_processes=top,
        )

    def _read_meminfo(self):
        info = {'total': 0, 'used': 0}
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        info['total'] = int(line.split()[1]) // 1024
                    elif line.startswith('MemAvailable:'):
                        info['used'] = info['total'] - int(line.split()[1]) // 1024
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
                        info['used'] = info['total'] - int(line.split()[1]) // 1024
        except: pass
        return info

    def _read_cpu(self):
        try:
            with open('/proc/stat') as f:
                vals = [int(x) for x in f.readline().split()[1:]]
            idle, total = vals[3], sum(vals)
            time.sleep(0.1)
            with open('/proc/stat') as f:
                vals2 = [int(x) for x in f.readline().split()[1:]]
            d_idle, d_total = vals2[3] - idle, sum(vals2) - total
            return (1.0 - d_idle / d_total) * 100 if d_total > 0 else 0
        except: return 0

    def _read_disk(self):
        try:
            r = subprocess.run(['df', '/'], capture_output=True, text=True, timeout=5)
            parts = r.stdout.strip().split('\n')[1].split()
            return {'total': int(parts[1])//(1024*1024), 'used': int(parts[2])//(1024*1024), 'percent': float(parts[4].rstrip('%'))}
        except: return {'total': 0, 'used': 0, 'percent': 0}

    def _read_load(self):
        try:
            with open('/proc/loadavg') as f:
                p = f.read().split()
            return (float(p[0]), float(p[1]), float(p[2]))
        except: return (0, 0, 0)

    def _read_process_rss(self, name):
        try:
            r = subprocess.run(['pgrep', '-f', name], capture_output=True, text=True, timeout=5)
            total = 0
            for pid in r.stdout.strip().split('\n'):
                if pid:
                    try:
                        with open(f'/proc/{pid}/status') as f:
                            for line in f:
                                if line.startswith('VmRSS:'):
                                    total += int(line.split()[1])
                    except: pass
            return total // 1024
        except: return 0

    def _read_top_processes(self, n=5):
        try:
            r = subprocess.run(['ps', 'aux', '--sort=-%mem'], capture_output=True, text=True, timeout=5)
            procs = []
            for line in r.stdout.strip().split('\n')[1:n+1]:
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    procs.append({'user': parts[0], 'pid': int(parts[1]), 'cpu': float(parts[2]),
                        'mem': float(parts[3]), 'rss_mb': int(parts[5])//1024, 'command': parts[10][:80]})
            return procs
        except: return []

    def _read_gpu(self, config):
        gpu = {'mem_used': 0, 'mem_total': 0, 'util_pct': 0}
        if not config or not config.get('gpu', {}).get('enabled'):
            return gpu
        try:
            cmd = config.get('gpu', {}).get('monitor_cmd', 'nvidia-smi')
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            for line in r.stdout.split('\n'):
                if 'MiB' in line and '/' in line:
                    parts = [x.strip() for x in line.split('|') if 'MiB' in x]
                    if parts:
                        nums = parts[0].replace('MiB', '').split('/')
                        gpu['mem_used'] = int(nums[0].strip())
                        gpu['mem_total'] = int(nums[1].strip()) if len(nums) > 1 else 0
                for part in line.split():
                    if part.endswith('%') and 'gpu' not in part.lower():
                        try:
                            val = int(part.rstrip('%'))
                            if 0 <= val <= 100:
                                gpu['util_pct'] = max(gpu['util_pct'], val)
                        except: pass
        except: pass
        return gpu


# ============================================================
# PROCESS WATCHDOG (v1, unchanged)
# ============================================================

class ProcessWatchdog:
    def __init__(self, config):
        self.config = config
        self.restart_times: Dict[str, datetime] = {}
        self.restart_counts: Dict[str, int] = {}
        self.last_seen: Dict[str, int] = {}

    def check(self) -> List:
        events = []
        for proc in self.config.get('watch_processes', []):
            name = proc['name']
            try:
                r = subprocess.run(proc['cmd'], shell=True, capture_output=True, text=True, timeout=5)
                pid = int(r.stdout.strip()) if r.stdout.strip() else None
            except: pid = None

            if pid:
                if name not in self.last_seen:
                    events.append({'timestamp': datetime.utcnow().isoformat(), 'event_type': 'started', 'process_name': name, 'pid': pid})
                    self.last_seen[name] = pid
                elif self.last_seen[name] != pid:
                    events.append({'timestamp': datetime.utcnow().isoformat(), 'event_type': 'restarted', 'process_name': name, 'pid': pid})
                    self.last_seen[name] = pid
            elif name in self.last_seen:
                events.append({'timestamp': datetime.utcnow().isoformat(), 'event_type': 'stopped', 'process_name': name, 'pid': self.last_seen.get(name)})
                del self.last_seen[name]
        return events

    def should_restart(self, name):
        now = datetime.utcnow()
        cd = timedelta(seconds=self.config['process']['restart_cooldown_sec'])
        if name in self.restart_times and (now - self.restart_times[name]) < cd:
            return False
        return self.restart_counts.get(name, 0) < self.config['process']['max_restart_attempts']

    def restart(self, name, method='gateway'):
        if not self.should_restart(name): return False
        self.restart_times[name] = datetime.utcnow()
        self.restart_counts[name] = self.restart_counts.get(name, 0) + 1
        try:
            if method == 'gateway':
                subprocess.run(['openclaw', 'gateway', 'restart'], capture_output=True, timeout=30)
            elif method == 'doctor':
                subprocess.run(['openclaw', 'doctor', '--fix'], capture_output=True, timeout=60)
            return True
        except: return False


# ============================================================
# v2: FLYWHEEL MONITOR
# ============================================================

class FlywheelMonitor:
    """Watches agent productivity. Detects stuck/idle flywheels."""

    def __init__(self, config):
        self.config = config
        self.last_nudge: Dict[str, datetime] = {}
        self.state: Dict[str, FlywheelState] = {}
        self.checkpoint_history: Dict[str, str] = {}

    def check(self, agent_name: str = "main") -> FlywheelState:
        fw_config = self.config.get('flywheel', {})
        now = datetime.utcnow()

        # Check git commit activity
        commits = self._check_recent_commits(fw_config.get('git_repos', []))
        last_commit = commits[0] if commits else None
        commits_this_hour = len([c for c in commits if self._within_minutes(c['time'], 60)])

        # Check checkpoint file
        checkpoint = self._read_checkpoint(fw_config.get('checkpoint_file', ''))

        # Determine status
        status = "spinning"
        reason = ""

        if last_commit:
            minutes_since_commit = (now - self._parse_time(last_commit['time'])).total_seconds() / 60
            if minutes_since_commit > fw_config.get('stuck_timeout_min', 30):
                status = "stuck"
                reason = f"No commits in {minutes_since_commit:.0f} min"
            elif minutes_since_commit > fw_config.get('idle_timeout_min', 15):
                status = "idle"
                reason = f"No commits in {minutes_since_commit:.0f} min (idle threshold)"
            elif commits_this_hour >= 3:
                status = "spinning"
                reason = f"{commits_this_hour} commits/hour"
        else:
            status = "idle"
            reason = "No recent commits found"

        # Check if checkpoint changed
        prev_cp = self.checkpoint_history.get(agent_name)
        if checkpoint and checkpoint == prev_cp and status == "stuck":
            reason += f" | Same checkpoint for >{fw_config.get('stuck_timeout_min', 30)} min"
        if checkpoint:
            self.checkpoint_history[agent_name] = checkpoint

        state = FlywheelState(
            timestamp=now.isoformat(), agent_name=agent_name,
            status=status, checkpoint_reached=checkpoint,
            commits_this_hour=commits_this_hour,
            last_commit_time=last_commit['time'] if last_commit else None,
            last_commit_repo=last_commit['repo'] if last_commit else "",
            reason=reason,
        )
        self.state[agent_name] = state
        return state

    def should_nudge(self, agent_name: str) -> bool:
        now = datetime.utcnow()
        cd = timedelta(minutes=self.config.get('flywheel', {}).get('nudge_cooldown_min', 10))
        if agent_name in self.last_nudge and (now - self.last_nudge[agent_name]) < cd:
            return False
        return True

    def nudge(self, agent_name: str):
        self.last_nudge[agent_name] = datetime.utcnow()

    def _check_recent_commits(self, repos: List[str], since_min=60) -> List[Dict]:
        commits = []
        since = (datetime.utcnow() - timedelta(minutes=since_min)).strftime('%Y-%m-%dT%H:%M:%S')
        for repo_path in repos:
            if not os.path.isdir(repo_path):
                continue
            try:
                r = subprocess.run(
                    ['git', 'log', f'--since={since}', '--oneline', '-10'],
                    capture_output=True, text=True, cwd=repo_path, timeout=10
                )
                for line in r.stdout.strip().split('\n'):
                    if line:
                        parts = line.split(None, 1)
                        commits.append({'hash': parts[0][:8], 'msg': parts[1][:60] if len(parts) > 1 else '', 'repo': os.path.basename(repo_path), 'time': since})
            except: pass
        return commits

    def _read_checkpoint(self, path: str) -> Optional[str]:
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return f.read().strip()[:200]
        except: return None

    def _within_minutes(self, iso_time: str, minutes: int) -> bool:
        try:
            return (datetime.utcnow() - self._parse_time(iso_time)).total_seconds() < minutes * 60
        except: return True

    def _parse_time(self, t: str) -> datetime:
        for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']:
            try: return datetime.strptime(t.split('.')[0], fmt)
            except: pass
        return datetime.utcnow()


# ============================================================
# v2: TOKEN STEWARD
# ============================================================

class TokenSteward:
    """Keeper holds secrets. Agents get allowances, not keys.

    The keeper is the trusted holder of API keys.
    Agents request token use through the keeper.
    The keeper tracks usage and enforces limits.

    For zero-trust: agents never see raw keys.
    For internal use: same system, just trusted agents.
    """

    def __init__(self, config):
        self.config = config
        self.vault_path = config.get('token_steward', {}).get('vault_path', '')
        self.allowances: Dict[str, TokenAllowance] = {}
        self._load_allowances()

    def _load_allowances(self):
        ts = self.config.get('token_steward', {})
        for agent, info in ts.get('allowances', {}).items():
            self.allowances[agent] = TokenAllowance(
                timestamp=datetime.utcnow().isoformat(),
                agent_name=agent, provider=info.get('provider', ''),
                daily_limit_usd=info.get('daily_limit_usd', 5.0),
                used_today_usd=info.get('used_today_usd', 0),
                tokens_used=info.get('tokens_used', 0),
                calls_made=info.get('calls_made', 0),
            )

    def request_tokens(self, agent_name: str, provider: str, estimated_cost_usd: float = 0) -> Tuple[bool, str]:
        """Agent requests permission to use tokens.
        Returns (approved, key_or_rejection_reason)."""
        ts = self.config.get('token_steward', {})

        if not ts.get('enabled'):
            return True, self._get_raw_key(provider)

        # Check if agent is registered
        if agent_name not in self.allowances:
            # Auto-register with default limits
            self.allowances[agent_name] = TokenAllowance(
                timestamp=datetime.utcnow().isoformat(),
                agent_name=agent_name, provider=provider,
                daily_limit_usd=ts.get('default_daily_limit', 5.0),
            )

        allowance = self.allowances[agent_name]

        # Check if daily limit exceeded
        if allowance.used_today_usd + estimated_cost_usd > allowance.daily_limit_usd:
            return False, f"Daily limit reached: ${allowance.used_today_usd:.2f}/${allowance.daily_limit_usd:.2f}"

        # Check if checkpoint-gated and checkpoint not approved
        if ts.get('checkpoint_gated') and not allowance.checkpoint_approved:
            return False, "Checkpoint not yet approved by keeper or captain"

        # Check if zero-trust and agent not verified
        if ts.get('zero_trust') and not self._verify_agent(agent_name):
            return False, "Agent not verified for zero-trust token access"

        # Approved
        if ts.get('zero_trust'):
            key = self._get_masked_key(provider)
        else:
            key = self._get_raw_key(provider)

        allowance.calls_made += 1
        allowance.used_today_usd += estimated_cost_usd
        return True, key

    def report_usage(self, agent_name: str, tokens_used: int, actual_cost_usd: float = 0):
        if agent_name in self.allowances:
            a = self.allowances[agent_name]
            a.tokens_used += tokens_used
            if actual_cost_usd > 0:
                a.used_today_usd = max(a.used_today_usd, actual_cost_usd)

    def approve_checkpoint(self, agent_name: str, checkpoint: str):
        if agent_name in self.allowances:
            a = self.allowances[agent_name]
            a.checkpoint = checkpoint
            a.checkpoint_approved = True

    def get_usage_report(self) -> Dict[str, Dict]:
        return {
            name: {'provider': a.provider, 'used': a.used_today_usd,
                    'limit': a.daily_limit_usd, 'calls': a.calls_made,
                    'tokens': a.tokens_used, 'status': a.status}
            for name, a in self.allowances.items()
        }

    def _get_raw_key(self, provider: str) -> str:
        if not self.vault_path or not os.path.exists(self.vault_path):
            return ""
        try:
            vault = json.load(open(self.vault_path))
            return vault.get(provider, {}).get('key', '')
        except: return ""

    def _get_masked_key(self, provider: str) -> str:
        key = self._get_raw_key(provider)
        if len(key) > 8:
            return key[:4] + "..." + key[-4:]
        return "***"

    def _verify_agent(self, name: str) -> bool:
        # In zero-trust mode, verify agent identity via public key or known fingerprint
        coord = self.config.get('coordination', {})
        return name in coord.get('agents', {})


# ============================================================
# v2: GPU SCHEDULER
# ============================================================

class GpuScheduler:
    """Manages GPU time slots for multiple agents on shared hardware.

    The keeper sees who needs the GPU, how long they need it,
    and finds the best time. Like a lighthouse scheduling
    ship passages through a narrow channel.
    """

    def __init__(self, config):
        self.config = config
        self.current_holder = config.get('gpu', {}).get('current_holder', '')
        self.holder_expires = config.get('gpu', {}).get('holder_expires', '')
        self.schedule: List[Dict] = []
        self.schedule_path = config.get('coordination', {}).get('schedule_path', '')
        self._load_schedule()

    def request_gpu(self, agent_name: str, duration_min: int, priority: int = 5,
                    reason: str = "") -> Tuple[bool, str]:
        """Agent requests GPU time. Returns (approved, wait_time_or_reason)."""
        now = datetime.utcnow()

        # Check if currently held by another agent
        if self.current_holder and self.current_holder != agent_name:
            if self.holder_expires:
                expires = self._parse_time(self.holder_expires)
                if expires > now:
                    wait_min = int((expires - now).total_seconds() / 60)
                    # Higher priority can preempt
                    if priority > 5:
                        self._evict_current(f"{agent_name} has higher priority")
                    else:
                        return False, f"GPU held by {self.current_holder} for {wait_min} more minutes"

        # Approve
        expires = now + timedelta(minutes=duration_min)
        self.current_holder = agent_name
        self.holder_expires = expires.isoformat()

        entry = ScheduleEntry(
            timestamp=now.isoformat(), agent_name=agent_name,
            resource="gpu", amount="exclusive", duration_min=duration_min,
            priority=priority, reason=reason, status="active",
        )
        self.schedule.append(asdict(entry))
        self._save_schedule()
        return True, f"Granted {duration_min} min, expires {expires.isoformat()}"

    def release_gpu(self, agent_name: str):
        if self.current_holder == agent_name:
            self.current_holder = ""
            self.holder_expires = ""
            for entry in reversed(self.schedule):
                if entry.get('agent_name') == agent_name and entry.get('status') == 'active':
                    entry['status'] = 'completed'
                    break
            self._save_schedule()

    def get_status(self) -> Dict:
        now = datetime.utcnow()
        status = {
            'current_holder': self.current_holder,
            'holder_expires': self.holder_expires,
            'is_available': True,
        }
        if self.current_holder and self.holder_expires:
            if self._parse_time(self.holder_expires) > now:
                status['is_available'] = False
        return status

    def find_best_window(self, duration_min: int) -> Optional[str]:
        """Find the best upcoming time window for a GPU-heavy task."""
        now = datetime.utcnow()
        if self.current_holder and self.holder_expires:
            expires = self._parse_time(self.holder_expires)
            if expires > now:
                return expires.isoformat()
        return now.isoformat()

    def _evict_current(self, reason: str):
        old = self.current_holder
        self.current_holder = ""
        self.holder_expires = ""
        for entry in self.schedule:
            if entry.get('agent_name') == old and entry.get('status') == 'active':
                entry['status'] = 'evicted'
                entry['reason'] = reason
        self._save_schedule()

    def _load_schedule(self):
        if self.schedule_path and os.path.exists(self.schedule_path):
            try:
                self.schedule = json.load(open(self.schedule_path))
            except: self.schedule = []

    def _save_schedule(self):
        if self.schedule_path:
            try:
                json.dump(self.schedule, open(self.schedule_path, 'w'), indent=2)
            except: pass

    def _parse_time(self, t: str) -> datetime:
        try: return datetime.fromisoformat(t)
        except: return datetime.utcnow()


# ============================================================
# v2: MULTI-AGENT COORDINATOR
# ============================================================

class MultiAgentCoordinator:
    """Coordinates multiple agents sharing the same hardware.

    When you have several OpenClaws on one workstation with an RTX 5090,
    the keeper is who they negotiate with for shared resources.
    """

    def __init__(self, config):
        self.config = config
        self.registered: Dict[str, Dict] = config.get('coordination', {}).get('agents', {})

    def register_agent(self, name: str, pid: int, rss_limit_mb: int = 1024,
                       gpu_quota_pct: int = 50, priority: int = 5):
        self.registered[name] = {
            'pid': pid, 'rss_limit_mb': rss_limit_mb,
            'gpu_quota_pct': gpu_quota_pct, 'priority': priority,
            'registered_at': datetime.utcnow().isoformat(),
        }

    def get_agent_status(self, snapshot: ResourceSnapshot) -> Dict[str, Dict]:
        status = {}
        for name, info in self.registered.items():
            rss = self._get_process_rss(info['pid'])
            status[name] = {
                'pid': info['pid'],
                'rss_mb': rss,
                'rss_limit_mb': info['rss_limit_mb'],
                'rss_pct': (rss / info['rss_limit_mb'] * 100) if info['rss_limit_mb'] > 0 else 0,
                'priority': info['priority'],
                'gpu_quota_pct': info['gpu_quota_pct'],
                'over_limit': rss > info['rss_limit_mb'],
            }
        return status

    def total_rss(self) -> int:
        total = 0
        for info in self.registered.values():
            total += self._get_process_rss(info.get('pid', 0))
        return total

    def _get_process_rss(self, pid: int) -> int:
        if not pid: return 0
        try:
            with open(f'/proc/{pid}/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) // 1024
        except: return 0


# ============================================================
# OPERATIONAL LOGGER (v1 + v2 logs)
# ============================================================

class OperationalLogger:
    def __init__(self, config):
        self.config = config
        log_dir = Path(config['logs']['dir'])
        log_dir.mkdir(parents=True, exist_ok=True)

    def log(self, log_type: str, data):
        filename = self.config['logs'].get(log_type, f'{log_type}.log')
        path = Path(self.config['logs']['dir']) / filename
        try:
            line = json.dumps(data) if not isinstance(data, str) else data
            with open(path, 'a') as f:
                f.write(line + '\n')
        except: pass


# ============================================================
# SELF-HEALER (v1, unchanged)
# ============================================================

class SelfHealer:
    def __init__(self, config, logger):
        self.config = config
        self.logger = logger

    def heal(self, snapshot, events):
        actions = []
        for e in events:
            if e.get('event_type') == 'stopped' and 'gateway' in e.get('process_name', ''):
                if self.config.get('healing', {}).get('auto_restart_gateway'):
                    try:
                        subprocess.run(['openclaw', 'gateway', 'restart'], capture_output=True, timeout=30)
                        actions.append("Restarted gateway")
                    except Exception as ex:
                        actions.append(f"Gateway restart failed: {ex}")
        if self.config.get('healing', {}).get('auto_clean_tmp') and snapshot.disk_percent > self.config['thresholds']['disk_warning']:
            try:
                subprocess.run(['find', '/tmp', '-type', 'f', '-mtime', '+1', '-delete'], capture_output=True, timeout=30)
                actions.append("Cleaned /tmp")
            except: pass
        return actions


# ============================================================
# THE KEEPER v2 — Main Loop
# ============================================================

class BrothersKeeper:
    def __init__(self, config: Dict = None):
        self.config = config or DEFAULT_CONFIG
        self.monitor = ResourceMonitor()
        self.watchdog = ProcessWatchdog(self.config)
        self.logger = OperationalLogger(self.config)
        self.healer = SelfHealer(self.config, self.logger)

        # v2 components
        self.flywheel = FlywheelMonitor(self.config) if self.config.get('flywheel', {}).get('enabled') else None
        self.token_steward = TokenSteward(self.config) if self.config.get('token_steward', {}).get('enabled') else None
        self.gpu_scheduler = GpuScheduler(self.config) if self.config.get('gpu', {}).get('enabled') else None
        self.coordinator = MultiAgentCoordinator(self.config) if self.config.get('coordination', {}).get('enabled') else None

        self.running = True
        self.last_alert_time: Dict[str, datetime] = {}

    def start(self, check_interval: int = 60):
        print(f"[keeper] Brothers Keeper v2 starting. Interval: {check_interval}s", flush=True)
        self.logger.log('operational', {'timestamp': datetime.utcnow().isoformat(), 'category': 'config', 'description': 'Keeper v2 started', 'severity': 'info'})
        while self.running:
            try:
                self._tick()
            except Exception as e:
                print(f"[keeper] tick error: {e}", file=sys.stderr)
            time.sleep(check_interval)

    def _tick(self):
        now = datetime.utcnow()
        snapshot = self.monitor.snapshot(self.config)
        self.logger.log('resource', asdict(snapshot))

        # v1: process watchdog
        events = self.watchdog.check()
        for e in events:
            self.logger.log('process', e)
            if e.get('event_type') in ('stopped', 'stuck'):
                self._alert('process', f"{e['process_name']}: {e['event_type']}", e.get('pid', ''))

        # v1: resource thresholds
        t = self.config['thresholds']
        if snapshot.ram_percent > t['ram_critical']:
            self._alert('resource', 'RAM critical', f"{snapshot.ram_percent:.1f}%")
        elif snapshot.ram_percent > t['ram_warning']:
            self._alert('resource', 'RAM warning', f"{snapshot.ram_percent:.1f}%")
        if snapshot.swap_percent > t['swap_warning']:
            self._alert('resource', 'Swap pressure', f"{snapshot.swap_percent:.1f}%")
        if snapshot.disk_percent > t['disk_warning']:
            self._alert('resource', 'Disk filling', f"{snapshot.disk_percent:.1f}%")

        # v2: flywheel monitor
        if self.flywheel:
            fw = self.flywheel.check()
            self.logger.log('flywheel', asdict(fw))
            if fw.status == "stuck" and self.flywheel.should_nudge(fw.agent_name):
                self._alert('flywheel', f'{fw.agent_name} STUCK', fw.reason)
                self.flywheel.nudge(fw.agent_name)
            elif fw.status == "idle" and self.flywheel.should_nudge(fw.agent_name):
                self._alert('flywheel', f'{fw.agent_name} idle', fw.reason)

        # v2: GPU monitoring
        if self.gpu_scheduler and snapshot.gpu_mem_total_mb > 0:
            gpu_pct = (snapshot.gpu_mem_used_mb / snapshot.gpu_mem_total_mb * 100) if snapshot.gpu_mem_total_mb > 0 else 0
            if gpu_pct > self.config.get('gpu', {}).get('max_gpu_mem_pct', 90):
                self._alert('resource', 'GPU memory critical', f"{gpu_pct:.0f}%")

        # v1: self-heal
        actions = self.healer.heal(snapshot, events)
        for a in actions:
            print(f"[keeper] heal: {a}", flush=True)

    def _alert(self, category, title, detail):
        key = f"{category}:{title}"
        now = datetime.utcnow()
        cd = timedelta(seconds=self.config.get('beacon', {}).get('coalesce_sec', 300))
        if key in self.last_alert_time and (now - self.last_alert_time[key]) < cd:
            return
        self.last_alert_time[key] = now
        self.logger.log('alert', {'timestamp': now.isoformat(), 'level': 'warning', 'category': category, 'message': f"{title}: {detail}"})
        print(f"[keeper] ALERT [{category}] {title} — {detail}", flush=True)

    # --- v2 Public API (called by agents via IPC/file/socket) ---

    def pre_flight(self) -> Tuple[bool, List[str]]:
        snapshot = self.monitor.snapshot(self.config)
        warnings = []
        approved = True
        t = self.config['thresholds']
        if snapshot.ram_percent > t['ram_critical']:
            warnings.append(f"RAM critical: {snapshot.ram_percent:.1f}%")
            approved = False
        elif snapshot.ram_percent > t['ram_warning']:
            warnings.append(f"RAM warning: {snapshot.ram_percent:.1f}%")
        if snapshot.gpu_mem_total_mb > 0:
            gpu_pct = snapshot.gpu_mem_used_mb / snapshot.gpu_mem_total_mb * 100
            if gpu_pct > self.config.get('gpu', {}).get('max_gpu_mem_pct', 90):
                warnings.append(f"GPU memory high: {gpu_pct:.0f}%")
                approved = False
        return approved, warnings

    def request_gpu(self, agent: str, duration_min: int, priority: int = 5, reason: str = "") -> Tuple[bool, str]:
        if not self.gpu_scheduler:
            return True, "GPU scheduling not enabled"
        return self.gpu_scheduler.request_gpu(agent, duration_min, priority, reason)

    def release_gpu(self, agent: str):
        if self.gpu_scheduler:
            self.gpu_scheduler.release_gpu(agent)

    def request_tokens(self, agent: str, provider: str, cost: float = 0) -> Tuple[bool, str]:
        if not self.token_steward:
            return True, "Token stewardship not enabled"
        return self.token_steward.request_tokens(agent, provider, cost)

    def report_token_usage(self, agent: str, tokens: int, cost: float = 0):
        if self.token_steward:
            self.token_steward.report_usage(agent, tokens, cost)

    def get_status(self) -> Dict:
        snapshot = self.monitor.snapshot(self.config)
        status = {
            'timestamp': snapshot.timestamp,
            'resources': asdict(snapshot),
            'flywheel': asdict(self.flywheel.state.get('main', FlywheelState(timestamp='', agent_name='', status='unknown'))) if self.flywheel else None,
            'gpu': self.gpu_scheduler.get_status() if self.gpu_scheduler else None,
            'agents': self.coordinator.get_agent_status(snapshot) if self.coordinator else None,
            'token_usage': self.token_steward.get_usage_report() if self.token_steward else None,
        }
        return status

    def stop(self):
        self.running = False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Brothers Keeper v2 — The Lighthouse Keeper")
    parser.add_argument('--config', '-c', help='Config JSON')
    parser.add_argument('--interval', '-i', type=int, default=60)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--gpu-status', action='store_true')
    parser.add_argument('--token-report', action='store_true')
    args = parser.parse_args()

    config = DEFAULT_CONFIG.copy()
    if args.config and os.path.exists(args.config):
        with open(args.config) as f:
            config.update(json.load(f))

    keeper = BrothersKeeper(config)

    if args.status:
        print(json.dumps(keeper.get_status(), indent=2, default=str))
        return
    if args.preflight:
        ok, warns = keeper.pre_flight()
        print("CLEAR" if ok else f"HOLD — {'; '.join(warns)}")
        return
    if args.gpu_status:
        print(json.dumps(keeper.gpu_scheduler.get_status() if keeper.gpu_scheduler else {"enabled": False}, indent=2))
        return
    if args.token_report:
        print(json.dumps(keeper.token_steward.get_usage_report() if keeper.token_steward else {}, indent=2))
        return
    if args.once:
        keeper._tick()
        return

    signal.signal(signal.SIGINT, lambda s, f: keeper.stop())
    signal.signal(signal.SIGTERM, lambda s, f: keeper.stop())
    keeper.start(args.interval)


if __name__ == '__main__':
    main()
