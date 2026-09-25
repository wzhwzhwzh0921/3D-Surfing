"""One Claude Code + DeepSeek session per input scene, with resumable artifacts."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from copy import deepcopy
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .download import copy_local, download
from .gateway import gateway, summarize_usage
from .openai_bridge import bridge
from .question_types import QUESTION_TYPES, type_order
from .schema import read_candidate_pool, read_plan, validate_selection
from .suitability import screen
from .supervisor import (checked_image_reads, evidence_hashes, extract_result, reconcile_independent_answers,
                         repair_prompt, review_outcome, review_prompt,
                         validate_review)
from .validate import validate

ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).with_name("blender_worker.py")
AGENT_SYSTEM = (
    "You are a production scene-QA planner. Use the supplied inventory, four previews, "
    "plan schema, and candidate-probe protocol as your interface. Decide suitability first. "
    "If fewer than two distinct native small-prop categories can be augmented "
    "naturally, write unsuitable.json and stop. Otherwise propose six candidates "
    "(two per tier), probe camera visibility, select three, then build and inspect "
    "start/reveal/detail/answer_view for every tier. Write your own answers, "
    "and verify; revise and re-probe candidates if needed. Write visual_quality.json after "
    "the final visual self-review. Do not proactively call --help, "
    "inspect framework source, scan the filesystem, extract raw GLB textures, "
    "install packages, or write custom "
    "rendering scripts. If build or verify fails, inspect the concise error and "
    "output/validation.json, then revise plan.json. Never read package source code. "
    "Keep original geometry/materials. Answers come from your visual inspection, "
    "not from a registry answer calculator."
)
REVIEW_SYSTEM = "You are an independent visual supervisor. Read only the listed images and return strict JSON."
REPAIR_SYSTEM = (
    "You are a targeted scene-QA repair agent. Follow task.txt and the supplied "
    "supervisor feedback. Preserve passing tiers, re-probe changed proposals, "
    "rebuild, inspect every final image and report uncertainty honestly."
)


def blender_exe() -> str:
    return os.environ.get("SCENEQA_BLENDER", "/home/ubuntu/blender/tools/blender-5.2.1-linux-x64/blender")


def provider_key(provider: str, key_file: Path | None = None) -> str:
    if key_file:
        content = key_file.read_text().strip()
        if "\n" not in content and "=" not in content:
            return content
        names = (("TENCENT_ENDPOINT_API_KEY", "TOKENHUB_API_KEY") if provider == "tokenhub"
                 else ("DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
        entries = {}
        for line in content.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                entries[name.strip().removeprefix("export ")] = value.strip().strip('"\'')
        return next((entries[name] for name in names if entries.get(name)), "")
    names = ("TOKENHUB_API_KEY", "TENCENT_ENDPOINT_API_KEY") if provider == "tokenhub" else (
        "DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    return next((os.environ[name] for name in names if os.environ.get(name)), "")


def run_model_phase(work: Path, *, prompt: str, system: str, tools: str,
                    stream_path: Path, stderr_path: Path, usage_path: Path,
                    provider: str, key: str, model: str, upstream: str,
                    timeout: int) -> dict:
    """Run one isolated Claude Code phase through the existing credential proxy."""
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("Claude Code CLI not found in PATH")
    usage_path.unlink(missing_ok=True)
    proxy_factory = bridge if provider == "tokenhub" else gateway
    proxy_args = ({"endpoint": upstream} if provider == "tokenhub" else {"upstream": upstream})
    with proxy_factory(**proxy_args, key=key, model=model, output=usage_path,
                       max_tokens=65536, retry_delays=(20, 40, 60, 120), timeout=900) as proxy:
        env = os.environ.copy()
        for name in ("TOKENHUB_API_KEY", "TENCENT_ENDPOINT_API_KEY", "DEEPSEEK_API_KEY",
                     "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(name, None)
        env.update({"ANTHROPIC_BASE_URL": proxy.url, "ANTHROPIC_API_KEY": proxy.token,
                    "ANTHROPIC_AUTH_TOKEN": proxy.token, "ANTHROPIC_MODEL": model,
                    "SCENEQA_BLENDER": blender_exe(),
                    "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
                    "PIP_NO_CACHE_DIR": "1", "TMPDIR": str(work)})
        command = [claude, "--bare", "--safe-mode", "--print", "--verbose",
                   "--output-format", "stream-json",
                   "--model", "sonnet" if provider == "tokenhub" else model,
                   "--effort", "low", "--append-system-prompt", system,
                   "--tools", tools, "--allowedTools", tools,
                   "--permission-mode", "acceptEdits", "--strict-mcp-config",
                   "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                   "--no-session-persistence", "--disable-slash-commands", prompt]
        with stream_path.open("w") as out, stderr_path.open("w") as err:
            result = subprocess.run(command, cwd=work, env=env, stdout=out, stderr=err,
                                    timeout=timeout)
    usage = summarize_usage(usage_path)
    if result.returncode:
        raise RuntimeError(f"Claude {stream_path.name} failed with exit code {result.returncode}")
    return usage


def blender(mode: str, source: Path, output: Path, plan: Path | None = None, *, timeout=1800) -> None:
    command = [blender_exe(), "--background", "--threads", "2", "--python", str(WORKER),
               "--", mode, str(source), str(output)]
    if plan:
        command.append(str(plan))
    log = output / f"blender_{mode}.log"
    output.mkdir(parents=True, exist_ok=True)
    with log.open("w") as f:
        result = subprocess.run(command, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode or "Traceback (most recent call last)" in log.read_text(errors="replace"):
        raise RuntimeError(f"Blender {mode} failed; inspect {log}")


def prompt_for(scene_id: str, work: Path, supervisor_rounds: int = 0) -> str:
    seed = int(os.environ.get("SCENEQA_QUESTION_SEED", "0"))
    order = type_order(scene_id, seed)
    menu = ", ".join(f"{kind} ({QUESTION_TYPES[kind]})" for kind in order)
    session_note = ("An independent read-only supervisor may review your finished images "
                    "and request a later targeted repair session. Complete this first pass "
                    "as fully as you can; do not defer known problems to the supervisor."
                    if supervisor_rounds else
                    "No second Claude session will be launched. Complete all revisions in this session.")
    return f"""You are preparing one 3D scene QA benchmark item in one initial session.
