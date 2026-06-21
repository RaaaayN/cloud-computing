import argparse
import logging
import os
import time
from autoscaler.k8s_client import KubernetesScaleClient
from autoscaler.policies.queue_slo_policy import PolicyConfig, QueueSloPolicy
from autoscaler.prometheus_client import PrometheusApiClient, PrometheusQueries


def get_env_float(name: str, default_value: float) -> float:
    return float(os.getenv(name, str(default_value)))


def get_env_int(name: str, default_value: int) -> int:
    return int(os.getenv(name, str(default_value)))


def build_policy_config() -> PolicyConfig:
    return PolicyConfig(
        min_replicas=get_env_int("REPLICA_MIN", 1),
        max_replicas=get_env_int("REPLICA_MAX", 10),
        max_delta_per_cycle=get_env_int("MAX_DELTA_PER_CYCLE", 1),
        warn_latency_seconds=get_env_float("S_WARN", 0.45),
        safe_latency_seconds=get_env_float("S_SAFE", 0.35),
        queue_threshold=get_env_float("QUEUE_THRESHOLD", 3.0),
        headroom=get_env_float("HEADROOM", 1.2),
        drain_target_seconds=get_env_float("DRAIN_TARGET_SECONDS", 10.0),
        cooldown_cycles=get_env_int("COOLDOWN_CYCLES", 4),
    )


def build_prom_queries() -> PrometheusQueries:
    # Server-side service latency (queue wait + inference), the SLO metric.
    default_p99_query = (
        "histogram_quantile("
        "0.99,"
        "sum(rate(dispatcher_request_duration_seconds_bucket[1m])) by (le)"
        ")"
    )
    return PrometheusQueries(
        queue_depth=os.getenv("PROM_QUERY_QUEUE_DEPTH", "dispatcher_queue_depth"),
        p99_latency=os.getenv("PROM_QUERY_P99_LATENCY", default_p99_query),
        arrival_rate=os.getenv("PROM_QUERY_ARRIVAL_RATE", "rate(dispatcher_requests_total[1m])"),
    )


def run_loop(dry_run: bool) -> None:
    logger = logging.getLogger("autoscaler")

    interval_seconds = get_env_int("INTERVAL_SEC", 15)
    deployment = os.getenv("DEPLOYMENT_NAME", "inference")
    namespace = os.getenv("DEPLOYMENT_NAMESPACE", "default")
    current_replicas = get_env_int("INITIAL_REPLICAS", 1)
    service_time_seconds = get_env_float("SERVICE_TIME_SECONDS", 0.2)
    prom_url = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")

    prom_client = PrometheusApiClient(base_url=prom_url)
    k8s_client = KubernetesScaleClient()
    policy = QueueSloPolicy(config=build_policy_config())
    queries = build_prom_queries()

    logger.info(
        "Starting autoscaler loop deployment=%s namespace=%s interval=%ss dry_run=%s",
        deployment,
        namespace,
        interval_seconds,
        dry_run,
    )

    while True:
        queue_depth = prom_client.query_scalar(queries.queue_depth)
        p99_latency = prom_client.query_scalar(queries.p99_latency)
        arrival_rate = prom_client.query_scalar(queries.arrival_rate)

        decision = policy.decide(
            current_replicas=current_replicas,
            queue_depth=queue_depth,
            p99_latency_seconds=p99_latency,
            arrival_rate_rps=arrival_rate,
            service_time_seconds=service_time_seconds,
        )

        logger.info(
            "MAPE decision reason=%s queue_depth=%.3f p99=%.3f arrival_rate=%.3f "
            "current=%d desired=%d",
            decision.reason,
            queue_depth,
            p99_latency,
            arrival_rate,
            current_replicas,
            decision.desired_replicas,
        )

        if not dry_run and decision.desired_replicas != current_replicas:
            k8s_client.patch_replicas(
                namespace=namespace,
                deployment=deployment,
                replicas=decision.desired_replicas,
            )
            logger.info("Patched replicas to %d", decision.desired_replicas)

        current_replicas = decision.desired_replicas
        time.sleep(interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Custom MAPE autoscaler")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute scaling decisions without patching the deployment",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args()
    run_loop(dry_run=args.dry_run)

