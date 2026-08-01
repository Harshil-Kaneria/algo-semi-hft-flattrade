# Architecture Patterns for Low-Latency Trading Systems

## Ring buffers / SPSC-MPSC queues

- Single-producer/single-consumer (SPSC) ring buffers are the cheapest form of inter-thread handoff — no locks, no CAS loop needed if you're careful with memory ordering (acquire/release on the read/write index, not seq_cst).
- For multi-producer, prefer a small number of SPSC queues (one per producer thread) fanned into a consumer, over a single MPSC queue with contention — this avoids cache-line ping-pong on a shared write index.
- Size ring buffers as a power of two so index wraparound is a mask instead of a modulo.
- Pad the read and write index onto separate cache lines (avoid false sharing) — this is one of the most common easy wins/mistakes.

## Memory layout

- Struct-of-arrays over array-of-structs for hot data accessed in tight loops (e.g., iterating all price levels) — better cache locality than array-of-structs with many rarely-used fields.
- Keep hot fields (price, quantity, timestamp) together; push cold/rarely-touched fields (e.g., debug metadata) to a separate structure.
- Preallocate all order/message pool objects at startup; use a free-list/object pool rather than allocating per message.
- Align hot structures to cache-line size (64 bytes on most x86) to avoid straddling.

## Order book representation

- For a given symbol with a bounded, known price granularity (tick size), a flat array indexed by price offset from a reference price is often faster than a `std::map`/`BTreeMap`, at the cost of memory if the price range is wide.
- For wider/unknown price ranges, an intrusive doubly-linked list per price level (orders within a level) plus a sorted structure of levels (e.g., a skip list or small flat array with binary search) is a common middle ground.
- Keep top-of-book (best bid/ask) cached separately and updated incrementally rather than recomputed from the full book on every message.

## Threading model

- Common pattern: one thread pinned per core doing feed handling, one (or more) for strategy logic, one for order entry — connected via ring buffers, not locks.
- Pin threads to cores (`taskset`/`sched_setaffinity` on Linux) and isolate those cores from the general scheduler (`isolcpus` boot parameter) to avoid context-switch jitter.
- Busy-poll (spin) on the ring buffer in the hot path instead of blocking/condition-variable waits — trades CPU usage for latency, which is the right trade in HFT but should be called out explicitly since it's not free.

## Common pitfalls to flag in review

- Using a general-purpose logger (with formatting/allocation) synchronously on the hot path — log to a lock-free queue and format on a separate thread instead.
- Reading system time (`clock_gettime`) more often than needed — it's not free; cache a timestamp per event rather than calling it repeatedly.
- Virtual function calls / dynamic dispatch in the innermost loop where a compile-time-resolved alternative (templates/generics, CRTP) would let the compiler inline.
- False sharing from unrelated atomics/counters placed on the same cache line as hot data.
- Silent handling of sequence gaps in market data — always resync explicitly, never just "keep going."
