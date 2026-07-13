# Qingzhou MOA Scaffold

MOA (Mixture of Agents) is intentionally kept as a disabled scaffold.

## Current Decision

Qingzhou-agent should use `delegate_task` for current multi-agent work.

MOA is not registered as a tool and is not exposed to the main agent because it
fan-outs the same question to multiple advisory agents and then asks an
aggregator to synthesize the result. That pattern is useful for high-stakes
analysis and design comparison, but it spends substantially more tokens and adds
latency.

## Existing Scaffold

- `agent/moa.py` defines the future request shape.
- `tools/registry.py` contains a comment near `delegate_task` explaining why MOA
  is not registered.
- `tests/test_moa_scaffold.py` verifies the scaffold remains disabled.

## Future Shape

When enabled later, MOA should use this flow:

1. Normalize a `MoaRequest` with a question, shared context, advisory agents, and
   optional aggregator model.
2. Run advisory agents in parallel with strict token and step limits.
3. Pass advisory answers to one aggregator.
4. Return the aggregator answer plus advisory summaries and token metadata.

## Non-goals For Now

- No runtime tool registration.
- No model calls.
- No automatic use from the main agent.
- No UI changes.
