"""Run inside Blender: inventory, native-object duplication and visibility evidence.

Invocation: blender -b -t 2 --python blender_worker.py -- MODE SOURCE OUT [PLAN]
"""
import json
import hashlib
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

# Blender executes this file directly; expose sibling policy module without
# relying on the shell's current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from occlusion_policy import local_occlusion_passes


def args():
    return sys.argv[sys.argv.index("--") + 1:]


def reset_import(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(path))
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        raise ValueError("Source GLB contains no meshes")
    return meshes


def world_corners(obj):
    return [obj.matrix_world @ Vector(c) for c in obj.bound_box]


def bounds(objects):
    p = [v for obj in objects for v in world_corners(obj)]
    return [min(v[i] for v in p) for i in range(3)], [max(v[i] for v in p) for i in range(3)]


def overlap_ratio(a, b):
    al, ah = a
    bl, bh = b
    inter = math.prod(max(0, min(ah[i], bh[i]) - max(al[i], bl[i])) for i in range(3))
    va = math.prod(max(ah[i] - al[i], 1e-8) for i in range(3))
    vb = math.prod(max(bh[i] - bl[i], 1e-8) for i in range(3))
    return inter / min(va, vb)


def camera_for(objects, azimuth, elevation):
    lo, hi = bounds(objects)
    center = Vector([(a + b) / 2 for a, b in zip(lo, hi)])
    diagonal = (Vector(hi) - Vector(lo)).length
    radius = max(diagonal * 1.5, 2)
    a, e = math.radians(azimuth), math.radians(elevation)
    position = center + Vector((radius * math.cos(e) * math.cos(a),
                                radius * math.cos(e) * math.sin(a), radius * math.sin(e)))
    camera = bpy.data.objects.get("SceneQA_Camera")
    if camera is None:
        camera_data = bpy.data.cameras.new("SceneQA_Camera")
        camera = bpy.data.objects.new("SceneQA_Camera", camera_data)
        bpy.context.scene.collection.objects.link(camera)
    camera.location = position
    camera.rotation_euler = (center - position).to_track_quat("-Z", "Y").to_euler()
    camera.data.type = "ORTHO"
    camera.data.ortho_scale = max(diagonal * 1.3, 2)
    # The default far plane is 1000 Blender units. Large imported scenes can
    # put this camera beyond it, leaving every preview blank despite valid mesh.
    camera.data.clip_end = max(1000, radius * 4)
    bpy.context.scene.camera = camera
    return camera


def visible_from(camera, obj):
    from bpy_extras.object_utils import world_to_camera_view
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = [sum(world_corners(obj), Vector()) / 8] + world_corners(obj)
    for target in points:
        ndc = world_to_camera_view(scene, camera, target)
        if not (0 <= ndc.x <= 1 and 0 <= ndc.y <= 1 and ndc.z > 0):
            continue
        direction = target - camera.location
        hit, _, _, _, hit_obj, _ = scene.ray_cast(depsgraph, camera.location, direction.normalized(), distance=direction.length + 0.001)
        if hit and hit_obj == obj:
            return True
    return False


def landmark_snapshot(camera, objects, resolution=640):
    """Cheap camera-space landmark evidence, never an answer judgment."""
    from bpy_extras.object_utils import world_to_camera_view
    scene = bpy.context.scene
    coords = [world_to_camera_view(scene, camera, p)
              for obj in objects for p in world_corners(obj)]
    xs = [p.x for p in coords if p.z > 0]
    ys = [p.y for p in coords if p.z > 0]
    if not xs or not ys:
        return {"visible": False, "extent_px": 0, "bbox_px": [0, 0, 0, 0]}
    x0, x1 = max(0, min(xs)), min(1, max(xs))
    y0, y1 = max(0, min(ys)), min(1, max(ys))
    extent = max(0, x1 - x0, y1 - y0) * resolution
    return {"visible": any(visible_from(camera, obj) for obj in objects),
            "extent_px": round(extent, 1),
            "bbox_px": [round(v * resolution, 1) for v in (x0, y0, x1, y1)]}


