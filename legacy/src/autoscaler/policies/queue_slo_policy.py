from dataclasses import dataclass
from math import ceil
from typing import Literal

DecisionReason = Literal[
    "fast_scale_up",
    "capacity_scale_up",
    "scale_down",
    "hold",
]


@dataclass(frozen=True)
class PolicyConfig:
    min_replicas: int = 1
    max_replicas: int = 10
    max_delta_per_cycle: int = 1
    slo_seconds: float = 0.5
    warn_latency_seconds: float = 0.45
    safe_latency_seconds: float = 0.35
    queue_threshold: float = 3.0
    headroom: float = 1.2
    drain_target_seconds: float = 10.0
    cooldown_cycles: int = 4


@dataclass(frozen=True)
class ScaleDecision:
    desired_replicas: int
    reason: DecisionReason


class QueueSloPolicy:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config
        self._consecutive_high_pressure = 0
        self._consecutive_scale_down_candidates = 0

    def decide(
        self,
        current_replicas: int,
        queue_depth: float,
        p99_latency_seconds: float,
        arrival_rate_rps: float,
        service_time_seconds: float,
    ) -> ScaleDecision:
        raw_base = ceil(arrival_rate_rps * service_time_seconds * self.config.headroom)
        raw_queue = ceil((queue_depth * service_time_seconds) / self.config.drain_target_seconds)
        raw_desired = max(raw_base, raw_queue, self.config.min_replicas)
        raw_desired = min(raw_desired, self.config.max_replicas)

        high_pressure = (
            p99_latency_seconds > self.config.warn_latency_seconds
            or queue_depth > self.config.queue_threshold
        )
        if high_pressure:
            self._consecutive_high_pressure += 1
            self._consecutive_scale_down_candidates = 0
        else:
            self._consecutive_high_pressure = 0

        if self._consecutive_high_pressure >= 2:
            desired = min(
                self.config.max_replicas,
                current_replicas + self.config.max_delta_per_cycle,
            )
            return ScaleDecision(desired_replicas=desired, reason="fast_scale_up")

        if raw_desired > current_replicas:
            desired = min(
                raw_desired,
                current_replicas + self.config.max_delta_per_cycle,
            )
            return ScaleDecision(desired_replicas=desired, reason="capacity_scale_up")

        can_scale_down = (
            queue_depth <= 0.0
            and p99_latency_seconds < self.config.safe_latency_seconds
            and raw_desired < current_replicas
        )
        if can_scale_down:
            self._consecutive_scale_down_candidates += 1
            if self._consecutive_scale_down_candidates >= self.config.cooldown_cycles:
                desired = max(
                    self.config.min_replicas,
                    current_replicas - self.config.max_delta_per_cycle,
                )
                self._consecutive_scale_down_candidates = 0
                return ScaleDecision(desired_replicas=desired, reason="scale_down")
        else:
            self._consecutive_scale_down_candidates = 0

        return ScaleDecision(desired_replicas=current_replicas, reason="hold")