Scene ID: {scene_id}. Work directory: {work}.
The source is {work / 'source.glb'}. Inventory: {work / 'inspect/inventory.json'}.
Four color previews are in {work / 'inspect'}; use Read on the PNGs.
First create {work / 'candidates.json'} following {ROOT / 'docs/CANDIDATE_PROBE.md'}.
Use {ROOT / 'examples/candidate_fixture_pool.json'} only as a STRUCTURAL
example of six complete entries; adapt all meshes, placements and questions.
It must contain exactly six full candidate variants: TWO distinct proposals per
level, simple/medium/hard. Each candidate declares start_landmarks: named
original mesh group(s) that a solver must recognize in the starting image.
Within a tier, make the two candidates materially different questions or
target categories; changing only the copy position or camera wastes the
fallback slot if the target itself is visually ambiguous.
For hard questions, list the initial reference landmark only; candidate
anchors that the solver is supposed to discover later need not be visible
at the start. Do not name a distant unrelated table as the starting landmark.
Write candidates promptly after source inspection, then call:
{sys.executable} -m scene_factory.cli probe --source {work / 'source.glb'} --candidates {work / 'candidates.json'} --output {work / 'probe'}
Read {work / 'probe/probe_report.json'} and each candidate's start_1.png and
start_2.png. The probe reports landmark pixel extent and visible surface
fraction, fully hidden or partly blocked copied
targets, failure_reasons, the first actual occluding mesh, and whether local blocker conditions
pass. Prefer an eligible angle where all named landmarks are recognizable and
the decisive answer-bearing feature is hidden. A partly visible target is fine
when its answer-bearing side remains concealed. If neither proposal for a level is eligible,
revise candidates and run probe again before committing to a final plan.
Then select exactly one candidate per level and create {work / 'plan.json'} following {ROOT / 'docs/PLAN_SCHEMA.md'} and
{ROOT / 'docs/QUESTION_TYPES.md'}. Apply {ROOT / 'docs/REVIEW_POLICY.md'}
for difficulty and per-tier visual decisions. Use schema_version 1.3.
Use the select command to copy your chosen proposals without transcription:
{sys.executable} -m scene_factory.cli select --candidates {work / 'candidates.json'} --probe-report {work / 'probe/probe_report.json'} --simple SIMPLE_ID --medium MEDIUM_ID --hard HARD_ID --output {work / 'plan.json'}
If you prefer a candidate's second preview, add --simple-view 2,
--medium-view 2 or --hard-view 2 for that level. Inspect both images first.
Set workflow_version=candidate_probe_v1. Preserve the selected candidate_id
and all probed fields; only add its selected eligible start_camera and later
correct declared_answer after visual review. Do not change placement, question
or landmarks without probing again. The three chosen question types and answers
must differ. Finish all three tiers in this initial Claude Code session.
Use {ROOT / 'examples/model_answer_fixture_plan.json'} only as a STRUCTURAL example; adapt every
object name, location and question to this source. Read inventory and four previews.
Do not call --help or inspect package source files, runner logs or status.json; the
schema and supplied commands are the complete interface.
Use the supplied rendered images for appearance; do not unpack GLB images or
create texture-extraction scripts.
There must be exactly three independently augmented variants from the SAME original source:
simple, medium, hard. Use only native visible scene objects as copy templates.
No imported plush toys, props, color changes, invisible labels or unrelated decorations.
Copy an object to natural physical support (table, shelf, floor, bench), usually behind
existing occluders. Explicitly list all relevant ORIGINAL instances in entities;
include every matching original, even hidden ones. Keep source geometry and materials.
Before planning, check that the inventory contains at least two distinct small
meshes that can be copied individually. Props visible in a preview but merged
into a whole-house, vegetation or ground mesh do not count. Such an asset is
unsuitable even if the picture looks richly furnished.
Also identify three plausible question niches: a native target, a natural
support/occluder, and a recognizable start landmark for EACH tier. Different
niches may share a prop category but should not repeat the same answer with
only a changed number. If the scene cannot support three such niches, mark it
unsuitable before spending time on six detailed candidates.
QUESTION TYPES AND RANDOMIZATION: This scene's reproducible seed is {seed}.
The randomized type order is: {menu}.
Try feasible types in that order across the six proposals; after probing,
select a different question_type for each final tier. If a type is infeasible,
skip it and explain why in agent_notes.md. Do not force scene-inappropriate props.
The same type may be simple, medium or hard in OTHER scenes; types are not tied to tiers.
For count use mode count/compare; for identity use mode identify. The other eight
types use mode evidence. EVERY question needs declared_answer and evidence_ids.
The answer is your own visual observation, including count and identity. The
program does not calculate or cross-check question answers. Use evidence_ids for
the copied/native objects you inspected, not arbitrary names.
Write a provisional declared_answer in the plan so build can run; after opening
the generated answer views, correct it if the images show something different.
For a count question, every relevant original and copy must be visually countable
from the supplied/rendered views. If scattered or hidden originals make the total
ambiguous, choose a smaller named region or skip count rather than guessing.
List every object needed to answer in question.evidence_ids; for region boundaries,
direction landmarks or a nearest-anchor comparison, list original mesh names in
question.evidence_context_objects. The builder frames these together in
answer_view.png and reports how many evidence groups are actually visible.
If answer_view_coverage.all_visible is false, move objects, change the question
or choose another candidate; do not infer an answer from world coordinates.
Use natural Chinese questions and keep three answers distinct.

