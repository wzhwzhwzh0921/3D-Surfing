"""Model extension boundary: multimodal chat request in, chat completion out."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Protocol


class ModelBackend(Protocol):
    model: str
    status: dict

    def complete(self, payload: dict) -> dict: ...


def configured_model():
    return (os.getenv('SCENEQA_MODEL') or os.getenv('SCENEQA_CODEX_MODEL')
            or os.getenv('LUNA_MODEL') or 'gpt-6.1-sol')


def create_model_backend(model: str) -> ModelBackend:
    backend = os.getenv('SCENEQA_MODEL_BACKEND', 'codex')
    if backend == 'codex':
        from .codex_backend import CodexBackend
        return CodexBackend(model)
    if backend == 'plugin':
        value = os.getenv('SCENEQA_MODEL_FACTORY', '')
        module, separator, name = value.partition(':')
        if not separator or not module or not name:
            raise ValueError('Plugin mode requires SCENEQA_MODEL_FACTORY=package.module:factory')
        adapter = getattr(importlib.import_module(module), name)(model=model)
        if not callable(getattr(adapter, 'complete', None)) or not adapter.status.get('account_fingerprint'):
            raise ValueError('Model adapter needs complete(payload) and a non-secret status/account_fingerprint')
        return adapter
    raise ValueError('Gateway backend must be codex or plugin')


def model_identity():
    backend = os.getenv('SCENEQA_MODEL_BACKEND', 'codex')
    if backend == 'codex':
        from .codex_backend import login_status
        return login_status()
    if backend == 'plugin':
        return create_model_backend(configured_model()).status
    if backend == 'relay':
        key = os.getenv('LUNA_API_KEY') or os.getenv('SCENEQA_API_KEY')
        if not key and os.getenv('LUNA_KEY_FILE'):
            key = Path(os.environ['LUNA_KEY_FILE']).expanduser().read_text().strip()
        if not key:
            raise ValueError('Relay mode requires SCENEQA_API_KEY, LUNA_API_KEY or LUNA_KEY_FILE')
        identity = json.dumps({'url': os.getenv('LUNA_CHAT_URL') or os.getenv('SCENEQA_API_URL'),
                               'key_hash': hashlib.sha256(key.encode()).hexdigest()})
        return {'auth_method': 'api', 'account_fingerprint': hashlib.sha256(identity.encode()).hexdigest()[:16]}
    raise ValueError('SCENEQA_MODEL_BACKEND must be codex, plugin or relay')


def configure_relay():
    """Normalize public configuration into the legacy wire client environment."""
    url = os.getenv('SCENEQA_API_URL') or os.getenv('LUNA_CHAT_URL')
    if not url:
        raise ValueError('Relay mode requires SCENEQA_API_URL (full chat/completions URL)')
    os.environ['LUNA_CHAT_URL'] = url
    os.environ['LUNA_MODEL'] = configured_model()
    if os.getenv('SCENEQA_API_KEY'):
        os.environ['LUNA_API_KEY'] = os.environ['SCENEQA_API_KEY']
        os.environ.pop('LUNA_KEY_FILE', None)