def visible_sample_fraction(camera, objects):
    """Prefer evidence views where each object exposes more than one tiny edge."""
    from bpy_extras.object_utils import world_to_camera_view
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    samples = hits = 0
    for obj in objects:
        verts = obj.data.vertices
        if not verts:
            continue
        step = max(1, len(verts) // 24)
        for index in list(range(0, len(verts), step))[:24]:
            vertex = verts[index]
            target = obj.matrix_world @ vertex.co
            ndc = world_to_camera_view(scene, camera, target)
            if not (0 <= ndc.x <= 1 and 0 <= ndc.y <= 1 and ndc.z > 0):
                continue
            direction = target - camera.location
            samples += 1
            hit, _, _, _, hit_obj, _ = scene.ray_cast(
                depsgraph, camera.location, direction.normalized(),
                distance=direction.length + 0.001)
            hits += bool(hit and hit_obj == obj)
    return hits / samples if samples else 0


def candidate_camera_options(variant, augmented, spawned, original_by_name, anchor_evidence=None):
    landmarks = variant.get("start_landmarks") or []
    if not landmarks:
        raise ValueError("Candidate must declare start_landmarks with original mesh objects")
    landmark_groups = []
    for landmark in landmarks:
        names = landmark.get("objects") or []
        if not landmark.get("label") or not names or any(n not in original_by_name for n in names):
            raise ValueError(f"Invalid start landmark: {landmark}")
        landmark_groups.append((landmark["label"], [original_by_name[n] for n in names]))
    angles = [(az, el) for el in (15, 25, 35, 45, 60) for az in range(0, 360, 30)]
    guess = variant.get("start_camera") or {}
    if "azimuth_deg" in guess and "elevation_deg" in guess:
        pair = (guess["azimuth_deg"], guess["elevation_deg"])
        if pair not in angles:
            angles.insert(0, pair)
    options = []
    for az, el in angles:
        camera = camera_for(augmented, az, el)
        landmark_evidence = {}
        for label, objects in landmark_groups:
            # A ray hitting a single corner can make an unreadable landmark pass.
            snapshot = landmark_snapshot(camera, objects, 512)
            snapshot["visible_fraction"] = (round(visible_sample_fraction(camera, objects), 3)
                                            if snapshot["visible"] and snapshot["extent_px"] >= 35
                                            else 0.0)
            landmark_evidence[label] = snapshot
        visible_copies = [cid for cid, objects in spawned.items()
                          if any(visible_from(camera, obj) for obj in objects)]
        hidden = sorted(set(spawned) - set(visible_copies))
        landmark_ok = all(x["visible"] and x["extent_px"] >= 35 and
                          x["visible_fraction"] >= 0.25
                          for x in landmark_evidence.values())
        occlusion_checked = bool(variant.get("occlusion_targets")) and landmark_ok and bool(hidden)
        occlusion = {}
        for item in variant.get("occlusion_targets", []):
            cid = item["target_copy_id"]
            names = item["blocker_objects"]
            if cid not in spawned or any(n not in original_by_name for n in names):
                raise ValueError(f"Invalid occlusion target in candidate: {item}")
            if occlusion_checked:
                occlusion[cid] = occlusion_evidence(camera, spawned[cid], names,
                                                    [original_by_name[n] for n in names], augmented)
        passing_blockers = {cid for cid, e in occlusion.items() if
                            cid in hidden and local_occlusion_passes(e, candidate_probe=True)}
        local_blocker_ok = (bool(passing_blockers) if variant["level"] == "medium" else
                            variant.get("anchor_query", {}).get("target_copy_id") in passing_blockers)
        anchor_ok = (variant["level"] != "hard" or
                     (anchor_evidence and
                      anchor_evidence["resolved_anchor_object"] ==
                      variant["anchor_query"]["answer_anchor_object"] and
                      anchor_evidence["margin_ratio"] >= 0.15))
        eligible = landmark_ok and bool(hidden) and (variant["level"] == "simple" or local_blocker_ok) and anchor_ok
        failure_reasons = []
        if not landmark_ok:
            failure_reasons.append("start_landmark_unreadable")
        if not hidden:
            failure_reasons.append("answer_target_visible_at_start")
        if variant["level"] != "simple" and not local_blocker_ok:
            failure_reasons.append("local_occlusion_missing" if occlusion_checked
                                   else "local_occlusion_not_checked")
        if not anchor_ok:
            failure_reasons.append("nearest_anchor_invalid")
        score = (1000 if eligible else 0) + (200 if landmark_ok else 0) + (100 if hidden else 0)
        score += min((x["extent_px"] for x in landmark_evidence.values()), default=0)
        score += 100 * max((e["blocked_fraction"] for e in occlusion.values()), default=0)
        options.append({"azimuth_deg": az, "elevation_deg": el,
                        "landmarks": landmark_evidence, "hidden_copy_ids": hidden,
                        "occlusion_evidence": occlusion,
                        "occlusion_checked": occlusion_checked,
                        "eligible": eligible,
                        "failure_reasons": failure_reasons,
                        "score": round(score, 2)})
    return sorted(options, key=lambda x: x["score"], reverse=True)


def answer_framing(variant, spawned, original_by_name):
    """Frame all declared visual evidence together where a reachable view exists."""
    entities = {e["id"]: e for e in variant["entities"]}
    groups = []
    for eid in variant["question"].get("evidence_ids", []):
        if eid in spawned:
            groups.append(spawned[eid])
        elif eid in entities:
            groups.append([original_by_name[n] for n in entities[eid]["objects"]])
    for name in variant["question"].get("evidence_context_objects", []):
        if name not in original_by_name:
            raise ValueError(f"Unknown evidence context mesh: {name}")
        groups.append([original_by_name[name]])
    if not groups:
        groups = [next(iter(spawned.values()))]
    framing = list({obj.name: obj for group in groups for obj in group}.values())
    best = None
    for el in (20, 35, 45, 60, 75):
        for az in range(0, 360, 30):
            camera = camera_for(framing, az, el)
            fractions = [visible_sample_fraction(camera, group) for group in groups]
            visible = [fraction > 0 or any(visible_from(camera, obj) for obj in group)
                       for fraction, group in zip(fractions, groups)]
            extent = landmark_snapshot(camera, framing, 768)["extent_px"]
            score = (sum(visible) * 10000 + min(fractions) * 3000 +
                     sum(fractions) * 300 + min(extent, 768))
            if best is None or score > best[0]:
                best = (score, az, el, visible, fractions)
    return framing, best[1], best[2], {"visible_groups": sum(best[3]),
                                      "required_groups": len(groups),
                                      "all_visible": all(best[3]),
                                      "visible_fractions": [round(v, 3) for v in best[4]]}


def occlusion_evidence(camera, objects, blocker_names, blockers, scene_objects):
    """Measure whether native scene geometry hides target surface samples at start."""
    from bpy_extras.object_utils import world_to_camera_view
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    sampled = 0
    blocked = 0
    hit_names = {}
    gaps = []
    scene_lo, scene_hi = bounds(scene_objects)
    target_lo, target_hi = bounds(objects)
    scene_span = max(scene_hi[0] - scene_lo[0], scene_hi[1] - scene_lo[1], 0.001)
    scene_diagonal = (Vector(scene_hi) - Vector(scene_lo)).length
    target_span = max(target_hi[0] - target_lo[0], target_hi[1] - target_lo[1])
    blocker_bounds = [bounds([obj]) for obj in blockers]
    blocker_width_ratio = max(max(hi[i] - lo[i] for i in (0, 1))
                              for lo, hi in blocker_bounds) / scene_span
    gap_limit = max(scene_diagonal * 0.15, target_span * 1.5)
    for obj in objects:
        vertices = obj.data.vertices
        step = max(1, len(vertices) // 48)
        for index in range(0, len(vertices), step):
            if sampled >= 48:
                break
            vertex = vertices[index]
            target = obj.matrix_world @ vertex.co
            ndc = world_to_camera_view(scene, camera, target)
            if not (0 <= ndc.x <= 1 and 0 <= ndc.y <= 1 and ndc.z > 0):
                continue
            direction = target - camera.location
            distance = direction.length
            if distance <= 0.001:
                continue
            sampled += 1
            hit, hit_location, _, _, hit_obj, _ = scene.ray_cast(
                depsgraph, camera.location, direction.normalized(), distance=distance - 0.001)
            if hit and hit_obj not in objects:
                hit_names[hit_obj.name] = hit_names.get(hit_obj.name, 0) + 1
                if hit_obj.name in blocker_names:
                    blocked += 1
                    gaps.append((target - hit_location).length)
    return {"samples": sampled, "blocked_samples": blocked,
            "blocked_fraction": round(blocked / sampled, 4) if sampled else 0,
            "first_hit_objects": hit_names,
            "mean_blocker_gap": round(sum(gaps) / len(gaps), 4) if gaps else None,
            "max_allowed_gap": round(gap_limit, 4),
            "blocker_width_ratio": round(blocker_width_ratio, 4),
            "expected_blocker_objects": blocker_names}


def render(objects, out, azimuth, elevation, resolution=640):
    camera_for(objects, azimuth, elevation)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = resolution
    scene.render.resolution_y = resolution
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.render.filepath = str(out)
    # Ambient viewport-like render with material colors and textures.
    world = bpy.data.worlds.new("SceneQA World")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    bg.inputs[0].default_value = (0.75, 0.75, 0.75, 1)
    bg.inputs[1].default_value = 1.0
    scene.world = world
    light_data = bpy.data.lights.new("SceneQA Light", "AREA")
    light = bpy.data.objects.new("SceneQA Light", light_data)
    scene.collection.objects.link(light)
    light.location = scene.camera.location
    light_data.energy = 1000
    light_data.shape = "DISK"
    light_data.size = 10
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(light, do_unlink=True)


def inspect(source, out):
    meshes = reset_import(source)
    lo, hi = bounds(meshes)
    inventory = {"source": str(source), "bounds": {"min": lo, "max": hi},
                 "meshes": [], "materials": sorted({m.name for o in meshes for m in o.data.materials if m})}
    for o in meshes:
        a, b = bounds([o])
        inventory["meshes"].append({"name": o.name, "bounds": [a, b],
                                    "vertices": len(o.data.vertices),
                                    "materials": [m.name for m in o.data.materials if m]})
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2))
    for name, az, el in [("front", 45, 30), ("back", 225, 30), ("top", 45, 65), ("side", 135, 25)]:
        render(meshes, Path(out) / f"source_{name}.png", az, el)