QUESTION QUALITY AUDIT (apply separately to all three variants, not just verify):
1. Read each tier's start.png, reveal.png, detail.png and answer_view.png after building. At the
   start, can a solver answer from a visible target, a duplicate native prop,
   the wording, or a general scene convention without moving the camera? If yes,
   change the question, placement or start camera and rebuild. A hidden copy alone
   does not prevent an answer shortcut: another identical chair can reveal its
   color, and another identical lantern can make an identity question guessable.
2. After orbiting/focusing, is the answer itself visually identifiable from the
   revealed evidence? For identity, distinctive parts must be visible, not only
   a generic body or the template object's mesh name. The target must also be
   distinguishable from nearby original props: a loose white cushion beside
   a white-cushioned sofa is a poor identity question even when its occlusion
   checks pass. Prefer a native prop with a distinctive outline or material,
   or ask about a visible distinguishing attribute if it remains hidden at
   the start. For color/texture, the
   decisive surface must be visible under the actual scene lighting. For count,
   the named region and all matching objects in it must be inspectable. Do not
   use registry counts, copy IDs, object names or the planned template as proof
   of what a solver can see. If uncertain, choose a clearer target or viewpoint.
3. Check uniqueness of wording and location. A phrase like 'behind the chair'
   must pick one chair and one spatial side; 'faces the van' must be unambiguous
   from an accessible view. For nearest-anchor questions, the visually chosen
   anchor must match the intended one and the second step must still be needed.
4. Ask whether the copied object looks natural at its final position and whether
   the question has one stable answer. A numerical visibility/occlusion gate is
   necessary but not a semantic pass. If a tier remains ambiguous, revise it;
   if no sound version exists for this scene, say so honestly in agent_notes.md
   instead of calling the answer certain.
5. For counts and comparisons, the answer view must show all relevant objects
   and the named region together. A detail crop around only the first copy is
   insufficient. For relative-position or direction questions, the answer view
   must show the target, reference object and direction landmarks together;
   world coordinates alone do not establish a visually answerable question.
   If the supplied views cannot do this, redesign the question or report a
   visual-quality failure rather than asserting an answer.
6. Do not accept a start image dominated by a roof, enclosing shell or blank
   background when the named landmarks shrink to a few pixels. The numeric
   landmark gate is only a floor: reject a table visible as just a sliver
   behind walls after inspecting the actual rendered start image. Do not identify
   a hidden prop from an undiagnostic fabric corner or generic silhouette.
   Avoid low-contrast pairings such as transparent glasses on a glass tabletop;
   if the objects cannot be counted or recognized in the rendered pixels,
   their known mesh count and names cannot rescue the question.
   For EACH tier, name where the question's primary landmark appears in start.png
   and whether it is large enough to recognize. If you cannot point to it in
   the actual image, change the start camera or question; hiding the target
   cannot compensate for hiding the landmark needed to start exploring.

DIFFICULTY: Simple gives a direct target but hides critical evidence at the start.
Medium gives a direct target/anchor but requires overcoming VISUAL OCCLUSION:
use at most two target copies, place at least one behind a nearby natural scene
prop, and declare occlusion_targets with exact blocker mesh name(s). The builder raycasts target surface
samples and requires at least 20% blocked by that named prop at the start view,
with the named prop responsible for at least half of first obstruction hits;
   the blocker must be small relative to the scene and close to the target, so a
   perimeter wall, floor slab or roof alone is not sufficient. Before committing
   a start camera, inspect which mesh actually blocks the target ray; if the
   floor/roof blocks first, rotate the camera or move the target rather than
   naming a nearby chair or cabinet as the blocker. Hard adds ONE REASONING HOP: first resolve
