"""Appended to the frozen base worker; executed only on the K8s renderer."""


def _screen_draw(stage, renderer, matrix, yfov):
    cam = stage.add(pyrender.PerspectiveCamera(yfov=yfov), pose=matrix)
    light = stage.add(pyrender.DirectionalLight(color=np.ones(3), intensity=3), pose=matrix)
    try:
        return renderer.render(stage)
    finally:
        stage.remove_node(cam)
        stage.remove_node(light)


def _screen_focus(depth, matrix, yfov, box):
    height, width = depth.shape
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) * .5 * width, (y1 + y2) * .5 * height
    left, top = max(0, int(x1 * width)), max(0, int(y1 * height))
    right, bottom = min(width, max(left + 1, int(x2 * width))), min(height, max(top + 1, int(y2 * height)))
    ys, xs = np.nonzero(depth[top:bottom, left:right] > 0)
    if not len(xs):
        raise ValueError('No visible 3D surface inside requested bbox')
    xs, ys = xs + left, ys + top
    nearest = np.argmin((xs + .5 - cx) ** 2 + (ys + .5 - cy) ** 2)
    x, y = int(xs[nearest]), int(ys[nearest])
    distance = float(depth[y, x])
    focal = height / (2 * np.tan(yfov / 2))
    point = np.array([((x + .5) - width / 2) * distance / focal,
                      -((y + .5) - height / 2) * distance / focal, -distance, 1.])
    return (matrix @ point)[:3], [x / width, y / height]


def screen_view_main():
    source, out, plan_path = map(Path, sys.argv[1:4])
    out.mkdir(parents=True, exist_ok=True)
    plan = json.loads(plan_path.read_text())
    if plan.get('mode') != 'screen_inspection':
        raise ValueError('Only screen_inspection requests are supported')
    scene = trimesh.load(source, force='scene')
    for geom in scene.geometry.values():
        material = getattr(getattr(geom, 'visual', None), 'material', None)
        if material is not None:
            for attr in ('baseColorTexture', 'metallicRoughnessTexture', 'normalTexture', 'occlusionTexture', 'emissiveTexture', 'image'):
                image = getattr(material, attr, None)
                if getattr(image, 'mode', None) in ('LA', '1'):
                    setattr(material, attr, image.convert('RGBA'))
    bounds = np.asarray(scene.bounds)
    center, extent = bounds.mean(0), float(np.max(bounds[1] - bounds[0]))
    if not np.isfinite(bounds).all() or extent <= 0:
        raise ValueError('Invalid scene bounds')
    stage = pyrender.Scene.from_trimesh_scene(renderable(scene), ambient_light=[.9] * 3, bg_color=[245, 245, 245, 255])
    renderer = pyrender.OffscreenRenderer(768, 768)
    records, depths = [], {}
    try:
        for request in plan['requests']:
            name = request['label']
            camera = request.get('reference_camera')
            if camera is None:
                label = request['reference_view']
                az = int(label.split('_')[1])
                zoom = label.split('_')[2]
                matrix = pose(center, extent, az, distance=1.7 if zoom == 'overview' else 1.15)
                yfov = float(np.pi / 4)
            else:
                matrix, yfov = np.asarray(camera['matrix'], dtype=float), float(camera['yfov'])
            key = json.dumps([matrix.tolist(), yfov])
            if key not in depths:
                _, depths[key] = _screen_draw(stage, renderer, matrix, yfov)
            focus, pixel = _screen_focus(depths[key], matrix, yfov, request['bbox'])
            direction = matrix[:3, 3] - focus
            radius = float(np.linalg.norm(direction))
            if not np.isfinite(radius) or radius <= extent * 1e-9:
                raise ValueError('Focus surface is too close to the camera')
            azimuth = float(np.rad2deg(np.arctan2(direction[0], direction[2]))) + request['yaw']
            elevation = float(np.rad2deg(np.arcsin(np.clip(direction[1] / radius, -1, 1)))) + request['pitch']
            elevation = float(np.clip(elevation, -85, 85))
            radius = float(np.clip(radius / request['zoom'], extent * .015, extent * 5))
            matrix = pose(focus, radius, azimuth, el=elevation, distance=1.)
            rgb, _ = _screen_draw(stage, renderer, matrix, yfov)
            Image.fromarray(rgb).save(out / (name + '.png'))
            records.append({'label': name, 'image': name + '.png', 'focus_pixel': pixel,
                            'camera': {'matrix': matrix.tolist(), 'position': matrix[:3, 3].tolist(),
                                       'target': focus.tolist(), 'yfov': yfov}, 'request': request})
    finally:
        renderer.delete()
    (out / 'views.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(json.dumps({'mode': 'screen_inspection', 'views': len(records)}))


if __name__ == '__main__':
    screen_view_main()
