"""Round-robin public source adapters with one transactional discovery cursor."""
from collections import Counter
from copy import deepcopy
from pathlib import Path


def discover_multi(cache_dir, limit, cursor, *, config, excluded_uids):
    from .live_sources import discover_live
    from .sources import _hash

    fingerprint = _hash(config)
    children = {item["provider"]: item for item in config["sources"]}
    state = deepcopy(cursor) if cursor is not None else {
        "protocol": config["protocol"], "source_fingerprint": fingerprint,
        "seen": [], "sources": {name: {"cursor": None, "emitted": 0,
            "exhausted": False, "stats": {}} for name in children}}
    if state.get("source_fingerprint") != fingerprint or state.get("protocol") != config["protocol"]:
        raise ValueError("Multi-source cursor belongs to another source/configuration")
    seen, rows = set(state["seen"]), []
    while len(rows) < limit:
        active = [name for name in children if not state["sources"][name]["exhausted"]]
        if not active:
            break
        name = min(active, key=lambda item: state["sources"][item]["emitted"])
        progress = state["sources"][name]
        # No exception is swallowed: a failed adapter must not become exhaustion
        # or silently change the intended source mix. The caller commits rows
        # and the returned cursor together only after this call succeeds.
        result = discover_live(Path(cache_dir) / name, 1, progress["cursor"],
            config=children[name], excluded_uids=excluded_uids | seen)
        progress.update(cursor=result["cursor"], stats=result["stats"],
                        exhausted=result["exhausted"])
        for row in result["rows"]:
            if row["uid"] in seen or row["uid"] in excluded_uids:
                continue
            seen.add(row["uid"])
            rows.append(row)
            progress["emitted"] += 1
        if not result["rows"] and not result["exhausted"]:
            raise RuntimeError("Source adapter returned neither a row nor exhaustion")
    state["seen"] = sorted(seen)
    exhausted = all(item["exhausted"] for item in state["sources"].values())
    limited = config["budget_limited_sources"] or any(
        item["stats"].get("exhaustion_reason") not in (None, "end_of_source")
        for item in state["sources"].values())
    filtered = Counter()
    for item in state["sources"].values():
        filtered.update(item["stats"].get("filtered", {}))
    return dict(rows=rows, cursor=state, exhausted=exhausted,
        source_fingerprint=fingerprint, stats=dict(provider="multi-source",
            scanned=sum(item["stats"].get("scanned", 0) for item in state["sources"].values()),
            pages=sum(item["stats"].get("pages", 0) for item in state["sources"].values()),
            returned=len(rows), filtered=dict(filtered),
            sources={name: {**item["stats"], "emitted": item["emitted"],
                           "exhausted": item["exhausted"]}
                     for name, item in state["sources"].items()},
            exhaustion_reason=("search_budget" if limited else "end_of_source") if exhausted else None))