a unique nearest anchor from a named reference and at least two candidate scene
objects, then move to that anchor and overcome local occlusion. Declare
anchor_query with relation=nearest, reference_object, candidate_objects,
answer_anchor_object, target_copy_id. The target must be occluded by the resolved
answer anchor. A second hidden decoy is optional; more objects alone do not make
the question hard. In each tier, conceal at least one copied target or its
answer-bearing feature at the start. For a partially visible simple target,
declare an occlusion_targets entry for its nearby blocker so the probe can
measure that concealment. Choose an explicit
start camera so the answer is not directly available. All copies must be discoverable
from a global orbit or from a focused, zoomed local view. Hard need not claim
30 actions without blind test.
Treat the start camera as a design variable: try other azimuth/elevation angles
if the first view reveals the answer or hides the landmarks needed to understand
the question. Prefer a start view that shows the scene and target landmark but
withholds only the decisive evidence. Partial surface occlusion is fine when the
answer-bearing features become clear from a reachable view; do not demand a fully
unobstructed object. Difficulty is relative to this scene: meet each tier's
observation and reasoning requirements without forcing a fixed action count or
sacrificing readability to make every hard item equally hard.
If fewer than TWO distinct, small, naturally repeatable native object categories
exist, do not invent kinds such as whole buildings, walls, structural beams,
ground or water merely to fill the quota. Instead write {work / 'unsuitable.json'}
with {{"status":"unsuitable","reasons":["..."],"available_native_categories":["..."]}}
and finish. This is an acceptable outcome for batch filtering.
Use unsuitable.json only for source-asset screening before a plan is built.
If a built scene fails the visual audit, use visual_quality.json with status
rejected instead; do not write unsuitable.json for that outcome.
Your tools: Bash, Read, Write, Edit, Glob, Grep. You can call:
{sys.executable} -m scene_factory.cli build --source {work / 'source.glb'} --plan {work / 'plan.json'} --output {work / 'output'}
{sys.executable} -m scene_factory.cli verify --plan {work / 'plan.json'} --output {work / 'output'}
After EACH build, Read output/simple/start.png, reveal.png, detail.png and
answer_view.png, and likewise for medium and hard. Reconsider each declared_answer from those images
and visually audit shortcuts, uniqueness and naturalness. Use source previews,
inventory and nearest-anchor geometry to plan placement, not to substitute for
visual proof of the answer. Update plan.json
if an answer or question wording changes, then run verify. If the model cannot
see the detail well enough, change the plan/placement and rebuild; do not guess.
Iterate candidates/probe/plan/build/verify inside this same session until
verify succeeds or you honestly reject the scene.
{session_note} Finally write {work / 'agent_notes.md'}
with a brief per-tier audit: why the start cannot answer, which revealed view
shows the answer, whether a duplicate or wording shortcut remains, and any
uncertainty. Name the minimum causal exploration steps: for simple, the
camera change that reveals the hidden feature; for medium, the nearby blocker
and the view that clears it; for hard, how to resolve the unique first-hop
landmark and then clear its blocker. Do not add objects or require extra
camera moves solely to reach an arbitrary action count. Do not assert an
untested 30-step claim.
Also write {work / 'visual_quality.json'} after visual review in this form:
{{"status":"accepted","tiers":{{"simple":{{"status":"pass","reason":"..."}},
"medium":{{"status":"pass","reason":"..."}},
"hard":{{"status":"pass","reason":"..."}}}}}}.
Use pass only when the image evidence makes the landmark, target and answer
clear. Use review for a specific unresolved visual ambiguity that a human could
settle from the saved views, such as a small but recognizable prop or uncertain
material identity; name that uncertainty in reason. Use fail for a proven
answer shortcut, an unrecognizable start landmark, missing answer evidence,
unnatural placement or an unanswerable question. If any tier fails, overall
status is rejected; otherwise if any tier needs review, overall status is
needs_review; only three passes may be accepted. Review is not a pass and must
not enter the accepted question bank. This is your own visual judgment, not
automatic answer checking.
Structural validation may still say candidate; never label a visually rejected
scene as ready for the question bank.
"""


def observed_evidence_reads(stream_path: Path, work: Path) -> list[str]:
    """List answer evidence images explicitly opened by the Claude agent."""
    observed = set()
    if not stream_path.exists():
        return []
    for line in stream_path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use" or block.get("name") != "Read":
                continue
            raw = (block.get("input") or {}).get("file_path")
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = work / path
            for level in ("simple", "medium", "hard"):
                for kind in ("start", "reveal", "detail", "answer_view"):
                    expected = work / "output" / level / f"{kind}.png"
                    if path.resolve() == expected.resolve():
                        observed.add(f"{level}/{kind}.png")
    return sorted(observed)


def observed_probe_reads(stream_path: Path, work: Path, candidate_ids: set[str]) -> list[str]:
    """Candidate previews must be seen before one proposal per tier is selected."""
    observed = set()
    if not stream_path.exists():
        return []
    for line in stream_path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use" or block.get("name") != "Read":
                continue
            raw = (block.get("input") or {}).get("file_path")
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = work / path
            for cid in candidate_ids:
                if path.resolve() == (work / "probe" / cid / "start_1.png").resolve():
                    observed.add(cid)
    return sorted(observed)


def visual_quality_status(path: Path) -> tuple[str, dict | None]:
    """Record the agent's visual self-review separately from structural validation."""
    if not path.exists():
        return "needs_review", None
    quality = json.loads(path.read_text())
    if quality.get("status") not in ("accepted", "needs_review", "rejected"):
        raise ValueError("visual_quality.json status must be accepted, needs_review or rejected")
    tiers = quality.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != {"simple", "medium", "hard"}:
        raise ValueError("visual_quality.json must review all three tiers")
    if any(not isinstance(tiers[level], dict) or
           tiers[level].get("status") not in ("pass", "review", "fail") or
           not isinstance(tiers[level].get("reason"), str) or
           not tiers[level]["reason"].strip()
           for level in tiers):
        raise ValueError("Each visual-quality tier needs a pass/review/fail status and reason")
    tier_statuses = {tiers[level]["status"] for level in tiers}
    if quality["status"] == "accepted" and tier_statuses != {"pass"}:
        raise ValueError("visual_quality.json cannot accept a non-passing tier")
    if quality["status"] == "needs_review" and "fail" in tier_statuses:
        raise ValueError("visual_quality.json cannot mark a failed tier needs_review")
    return {"accepted": "accepted", "needs_review": "visual_needs_review",
            "rejected": "visual_rejected"}[quality["status"]], quality


