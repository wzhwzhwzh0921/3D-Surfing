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

# Retrieval buckets balance variety; they never accept/reject scene quality.
SEARCH_GROUPS = [
    ("outdoor_market", ["market", "bazaar", "street food", "food stall"]),
    ("indoor_work", ["workshop", "laboratory", "blacksmith", "studio interior"]),
    ("outdoor_waterfront", ["dock", "harbor", "harbour", "boatyard", "fishing village"]),
    ("indoor_home", ["living room", "bedroom", "furnished", "apartment interior"]),
    ("outdoor_garden", ["courtyard", "garden", "backyard", "greenhouse"]),
    ("indoor_food", ["tavern", "bakery", "restaurant interior", "cafe interior", "pub interior"]),
    ("outdoor_camp", ["campsite", "campground", "picnic", "cabin scene"]),
    ("indoor_retail", ["antique shop", "grocery", "bookstore", "flower shop", "convenience store"]),
    ("outdoor_street", ["street scene", "street corner", "alley", "storefront", "town square"]),
    ("indoor_kitchen", ["kitchen", "dining room", "food preparation"]),
    ("outdoor_industry", ["construction site", "junkyard", "scrapyard", "industrial yard", "gas station"]),
    ("indoor_storage", ["warehouse", "storeroom", "storage room", "garage interior", "attic"]),
    ("outdoor_farm", ["farmyard", "farm scene", "stable yard", "barnyard", "ranch scene"]),
    ("indoor_public", ["library interior", "classroom", "museum interior", "office interior"]),
    ("outdoor_transit", ["train station", "railway station", "bus stop", "platform scene"]),
    ("indoor_hobby", ["game room", "music room", "art studio", "sewing room"]),
    ("outdoor_historic", ["temple courtyard", "castle courtyard", "village scene", "ruined courtyard"]),
    ("indoor_hospitality", ["hotel lobby", "reception", "barbershop", "salon interior"]),
    ("outdoor_leisure", ["playground", "amusement park", "rooftop", "skatepark"]),
    ("indoor_specialty", ["control room", "medical room", "workbench", "collectors room"]),
]

LIVE_PROFILES = {
    "prosceneverse-search": {
        "dataset": "Stable-X/ProSceneverse", "metadata_url": SNAPSHOT_URL,
        "glb_resolution": "objaverse_object_paths_index_v1",
    },
    "scansceneverse-search": {
        "dataset": "Stable-X/ScanSceneverse",
        "metadata_url": "https://huggingface.co/datasets/Stable-X/ScanSceneverse/resolve/main/metadata.parquet",
        "glb_resolution": "objaverse_object_paths_index_v1",
    },
}

# Search indices retain only matched metadata. Avoid rescanning 125k long
# descriptions for every single-scene production step; never cache GLBs here.
_MATCH_CACHE = {}


def groups(queries):
    return [("keyword_search", queries)] if queries else SEARCH_GROUPS


def read_metadata(path):
    from .sources import DiscoveryError
    try:
        import pyarrow.parquet as parquet
    except ImportError as error:
        raise DiscoveryError("Fresh online metadata requires pip install -e '.[batch]'") from error
    return parquet.read_table(path, use_threads=False).to_pylist()


def fresh_metadata(cache_dir, fingerprint, *, url=SNAPSHOT_URL, load_rows=True):
    from .sources import _atomic, _fetch, DiscoveryError
    directory = Path(cache_dir) / fingerprint
    path, receipt_path = directory / "metadata.parquet", directory / "metadata-receipt.json"
    if not path.exists() or not receipt_path.exists():
        data, headers = _fetch(url, max_bytes=64_000_000)
        receipt = dict(url=url, fetched_at=datetime.now(timezone.utc).isoformat(),
                       metadata_sha256=hashlib.sha256(data).hexdigest(),
                       revision=next((v for k,v in headers.items() if k.lower()=="x-repo-commit"),None))
        _atomic(path, data)
        _atomic(receipt_path,json.dumps(receipt).encode())
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("url") != url or hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get("metadata_sha256"):
        raise DiscoveryError("This run's online metadata snapshot changed")
    return read_metadata(path) if load_rows else path, receipt


