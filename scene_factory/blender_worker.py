"""Run inside Blender: inventory, native-object duplication and visibility evidence.

Invocation: blender -b -t 2 --python blender_worker.py -- MODE SOURCE OUT [PLAN]
"""
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree


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


def render(objects, out, azimuth, elevation):
    camera_for(objects, azimuth, elevation)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 640
    scene.render.resolution_y = 640
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
    hit, _, _, _ = bvh.ray_cast(origin, Vector((0, 0, -1)), 2000)
    if hit is None:
        raise ValueError(f"No support surface under ({x}, {y}) on {obj.name}")
    return hit.z


def build(source, out, plan_path):
    plan = json.loads(Path(plan_path).read_text())
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    report = []
    for variant in plan["variants"]:
        meshes = reset_import(source)
        original_names = {o.name for o in meshes}
        original_by_name = {o.name: o for o in meshes}
        originals = {e["id"]: e for e in variant["entities"]}
        spawned = {}
        for entity in originals.values():
            for name in entity["objects"]:
                if name not in original_by_name:
                    raise ValueError(f"Original mesh missing: {name}")
        for copy in variant["copies"]:
            entity = originals[copy["from"]]
            sources = [original_by_name[name] for name in entity["objects"]]
            xy = copy["xy"]
            supp = original_by_name.get(copy["support_object"])
            if supp is None:
                raise ValueError(f"Support mesh missing: {copy['support_object']}")
            low, high = bounds(sources)
            center = Vector(((low[0] + high[0]) / 2, (low[1] + high[1]) / 2, low[2]))
            z = support_z(supp, xy[0], xy[1], max(high[2], bounds(meshes)[1][2]))
            angle = math.radians(copy.get("yaw_deg", 0))
            transform = Matrix.Translation((xy[0], xy[1], z + 0.002)) @ Matrix.Rotation(angle, 4, "Z") @ Matrix.Translation(-center)
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
        cam = variant["start_camera"]
        level_dir = out / variant["level"]
        level_dir.mkdir(exist_ok=True)
        start_az, start_el = cam["azimuth_deg"], cam["elevation_deg"]
        render(augmented, level_dir / "start.png", start_az, start_el)
        start_cam = camera_for(augmented, start_az, start_el)
        start_vis = {cid: any(visible_from(start_cam, o) for o in objects) for cid, objects in spawned.items()}
        probes = []
        for el in (20, 45, 70):
            for az in range(0, 360, 45):
                probe_cam = camera_for(augmented, az, el)
                visible = [cid for cid, objects in spawned.items() if any(visible_from(probe_cam, o) for o in objects)]
                probes.append({"azimuth_deg": az, "elevation_deg": el, "visible_copy_ids": visible})
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
                       "start_visible_copies": start_vis, "view_probes": probes,
                       "start_camera": cam})
    (out / "build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        mode, source, out, *rest = args()
        if mode == "inspect":
            inspect(source, out)
        elif mode == "build":
            build(source, out, rest[0])
        else:
            raise ValueError(f"Unknown worker mode: {mode}")
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
