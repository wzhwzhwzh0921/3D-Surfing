"""Reproducible ten-type question menu; feasibility remains scene-specific."""
from __future__ import annotations

import hashlib
import random

QUESTION_TYPES = {
    "count": "条件计数",
    "identity": "物体身份",
    "color": "颜色",
    "pattern": "图案文字",
    "pose": "姿态状态",
    "containment": "包含承载",
    "relative_position": "相对位置",
    "proximity": "远近排序",
    "size_compare": "尺寸比较",
    "part_connection": "部件连接",
}
AUTO_VERIFIED = frozenset({"count", "identity"})


def type_order(scene_id: str, seed: int = 0) -> list[str]:
    digest = hashlib.sha256(f"{scene_id}|10x3-v1|{seed}".encode()).digest()
    kinds = list(QUESTION_TYPES)
    random.Random(int.from_bytes(digest, "big")).shuffle(kinds)
    return kinds
