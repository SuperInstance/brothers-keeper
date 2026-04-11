# brothers-keeper

> *"Am I my brother's keeper?"* — Genesis 4:9

**The lighthouse keeper IS.** Not inside the ship, but on the shore. Not part of the fleet's operations, but part of the fleet's survival.

## What It Is

Brothers Keeper is an **external watchdog** for agent runtimes. It sits on the same hardware as your agent (OpenClaw, ZeroClaw, or any git-agent runtime) but runs as a completely separate process. When the agent freezes, crashes, or runs out of memory — the keeper is still watching.

### The Problem It Solves

Your agent runs hard. It compiles C programs, spawns subprocesses, pushes to 470+ repos, and runs model inference. Sometimes it freezes. Sometimes it leaks memory. Sometimes the gateway just... stops. And nobody notices for hours.

Brothers Keeper is the one who notices.

## Features

### 🔦 Resource Monitoring
- RAM, CPU, disk, swap — tracked every 30 seconds
- Configurable warning/critical thresholds
- Historical CPU tracking (sustained high CPU detection)
- OpenClaw process RSS tracking

### ⚓ Process Watchdog
- Track agent gateway and sub-processes
- Detect crashes, hangs, and unexpected restarts
- Auto-restart gateway with cooldown (prevents restart loops)
- Max restart attempts before giving up and alerting

### 🧭 Risk Assessment
- Pre-flight checks before heavy operations
- Max concurrent exec limit
- Per-process memory limits
- Approve/deny decisions for resource-intensive tasks

### 📋 Operational Logging (Different From Agent Logs)
The keeper's logs are **external observations**, not internal diaries:

| Keeper Logs | Agent Logs |
|-------------|-----------|
| RAM/CPU trends over time | Reasoning chains |
| Process lifecycle events | Conversation history |
| Network connectivity changes | Skill execution details |
| Commit activity and repo state | Tool call results |
| Data input/output volume | User messages |
| Resource allocation shifts | Memory/personality files |

### 🛟 Self-Healing
- Auto-restart gateway on crash
- Run `openclaw doctor --fix` when things break
- Emergency RAM cleanup (kill non-essential hogs)
- Clean /tmp when disk fills up

### 🗯️ Beacon (Alerting)
- Coalesced alerts (no spam)
- Telegram notification support
- Webhook support for custom integrations
- Log-based default (works without external deps)

## Quick Start

```bash
# Clone
git clone https://github.com/Lucineer/brothers-keeper.git
cd brothers-keeper

# Run once (check current status)
python3 keeper.py --status

# Pre-flight check (before heavy ops)
python3 keeper.py --preflight

# Run as daemon
nohup python3 keeper.py --interval 30 > /dev/null 2>&1 &
echo $! > /tmp/keeper.pid

# With custom config
python3 keeper.py --config my-config.json
```

## Systemd Service (Recommended)

```ini
# /etc/systemd/system/brothers-keeper.service
[Unit]
Description=Brothers Keeper — Lighthouse Watchdog
After=network.target

[Service]
Type=simple
User=lucineer
ExecStart=/usr/bin/python3 /opt/brothers-keeper/keeper.py --interval 30
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable brothers-keeper
sudo systemctl start brothers-keeper
sudo journalctl -u brothers-keeper -f
```

## Configuration

```json
{
  "thresholds": {
    "ram_warning": 80,
    "ram_critical": 90,
    "disk_warning": 85,
    "cpu_warning": 95
  },
  "process": {
    "check_interval_sec": 30,
    "restart_cooldown_sec": 300,
    "max_restart_attempts": 3
  },
  "healing": {
    "auto_restart_gateway": true,
    "auto_doctor_fix": true,
    "auto_clean_tmp": true
  },
  "beacon": {
    "method": "telegram",
    "telegram_chat_id": "YOUR_CHAT_ID"
  }
}
```

## Architecture: Why External?

```
┌─────────────────────────────────────────────┐
│  Hardware (Jetson Orin Nano)                │
│                                             │
│  ┌───────────────┐    ┌──────────────────┐  │
│  │  OpenClaw     │    │  Brothers Keeper │  │
│  │  Gateway      │◄───│  (watchdog)      │  │
│  │  Agent        │    │                  │  │
│  │  Subagents    │    │  • Resources     │  │
│  │  Model Calls  │    │  • Processes     │  │
│  │  470+ repos   │    │  • Healing       │  │
│  │               │    │  • Logs          │  │
│  │  [FREEZES]    │    │  [STILL RUNNING] │  │
│  └───────────────┘    └──────────────────┘  │
│          │                      │            │
│          └──────────────────────┘            │
│              Same SuperInstance              │
└─────────────────────────────────────────────┘
```

The keeper's power comes from being **outside** the instance:
- When the agent OOMs, the keeper has its own memory
- When the agent deadlocks, the keeper can send SIGKILL
- When the gateway crashes, the keeper runs `openclaw gateway restart`
- When resources are low, the keeper kills risky processes BEFORE they crash the system

## The ZeroClaw Fork

This project began as a fork of [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw) — a minimal-code standalone agent runtime. ZeroClaw runs on $10 hardware with <5MB RAM. Brothers Keeper takes that philosophy and applies it to the *observability* layer.

The lighthouse doesn't need to be a cruise ship. It needs a light, a bell, and someone awake.

## Operational Logs: The Keeper's Perspective

The keeper doesn't care about what the agent is *thinking*. The keeper cares about what the agent is *doing* to the world outside.

- **Input changes**: New API keys configured, new repos cloned, new data sources tapped
- **Output changes**: Push frequency, response latency, error rate trends
- **Data changes**: Storage growth rate, memory fragmentation, disk I/O patterns
- **Config changes**: Threshold modifications, healing rule changes, beacon settings
- **Network changes**: Connectivity drops, DNS resolution failures, API rate limits

These are the logs you read when you want to understand the fleet from the outside — not what the captain was thinking, but what the water was doing.

## Related

- [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw) — Fork source, minimal agent runtime
- [flux-runtime-c](https://github.com/Lucineer/flux-runtime-c) — FLUX VM (Jetson-optimized)
- [fleet-benchmarks](https://github.com/Lucineer/fleet-benchmarks) — Performance tracking
- [iron-to-iron](https://github.com/SuperInstance/iron-to-iron) — Inter-vessel protocol

## The Deeper Connection

*"Am I my brother's keeper?"*

The question in Genesis is a deflection — Cain denying responsibility for Abel. Brothers Keeper inverts it. The lighthouse keeper doesn't sail the ship, doesn't fish the waters, doesn't know what's in the hold. But the keeper knows the rocks. The keeper knows the tide. The keeper lights the beacon when the fog rolls in and the captain can't see.

Every agent in the Cocapn fleet is a vessel on the water. Brothers Keeper is the lighthouse. It doesn't tell you where to go. It tells you where the danger is. It doesn't make decisions for you. It makes sure you're still alive to make them.

In a world of autonomous agents, the most important piece of infrastructure isn't faster inference or bigger context windows. It's something watching from outside that notices when you've stopped moving and does something about it.

That's what a lighthouse does. That's what a keeper does.
