"""Explicit render location; importing orchestration never starts a renderer."""
import os
from pathlib import Path

SERVICE_ACCOUNT_NAMESPACE = Path('/var/run/secrets/kubernetes.io/serviceaccount/namespace')


def render_backend():
    value = os.getenv('SCENEQA_RENDER_BACKEND', 'local')
    if value not in {'local', 'k8s'}:
        raise ValueError('SCENEQA_RENDER_BACKEND must be local or k8s')
    return value


def require_render_environment():
    if render_backend() == 'k8s' and (
        not os.environ.get('KUBERNETES_SERVICE_HOST') or not SERVICE_ACCOUNT_NAMESPACE.is_file()
    ):
        raise RuntimeError('K8s rendering was selected; this worker must run in Kubernetes. No local fallback.')


def require_k8s_rendering():
    """Compatibility alias for older callers; follows the configured backend."""
    require_render_environment()
