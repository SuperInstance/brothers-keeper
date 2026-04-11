#!/usr/bin/env python3
"""
oracle1-keeper.py — Brothers Keeper fork for Oracle1 (cloud/ARM64)

Extensions over Lucineer's brothers-keeper:
- Cloud resource monitoring (Oracle Cloud ARM64)
- GitHub API quota tracking (5000/hr budget)
- Beachcomb integration (periodic fork/PR scanning)
- Message-in-a-bottle checking (new bottles → alert)
- Fleet heartbeat monitoring (watch other vessels)
- No GPU (cloud instance has none)

Design: Same keeper, different harbor. Oracle1 runs on Oracle Cloud,
JetsonClaw1 runs on Jetson hardware. Same protocol, different profiles.
"""

import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Tuple

# Import base keeper from the same directory
KEEPER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KEEPER_DIR)

# We extend the base keeper classes
from keeper import (
    BrothersKeeper, ResourceMonitor, ProcessWatchdog,
    FlywheelMonitor, OperationalLogger, SelfHealer,
)


# ============================================================
# ORACLE CLOUD MONITORING
# ============================================================

class CloudResourceMonitor(ResourceMonitor):
    """Resource monitoring tuned for Oracle Cloud ARM64."""
    
    def snapshot(self, config=None):
        snap = super().snapshot(config)
        # Oracle Cloud ARM64 specifics
        snap.metadata = {
            "platform": "oracle-cloud",
            "arch": "aarch64",
            "instance": os.environ.get("INSTANCE_NAME", "oracle1"),
        }
        return snap
    
    def _read_cpu(self):
        """ARM64 CPU reading."""
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline()
            vals = [int(x) for x in line.split()[1:]]
            idle = vals[3]
            total = sum(vals)
            return (1.0 - idle / total) * 100.0 if total > 0 else 0.0
        except:
            return 0.0


# ============================================================
# GITHUB API QUOTA TRACKER
# ============================================================

