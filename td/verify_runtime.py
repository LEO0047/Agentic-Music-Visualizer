"""Capture real TD errors and pixels; never certify a show from stub tests.

After building and letting the network run, execute in the TD Textport:
    import sys
    sys.path.insert(0, '/Users/leohuang/Repos/Agentic-Music-Visualizer/td')
    import verify_runtime
    verify_runtime.capture(op('/project1/amv'))

This does not change scene parameters, enable recording or rebuild the graph.
The report and screenshot are local, under artifacts/td-runtime/<timestamp>/.
"""
from datetime import datetime
import json
from pathlib import Path


def capture(network, directory=None):
    if network is None:
        raise RuntimeError('Build /project1/amv in TouchDesigner first.')
    directory = Path(directory) if directory else (
        Path(__file__).resolve().parents[1] / 'artifacts' / 'td-runtime'
        / datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    directory.mkdir(parents=True, exist_ok=True)
    report = {
        'captured_at': datetime.now().astimezone().isoformat(),
        'network': network.path,
        'status': 'CAPTURED_REQUIRES_REVIEW',
        'errors': network.errors(recurse=True),
        'script_errors': network.scriptErrors(recurse=True),
        'warnings': network.warnings(recurse=True),
        'features': {},
        'image': None,
        'fps': None,
        'audio_reactivity_verified': False,
        'visual_quality_verified': False,
    }
    features = network.op('features')
    if features is not None:
        report['features'] = {c.name: float(c[0]) for c in features.chans()
                              if len(c)}
    out = network.op('out')
    if out is None:
        report['status'] = 'MISSING_OUTPUT'
    else:
        try:
            out.cook(force=True)
            pixels = out.numpyArray(delayed=False)
            if pixels is None:
                raise RuntimeError('TOP returned no pixels')
            rgb = pixels[:, :, :3]
            image_path = directory / 'output.png'
            out.save(str(image_path), asynchronous=False)
            report['image'] = {
                'filename': 'output.png', 'exists': image_path.is_file(),
                'width': int(out.width), 'height': int(out.height),
                'rgb_min': float(rgb.min()), 'rgb_max': float(rgb.max()),
                'rgb_std': float(rgb.std()),
            }
            # Refresh after cooking: lazy shader/expression failures may only
            # appear when the output is actually requested.
            report['errors'] = network.errors(recurse=True)
            report['script_errors'] = network.scriptErrors(recurse=True)
        except Exception as exc:
            report['status'] = 'CAPTURE_FAILED'
            report['capture_error'] = str(exc)
    (directory / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('[amv] Runtime evidence:', directory)
    print('[amv] This snapshot does not verify FPS, audio sync or visual quality.')
    return report
