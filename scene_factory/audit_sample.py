"""Reproducible, diverse pilot sample from the qualified scene catalog."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path


INDOOR = frozenset({
    "多房间与多层住宅", "居家室内", "办公与公共室内", "商业公共空间",
})
OUTDOOR = frozenset({
    "城镇村落", "庭院花园", "交通枢纽与港口", "林地营地", "街角巷道",
    "城市街区与园区", "水岸码头", "农场乡居", "露台阳台",
    "工业厂区与仓储", "历史建筑群",
})


def environment(category: str) -> str:
    if category in INDOOR:
        return "indoor"
    if category in OUTDOOR:
        return "outdoor"
    return "unclassified"


def sample_catalog(rows: list[dict], *, per_tier: int = 10, seed: int = 0,
                   max_bytes: int = 100_000_000) -> list[dict]:
    if per_tier < 5:
        raise ValueError("per_tier must be at least 5 to cover three environments")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    share = round(per_tier * 0.4)
    quotas = {"indoor": share, "outdoor": share,
              "unclassified": per_tier - 2 * share}
    category_cap = 2 if per_tier <= 10 else max(3, (3 * per_tier + 9) // 10)
    if quotas["unclassified"] < 1:
        raise ValueError("per_tier cannot produce an unclassified quota")
    candidates = {}
    for row in rows:
        uid = row.get("uid")
        if (not uid or row.get("tier") not in ("simple", "complex")
                or not row.get("download_url") or not isinstance(row.get("bytes"), int)
                or row["bytes"] <= 0 or row["bytes"] > max_bytes):
            continue
        candidates.setdefault(uid, row)
    chosen = []
    used_authors = set()
    category_counts = Counter()
    for tier in ("simple", "complex"):
        for kind in ("indoor", "outdoor", "unclassified"):
            pool = [row for row in candidates.values()
                    if row["tier"] == tier and environment(row.get("category", "")) == kind]
            for _ in range(quotas[kind]):
                eligible = [row for row in pool
                            if row["uid"] not in {x["uid"] for x in chosen}
                            and category_counts[row["category"]] < category_cap
                            and (not row.get("author") or row["author"] not in used_authors)]
                if not eligible:
                    raise ValueError(f"Not enough eligible {tier}/{kind} scenes for the audit quota")
                selected = min(eligible, key=lambda row: (
                    category_counts[row["category"]],
                    hashlib.sha256(f"{seed}:{row['uid']}".encode()).hexdigest()))
                chosen.append({**selected, "audit_environment": kind,
                               "audit_seed": seed})
                category_counts[selected["category"]] += 1
                if selected.get("author"):
                    used_authors.add(selected["author"])
    return chosen


def write_sample(catalog: Path, output: Path, *, per_tier=10, seed=0,
                 max_bytes=100_000_000) -> dict:
    rows = [json.loads(line) for line in catalog.read_text().splitlines() if line.strip()]
    selected = sample_catalog(rows, per_tier=per_tier, seed=seed, max_bytes=max_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected))
    counts = Counter((row["tier"], row["audit_environment"]) for row in selected)
    return {"output": str(output), "scenes": len(selected), "questions_if_built": 3 * len(selected),
            "quota": {f"{tier}/{kind}": count for (tier, kind), count in sorted(counts.items())},
            "seed": seed, "max_bytes": max_bytes,
            "download_verified": False, "human_reviewed": False}