def retrieval_priority(row):
    """Rank downloads, never accept/reject a scene from its title or metadata."""
    name = str(row.get("name", "")).casefold()
    caption = str(row.get("caption", "")).casefold()
    scene_terms = ("scene", "diorama", "environment", "interior", "furnished")
    content_terms = ("props", "tools", "shelves", "furniture", "objects", "stalls", "detailed", "cluttered")
    return sum(3 for term in scene_terms if term in name) + sum(2 for term in scene_terms if term in caption) + sum(1 for term in content_terms if term in caption)


def discover_live(cache_dir, limit, cursor, *, config, excluded_uids):
    from .sources import (_hash, _paths, PATHS_URL, DiscoveryError,
                          OBJAVERSE_BASE, normalize_source, source_restriction)
    fingerprint = _hash(config)
    searches = groups(config["queries"])
    provider = config["provider"]
    profile = LIVE_PROFILES[provider]
    state = deepcopy(cursor) if cursor else dict(
        protocol=config["protocol"], source_fingerprint=fingerprint, pages=0,
        scanned=0, seen=[], filtered={}, groups={name:dict(offset=0, emitted=0,
        done=False, buffer=[]) for name, _ in searches})
    if state.get("source_fingerprint") != fingerprint:
        raise ValueError("Live search cursor belongs to another source/configuration")
    metadata_path, receipt = fresh_metadata(cache_dir, fingerprint,
        url=profile["metadata_url"], load_rows=False)
    if state.get("metadata_sha256") not in (None, receipt["metadata_sha256"]):
        raise DiscoveryError("Online metadata changed during this run")
    state["metadata_sha256"] = receipt["metadata_sha256"]
    if profile["glb_resolution"] != "objaverse_object_paths_index_v1":
        raise DiscoveryError("This source needs an explicit GLB resolver")
    paths, paths_sha256 = _paths(Path(cache_dir))
    if state.get("paths_sha256") not in (None, paths_sha256):
        raise DiscoveryError("The GLB path index changed during this run")
    state["paths_sha256"] = paths_sha256
    cache_key = (str(metadata_path.resolve()), fingerprint, receipt["metadata_sha256"])
    matches = _MATCH_CACHE.get(cache_key)
    if matches is None:
        metadata = read_metadata(metadata_path)
        texts = [" ".join(str(row.get(key) or "") for key in
                 ("name", "caption", "description", "tags")).casefold() for row in metadata]
        matches = {name: sorted((row for row, text in zip(metadata, texts)
                                if any(term.casefold() in text for term in terms)),
                                key=retrieval_priority, reverse=True)
                   for name, terms in searches}
        if len(_MATCH_CACHE) >= 2:
            _MATCH_CACHE.pop(next(iter(_MATCH_CACHE)))
        _MATCH_CACHE[cache_key] = matches
    seen, filtered = set(state["seen"]), Counter(state["filtered"])
    rows = []
    while len(rows) < limit:
        active = [(name, terms) for name, terms in searches
                  if state["groups"][name]["buffer"] or not state["groups"][name]["done"]]
        if not active:
            break
        environment_totals = Counter()
        for bucket, progress in state["groups"].items():
            environment_totals[bucket.split("_")[0]] += progress["emitted"]
        name, terms = min(active, key=lambda item: (
            environment_totals[item[0].split("_")[0]], state["groups"][item[0]]["emitted"]))
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
                       discovery=dict(provider=provider, dataset=profile["dataset"],
                                      **receipt, queries=terms, search_bucket=name,
                                      glb_resolution=profile["glb_resolution"],
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
                stats=dict(provider=provider, scanned=state["scanned"], pages=state["pages"],
                           returned=len(rows), filtered=dict(filtered),
                           exhaustion_reason=state.get("exhaustion_reason") or ("end_of_source" if exhausted else None)))
