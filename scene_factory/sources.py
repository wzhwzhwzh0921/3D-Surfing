"""Bounded source discovery; scene quality is judged by the visual model later.

``discover_batch`` returns a JSON-serializable opaque cursor. Persist the returned
rows and cursor together before processing assets. Resume pending rows first;
pass all already decided UIDs through ``excluded_uids``. An exception leaves the
input cursor untouched and must be treated as infrastructure failure, not reject.

The default provider reads public ProSceneverse dataset-viewer pages and joins
them with Objaverse's public GLB path index. Individual asset licenses and author
fields are retained; a dataset license is never substituted for an asset license.
Only requests and the Python standard library are required.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlparse

import requests

PROTOCOL = "sceneqa-discovery-v2-model-screen"
DATASET = "Stable-X/ProSceneverse"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
OBJAVERSE_BASE = "https://huggingface.co/datasets/allenai/objaverse/resolve/main/"
PATHS_URL = OBJAVERSE_BASE + "object-paths.json.gz"
SAFE_UID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SIZE_FIELDS = ("size_bytes", "bytes", "file_size_bytes", "glb_size_bytes",
               "fileSize", "size", "glb_bytes", "asset_bytes")


class DiscoveryError(RuntimeError):
    """Retryable source/infrastructure problem (not an unsuitable asset)."""


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def _fetch(url: str, *, max_bytes: int, retries: int = 3) -> tuple[bytes, dict]:
    """Bounded HTTP, including numeric Retry-After; no secrets or HF token needed."""
    for attempt in range(retries + 1):
        try:
            with requests.get(url, stream=True, timeout=(20, 90),
                              headers={"User-Agent": "scene-qa-factory/0.1 metadata-discovery"}) as response:
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt == retries:
                        raise DiscoveryError(f"Source HTTP {response.status_code}: {url}")
                    delay = response.headers.get("Retry-After", "")
                    time.sleep(min(float(delay) if delay.isdigit() else 2 ** attempt, 30))
                    continue
                response.raise_for_status()
                if int(response.headers.get("Content-Length", "0")) > max_bytes:
                    raise DiscoveryError(f"Metadata response exceeds {max_bytes} bytes: {url}")
                chunks, total = [], 0
                for chunk in response.iter_content(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise DiscoveryError(f"Metadata response exceeds {max_bytes} bytes: {url}")
                    chunks.append(chunk)
                return b"".join(chunks), dict(response.headers)
        except (requests.RequestException, ValueError) as exc:
            # Permanent HTTP failures also remain infrastructure errors, never rejects.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == retries or (status is not None and 400 <= status < 500 and status != 429):
                raise DiscoveryError(f"Metadata request failed: {url}: {exc}") from exc
            time.sleep(min(2 ** attempt, 30))
    raise DiscoveryError(f"Metadata retry budget exhausted: {url}")


def normalize_source(row: dict) -> dict:
    """Retain metadata and normalize aliases; reject unsafe path identifiers."""
    if not isinstance(row, dict):
        raise ValueError("Source row must be an object")
    result = dict(row)
    uid = str(row.get("uid") or row.get("scene_id") or "")
    url = row.get("glb_url") or row.get("download_url") or row.get("url")
    if not uid and isinstance(url, str):
        uid = Path(urlparse(url).path).stem
    if not SAFE_UID.fullmatch(uid):
        raise ValueError("Source uid must be a safe 1–80 character path identifier")
    if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise ValueError("Source requires a full HTTP(S) GLB download URL")
    result.update(uid=uid, scene_id=uid, url=url, glb_url=url, download_url=url)
    category = row.get("category")
    result["category"] = category.strip() if isinstance(category, str) and category.strip() else "UNKNOWN"
    license_value = row.get("license_slug") or row.get("license")
    if isinstance(license_value, dict):
        license_value = license_value.get("slug")
    if license_value:
        result["license_slug"] = license_value
    return result


def source_size_bytes(row: dict) -> int | None:
    """Use usable size hints only; actual downloads always enforce the budget.

    Missing/malformed metadata does not imply low scene quality. Use the largest
    valid hint if aliases disagree, rather than depending on dictionary order.
    """
    values = []
    for field in SIZE_FIELDS:
        value = row.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            values.append(int(value))
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            values.append(int(value))
    return max(values) if values else None


def source_restriction(row: dict, *, queries: list[str], max_bytes: int, online: bool) -> str | None:
    """Source availability/budget restrictions only, never scene quality.

    Online attribution/license restrictions are unchanged. Categories merely
    balance sampling. A user-specified query narrows retrieval, not acceptance.
    """
    if online:
        if row.get("license_slug") not in ("by", "by-sa", "cc0"):
            return "license"
        if not row.get("source_url") or not row.get("author_username"):
            return "missing_attribution"
    size = source_size_bytes(row)
    if size is not None and size > max_bytes:
        return "asset_bytes"
    text = " ".join(str(row.get(k) or "") for k in ("name", "caption", "description", "tags"))
    if queries and not any(query.casefold() in text.casefold() for query in queries):
        return "query"
    return None


def _eligible(row: dict, *, queries: list[str], max_bytes: int, online: bool,
              max_faces=None, min_faces=None, min_materials=None, min_textures=None) -> str | None:
    """Compatibility alias; retired geometry/appearance thresholds are ignored."""
    return source_restriction(row, queries=queries, max_bytes=max_bytes, online=online)


def _paths(cache_dir: Path) -> tuple[dict, str]:
    path = cache_dir / "objaverse-object-paths.json.gz"
    if not path.exists():
        data, _ = _fetch(PATHS_URL, max_bytes=32_000_000)
        try:
            decoded = json.loads(gzip.decompress(data))
            if not isinstance(decoded, dict):
                raise ValueError("Path index must be an object")
        except (ValueError, OSError) as exc:
            raise DiscoveryError("Invalid Objaverse path index") from exc
        _atomic(path, data)
    try:
        data = path.read_bytes()
        decoded = json.loads(gzip.decompress(data))
        if not isinstance(decoded, dict):
            raise ValueError("Path index must be an object")
        return decoded, hashlib.sha256(data).hexdigest()
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"Invalid cached Objaverse path index: {path}") from exc


def _page(cache_dir: Path, offset: int, length: int) -> dict:
    path = cache_dir / f"page-{offset:08d}.json"
    if not path.exists():
        url = ROWS_URL + "?" + urlencode(dict(dataset=DATASET, config="default",
                                            split="train", offset=offset, length=length))
        data, headers = _fetch(url, max_bytes=8_000_000)
        try:
            payload = json.loads(data)
            if (not isinstance(payload.get("rows"), list)
                    or not isinstance(payload.get("num_rows_total"), int)
                    or payload.get("partial")):
                raise ValueError("Incomplete dataset-viewer response")
            payload["_discovery"] = {
                "url": url, "revision": next((v for k, v in headers.items() if k.lower() == "x-revision"), None),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        except (ValueError, AttributeError) as exc:
            raise DiscoveryError("Invalid ProSceneverse metadata page") from exc
        _atomic(path, json.dumps(payload, ensure_ascii=False).encode())
    try:
        payload = json.loads(path.read_text())
        if (not isinstance(payload["rows"], list)
                or not isinstance(payload["num_rows_total"], int)
                or not isinstance(payload["_discovery"], dict)
                or payload.get("partial")):
            raise ValueError("Invalid page structure")
        return payload
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DiscoveryError(f"Invalid cached metadata page: {path}") from exc


def source_config(*, provider: str = "prosceneverse", catalog: Path | None = None,
                  queries: list[str] | None = None, max_bytes: int = 100_000_000,
                  max_faces: int | None = None, max_scan: int = 10_000,
                  max_pages: int = 100, page_size: int = 100) -> dict:
    """Predict stable source configuration without network access.

    Catalog fingerprints read the file's content. Its machine-specific path is
    deliberately excluded, so copying a catalog does not invalidate its cursor.
    max_faces is a retired compatibility argument; it does not filter sources
    or change this fingerprint. Scene quality belongs to the model screen.
    """
    if catalog is not None:
        provider = "catalog"
    config = dict(protocol=PROTOCOL, provider=provider,
                  queries=sorted(set(q.strip().casefold() for q in queries or [] if q.strip())),
                  max_bytes=max_bytes, max_scan=max_scan, quality_screen="visual_model",
                  max_pages=max_pages, page_size=page_size, dataset=DATASET)
    if catalog is not None:
        try:
            config["catalog_sha256"] = hashlib.sha256(Path(catalog).read_bytes()).hexdigest()
        except OSError as exc:
            raise DiscoveryError(f"Unreadable JSONL catalog: {catalog}") from exc
    elif provider == "prosceneverse-search":
        from .live_sources import groups, SNAPSHOT_URL
        config["search_groups"] = groups(config["queries"])
        config["search_transport"] = "fresh_metadata_snapshot_v1"
        config["metadata_url"] = SNAPSHOT_URL
        config["glb_resolution"] = "objaverse_object_paths_index_v1"
    return config


def source_fingerprint(**kwargs) -> str:
    """Hash ``source_config`` for controller checkpoint compatibility checks."""
    return _hash(source_config(**kwargs))


def discover_batch(cache_dir: Path, limit: int, cursor: dict | None = None, *,
                   provider: str = "prosceneverse", catalog: Path | None = None,
                   queries: list[str] | None = None, max_bytes: int = 100_000_000,
                   excluded_uids: set[str] | None = None, max_faces: int | None = None,
                   max_scan: int = 10_000, max_pages: int = 100, page_size: int = 100) -> dict:
    """Discover at most ``limit`` rows, preserving deterministic resumable order.

    ``exhausted`` means the configured source window and pending buffer are empty.
    ``stats.exhaustion_reason`` distinguishes end_of_source from scan_budget or
    page_budget. Raising caps/altering queries requires a new cursor. A supplied
    catalog selects replay automatically and is fingerprinted by content hash.
    Neither successful nor excluded sources are rediscovered with this cursor.
    GLB URLs come from the actual Objaverse index; asset HTTP/size validation is
    delegated to the existing downloader (only metadata is fetched here).
    """
    if limit <= 0 or max_scan <= 0 or max_pages <= 0 or not 1 <= page_size <= 100:
        raise ValueError("Positive discovery budgets required; page_size must be 1–100")
    if max_bytes <= 0:
        raise ValueError("Positive asset byte budget required")
    cache_dir = Path(cache_dir)
    queries = sorted(set(q.strip().casefold() for q in queries or [] if q.strip()))
    excluded = set(excluded_uids or ())
    if catalog is not None:
        provider = "catalog"
    if provider not in ("catalog", "prosceneverse", "prosceneverse-search"):
        raise ValueError(f"Unknown discovery provider: {provider}")
    if provider == "catalog" and catalog is None:
        raise ValueError("catalog provider requires a JSONL catalog path")
    config = source_config(provider=provider, catalog=catalog, queries=queries,
                           max_bytes=max_bytes, max_faces=max_faces, max_scan=max_scan,
                           max_pages=max_pages, page_size=page_size)
    if provider == "prosceneverse-search":
        from .live_sources import discover_live
        return discover_live(cache_dir, limit, cursor, config=config, excluded_uids=excluded)
    catalog_rows = None
    if catalog is not None:
        try:
            data = Path(catalog).read_bytes()
            config["catalog_sha256"] = hashlib.sha256(data).hexdigest()
            catalog_rows = [json.loads(line) for line in data.splitlines() if line.strip()]
        except (OSError, ValueError) as exc:
            raise DiscoveryError(f"Unreadable JSONL catalog: {catalog}") from exc
    fingerprint = _hash(config)
    state = deepcopy(cursor) if cursor is not None else dict(
        protocol=PROTOCOL, source_fingerprint=fingerprint, offset=0, pages=0,
        scanned=0, buffer=[], seen=[], categories={}, filtered={}, source_done=False,
        exhaustion_reason=None)
    if state.get("protocol") != PROTOCOL or state.get("source_fingerprint") != fingerprint:
        raise ValueError("Discovery cursor belongs to different source/configuration")
    rows, seen = [], set(state["seen"])
    filtered, category_counts = Counter(state["filtered"]), Counter(state["categories"])
    paths = None
    while len(rows) < limit:
        if state["buffer"]:
            # Prefer the least emitted category, retain source order for ties.
            i = min(range(len(state["buffer"])), key=lambda i: category_counts[state["buffer"][i].get("category", "UNKNOWN")])
            row = state["buffer"].pop(i)
            if row["uid"] in excluded:
                filtered["excluded"] += 1
                continue
            rows.append(row)
            category_counts[row.get("category", "UNKNOWN")] += 1
            continue
        if state["source_done"]:
            break
        if state["scanned"] >= max_scan or state["pages"] >= max_pages:
            state.update(source_done=True, exhaustion_reason=(
                "scan_budget" if state["scanned"] >= max_scan else "page_budget"))
            continue
        length = min(page_size, max_scan - state["scanned"])
        if catalog_rows is not None:
            batch = catalog_rows[state["offset"]:state["offset"] + length]
            total = len(catalog_rows)
            provenance = dict(provider="catalog", catalog_sha256=config["catalog_sha256"])
        else:
            if paths is None:
                paths, digest = _paths(cache_dir)
                if state.get("paths_sha256") not in (None, digest):
                    raise DiscoveryError("Objaverse index changed during discovery; restore cached snapshot")
                state["paths_sha256"] = digest
            page = _page(cache_dir / fingerprint, state["offset"], length)
            revision = page.get("_discovery", {}).get("revision")
            if state.get("revision") not in (None, revision):
                raise DiscoveryError("ProSceneverse revision changed; restore cached snapshot or start a new cursor")
            state["revision"] = revision
            try:
                batch = [entry["row"] for entry in page["rows"]]
            except (KeyError, TypeError) as exc:
                raise DiscoveryError("Malformed dataset-viewer rows") from exc
            if len(batch) > length:
                raise DiscoveryError("Source returned more rows than its requested scan budget")
            total = page["num_rows_total"]
            provenance = dict(provider=provider, dataset=DATASET, **page["_discovery"],
                              objaverse_index_url=PATHS_URL, objaverse_index_sha256=digest)
        if not batch and state["offset"] < total:
            raise DiscoveryError("Source returned an empty page before its declared end")
        for raw in batch:
            if not isinstance(raw, dict):
                raise DiscoveryError("Metadata row is not an object")
            raw = dict(raw)
            if paths is not None:
                uid = str(raw.get("scene_id") or raw.get("uid") or "")
                path = paths.get(uid)
                if not isinstance(path, str) or not re.fullmatch(r"glbs/[0-9]{3}-[0-9]{3}/[a-f0-9]{32}\.glb", path) or Path(path).stem != uid:
                    filtered["no_objaverse_glb"] += 1
                    continue
                raw["glb_url"] = OBJAVERSE_BASE + path
            try:
                row = normalize_source(raw)
            except ValueError:
                filtered["invalid_identifier_or_url"] += 1
                continue
            if row["uid"] in seen or row["uid"] in excluded:
                seen.add(row["uid"])
                filtered["duplicate_or_excluded"] += 1
                continue
            seen.add(row["uid"])
            reason = source_restriction(row, queries=queries, max_bytes=max_bytes, online=paths is not None)
            if reason:
                filtered[reason] += 1
                continue
            row["discovery"] = provenance
            row["stage"] = "source_discovered"
            state["buffer"].append(row)
        state["offset"] += len(batch)
        state["scanned"] += len(batch)
        state["pages"] += 1
        if state["offset"] >= total:
            state.update(source_done=True, exhaustion_reason="end_of_source")
    state.update(seen=sorted(seen), categories=dict(category_counts), filtered=dict(filtered))
    exhausted = bool(state["source_done"] and not state["buffer"])
    return dict(rows=rows, cursor=state, exhausted=exhausted,
                source_fingerprint=fingerprint,
                stats=dict(provider=provider, scanned=state["scanned"], pages=state["pages"],
                           returned=len(rows), pending=len(state["buffer"]),
                           filtered=dict(filtered), categories=dict(category_counts),
                           exhaustion_reason=state["exhaustion_reason"] if exhausted else None))
