"""Bounded, resumable download of a binary GLB with integrity checks."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import time
from urllib.parse import urlparse

import requests


def glb_header(path: Path) -> None:
    with path.open("rb") as f:
        header = f.read(12)
    if len(header) != 12 or header[:4] != b"glTF" or int.from_bytes(header[4:8], "little") != 2:
        raise ValueError("Not a binary glTF 2.0 file")
    if int.from_bytes(header[8:12], "little") != path.stat().st_size:
        raise ValueError("GLB header length differs from file length")


def download(url: str, target: Path, *, max_bytes: int = 100_000_000,
             expected_sha256: str | None = None, retries: int = 4) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError("A full HTTP(S) URL is required")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            glb_header(target)
            sha = hashlib.sha256(target.read_bytes()).hexdigest()
            if expected_sha256 is None or sha == expected_sha256:
                return {"path": str(target), "bytes": target.stat().st_size, "sha256": sha, "cached": True}
        except ValueError:
            pass
    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(retries + 1):
        digest = hashlib.sha256()
        total = 0
        try:
            with requests.get(url, stream=True, timeout=(20, 120)) as response:
                if response.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"Retryable HTTP {response.status_code}")
                response.raise_for_status()
                declared = int(response.headers.get("Content-Length", "0"))
                if declared > max_bytes:
                    raise ValueError(f"Asset exceeds {max_bytes} byte cap")
                with partial.open("wb") as f:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(f"Asset exceeds {max_bytes} byte cap")
                        digest.update(chunk)
                        f.write(chunk)
            glb_header(partial)
            sha = digest.hexdigest()
            if expected_sha256 and sha != expected_sha256:
                raise ValueError("SHA256 mismatch")
            os.replace(partial, target)
            return {"path": str(target), "bytes": total, "sha256": sha, "cached": False}
        except (requests.RequestException, OSError):
            partial.unlink(missing_ok=True)
            if attempt >= retries:
                raise
            time.sleep(min(2 ** attempt * 5, 40))
        except ValueError:
            partial.unlink(missing_ok=True)
            raise
    raise RuntimeError("Download exhausted")


def copy_local(source: Path, target: Path, max_bytes: int = 100_000_000) -> dict:
    glb_header(source)
    if source.stat().st_size > max_bytes:
        raise ValueError("Source exceeds size cap")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return {"path": str(target), "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "cached": False}
