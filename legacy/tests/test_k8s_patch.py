from autoscaler.k8s_client import KubernetesScaleClient


def test_patch_replicas_calls_k8s_api(monkeypatch) -> None:
    called: dict[str, object] = {}

    class FakeAppsApi:
        def patch_namespaced_deployment_scale(self, name: str, namespace: str, body: dict) -> None:
            called["name"] = name
            called["namespace"] = namespace
            called["body"] = body

    monkeypatch.setattr(
        "autoscaler.k8s_client.KubernetesScaleClient._load_apps_api",
        lambda self: FakeAppsApi(),
    )

    client = KubernetesScaleClient()
    client.patch_replicas(namespace="default", deployment="inference", replicas=4)

    assert called == {
        "name": "inference",
        "namespace": "default",
        "body": {"spec": {"replicas": 4}},
    }