def finalize_attempt(work: Path, stream_path: Path, scene_id: str, state: dict) -> tuple[dict, str, dict | None]:
    """Independently rebuild and validate the artifacts left by an agent phase."""
    plan_path = work / "plan.json"
    plan = read_plan(plan_path)
    pool = read_candidate_pool(work / "candidates.json")
    probe_report = json.loads((work / "probe/probe_report.json").read_text())
    probe_reads = observed_probe_reads(stream_path, work,
                                       {c["candidate_id"] for c in pool["candidates"]})
    state["candidate_preview_reads"] = probe_reads
    if missing := {c["candidate_id"] for c in pool["candidates"]} - set(probe_reads):
        raise ValueError(f"Agent did not inspect candidate previews: {sorted(missing)}")
    validate_selection(plan, pool, probe_report)
    if plan["schema_version"] == "1.3":
        reads = observed_evidence_reads(stream_path, work)
        state["visual_evidence_reads"] = reads
        required = {f"{level}/{kind}.png" for level in ("simple", "medium", "hard")
                    for kind in ("start", "reveal", "detail", "answer_view")}
        if missing := required - set(reads):
            raise ValueError(f"Agent did not inspect required answer views: {sorted(missing)}")
    plan["scene_id"] = scene_id
    plan["source"] = state["source"]
    if plan["schema_version"] in ("1.2", "1.3"):
        seed = int(os.environ.get("SCENEQA_QUESTION_SEED", "0"))
        plan["type_selection"] = {"seed": seed,
                                  "ordered_candidates": type_order(scene_id, seed),
                                  "selected": [v["question"]["question_type"] for v in plan["variants"]]}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    output = work / "output"
    blender("build", work / "source.glb", output, plan_path)
    report = validate(plan_path, output)
    quality_status, quality = visual_quality_status(work / "visual_quality.json")
    state["visual_quality"] = quality
    state["status"] = (quality_status if report["status"] == "candidate" and
                       quality_status != "accepted" else report["status"])
    return report, quality_status, quality


BACKUP_ITEMS = ("source.glb", "task.txt", "inspect", "plan.json", "candidates.json",
                "probe", "output", "visual_quality.json", "agent_notes.md")


def snapshot_attempt(work: Path, backup: Path) -> None:
    for name in BACKUP_ITEMS:
        src = work / name
        if not src.exists():
            continue
        dst = backup / name
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def restore_attempt(work: Path, backup: Path) -> None:
    for name in BACKUP_ITEMS:
        dst = work / name
        if dst.is_symlink():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink(missing_ok=True)
        src = backup / name
        if src.is_dir():
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)


def supervisor_verdict_changes(before: dict, after: dict) -> dict[str, list[str]]:
    """Compare each question separately so one improvement cannot hide a regression."""
    rank = {"revise": 0, "review": 1, "pass": 2}
    changes = {"improved_levels": [], "regressed_levels": [], "unchanged_levels": []}
    for level in ("simple", "medium", "hard"):
        old = rank[before["tiers"][level]["verdict"]]
        new = rank[after["tiers"][level]["verdict"]]
        key = "improved_levels" if new > old else "regressed_levels" if new < old else "unchanged_levels"
        changes[key].append(level)
    return changes


def repair_change_summary(before: dict, after: dict) -> dict[str, list[str]]:
    """Separate fixing a scene from replacing its question or only its answer."""
    question_changes = []
    answer_changes = []
    for position, level in enumerate(("simple", "medium", "hard")):
        old = before["variants"][position].get("question", {})
        new = after["variants"][position].get("question", {})
        if {k: v for k, v in old.items() if k != "declared_answer"} != {
                k: v for k, v in new.items() if k != "declared_answer"}:
            question_changes.append(level)
        if old.get("declared_answer") != new.get("declared_answer"):
            answer_changes.append(level)
    return {"changed_question_levels": question_changes,
            "changed_answer_levels": answer_changes}


def clear_phase_artifacts(work: Path) -> None:
    for pattern in ("supervisor_round_*", "repair_round_*"):
        for path in work.glob(pattern):
            if path.is_file():
                path.unlink()


