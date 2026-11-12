"""Freeze a small, diverse development cohort without viewing solve outcomes."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path


def select_cases(cases: list[dict], models: int, presets: set[str]) -> list[dict]:
    if models < 1 or not presets or not presets <= {f'q{i:02d}' for i in range(10)}:
        raise ValueError('Choose a positive source count and valid export presets')
    grouped = defaultdict(list)
    for case in cases:
        if case['split'] == 'development':
            grouped[case['source_model_id']].append(case)
    strata = defaultdict(list)
    for identifier, rows in grouped.items():
        if {c['preset'] for c in rows} != {f'q{i:02d}' for i in range(10)}:
            raise ValueError('A source must have all ten fixtures before selection')
        truth = rows[0]['ground_truth']
        kinds = tuple(sorted(truth['brep_face_type_counts']))
        strata[(truth['solid_count'], kinds)].append(identifier)
    # In each topology/surface stratum prefer cheaper fixtures for the inner
    # loop. Larger sources remain in the full corpus, not silently excluded.
    cost = {key: sum(c['triangles'] for c in rows if c['preset'] in presets)
            for key, rows in grouped.items()}
    queues = [sorted(ids, key=lambda key: (cost[key], key))
              for _, ids in sorted(strata.items())]
    selected = []
    while queues and len(selected) < models:
        for queue in queues:
            if queue and len(selected) < models:
                selected.append(queue.pop(0))
        queues = [queue for queue in queues if queue]
    return [case for key in selected for case in grouped[key] if case['preset'] in presets]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--models', type=int, default=6)
    parser.add_argument('--preset', action='append', default=[])
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Preserve the frozen cohort; choose a new output path')
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    presets = set(args.preset or ['q00', 'q05', 'q09'])
    cases = select_cases(manifest['cases'], args.models, presets)
    if len(cases) != args.models * len(presets):
        raise ValueError('Not enough complete development sources yet')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        'selection': 'Development only; surface/solid strata, then minimum triangle cost',
        'parent_manifest_sha256': hashlib.sha256(raw).hexdigest(),
        'source_model_count': args.models, 'case_count': len(cases),
        'complete_full_corpus': False, 'presets': sorted(presets), 'cases': cases,
    }, indent=2) + '\n')
    print(f'Frozen {args.models} development sources / {len(cases)} cases')


if __name__ == '__main__':
    main()
