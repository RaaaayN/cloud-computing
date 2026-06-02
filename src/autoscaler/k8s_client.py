import logging
from typing import Any


class KubernetesScaleClient:
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.apps_api = self._load_apps_api()

    def _load_apps_api(self) -> Any:
        from kubernetes import client, config
        from kubernetes.config.config_exception import ConfigException

        try:
            config.load_incluster_config()
            self.logger.info("Using in-cluster Kubernetes config")
        except ConfigException:
            config.load_kube_config()
            self.logger.info("Using local kubeconfig")
        return client.AppsV1Api()

    def patch_replicas(self, namespace: str, deployment: str, replicas: int) -> None:
        body = {"spec": {"replicas": replicas}}
        self.apps_api.patch_namespaced_deployment_scale(
            name=deployment,
            namespace=namespace,
            body=body,
        )

