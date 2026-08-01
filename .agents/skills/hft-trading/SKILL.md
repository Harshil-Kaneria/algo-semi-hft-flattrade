---
name: hft-trading
description: Engineering guidance for building, reviewing, or optimizing high-frequency and low-latency trading systems — market data handlers, order books, matching/execution engines, smart order routers, backtesters, and exchange connectivity (FIX/ITCH/OUCH/binary protocols). Trigger this whenever the user mentions HFT, low-latency trading systems, market making, order books, tick-to-trade latency, exchange gateways, market data feed handlers, co-location, kernel bypass networking (DPDK/io_uring/RDMA), lock-free data structures for trading, backtesting engines, or trading risk controls — even if they just say "trading bot" or "exchange connector" and the context implies performance-critical code. This skill covers system architecture and code quality, not investment advice, alpha signal design, or specific trading strategies — it stays neutral on what to trade and focuses on how to build the system correctly and fast.
---

# High-Frequency Trading System Development

Guidance for an IDE/agent working on latency-sensitive trading infrastructure. This skill is about **engineering correctness and speed**, not about generating alpha, backtesting a specific edge, or giving financial advice. If the user asks for the latter, answer the code/architecture question but don't present it as investment advice.

## Scope check (do this first)

Before writing code, identify which layer of the stack the request touches — the right answer differs a lot by layer:

1. **Market data ingestion** — feed handlers, order book reconstruction, sequence gap detection
2. **Strategy/signal layer** — decision logic (usually *not* latency-critical to write, though it must run fast)
3. **Order management / execution** — order state machine, smart order routing, exchange gateways
4. **Risk layer** — pre-trade checks, position/exposure limits, kill switches
5. **Backtesting/simulation** — replaying historical data against strategy + execution logic
6. **Infra/ops** — clock sync, monitoring, deployment, co-location networking

Ask which layer if it's genuinely ambiguous and would change the approach; otherwise infer from context and state the assumption.

## Core engineering principles

These apply regardless of language:

- **Latency budget first.** Before optimizing, ask (or estimate) the target tick-to-trade budget (single-digit microseconds for colocated HFT, low milliseconds for retail-broker algo trading). This determines whether kernel bypass and lock-free structures are even justified — most "trading bot" requests are not actually sub-microsecond HFT and don't need this complexity. Don't over-engineer.
- **Determinism over cleverness.** Avoid unbounded loops, unpredictable branch patterns, and anything that can spike tail latency (p99.9 matters more than average).
- **No allocations / no GC on the hot path.** In C++/Rust: preallocate, use object pools, avoid `new`/`malloc` and heap-allocating containers in the critical path. In managed languages (Java/C#/Go), be explicit that the GC is a latency risk and consider off-heap structures or accept that true HFT is a poor fit for that runtime — flag this tradeoff to the user rather than silently proceeding.
- **Single-writer, lock-free where contended.** Prefer SPSC/MPSC ring buffers over mutexes on the hot path. Use `references/architecture.md` for concrete patterns.
- **Measure, don't guess.** Any latency claim should come with a benchmark methodology (see Testing section) — don't assert "this is faster" without a reproducible measurement.

## Language guidance

| Layer | Typical choice | Why |
|---|---|---|
| Feed handler / matching / OMS hot path | C++ (17/20) or Rust | Predictable latency, no GC, manual memory control |
| Strategy research, backtesting, data analysis | Python (pandas/numpy/polars) | Iteration speed; not on the hot path |
| Tooling, monitoring, config | Go or Python | Fine off the critical path |

If the user's codebase is already in a given language, work within it rather than suggesting a rewrite — call out latency tradeoffs instead.

## Market data handling

- Reconstruct the order book from an incremental feed (add/modify/delete messages) plus periodic snapshots; always handle **sequence gaps** explicitly (request a snapshot / resync, don't silently continue on a gap).
- Keep book state in a flat, cache-friendly structure (e.g., price-level array or intrusive linked list per side) rather than a generic sorted map when latency matters.
- Timestamp everything at ingress (NIC/kernel timestamp if available) to separate feed latency from processing latency.
- See `references/protocols.md` for FIX, ITCH, OUCH, and generic binary protocol parsing notes.

## Execution / order management

- Model orders as an explicit state machine (`New → PendingAck → Acked → PartiallyFilled → Filled/Canceled/Rejected`) — reject invalid transitions loudly rather than coercing state.
- Idempotency: every outbound order needs a unique client order ID; handle exchange acks/rejects/fills arriving out of order or duplicated.
- Smart order routing logic (if relevant) should be separable/testable from the network transport layer — don't couple routing decisions to socket code.

## Risk controls (always include unless explicitly told not to)

Even prototype/demo trading code should default to including:
- Pre-trade checks: max order size, max position, max order rate, price collar (reject orders too far from last trade/NBBO).
- A kill switch / circuit breaker that can halt new order submission independent of the strategy logic.
- Clear logging of every risk rejection with the reason.

If a user explicitly asks you to strip these out (e.g., "just give me the minimal loop"), comply but note in one line that production systems shouldn't ship without them.

## Backtesting & simulation

- Distinguish **event-driven backtesters** (replay real message sequences, more realistic) from **vectorized backtesters** (fast, but prone to look-ahead bias) — ask which fits the user's goal.
- Simulate realistic fills: partial fills, queue position at a price level, latency between decision and order arrival. A backtester that assumes instant full fills at the observed price will overstate performance — flag this if you see it.
- Keep the execution-simulation code path structurally similar to the live execution code path so backtest and live don't silently diverge ("backtest/live parity").

## Testing & benchmarking

- Unit test the order book and state machine against recorded/replayed exchange message sequences, including edge cases (gaps, out-of-order acks, self-trades).
- For latency claims: use a proper microbenchmark harness (e.g., Google Benchmark for C++, Criterion for Rust) with warmup, and report percentiles (p50/p99/p99.9), not just mean.
- Property-based / fuzz testing is well suited to protocol parsers (malformed or truncated messages should never crash the feed handler).

## Infra notes (mention when relevant, don't over-deliver by default)

- Clock sync: PTP (IEEE 1588), not NTP, for sub-microsecond cross-host timestamp comparison.
- Kernel bypass networking (DPDK, io_uring, Solarflare/onload, RDMA) is a significant engineering investment — only bring it up as a solution if the stated latency budget actually requires it.
- Co-location / cross-connect details are exchange- and vendor-specific; point the user to their exchange's technical documentation rather than guessing specifics.

## What this skill does not do

- Does not generate trading strategies, alpha signals, or "what should I trade" advice — that's a strategy/research question, not an engineering one, and isn't something to answer as financial advice.
- Does not help build tools whose purpose is market manipulation, spoofing, layering, wash trading, or evading exchange risk controls — decline that regardless of framing and explain why.
- Does not opine on regulatory compliance (Reg NMS, MiFID II, exchange-specific rules) beyond noting that such rules exist and vary by jurisdiction/venue — point to the user's compliance/legal team or the exchange rulebook for authoritative answers.

## Reference files

- `references/architecture.md` — concrete lock-free/ring-buffer patterns, memory layout tips, common pitfalls
- `references/protocols.md` — notes on FIX, ITCH, OUCH, and general binary market-data/order-entry protocol parsing