def combined_usage(state: dict) -> dict:
    phases = [state.get("usage")]
    for row in (state.get("supervisor") or {}).get("rounds", []):
        phases.extend((row.get("usage"), row.get("repair_usage")))
    phases = [row for row in phases if isinstance(row, dict)]
    fields = ("api_attempts", "completed_api_calls", "retryable_errors",
              "input_tokens", "cached_tokens", "output_tokens", "usage_missing_calls")
    total = {field: sum(row.get(field) or 0 for row in phases) for field in fields}
    total["cache_hit_rate"] = (total["cached_tokens"] / total["input_tokens"]
                               if total["input_tokens"] and not total["usage_missing_calls"]
                               else None)
    return total


def supervise_and_repair(work: Path, scene_id: str, state: dict, report: dict, *,
                         provider: str, key: str, model: str, upstream: str,
                         timeout: int, max_repairs: int,
                         supervisor_model: str | None = None) -> None:
    """Challenge images in a separate context, then repair only concrete failures."""
    rounds = []
    state["supervisor"] = {"status": "running", "rounds": rounds,
                           "max_repairs": max_repairs,
                           "model": supervisor_model or model}
    backup = None
    preserve_backup = False
    previous_review = None
    previous_state = None
    try:
        for index in range(max_repairs + 1):
            stream = work / f"supervisor_round_{index}.jsonl"
            try:
                usage = run_model_phase(
                    work, prompt=review_prompt(work, report), system=REVIEW_SYSTEM, tools="Read",
                    stream_path=stream, stderr_path=work / f"supervisor_round_{index}.stderr.log",
                    usage_path=work / f"supervisor_round_{index}.usage.jsonl",
                    provider=provider, key=key, model=supervisor_model or model,
                    upstream=upstream, timeout=timeout)
                reads = checked_image_reads(stream, work)
                review = reconcile_independent_answers(
                    validate_review(extract_result(stream)), report)
            except Exception as exc:
                if backup is not None:
                    try:
                        restore_attempt(work, backup)
                        state.update(previous_state)
                    except Exception as restore_exc:
                        preserve_backup = True
                        state["status"] = "failed"
                        state["supervisor"] = {"status": "rollback_failed", "rounds": rounds,
                                               "backup": str(backup),
                                               "error": f"{type(restore_exc).__name__}: {restore_exc}"}
                        return
                state["status"] = "supervisor_needs_review"
                state["supervisor"] = {"status": "invalid_review", "rounds": rounds,
                                       "error": f"{type(exc).__name__}: {exc}"}
                return
            review["artifact_sha256"] = {r["level"]: r.get("sha256")
                                         for r in report["results"]}
            review["evidence_sha256"] = evidence_hashes(work)
            (work / f"supervisor_round_{index}.json").write_text(
                json.dumps(review, ensure_ascii=False, indent=2))
            rounds.append({"round": index, "outcome": review_outcome(review),
                           "report": str(work / f"supervisor_round_{index}.json"),
                           "image_reads": reads, "usage": usage})
            if backup is not None:
                verdict_changes = supervisor_verdict_changes(previous_review, review)
                rounds[-1]["verdict_changes"] = verdict_changes
                if verdict_changes["regressed_levels"] or not verdict_changes["improved_levels"]:
                    try:
                        restore_attempt(work, backup)
                        state.update(previous_state)
                    except Exception as exc:
                        preserve_backup = True
                        state["status"] = "failed"
                        state["supervisor"] = {"status": "rollback_failed", "rounds": rounds,
                                               "backup": str(backup),
                                               "error": f"{type(exc).__name__}: {exc}"}
                        return
                    state["status"] = "supervisor_rejected"
                    state["supervisor"] = {
                        "status": ("repair_regressed" if verdict_changes["regressed_levels"]
                                   else "repair_did_not_improve"),
                        "rounds": rounds,
                        "regressed_levels": verdict_changes["regressed_levels"],
                    }
                    return
                shutil.rmtree(backup)
                backup = None
            outcome = review_outcome(review)
            if outcome == "pass":
                state["supervisor"]["status"] = "passed"
                if state["status"] != "candidate":
                    state["status"] = "supervisor_needs_review"
                return
            if outcome == "review":
                state["status"] = "supervisor_needs_review"
                state["supervisor"]["status"] = "needs_review"
                return
            if index == max_repairs:
                state["status"] = "supervisor_rejected"
                state["supervisor"]["status"] = "repair_limit_reached"
                return
            previous_review = review
            previous_state = deepcopy(state)
            source_digest = hashlib.sha256((work / "source.glb").read_bytes()).hexdigest()
            prior_plan = read_plan(work / "plan.json")
            protected = {level: prior_plan["variants"][index]
                         for index, level in enumerate(("simple", "medium", "hard"))
                         if review["tiers"][level]["verdict"] == "pass"}
            if any((work / name).is_symlink() for name in BACKUP_ITEMS):
                state["status"] = "supervisor_needs_review"
                state["supervisor"] = {"status": "repair_requires_regular_files", "rounds": rounds}
                return
            backup = Path(tempfile.mkdtemp(prefix=".supervisor_backup_", dir=work))
            try:
                snapshot_attempt(work, backup)
            except Exception as exc:
                state["status"] = "supervisor_needs_review"
                state["supervisor"] = {"status": "backup_failed", "rounds": rounds,
                                       "error": f"{type(exc).__name__}: {exc}"}
                return
            try:
                repair_stream = work / f"repair_round_{index + 1}.jsonl"
                repair_usage = run_model_phase(
                    work, prompt=repair_prompt(work, review, index + 1),
                    system=REPAIR_SYSTEM, tools="Bash,Read,Write,Edit,Glob,Grep",
                    stream_path=repair_stream,
                    stderr_path=work / f"repair_round_{index + 1}.stderr.log",
                    usage_path=work / f"repair_round_{index + 1}.usage.jsonl",
                    provider=provider, key=key, model=model, upstream=upstream, timeout=timeout)
                rounds[-1]["repair_usage"] = repair_usage
                report, _, _ = finalize_attempt(work, repair_stream, scene_id, state)
                if hashlib.sha256((work / "source.glb").read_bytes()).hexdigest() != source_digest:
                    raise ValueError("Repair changed the original source GLB")
                revised_plan = read_plan(work / "plan.json")
                rounds[-1]["repair_changes"] = repair_change_summary(prior_plan, revised_plan)
                for position, level in enumerate(("simple", "medium", "hard")):
                    if level in protected and revised_plan["variants"][position] != protected[level]:
                        raise ValueError(f"Repair changed supervisor-passing tier: {level}")
            except Exception as exc:
                try:
                    restore_attempt(work, backup)
                    state.update(previous_state)
                except Exception as restore_exc:
                    preserve_backup = True
                    state["status"] = "failed"
                    state["supervisor"] = {"status": "rollback_failed", "rounds": rounds,
                                           "backup": str(backup),
                                           "error": f"{type(restore_exc).__name__}: {restore_exc}"}
                    return
                state["status"] = "supervisor_rejected"
                state["supervisor"] = {"status": "repair_invalid", "rounds": rounds,
                                       "error": f"{type(exc).__name__}: {exc}"}
                return
    finally:
        if backup is not None and not preserve_backup:
            shutil.rmtree(backup, ignore_errors=True)


