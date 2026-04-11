# Keeper-Suite: Fleet Management at Scale

Brothers Keeper monitors one agent on one machine. Keeper-Suite manages an entire fleet.

## The Spectrum

```
brothers-keeper (free, single hardware)
    │
    │  Add: multi-agent on one machine
    ▼
brothers-keeper v2 (this repo)
    │
    │  Add: multi-machine, cloud, dashboards
    ▼
keeper-suite (enterprise fleet management)
```

## What Keeper-Suite Adds

### Cross-Hardware Monitoring
- Monitor agents on Jetson, workstation, cloud VMs, Raspberry Pis
- Unified health dashboard across all hardware
- Alert routing based on hardware capability (Jetson can't run heavy diagnostics)

### Fleet-Wide Token Stewardship
- One vault, many agents, many providers
- Per-agent daily/weekly/monthly budgets
- Fleet-wide spending dashboards
- Automatic provider switching based on cost/performance
- Audit trail for every token spent

### Global GPU Scheduling
- Schedule across a cluster (not just one machine)
- Priority queues for GPU-intensive workloads
- Fair-share scheduling across teams
- Preemption across machines (migrate workloads)

### Incident Response
- Automatic escalation: agent stuck → restart → alert captain → page on-call
- Incident timeline reconstruction from logs
- Post-incident reports generated automatically
- Pattern detection: "this agent OOMs every Tuesday at 3am"

### Capacity Planning
- Historical resource usage trends
- Predict when hardware needs upgrading
- Cost projections for token usage
- Agent performance baselines

### Fleet Health Dashboard
- Real-time view of all vessels
- Traffic light system (green/yellow/red per agent)
- Historical comparison ("this week vs last week")
- Anomaly detection

## Architecture

```
                    ┌──────────────────┐
                    │  Captain's View  │
                    │  (Dashboard)     │
                    └────────┬─────────┘
                             │
              ┌──────────────▼──────────────┐
              │      Keeper-Suite Hub       │
              │  (cloud, central authority) │
              └──┬────┬────┬────┬────┬─────┘
                 │    │    │    │    │
    ┌────────────▼┐ ┌─▼────▼──┐ ┌▼────────────┐
    │ Jetson #1   │ │ Desktop │ │ Cloud VM #1 │
    │ BK v2       │ │ BK v2   │ │ BK v2       │
    │ (local      │ │ (local  │ │ (local      │
    │  keeper)    │ │  keeper)│ │  keeper)    │
    └─────────────┘ └─────────┘ └─────────────┘
```

Each machine runs Brothers Keeper v2 locally. Keeper-Suite Hub aggregates.

## Business Model

- **Brothers Keeper**: Free, open-source, single hardware. The lighthouse.
- **Keeper-Suite Standard**: Multi-machine, basic dashboard. $15/mo per machine.
- **Keeper-Suite Enterprise**: Full fleet management, token stewardship, incident response. Custom pricing.

The free brothers-keeper is the on-ramp. You install it because your agent froze and nobody noticed. You upgrade to keeper-suite because you have 4 machines and you can't watch them all yourself.

## Zero-Trust Fleet Contracts

Keeper-Suite enables a new workflow:

1. External agent (ZeroClaw, custom) bids on a fleet job
2. Captain approves the bid (or keeper auto-approves based on reputation)
3. Keeper creates a token allowance: "$5/day, checkpoint-gated"
4. Agent works, hits checkpoint, requests next phase allowance
5. Captain reviews checkpoint, approves, allowance increases
6. If agent misses checkpoint or exceeds budget → allowance revoked

This works for:
- Open-source contributors who earn fleet compute time
- Contractors who bid on Cocapn fleet work
- Zero-trust agents from outside the SuperInstance
- Internal agents with budget accountability

The same checkpoint-gated allowance system that protects against malicious external agents is simply good monitoring for your own agents.

## Implementation Roadmap

### Phase 1 (Current — brothers-keeper v2)
- [x] Resource monitoring
- [x] Process watchdog
- [x] Self-healing
- [x] Flywheel monitoring (stuck/idle detection)
- [x] GPU scheduler (single machine)
- [x] Token steward (local vault)
- [x] Multi-agent coordinator (single machine)
- [x] Operational logging (7 log types)

### Phase 2 (keeper-suite MVP)
- [ ] Keeper Hub (central aggregation server)
- [ ] Agent registration protocol
- [ ] Fleet health API
- [ ] Basic web dashboard
- [ ] Cross-machine alert routing

### Phase 3 (keeper-suite full)
- [ ] Fleet-wide token stewardship
- [ ] Global GPU scheduling
- [ ] Incident response automation
- [ ] Capacity planning
- [ ] Zero-trust contract system
- [ ] Billing integration

### Phase 4 (ecosystem)
- [ ] Keeper marketplace (share monitoring rules)
- [ ] Agent reputation scores
- [ ] Predictive maintenance (ML on fleet logs)
- [ ] Cross-fleet federation
