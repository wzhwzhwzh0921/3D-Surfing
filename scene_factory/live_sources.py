"""Fresh online keyword discovery with separate outdoor and indoor searches.

Only per-run HTTP snapshots are cached for recovery. No historical scene pool
or local GLB is used as a source. Search hints are not visual environment labels.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

SNAPSHOT_URL = "https://huggingface.co/datasets/Stable-X/ProSceneverse/resolve/main/metadata.parquet"

SEARCH_GROUPS = [
    ("outdoor_market", ["market", "bazaar", "street food", "food stall"]),
    ("outdoor_work", ["dock", "harbor", "harbour", "boatyard", "blacksmith", "construction site"]),
    ("outdoor_leisure", ["campsite", "campground", "picnic", "playground", "courtyard", "garden"]),
    ("indoor_work", ["workshop", "tavern", "laboratory", "bakery", "antique shop"]),
    ("indoor_home", ["living room", "bedroom", "furnished", "studio interior"]),
]


def groups(queries):
    return [("keyword_search", queries)] if queries else SEARCH_GROUPS


def read_metadata(path):
    from .sources import DiscoveryError
    try:
        import pyarrow.parquet as parquet
    except ImportError as error:
        raise DiscoveryError("Fresh online metadata requires pip install -e '.[batch]'") from error
    return parquet.read_table(path, use_threads=False).to_pylist()


def fresh_metadata(cache_dir, fingerprint):
    from .sources import _atomic, _fetch, DiscoveryError
    directory = Path(cache_dir) / fingerprint
    path, receipt_path = directory / "metadata.parquet", directory / "metadata-receipt.json"
    if not path.exists() or not receipt_path.exists():
        data, headers = _fetch(SNAPSHOT_URL, max_bytes=32_000_000)
        receipt = dict(url=SNAPSHOT_URL, fetched_at=datetime.now(timezone.utc).isoformat(),
                       metadata_sha256=hashlib.sha256(data).hexdigest(),
                       revision=next((v for k,v in headers.items() if k.lower()=="x-repo-commit"),None))
        _atomic(path, data)
        _atomic(receipt_path,json.dumps(receipt).encode())
    receipt = json.loads(receipt_path.read_text())
    if hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get("metadata_sha256"):
        raise DiscoveryError("This run's online metadata snapshot changed")
    return read_metadata(path), receipt


def retrieval_priority(row):
    """Rank downloads, never accept/reject a scene from its title or metadata."""
    name = str(row.get("name", "")).casefold()
    caption = str(row.get("caption", "")).casefold()
    scene_terms = ("scene", "diorama", "environment", "interior", "furnished")
    content_terms = ("props", "tools", "shelves", "furniture", "objects", "stalls", "detailed", "cluttered")
    return sum(3 for term in scene_terms if term in name) + sum(2 for term in scene_terms if term in caption) + sum(1 for term in content_terms if term in caption)


def discover_live(cache_dir, limit, cursor, *, config, excluded_uids):
    from .sources import (_hash, _paths, PATHS_URL, DiscoveryError, DATASET,
                          OBJAVERSE_BASE, normalize_source, source_restriction)
    fingerprint = _hash(config)
    searches = groups(config["queries"])
    state = deepcopy(cursor) if cursor else dict(
        protocol=config["protocol"], source_fingerprint=fingerprint, pages=0,
        scanned=0, seen=[], filtered={}, groups={name:dict(offset=0, emitted=0,
        done=False, buffer=[]) for name, _ in searches})
    if state.get("source_fingerprint") != fingerprint:
        raise ValueError("Live search cursor belongs to another source/configuration")
    metadata, receipt = fresh_metadata(cache_dir, fingerprint)
    if state.get("metadata_sha256") not in (None, receipt["metadata_sha256"]):
        raise DiscoveryError("Online metadata changed during this run")
    state["metadata_sha256"] = receipt["metadata_sha256"]
    paths, paths_sha256 = _paths(Path(cache_dir))
    if state.get("paths_sha256") not in (None, paths_sha256):
        raise DiscoveryError("The GLB path index changed during this run")
    state["paths_sha256"] = paths_sha256
    matches = {}
    for name, terms in searches:
        matches[name] = sorted((row for row in metadata if any(term.casefold() in
            ' '.join(str(row.get(key) or '') for key in ('name','caption','description')).casefold()
            for term in terms)), key=retrieval_priority, reverse=True)
    seen, filtered = set(state["seen"]), Counter(state["filtered"])
    rows = []
    while len(rows) < limit:
        active = [(name, terms) for name, terms in searches
                  if state["groups"][name]["buffer"] or not state["groups"][name]["done"]]
        if not active:
            break
        name, terms = min(active, key=lambda item: state["groups"][item[0]]["emitted"])
        group = state["groups"][name]
        if group["buffer"]:
            row = group["buffer"].pop(0)
            if row["uid"] in excluded_uids:
                filtered["historical_or_decided_uid"] += 1
                continue
            rows.append(row)
            group["emitted"] += 1
            continue
        if state["scanned"] >= config["max_scan"] or state["pages"] >= config["max_pages"]:
            group["done"] = True
            state["exhaustion_reason"] = "search_budget"
            continue
        length = min(config["page_size"], config["max_scan"] - state["scanned"])
        entries = matches[name][group["offset"]:group["offset"]+length]
        for entry in entries:
            raw = dict(entry)
            uid = raw.get("scene_id", "")
            if uid in seen or uid in excluded_uids:
                filtered["historical_or_decided_uid"] += 1
                seen.add(uid)
                continue
            filename = paths.get(uid)
            if (not isinstance(filename,str) or
                not re.fullmatch(r"glbs/[0-9]{3}-[0-9]{3}/[a-f0-9]{32}\.glb", filename) or
                Path(filename).stem != uid):
                filtered["no_objaverse_glb"] += 1
                continue
            raw["glb_url"] = OBJAVERSE_BASE + filename
            try:
                row = normalize_source(raw)
            except ValueError:
                filtered["invalid_metadata"] += 1
                continue
            seen.add(uid)
            reason = source_restriction(row, queries=[], max_bytes=config["max_bytes"], online=True)
            if reason:
                filtered[reason] += 1
                continue
            row.update(environment_hint=name.split('_')[0] if name.startswith(('indoor_', 'outdoor_')) else "unknown",
                       discovery=dict(provider="prosceneverse-search", dataset=DATASET,
                                      **receipt, queries=terms, search_bucket=name,
                                      objaverse_index_url=PATHS_URL,
                                      objaverse_index_sha256=paths_sha256), stage="source_discovered")
            # Search hints are kept in provenance, never substituted for VLM labels.
            group["buffer"].append(row)
        group["offset"] += len(entries)
        group["buffer"].sort(key=retrieval_priority, reverse=True)
        group["done"] = group["offset"] >= len(matches[name])
        state["pages"] += 1
        state["scanned"] += len(entries)
    state.update(seen=sorted(seen), filtered=dict(filtered))
    exhausted = all(g["done"] and not g["buffer"] for g in state["groups"].values())
    return dict(rows=rows, cursor=state, exhausted=exhausted, source_fingerprint=fingerprint,
                stats=dict(provider="prosceneverse-search", scanned=state["scanned"], pages=state["pages"],
                           returned=len(rows), filtered=dict(filtered),
                           exhaustion_reason=state.get("exhaustion_reason") or ("end_of_source" if exhausted else None)))