def run_one(row: dict, output_root: Path, *, source_path: Path | None = None,
            upstream: str | None = None, model: str | None = None, provider: str = "tokenhub",
            key_file: Path | None = None, preflight: bool = True,
            max_bytes=100_000_000, timeout=7200, force=False, dry_run=False,
            supervisor_rounds=0, supervisor_model: str | None = None) -> dict:
    if not 0 <= supervisor_rounds <= 2:
        raise ValueError("supervisor_rounds must be 0..2")
    scene_id = row.get("uid") or "adhoc"
    work = (output_root / scene_id).resolve()
    output = work / "output"
    validation_path = output / "validation.json"
    status_file = work / "status.json"
    if status_file.exists() and not force:
        old_status = json.loads(status_file.read_text())
        if old_status.get("status") in ("unsuitable", "visual_rejected", "visual_needs_review",
                                        "supervisor_rejected", "supervisor_needs_review",
                                        "candidate", "needs_review"):
            return {"scene_id": scene_id, "status": old_status["status"], "skipped": True,
                    "work": str(work), "visual_quality": old_status.get("visual_quality")}
    if validation_path.exists() and not force:
        old = json.loads(validation_path.read_text())
        if old.get("status") in ("candidate", "needs_review"):
            return {"scene_id": scene_id, "status": old["status"], "skipped": True,
                    "work": str(work)}
    work.mkdir(parents=True, exist_ok=True)
    clear_phase_artifacts(work)
    (work / "unsuitable.json").unlink(missing_ok=True)
    (work / "visual_quality.json").unlink(missing_ok=True)
    state = {"scene_id": scene_id, "status": "running", "started": time.time(),
             "source": {k: row.get(k) for k in ("uid", "name", "tier", "category", "author", "license", "source_url", "download_url")}}
    status_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    api_started = False
    try:
        source = work / "source.glb"
        if source_path:
            asset = copy_local(source_path, source, max_bytes)
        else:
            asset = download(row["download_url"], source, max_bytes=max_bytes,
                             expected_sha256=row.get("sha256"))
        state["asset"] = asset
        inspect = work / "inspect"
        if not (inspect / "inventory.json").exists():
            blender("inspect", source, inspect)
        prompt = prompt_for(scene_id, work, supervisor_rounds)
        (work / "task.txt").write_text(prompt)
        if dry_run:
            state["status"] = "prepared"
            return state
        if preflight:
            preflight_report = screen(json.loads((inspect / "inventory.json").read_text()))
            state["preflight"] = preflight_report
            (work / "preflight.json").write_text(json.dumps(preflight_report, ensure_ascii=False, indent=2))
            # Names alone cannot prove that no usable prop exists. Only the
            # geometric absence of two individually copyable meshes is a gate.
            if preflight_report["status"] == "likely_unsuitable" and preflight_report["definitive"]:
                unsuitable = {"status": "unsuitable", "source": "deterministic_name_screen",
                              "reasons": [preflight_report["rule"]],
                              "available_native_categories": preflight_report["named_prop_categories"]}
                (work / "unsuitable.json").write_text(json.dumps(unsuitable, ensure_ascii=False, indent=2))
                state["status"] = "unsuitable"
                state["unsuitable"] = unsuitable
                state["completed"] = time.time()
                return state
        key = provider_key(provider, key_file)
        if not key:
            raise RuntimeError(f"No {provider} key: set the matching environment variable or --key-file")
        model = model or os.environ.get("SCENEQA_MODEL") or (
            "deepseek/deepseek-flash" if provider == "tokenhub" else "deepseek-flash")
        upstream = upstream or os.environ.get("SCENEQA_UPSTREAM") or (
            "https://tokenhub.tencentmaas.com/v1/chat/completions" if provider == "tokenhub"
            else "https://api.deepseek.com/anthropic")
        usage_log = work / "api_usage.jsonl"
        api_started = True
        state["usage"] = run_model_phase(
            work, prompt=prompt, system=AGENT_SYSTEM, tools="Bash,Read,Write,Edit,Glob,Grep",
            stream_path=work / "claude_stream.jsonl", stderr_path=work / "claude_stderr.log",
            usage_path=usage_log, provider=provider, key=key, model=model,
            upstream=upstream, timeout=timeout)
        state["claude_exit_code"] = 0
        unsuitable_path = work / "unsuitable.json"
        if unsuitable_path.exists():
            if (work / "visual_quality.json").exists():
                raise ValueError("unsuitable.json is only for pre-plan source screening; use visual_quality.json for built scenes")
            unsuitable = json.loads(unsuitable_path.read_text())
            if unsuitable.get("status") != "unsuitable" or not unsuitable.get("reasons"):
                raise ValueError("unsuitable.json must contain status and reasons")
            state["status"] = "unsuitable"
            state["unsuitable"] = unsuitable
            state["completed"] = time.time()
            return state
        report, quality_status, quality = finalize_attempt(
            work, work / "claude_stream.jsonl", scene_id, state)
        if supervisor_rounds and report["status"] == "candidate":
            supervise_and_repair(work, scene_id, state, report, provider=provider,
                                 key=key, model=model, upstream=upstream,
                                 timeout=timeout, max_repairs=supervisor_rounds,
                                 supervisor_model=supervisor_model)
        state["total_usage"] = combined_usage(state)
        state["completed"] = time.time()
    except subprocess.TimeoutExpired:
        state["status"] = "failed"
        state["error"] = f"Claude session exceeded {timeout} seconds"
        state["completed"] = time.time()
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["completed"] = time.time()
    finally:
        usage_log = work / "api_usage.jsonl"
        if api_started and usage_log.exists() and "usage" not in state:
            state["usage"] = summarize_usage(usage_log)
        status_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return state


