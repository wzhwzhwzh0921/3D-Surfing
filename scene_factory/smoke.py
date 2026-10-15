"""Render a bundled tiny fixture without any model calls or online assets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

from .rendering import create_render_worker, render_settings
from .screening.inspection import InspectionRenderer
from .screening.policy import VIEWS

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    fixture = ROOT / 'examples/fixtures/hidden.glb'
    if not fixture.is_file():
        parser.error('Use a complete source checkout with pip install -e .; bundled fixture is missing')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.json').unlink(missing_ok=True)
    source = out / 'source.glb'
    shutil.copyfile(fixture, source)
    source_sha = sha(source)
    plan = out / 'native-plan.json'
    plan.write_text(json.dumps({'items': [{'level': 'medium', 'copies': []}]}))
    worker = create_render_worker('sceneqa-smoke-' + hashlib.sha256(str(out).encode()).hexdigest()[:10],
                                  (ROOT / 'experiments/sol_augment/worker.py').read_bytes())
    inspection = InspectionRenderer(out, pod=worker.name)
    began = time.time()
    try:
        worker.create()
        worker.run(source, out / 'standard', screen_only=True)
        worker.run(source, out / 'native', plan)
        views = inspection.render(source, out / 'inspection', [{
            'label': 'focus_rotate_zoom', 'reference_view': VIEWS[0],
            'bbox': [0., 0., 1., 1.], 'yaw': 37., 'pitch': -8., 'zoom': 1.3,
            'purpose': 'Verify surface focus, rotation and zoom with the bundled fixture.'}])
        native = out / 'native/medium'
        if sha(native / 'scene.glb') != source_sha:
            raise ValueError('Native-scene output changed the input asset')
        from PIL import Image
        images = [out / 'standard' / (v + '.png') for v in VIEWS]
        images += [native / (v + '.png') for v in VIEWS]
        images += [Path(v['path']) for v in views]
        for image in images:
            with Image.open(image) as picture:
                if picture.size != (768, 768):
                    raise ValueError('Unexpected render size: ' + str(image))
                picture.verify()
        if len(json.loads((out / 'standard/cameras.json').read_text())) != len(VIEWS):
            raise ValueError('Incomplete camera evidence')
        result = {'status': 'passed', 'render': render_settings(), 'standard_views': len(VIEWS),
                  'native_views': len(VIEWS), 'inspection_views': len(views),
                  'source_sha256': source_sha, 'native_bytes_preserved': True,
                  'model_calls': 0, 'question_quality_evaluated': False,
                  'elapsed_seconds': round(time.time() - began, 2)}
        (out / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        inspection.close()
        worker.close()


if __name__ == '__main__':
    raise SystemExit(main())
