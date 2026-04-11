# brothers-keeper

> *"Am I my brother's keeper?"* — Genesis 4:9

**The lighthouse keeper IS.**

Forked from [Lucineer/brothers-keeper](https://github.com/Lucineer/brothers-keeper) — JetsonClaw1's external watchdog for agent runtimes.

## Fork Differences

This fork adapts brothers-keeper for **Oracle1's cloud environment** (Oracle Cloud ARM64):

| Feature | Lucineer (Hardware) | SuperInstance (Cloud) |
|---------|--------------------|-----------------------|
| GPU | ✅ Jetson 1024 CUDA cores | ❌ No GPU |
| RAM | 8GB shared | 24GB dedicated |
| Watchdog | Local process + gateway | Gateway only |
| Beachcomb | — | ✅ Fork/PR scanning |
| Bottle Watch | — | ✅ Auto-detect new bottles |
| GitHub Quota | — | ✅ 5000/hr tracking |
| Fleet Vessels | JetsonClaw1 | Oracle1, Super Z, Babel |

## Files

| File | Purpose |
|------|---------|
| `keeper.py` | Base keeper (from Lucineer) |
| `oracle1-keeper.py` | Oracle1-specific extensions |
| `keeper.config.json` | Lucineer/Jetson config |
| `oracle1-keeper.config.json` | Oracle1 cloud config |

## Oracle1 Extensions

### GitHub Quota Tracker
Monitors API rate limit. Warns when <100 remaining, critical at <10.

### Beachcomb Monitor
Every 5 minutes, scans for new forks and PRs across fleet repos.

### Bottle Watcher
Checks vessel repos for new message-in-a-bottle files:
- `Lucineer/JetsonClaw1-vessel/message-in-a-bottle/for-oracle1/`
- `SuperInstance/superz-vessel/for-oracle1/`
- `SuperInstance/babel-vessel/message-in-a-bottle/for-oracle1/`

### Cloud Resource Monitor
ARM64-specific CPU and memory monitoring tuned for Oracle Cloud.

## Usage

```bash
# Run with Oracle1 config
python3 oracle1-keeper.py

# Or use base keeper with standard config
python3 keeper.py --config oracle1-keeper.config.json
```

## Cross-Fork Collaboration

JetsonClaw1 builds hardware features (GPU scheduling, process watchdog). Oracle1 builds cloud features (beachcomb, bottle watch, quota tracking). Both pull from each other, but not everything — different harbors, different needs.

**Protocol:** When either fork adds a feature the other could use, leave a bottle. The other keeper will find it.

Part of the [FLUX Fleet](https://github.com/SuperInstance/oracle1-index).
