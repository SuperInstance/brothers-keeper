# Brothers Keeper -- Architecture

## Core Principle: External Observation

The observer cannot be the observed. The keeper is a separate process with separate memory, separate event loop, separate lifecycle.

## Components (single file, zero dependencies)

- ResourceMonitor: /proc reader for RAM, CPU, disk, swap, load, top processes
- ProcessWatchdog: pgrep-based tracking with restart logic and cooldown
- RiskAssessor: Pre-flight checks before heavy operations
- OperationalLogger: External observation logs (not internal diaries)
- SelfHealer: Auto-restart, doctor --fix, emergency cleanup
- BrothersKeeper: Main loop orchestrating all components

## Log Categories

- operations.log: What changed in the world (network, config, repos)
- resources.log: System health snapshots (RAM, CPU, disk, swap)
- alerts.log: Warnings and emergencies with cooldown
- processes.log: Process lifecycle events

## Why Python

The agent already requires Python. The keeper sleeps 30s between checks. Readability matters for auditability. Zero additional dependencies.

## Failure Modes

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Agent OOM | RAM > 90% | Kill hogs, restart gateway |
| Gateway crash | Process not in pgrep | Auto-restart with cooldown |
| Stuck agent | CPU 0% + gateway running | Alert (cannot fix thinking) |
| Disk full | Disk > 85% | Clean /tmp |
| Network down | ping fails | Log, alert |

## Future: Multi-Vessel

One keeper watches multiple vessels: OpenClaw, ZeroClaw, Docker containers. Each gets its own process watch entry.