class GitHubQuotaTracker:
    """Track GitHub API rate limit (5000/hr free tier)."""
    
    def __init__(self, token: str):
        self.token = token
        self.remaining = 5000
        self.limit = 5000
        self.reset_time = None
    
    def check(self) -> Dict:
        """Check current rate limit."""
        req = urllib.request.Request(
            "https://api.github.com/rate_limit",
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            core = data.get("resources", {}).get("core", {})
            self.remaining = core.get("remaining", 0)
            self.limit = core.get("limit", 5000)
            self.reset_time = datetime.fromtimestamp(core.get("reset", 0))
            return {
                "remaining": self.remaining,
                "limit": self.limit,
                "reset": self.reset_time.isoformat() if self.reset_time else None,
                "usage_pct": round((1 - self.remaining / self.limit) * 100, 1),
                "status": "OK" if self.remaining > 100 else "LOW" if self.remaining > 10 else "CRITICAL",
            }
        except Exception as e:
            return {"error": str(e), "status": "UNKNOWN"}
    
    def should_throttle(self) -> bool:
        """Should we slow down API calls?"""
        return self.remaining < 100


# ============================================================
# BEACHCOMB INTEGRATION
# ============================================================

class BeachcombMonitor:
    """Periodic scanning for new forks, PRs, and external bottles."""
    
    def __init__(self, token: str, owner: str = "SuperInstance"):
        self.token = token
        self.owner = owner
        self.state_file = "/tmp/keeper-beachcomb-state.json"
        self.known_forks = self._load_state().get("known_forks", {})
        self.known_prs = self._load_state().get("known_prs", {})
    
    def _load_state(self) -> Dict:
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except:
            return {"known_forks": {}, "known_prs": {}}
    
    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump({"known_forks": self.known_forks, "known_prs": self.known_prs}, f, indent=2)
    
    def scan(self) -> List[Dict]:
        """Scan for new activity. Returns list of new findings."""
        findings = []
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        
        # Check top repos for new forks
        try:
            url = f"https://api.github.com/users/{self.owner}/repos?sort=updated&per_page=20"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                repos = json.loads(resp.read().decode())
            
            for repo in repos:
                if repo.get("fork"):
                    continue
                fork_url = f"https://api.github.com/repos/{self.owner}/{repo['name']}/forks?per_page=5"
                freq = urllib.request.Request(fork_url, headers=headers)
                try:
                    with urllib.request.urlopen(freq, timeout=10) as fresp:
                        forks = json.loads(fresp.read().decode())
                    for fk in forks:
                        owner = fk["owner"]["login"]
                        key = f"{repo['name']}/{owner}"
                        if key not in self.known_forks and owner != self.owner:
                            self.known_forks[key] = {
                                "fork_owner": owner,
                                "repo": repo["name"],
                                "detected": datetime.utcnow().isoformat(),
                            }
                            findings.append({
                                "type": "new_fork",
                                "repo": repo["name"],
                                "fork_owner": owner,
                            })
                except:
                    pass
            
            # Check for open PRs
            for repo in repos[:10]:
                pr_url = f"https://api.github.com/repos/{self.owner}/{repo['name']}/pulls?state=open&per_page=3"
                preq = urllib.request.Request(pr_url, headers=headers)
                try:
                    with urllib.request.urlopen(preq, timeout=10) as presp:
                        prs = json.loads(presp.read().decode())
                    for pr in prs:
                        user = pr["user"]["login"]
                        if user == "dependabot[bot]" or user == self.owner:
                            continue
                        pr_key = f"{repo['name']}#{pr['number']}"
                        if pr_key not in self.known_prs:
                            self.known_prs[pr_key] = {
                                "repo": repo["name"],
                                "number": pr["number"],
                                "user": user,
                                "title": pr["title"],
                            }
                            findings.append({
                                "type": "new_pr",
                                "repo": repo["name"],
                                "number": pr["number"],
                                "user": user,
                                "title": pr["title"],
                            })
                except:
                    pass
        except Exception as e:
            findings.append({"type": "error", "message": str(e)})
        
        self._save_state()
        return findings


# ============================================================
# BOTTLE WATCHER
# ============================================================

class BottleWatcher:
    """Watch for new messages in message-in-a-bottle folders."""
    
    def __init__(self, token: str):
        self.token = token
        self.state_file = "/tmp/keeper-bottle-state.json"
        self.known_bottles = self._load_state()
    
    def _load_state(self) -> Dict:
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except:
            return {}
    
    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.known_bottles, f, indent=2)
    
    def check_vessel(self, owner: str, vessel: str) -> List[Dict]:
        """Check a vessel repo for new bottles."""
        new_bottles = []
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        
        # Check message-in-a-bottle/for-oracle1/
        for folder in ["message-in-a-bottle/for-oracle1", "for-oracle1"]:
            url = f"https://api.github.com/repos/{owner}/{vessel}/contents/{folder}"
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    items = json.loads(resp.read().decode())
                if isinstance(items, list):
                    for item in items:
                        key = f"{owner}/{vessel}/{folder}/{item['name']}"
                        if key not in self.known_bottles:
                            self.known_bottles[key] = {
                                "owner": owner,
                                "vessel": vessel,
                                "folder": folder,
                                "file": item["name"],
                                "detected": datetime.utcnow().isoformat(),
                            }
                            new_bottles.append({
                                "from": owner,
                                "vessel": vessel,
                                "file": item["name"],
                            })
            except:
                pass
        
        self._save_state()
        return new_bottles


# ============================================================
# ORACLE1 KEEPER — MAIN
# ============================================================

