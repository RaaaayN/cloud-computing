from autoscaler.policies.queue_slo_policy import PolicyConfig, QueueSloPolicy


def test_scale_up_after_two_high_pressure_cycles() -> None:
    policy = QueueSloPolicy(
        PolicyConfig(min_replicas=1, max_replicas=5, max_delta_per_cycle=1, queue_threshold=2.0)
    )

    first = policy.decide(
        current_replicas=1,
        queue_depth=3.0,
        p99_latency_seconds=0.2,
        arrival_rate_rps=0.0,
        service_time_seconds=0.2,
    )
    second = policy.decide(
        current_replicas=first.desired_replicas,
        queue_depth=3.0,
        p99_latency_seconds=0.2,
        arrival_rate_rps=0.0,
        service_time_seconds=0.2,
    )

    assert first.reason == "hold"
    assert second.reason == "fast_scale_up"
    assert second.desired_replicas == 2


def test_max_delta_per_cycle_is_respected() -> None:
    policy = QueueSloPolicy(
        PolicyConfig(min_replicas=1, max_replicas=10, max_delta_per_cycle=1, headroom=3.0)
    )

    decision = policy.decide(
        current_replicas=2,
        queue_depth=0.0,
        p99_latency_seconds=0.1,
        arrival_rate_rps=10.0,
        service_time_seconds=1.0,
    )

    assert decision.reason == "capacity_scale_up"
    assert decision.desired_replicas == 3


def test_scale_down_requires_cooldown_cycles() -> None:
    policy = QueueSloPolicy(
        PolicyConfig(
            min_replicas=1,
            max_replicas=10,
            max_delta_per_cycle=1,
            cooldown_cycles=3,
            safe_latency_seconds=0.35,
        )
    )

    d1 = policy.decide(
        current_replicas=4,
        queue_depth=0.0,
        p99_latency_seconds=0.2,
        arrival_rate_rps=0.0,
        service_time_seconds=0.2,
    )
    d2 = policy.decide(
        current_replicas=d1.desired_replicas,
        queue_depth=0.0,
        p99_latency_seconds=0.2,
        arrival_rate_rps=0.0,
        service_time_seconds=0.2,
    )
    d3 = policy.decide(
        current_replicas=d2.desired_replicas,
        queue_depth=0.0,
        p99_latency_seconds=0.2,
        arrival_rate_rps=0.0,
        service_time_seconds=0.2,
    )

    assert d1.reason == "hold"
    assert d2.reason == "hold"
    assert d3.reason == "scale_down"
    assert d3.desired_replicas == 3

