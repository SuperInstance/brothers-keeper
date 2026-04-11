# ZeroClaw Fork

Brothers Keeper forked from zeroclaw-labs/zeroclaw (30K stars) to demonstrate the lighthouse keeper concept on a minimal standalone runtime.

## What We Keep
- Minimal-code philosophy: one file, zero deps
- Hardware-first: runs on $10 boards
- Trait-driven ideas from zeroclaw-observability

## What We Add
- External watchdog (not internal observability)
- Self-healing (assumes runtime might not be healthy)
- Operational logging (external perspective, not agent diary)
- Multi-runtime support (OpenClaw, ZeroClaw, anything)

## Integration with ZeroClaw
The keeper watches from outside. No ZeroClaw internals needed. Just add process entries to the config.