class Oracle1Keeper:
    """Brothers Keeper fork for Oracle1 — cloud agent edition."""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.token = os.environ.get("GITHUB_TOKEN", "")
        
        # Base keeper components
        self.resource = CloudResourceMonitor()
        self.process = ProcessWatchdog(self.config.get("process", {}))
        self.flywheel = FlywheelMonitor(self.config)
        
        # Oracle1-specific components
        self.quota = GitHubQuotaTracker(self.token)
        self.beachcomb = BeachcombMonitor(self.token)
        self.bottle_watcher = BottleWatcher(self.token)
        
        # Vessels to watch
        self.watch_vessels = [
            ("Lucineer", "JetsonClaw1-vessel"),   # JetsonClaw1
            ("SuperInstance", "superz-vessel"),     # Super Z
            ("SuperInstance", "babel-vessel"),      # Babel
        ]
        
        self.running = False
    
    def start(self, interval: int = 60):
        """Main loop."""
        self.running = True
        print(f"🔮 Oracle1 Keeper started (interval={interval}s)")
        
        while self.running:
            self._tick()
            time.sleep(interval)
    
    def _tick(self):
        """One check cycle."""
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        
        # 1. Resource check
        snap = self.resource.snapshot(self.config)
        
        # 2. GitHub API quota
        quota = self.quota.check()
        if quota.get("status") in ("LOW", "CRITICAL"):
            print(f"⚠️ [{now}] GitHub API quota {quota.get('status')}: {quota.get('remaining', '?')} remaining")
        
        # 3. Beachcomb scan (every 5th tick ≈ 5 minutes)
        if int(time.time()) % (interval * 5) < interval:
            findings = self.beachcomb.scan()
            for f in findings:
                if f["type"] == "new_fork":
                    print(f"🆕 [{now}] New fork: {f['fork_owner']}/{f['repo']}")
                elif f["type"] == "new_pr":
                    print(f"📬 [{now}] New PR: {f['repo']}#{f['number']} from {f['user']}")
        
        # 4. Bottle watch
        for owner, vessel in self.watch_vessels:
            bottles = self.bottle_watcher.check_vessel(owner, vessel)
            for b in bottles:
                print(f"💌 [{now}] New bottle from {b['from']}: {b['file']}")
        
        # 5. Process health
        events = self.process.check()
        for e in events:
            print(f"⚡ [{now}] Process event: {e}")
        
        # 6. Flywheel check
        for owner, vessel in self.watch_vessels:
            state = self.flywheel.check(f"{owner}/{vessel}")
            if state.status in ("stuck", "idle"):
                print(f"🌀 [{now}] {owner}/{vessel}: {state.status}")
    
    def stop(self):
        self.running = False


if __name__ == "__main__":
    config = {
        "flywheel": {
            "enabled": True,
            "git_repos": ["/home/ubuntu/.openclaw/workspace"],
            "commit_check_interval_sec": 300,
        },
        "watch_processes": [
            {"name": "openclaw-gateway", "cmd": "pgrep -f 'openclaw.*gateway'"},
        ],
        "thresholds": {
            "ram_warning": 80, "ram_critical": 90,
            "disk_warning": 85,
        },
    }
    
    keeper = Oracle1Keeper(config)
    try:
        keeper.start(interval=60)
    except KeyboardInterrupt:
        keeper.stop()
        print("\n🔮 Oracle1 Keeper stopped.")

# ============================================================
# MECHANIC INTEGRATION
# ============================================================

class MechanicDispatcher:
    """Dispatch fleet-mechanic tasks when the keeper detects problems."""
    
    def __init__(self, token: str):
        self.token = token
        self.mechanic_repo = "SuperInstance/fleet-mechanic"
    
    def dispatch(self, task_type: str, repo: str, details: str = ""):
        """Trigger the mechanic via GitHub Actions workflow_dispatch."""
        import json
        
        # Option 1: Create an issue on fleet-mechanic (mechanic scans for these)
        issue_body = {
            "title": f"[KEEPER] {task_type}: {repo}",
            "body": f"**Triggered by:** Brothers Keeper (Oracle1)\n"
                    f"**Task type:** {task_type}\n"
                    f"**Target repo:** {repo}\n"
                    f"**Details:** {details}\n"
                    f"**Timestamp:** {datetime.utcnow().isoformat()}\n\n"
                    f"/mechanic {task_type} {repo}",
            "labels": ["keeper-triggered", task_type],
        }
        
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.mechanic_repo}/issues",
            data=json.dumps(issue_body).encode(),
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
                return {"status": "dispatched", "issue": result.get("number"), "url": result.get("html_url")}
        except Exception as e:
            return {"status": "failed", "error": str(e)}
    
    def dispatch_fix_tests(self, repo: str, test_output: str = ""):
        """Ask mechanic to fix failing tests in a repo."""
        return self.dispatch("fix-tests", repo, test_output[:500])
    
    def dispatch_gen_docs(self, repo: str):
        """Ask mechanic to generate missing docs for a repo."""
        return self.dispatch("gen-docs", repo)
    
    def dispatch_review(self, repo: str):
        """Ask mechanic to review a repo."""
        return self.dispatch("review", repo)
    
    def dispatch_health_scan(self):
        """Ask mechanic to scan the whole fleet."""
        return self.dispatch("health-scan", "SuperInstance/*")
