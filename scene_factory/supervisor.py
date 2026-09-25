"""Independent, image-only review contract for generated scene questions."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

LEVELS = ("simple", "medium", "hard")
VIEWS = ("start", "reveal", "detail", "answer_view")
VERDICTS = frozenset({"pass", "review", "revise"})


def evidence_hashes(work: Path) -> dict[str, str]:
    """Bind a review to the exact rendered pixels shown to the supervisor."""
    return {f"{level}/{view}.png": hashlib.sha256(
        (work / "output" / level / f"{view}.png").read_bytes()).hexdigest()
        for level in LEVELS for view in VIEWS}


def review_prompt(work: Path, validation: dict) -> str:
    """Expose questions, proposed answers and pixels, but no builder reasoning."""
    items = []
    for result in validation["results"]:
        level = result["level"]
        images = "\n".join(str(work / "output" / level / f"{view}.png") for view in VIEWS)
        items.append(f"{level}: question={result['question']!r}; proposed_answer={result['answer']['value']!r}\n"
                     f"Read these four images:\n{images}")
    return f"""You are the INDEPENDENT supervisor for a 3D exploration-QA item.
Do not read plan.json, candidates.json, visual_quality.json, inventory, code,
builder notes or previous supervisor reports. Judge only the three questions,
proposed answers and the twelve rendered images listed below. Use Read to open
EVERY image. You have no editing or shell tools.

{chr(10).join(items)}

For each tier, challenge the proposed answer and inspect:
1. Can the start image alone, or an identical original object, reveal the answer?
2. Is the named starting landmark recognizable, and is the question unambiguous?
3. Do reveal/detail/answer_view show the decisive evidence clearly enough?
4. Does the inserted object look natural, and does the stated difficulty require
   the intended exploration or extra reasoning hop?
5. If the pixels are insufficient, say review; never infer from object names.

Return ONLY one JSON object, without Markdown or other text:
{{"schema_version":"sceneqa_supervisor_v1","tiers":{{
 "simple":{{"verdict":"pass|review|revise","reason":"concrete visible evidence",
            "evidence_views":["simple/start.png","simple/answer_view.png"],
            "repair_instruction":"specific edit, required only for revise"}},
 "medium":{{"verdict":"pass|review|revise","reason":"concrete visible evidence",
            "evidence_views":["medium/start.png","medium/answer_view.png"],
            "repair_instruction":"specific edit, required only for revise"}},
 "hard":{{"verdict":"pass|review|revise","reason":"concrete visible evidence",
          "evidence_views":["hard/start.png","hard/answer_view.png"],
          "repair_instruction":"specific edit, required only for revise"}}}}}}
Replace every placeholder. In particular, each verdict must be exactly one of
pass, review or revise; never output the literal string pass|review|revise.
Use pass only when visual evidence supports the answer and intended exploration;
use revise for a concrete defect the builder can fix; use review when the images
cannot settle a remaining uncertainty. Do not mark a tier pass because a
deterministic geometry check passed. Keep reasons short and actionable.
"""


def extract_result(stream_path: Path) -> dict:
    """Read Claude Code's final result event; ignore intermediate tool text."""
    for line in reversed(stream_path.read_text(errors="replace").splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            if event.get("is_error"):
                raise ValueError("Supervisor model session failed")
            raw = (event.get("result") or "").strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            return json.loads(raw)
    raise ValueError("Supervisor stream has no final result")


def checked_image_reads(stream_path: Path, work: Path) -> list[str]:
    """Keep the supervisor blind to builder plans, notes and self-ratings."""
    allowed = {f"{level}/{view}.png" for level in LEVELS for view in VIEWS}
    seen = set()
    for line in stream_path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "Read":
                raise ValueError("Supervisor used a non-read tool")
            raw = (block.get("input") or {}).get("file_path")
            path = Path(raw or "")
            if not path.is_absolute():
                path = work / path
            try:
                rel = path.resolve().relative_to((work / "output").resolve()).as_posix()
            except ValueError as exc:
                raise ValueError("Supervisor read outside the rendered evidence") from exc
            if rel not in allowed:
                raise ValueError(f"Supervisor read non-evidence file: {rel}")
            seen.add(rel)
    if missing := allowed - seen:
        raise ValueError(f"Supervisor did not inspect all rendered evidence: {sorted(missing)}")
    return sorted(seen)


def validate_review(review: dict) -> dict:
    if review.get("schema_version") != "sceneqa_supervisor_v1":
        raise ValueError("Supervisor report has the wrong schema version")
    tiers = review.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != set(LEVELS):
        raise ValueError("Supervisor must review exactly simple, medium and hard")
    for level in LEVELS:
        row = tiers[level]
        if not isinstance(row, dict) or row.get("verdict") not in VERDICTS:
            raise ValueError(f"Supervisor {level} verdict must be pass/review/revise")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError(f"Supervisor {level} needs a concrete reason")
        views = row.get("evidence_views")
        allowed = {f"{level}/{view}.png" for view in VIEWS}
        if not isinstance(views, list) or not views or not set(views) <= allowed:
            raise ValueError(f"Supervisor {level} must cite its own evidence views")
        if row["verdict"] == "revise" and not str(row.get("repair_instruction", "")).strip():
            raise ValueError(f"Supervisor {level} needs a repair instruction")
    return review


def review_outcome(review: dict) -> str:
    verdicts = {row["verdict"] for row in review["tiers"].values()}
    return "revise" if "revise" in verdicts else "review" if "review" in verdicts else "pass"


def repair_prompt(work: Path, review: dict, round_index: int) -> str:
    issues = {level: row for level, row in review["tiers"].items()
              if row["verdict"] == "revise"}
    return f"""Repair round {round_index} for the existing scene-QA work in {work}.
Read {work / 'task.txt'} for the full production rules and inspect the current
{work / 'plan.json'}, {work / 'candidates.json'}, probe report, validation.json,
and the relevant rendered PNGs. An independent image-only supervisor found the
following concrete defects (not ground-truth answers):
{json.dumps(issues, ensure_ascii=False, indent=2)}

Fix ONLY the tiers marked revise. The runner will reject any edit to a pass
tier or to the source GLB. If changing any candidate's question, placement, landmark or
copy, update its candidate pool entry, rerun `sceneqa probe`, inspect both new
previews, reselect, then rebuild and verify. Do not edit GLB bytes directly.
Read `start_1.png` for each of the six final candidates (and `start_2.png`
when considering that view), then read all twelve final output images;
write a fresh visual_quality.json with a per-tier pass/review/fail reason.
If a requested repair cannot be made honestly, keep the strongest valid version
and mark the unresolved tier review/fail; never invent visual evidence or claim
that a geometry pass proves the answer. Finish with a concise summary.
"""