def support_z(obj, x, y, top):
    verts = [obj.matrix_world @ v.co for v in obj.data.vertices]
    polygons = [tuple(p.vertices) for p in obj.data.polygons]
    bvh = BVHTree.FromPolygons(verts, polygons, all_triangles=False)
    origin = Vector((x, y, top + 1000))
    # Imported scenes may be taller than 2000 units. Cast past this support
    # mesh's lowest point so floors remain reachable even below a tall roof.
    cast_distance = max(2000, origin.z - min(v.z for v in verts) + 1)
    hit, _, _, _ = bvh.ray_cast(origin, Vector((0, 0, -1)), cast_distance)
    if hit is None:
        raise ValueError(f"No support surface under ({x}, {y}) on {obj.name}")
    return hit.z


def build(source, out, plan_path, *, preview_only=False):
    plan = json.loads(Path(plan_path).read_text())
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    report = []
    for variant in plan["candidates" if preview_only else "variants"]:
        meshes = reset_import(source)
        original_names = {o.name for o in meshes}
        original_by_name = {o.name: o for o in meshes}
        anchor_evidence = None
        if variant.get("anchor_query"):
            query = variant["anchor_query"]
            names = [query["reference_object"], *query["candidate_objects"]]
            if any(name not in original_by_name for name in names):
                raise ValueError(f"Unknown anchor-query mesh in {names}")
            def xy_center(name):
                lo, hi = bounds([original_by_name[name]])
                return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2)
            rx, ry = xy_center(query["reference_object"])
            distances = {name: math.dist((rx, ry), xy_center(name))
                         for name in query["candidate_objects"]}
            ranked = sorted(distances, key=distances.get)
            anchor_evidence = {"relation": "nearest", "distances_xy": distances,
                               "resolved_anchor_object": ranked[0],
                               "margin_ratio": (distances[ranked[1]] - distances[ranked[0]]) /
                                               max(distances[ranked[1]], 1e-8),
                               "target_copy_id": query["target_copy_id"]}
        originals = {e["id"]: e for e in variant["entities"]}
        spawned = {}
        for entity in originals.values():
            for name in entity["objects"]:
                if name not in original_by_name:
                    raise ValueError(f"Original mesh missing: {name}")
        auto_adjustments = []
        for copy in variant["copies"]:
            entity = originals[copy["from"]]
            sources = [original_by_name[name] for name in entity["objects"]]
            xy = copy["xy"]
            supp = original_by_name.get(copy["support_object"])
            if supp is None:
                raise ValueError(f"Support mesh missing: {copy['support_object']}")
            low, high = bounds(sources)
            center = Vector(((low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]))
            angle = math.radians(copy.get("yaw_deg", 0))
            span = max(high[0] - low[0], high[1] - low[1], 0.1)
            directions = [(0, 0)] + [(math.cos(a), math.sin(a)) for a in
                                   (0, math.pi / 4, math.pi / 2, 3 * math.pi / 4,
                                    math.pi, 5 * math.pi / 4, 3 * math.pi / 2, 7 * math.pi / 4)]
            candidates = [(xy[0], xy[1])]
            for radius in (0.75 * span, 1.5 * span):
                candidates += [(xy[0] + dx * radius, xy[1] + dy * radius) for dx, dy in directions[1:]]
            chosen = None
            previous_bounds = {cid: bounds(objects) for cid, objects in spawned.items()}
            for candidate_xy in candidates:
                try:
                    z = support_z(supp, candidate_xy[0], candidate_xy[1], max(high[2], bounds(meshes)[1][2]))
                except ValueError:
                    continue
                transform = (Matrix.Translation((candidate_xy[0], candidate_xy[1], z + 0.002)) @
                             Matrix.Rotation(angle, 4, "Z") @ Matrix.Translation(-center))
                points = [transform @ v for src in sources for v in world_corners(src)]
                candidate_bounds = ([min(p[i] for p in points) for i in range(3)],
                                    [max(p[i] for p in points) for i in range(3)])
                if all(overlap_ratio(candidate_bounds, b) <= 0.3 for b in previous_bounds.values()):
                    chosen = (candidate_xy, transform)
                    break
            if chosen is None:
                raise ValueError(f"No collision-free nearby placement for {copy['id']}")
            actual_xy, transform = chosen
            if actual_xy != tuple(xy) and actual_xy != xy:
                auto_adjustments.append({"copy_id": copy["id"], "requested_xy": xy,
                                         "actual_xy": list(actual_xy), "reason": "copy overlap > 0.3"})
            made = []
            for src in sources:
                obj = src.copy()
                obj.data = src.data.copy()
                obj.parent = None
                bpy.context.scene.collection.objects.link(obj)
                obj.matrix_world = transform @ src.matrix_world
                obj.name = f"sceneqa__{variant['level']}__{copy['id']}__{src.name}"[:62]
                made.append(obj)
            spawned[copy["id"]] = made
        copy_bounds = {cid: bounds(objects) for cid, objects in spawned.items()}
        collisions = []
        ids = list(copy_bounds)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ratio = overlap_ratio(copy_bounds[a], copy_bounds[b])
                if ratio > 0.3:
                    collisions.append({"a": a, "b": b, "overlap_ratio": ratio})
        augmented = [o for o in bpy.context.scene.objects if o.type == "MESH"]
        if preview_only:
            candidate_id = variant["candidate_id"]
            candidate_dir = out / candidate_id
            candidate_dir.mkdir(exist_ok=True)
            options = candidate_camera_options(variant, augmented, spawned, original_by_name, anchor_evidence)
            selected = options[:2]
            if not selected[0]["eligible"]:
                # Show both a readable landmark and the best hidden-target view
                # when no camera passes. Otherwise the two previews can both
                # leak the answer and hide why the candidate failed.
                hidden_option = next((o for o in options if o["hidden_copy_ids"]), None)
                if hidden_option is not None and hidden_option not in selected:
                    selected[1] = hidden_option
            for index, option in enumerate(selected, 1):
                render(augmented, candidate_dir / f"start_{index}.png",
                       option["azimuth_deg"], option["elevation_deg"], 512)
            report.append({"candidate_id": candidate_id, "level": variant["level"],
                           "probe_protocol_version": "landmark_surface_v3",
                           "preview_resolution_px": 512,
                           "candidate_sha256": hashlib.sha256(json.dumps(
                               variant, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode()).hexdigest(),
                           "question": variant["question"]["text"],
                           "eligible_cameras": sum(o["eligible"] for o in options),
                           "anchor_query_evidence": anchor_evidence,
                           "recommended": selected,
                           "preview_images": [str(candidate_dir / f"start_{i}.png")
                                              for i in range(1, len(selected) + 1)],
                           "auto_adjustments": auto_adjustments})
            continue
        cam = variant["start_camera"]
        level_dir = out / variant["level"]
        level_dir.mkdir(exist_ok=True)
        start_az, start_el = cam["azimuth_deg"], cam["elevation_deg"]
        render(augmented, level_dir / "start.png", start_az, start_el)
        start_cam = camera_for(augmented, start_az, start_el)
        start_vis = {cid: any(visible_from(start_cam, o) for o in objects) for cid, objects in spawned.items()}
        occlusion = {}
        for requirement in variant.get("occlusion_targets", []):
            cid = requirement["target_copy_id"]
            blockers = requirement["blocker_objects"]
            if any(name not in original_by_name for name in blockers):
                raise ValueError(f"Unknown native occluder in {blockers}")
            occlusion[cid] = occlusion_evidence(start_cam, spawned[cid], blockers,
                                               [original_by_name[name] for name in blockers], augmented)
        probes = []
        for el in (20, 45, 70):
            for az in range(0, 360, 45):
                probe_cam = camera_for(augmented, az, el)
                visible = [cid for cid, objects in spawned.items() if any(visible_from(probe_cam, o) for o in objects)]
                probes.append({"azimuth_deg": az, "elevation_deg": el, "visible_copy_ids": visible})
        globally_visible = {cid for probe in probes for cid in probe["visible_copy_ids"]}
        local_probes = []
        for cid, objects in spawned.items():
            if cid in globally_visible:
                continue
            for el in (20, 45, 70):
                for az in range(0, 360, 45):
                    # Focus and zoom are available to the eventual QA agent.
                    # Probe whether a close object-centred view can reveal it.
                    probe_cam = camera_for(objects, az, el)
                    if any(visible_from(probe_cam, o) for o in objects):
                        local_probes.append({"copy_id": cid, "azimuth_deg": az,
                                             "elevation_deg": el})
                        break
                else:
                    continue
                break
        evidence_views = {}
        if plan.get("schema_version") == "1.3":
            # Give the answering model both a broad revealed view and a focused
            # target view. These are evidence, never automatic answer checks.
            best = max(probes, key=lambda p: len(p["visible_copy_ids"]))
            reveal_path = level_dir / "reveal.png"
            render(augmented, reveal_path, best["azimuth_deg"], best["elevation_deg"], 512)
            evidence_views["reveal"] = str(reveal_path)
            target_id = next((eid for eid in variant["question"].get("evidence_ids", [])
                              if eid in spawned), next(iter(spawned)))
            target_objects = spawned[target_id]
            detail_angle = None
            for el in (20, 45, 70):
                for az in range(0, 360, 45):
                    local_cam = camera_for(target_objects, az, el)
                    if any(visible_from(local_cam, obj) for obj in target_objects):
                        detail_angle = (az, el)
                        break
                if detail_angle:
                    break
            if detail_angle:
                detail_path = level_dir / "detail.png"
                render(target_objects, detail_path, *detail_angle, resolution=512)
                evidence_views["detail"] = str(detail_path)
            framing, az, el, coverage = answer_framing(variant, spawned, original_by_name)
            answer_path = level_dir / "answer_view.png"
            render(framing, answer_path, az, el, resolution=768)
            evidence_views["answer_view"] = str(answer_path)
            evidence_views["answer_view_camera"] = {"azimuth_deg": az, "elevation_deg": el}
            evidence_views["answer_view_coverage"] = coverage
            evidence_views["target_copy_id"] = target_id
        # Export only imported source meshes and their native-geometry copies.
        bpy.ops.object.select_all(action="DESELECT")
        for obj in augmented:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = augmented[0]
        glb = level_dir / "scene.glb"
        bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True)
        report.append({"level": variant["level"], "glb": str(glb),
                       "original_mesh_count": len(original_names), "export_mesh_count": len(augmented),
                       "copy_mesh_names": {k: [o.name for o in v] for k, v in spawned.items()},
                       "copy_bounds": copy_bounds, "copy_collisions": collisions,
                       "auto_adjustments": auto_adjustments,
                       "start_visible_copies": start_vis, "view_probes": probes,
                       "occlusion_evidence": occlusion,
                       "anchor_query_evidence": anchor_evidence,
                       "evidence_views": evidence_views,
                       "local_view_probes": local_probes,
                       "start_camera": cam})
    (out / ("probe_report.json" if preview_only else "build_report.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        mode, source, out, *rest = args()
        if mode == "inspect":
            inspect(source, out)
        elif mode == "build":
            build(source, out, rest[0])
        elif mode == "probe":
            build(source, out, rest[0], preview_only=True)
        else:
            raise ValueError(f"Unknown worker mode: {mode}")
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
