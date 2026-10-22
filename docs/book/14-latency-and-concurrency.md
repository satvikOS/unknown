# Latency, concurrency and performance engineering {#latency}

Ultra-low latency is an end-to-end property measured against a defined strategy opportunity. A fast internal function cannot compensate for delayed data, internet routing, broker pacing or exchange queue position. Separate feed-to-decision, decision-to-send, send-to-acknowledgment and send-to-fill distributions. Each has different ownership and measurement limits.

## Budget and instrumentation

Write total observable latency as

$$T=T_{decode}+T_{feature}+T_{signal}+T_{risk}+T_{queue}+T_{encode}+T_{network}+T_{broker}. \tag{14.1}$$

Some terms overlap in a pipeline; summing independent medians is not a correct tail estimate. Record timestamps on the same event path and compute the distribution of complete journeys. Report p50, p95, p99, p99.9, maximum observed, sample count, workload, hardware and warm-up rules. A maximum observed value is not a guaranteed upper bound.

Use monotonic clocks for local durations. Cross-host one-way latency needs synchronized clocks and an error budget. Wall-clock time can step; monotonic time cannot be persisted across reboot as though it were universal time. Store both appropriate clock identities and boot/session identifiers.

Initial performance targets are engineering hypotheses: keep deterministic pretrade processing comfortably below the strategy's useful signal lifetime and prevent queue growth under expected load. Do not advertise a microsecond SLA before benchmarking. Cloud language-model requests belong outside the immediate market-response loop; their outputs become bounded, versioned features or research artifacts.

## Native data path

Use fixed-size or preallocated records for frequent events, contiguous arrays for instrument state, integer price/quantity representations and bounded queues. Avoid dynamic memory allocation, blocking logging, DNS, database queries and model calls in the immediate risk function. Validate instrument metadata and compile policy tables before activating them.

A single-producer/single-consumer ring can reduce coordination overhead when the topology actually has one producer and one consumer. Publish a fully written record with release semantics and read it with acquire semantics; ownership and wraparound must be correct. Do not copy a lock-free algorithm without proving its memory-order assumptions. A simple mutex may be safer and fast enough for broker-mediated trading.

False sharing occurs when independent hot fields occupy the same cache line. Pad only after profiling. Pinning threads, disabling power management or busy polling may improve a specific Linux workload but consumes resources and is not a portable default for macOS. Measure the actual deployment target instead of applying exchange-colocation tuning rituals to a laptop.

## Queueing and overload

For stable systems, Little's law gives average queued/in-service work $L=\lambda W$. It relates averages, not worst-case delay. If arrival rate approaches service capacity, tails can grow dramatically. Bound queue lengths and reject or coalesce work according to semantics. Quotes can sometimes be replaced by newer snapshots; fills and money events cannot.

Prioritize cancellation and reconciliation ahead of optional analytics within actual broker constraints. Separate CPU and connection pools for research jobs so a burst of document summaries cannot starve account control. Apply per-agent quotas and global admission control. When overload persists, reduce optional work and disable new entries before critical state becomes stale.

## Benchmark methodology

Use a recorded event distribution with bursts, instrument skew, malformed inputs and realistic policy sizes. Benchmark end-to-end and individual kernels. Prevent the compiler from optimizing away results. Distinguish cold start, steady state and recovery. Include allocation counts, CPU utilization, queue depth and data-loss counters.

Avoid coordinated omission: a load generator that waits for each slow response before sending the next request can hide the backlog users would experience. Schedule arrivals independently where that matches production. Test prolonged runs for thermal throttling, memory growth and clock behavior, not just a short best-case loop.

## Real HFT boundary

Exchange-colocated high-frequency market making can require direct licensed feeds, exchange membership or sponsored access, specialized networks, precise clocking, detailed order-book reconstruction and substantial operational infrastructure. The IBKR internet-mediated path has different constraints. HFT research can share mathematics with the personal desk, but its latency and execution assumptions must be a separate capability track.

An AWS region near a broker is not automatically exchange colocation. A native desktop UI does not make cloud inference deterministic. The architecture preserves a future native direct-access path without making the first release depend on it. Performance work is accepted only when a measured bottleneck and an economic benefit justify the additional complexity.

