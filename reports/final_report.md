# Day 10 Reliability Final Report

## 1. Architecture Summary

This reliability layer implements a resilient, cost-aware LLM API gateway that protects client applications from backend provider latency, flakiness, and outright outages while reducing API costs through semantic caching.

```
                  User Request
                       |
                       v
                 [ReliabilityGateway]
                       |
        +--------------+--------------+
        |                             |
  [Cache Check]                       | (Cache Miss)
        |                             v
        | (HIT)             [Provider Fallback Chain]
        |                             |
        v                             +---> [CB: Primary] ---> Primary LLM
  Return Cached                       |       (Tripped? fail fast)
                                      |
                                      +---> [CB: Backup]  ---> Backup LLM
                                      |       (Tripped? fail fast)
                                      v
                         [Static Degraded Fallback]
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Prevents single transient network anomalies from tripping the breaker while shutting down failed providers within 3 errors. |
| reset_timeout_seconds | 2.0 | Probes the failed service quickly to check if it has recovered without overwhelming it with a retry storm. |
| success_threshold | 1 | Transition back to CLOSED immediately upon a single successful probe, ensuring fast recovery. |
| cache TTL | 300 | 5-minute cache lifespan balancing response freshness with high hit rates. |
| similarity_threshold | 0.92 | Captures semantically equivalent queries (e.g. minor typos, word ordering) while preventing false-hit matches on different dates/numbers. |
| load_test requests | 100 | Sufficient request volume to simulate cache warm-up and circuit breaker state oscillations. |

## 3. SLO Definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.67% | **Yes** |
| Latency P95 | < 2500 ms | 316.04 ms | **Yes** |
| Fallback success rate | >= 95% | 98.75% | **Yes** |
| Cache hit rate | >= 10% | 62.00% | **Yes** |
| Recovery time | < 5000 ms | 2544.80 ms | **Yes** |

## 4. Metrics

Below is the summary of the metrics captured during the multi-scenario chaos simulation:

| Metric | Value |
|---|---:|
| availability | 99.67% |
| error_rate | 0.33% |
| latency_p50_ms | 277.85 ms |
| latency_p95_ms | 316.04 ms |
| latency_p99_ms | 319.44 ms |
| fallback_success_rate | 98.75% |
| cache_hit_rate | 62.00% |
| estimated_cost | $0.048324 |
| estimated_cost_saved | $0.186000 |
| circuit_open_count | 9 |
| recovery_time_ms | 2544.80 ms |

## 5. Cache Comparison

The following table compares baseline healthy runs (`all_healthy`) with the cache enabled vs. disabled:

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 223.20 ms | 222.93 ms | -0.27 ms |
| latency_p95_ms | 312.45 ms | 293.38 ms | -19.07 ms |
| estimated_cost | $0.048890 | $0.017116 | -$0.031774 (64.98% saved) |
| cache_hit_rate | 0% | 64.00% | +64.00% |

## 6. Redis Shared Cache

### Why in-memory cache is insufficient for multi-instance deployments
An in-memory cache is local to each specific container/server instance. In a scaled deployment with multiple gateway instances behind a load balancer, cache state becomes fragmented. An instance cannot reuse cached responses stored in another instance. This leads to duplicate provider queries, higher API costs, and unequal response times.

### How `SharedRedisCache` solves this
`SharedRedisCache` externalizes cache entries to a centralized Redis instance. All instances share the same state. A cache write by instance A is immediately available to instance B. 

### Evidence of shared state
The unit test `test_shared_state_across_instances` spins up two independent `SharedRedisCache` clients pointing to the same Redis instance:
```python
def test_shared_state_across_instances() -> None:
    c1 = SharedRedisCache(redis_url="redis://localhost:6379/0", ttl_seconds=60, similarity_threshold=0.5, prefix="rl:test:shared:")
    c2 = SharedRedisCache(redis_url="redis://localhost:6379/0", ttl_seconds=60, similarity_threshold=0.5, prefix="rl:test:shared:")
    c1.flush()
    c1.set("shared query", "shared response")
    cached, _ = c2.get("shared query")
    assert cached == "shared response"
```
This test passes successfully, confirming cross-instance cache sharing.

### Redis CLI output
```bash
# docker compose exec redis redis-cli KEYS "rl:cache:*"
1) "rl:cache:5c412f71be80"
2) "rl:cache:f8b3c37894ea"
3) "rl:cache:c3d1f3b394ff"
```

## 7. Chaos Scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | All traffic fallbacks to backup, circuit opens. | Tripped primary breaker 3 times. Fail-fast active, 100% of requests routed to backup. | **Pass** |
| primary_flaky_50 | Circuit oscillates, traffic routes through primary and backup. | Breaker tripped and reset dynamically, fallback handled failed requests. | **Pass** |
| all_healthy | All requests go to primary, no circuit opens. | 0 circuit opens. All queries completed on primary or served from cache. | **Pass** |
| all_healthy_no_cache | All requests go to primary, no caching. | 0 circuit opens. Cost was significantly higher ($0.048890 vs $0.017116). | **Pass** |

## 8. Failure Analysis

### Remaining Weakness
If both primary and backup providers go down simultaneously, the gateway falls back to a hardcoded static text response. While this prevents application crashes, it represents a complete loss of service utility for the client.

### Proposed Improvement
1. **Local Backup LLM**: We can host a small local model (e.g. Llama-3-8B via Ollama/vLLM) on-prem. If all cloud providers fail, traffic is routed to the local backup model to serve degraded but functional responses.
2. **Cost-Aware Routing**: Track daily cost budgets. Once the threshold (e.g. 80%) is exceeded, dynamically route non-critical requests to cheaper fallback models.

## 9. Next Steps

1. **Redis Circuit State**: Store circuit breaker states (failure counters, open status) in Redis so that if one gateway instance detects a provider outage, all other instances fail fast immediately.
2. **Asynchronous Cache Revalidation**: Implement a stale-while-revalidate pattern. Serve slightly expired cache entries immediately to the user, and trigger an asynchronous background request to refresh the cache with the provider.