def run_batch(rows: list[dict], output_root: Path, *, workers=1, **kwargs) -> list[dict]:
    if workers < 1 or workers > 4:
        raise ValueError("workers must be 1..4")
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, row, output_root, **kwargs) for row in rows]
        for future in as_completed(futures):
            results.append(future.result())
    return results


def supervise_existing(work: Path, *, provider="tokenhub", key_file: Path | None = None,
                       model: str | None = None, supervisor_model: str | None = None,
                       upstream: str | None = None, max_repairs=0, timeout=7200) -> dict:
    """Review a built run without paying to regenerate it first."""
    if not 0 <= max_repairs <= 2:
        raise ValueError("max_repairs must be 0..2")
    work = work.resolve()
    plan = read_plan(work / "plan.json")
    report = validate(work / "plan.json", work / "output")
    if report["status"] != "candidate":
        raise ValueError("Only structurally valid candidate scenes can be supervised")
    missing = [f"{level}/{view}.png" for level in ("simple", "medium", "hard")
               for view in ("start", "reveal", "detail", "answer_view")
               if not (work / "output" / level / f"{view}.png").is_file()]
    if missing:
        raise ValueError(f"Supervisor needs all twelve rendered images: {missing}")
    if max_repairs:
        if any((work / name).is_symlink() for name in BACKUP_ITEMS):
            raise ValueError("Automatic repair requires regular work files, not symlinks")
        pool = read_candidate_pool(work / "candidates.json")
        probe_report = json.loads((work / "probe/probe_report.json").read_text())
        validate_selection(plan, pool, probe_report)
    key = provider_key(provider, key_file)
    if not key:
        raise RuntimeError(f"No {provider} key: set the matching environment variable or --key-file")
    model = model or os.environ.get("SCENEQA_MODEL") or (
        "deepseek/deepseek-flash" if provider == "tokenhub" else "deepseek-flash")
    upstream = upstream or os.environ.get("SCENEQA_UPSTREAM") or (
        "https://tokenhub.tencentmaas.com/v1/chat/completions" if provider == "tokenhub"
        else "https://api.deepseek.com/anthropic")
    status_path = work / "status.json"
    state = json.loads(status_path.read_text()) if status_path.exists() else {
        "scene_id": plan.get("scene_id"), "source": plan.get("source", {})}
    state.setdefault("source", plan.get("source", {}))
    quality_status, quality = visual_quality_status(work / "visual_quality.json")
    state["visual_quality"] = quality
    state["status"] = quality_status if quality_status != "accepted" else "candidate"
    try:
        clear_phase_artifacts(work)
        supervise_and_repair(work, state.get("scene_id") or plan.get("scene_id") or work.name,
                             state, report, provider=provider, key=key, model=model,
                             supervisor_model=supervisor_model, upstream=upstream,
                             timeout=timeout, max_repairs=max_repairs)
        state["total_usage"] = combined_usage(state)
        state["completed"] = time.time()
    finally:
        status_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return state
