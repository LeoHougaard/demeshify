"""Acquire pinned, licensed public STEP assets; native fixture checks are separate."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import urllib.request
import uuid
from pathlib import Path

REVISION = 'b8b3cfdfad88ba66f21002b3de51dc6f7d55ba5a'
REPOSITORY = 'KiCad/kicad-packages3D'
CATEGORIES = (('Connector_BarrelJack.3dshapes', 2), ('Connector_USB.3dshapes', 2),
              ('Button_Switch_THT.3dshapes', 4), ('Relay_THT.3dshapes', 6),
              ('Inductor_THT.3dshapes', 6))


def api(path: str) -> dict | list:
    return json.loads(subprocess.check_output([
        'gh', 'api', f'repos/{REPOSITORY}/contents/{path}?ref={REVISION}',
    ]))


def fetch_asset(info: dict, directory: Path, local_name: str | None = None) -> tuple[Path, str]:
    name = local_name or info['name']
    if (Path(name).name != name or Path(name).is_reserved()
            or not 0 < info['size'] <= 8 * 1024**2):
        raise ValueError('Unexpected asset name or size')
    if not info['download_url'].startswith(
        f'https://raw.githubusercontent.com/{REPOSITORY}/{REVISION}/'
    ):
        raise ValueError('Asset URL is not pinned to the declared repository revision')
    path = directory / name
    temporary = None
    if path.exists():
        data = path.read_bytes()
    else:
        request = urllib.request.Request(info['download_url'], headers={
            'User-Agent': 'demeshify-research-corpus',
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(info['size'] + 1)
        temporary = directory / (name + '.' + uuid.uuid4().hex + '.partial')
        temporary.write_bytes(data)
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if len(data) != info['size'] or blob != info['sha']:
        raise ValueError('Asset differs from pinned Git blob; existing/partial bytes preserved')
    if temporary is not None:
        temporary.replace(path)
    return path, hashlib.sha256(data).hexdigest()


def family_label(directory: str, name: str) -> str:
    if directory.startswith('Connector_BarrelJack'):
        return 'KiCad:BarrelJack_CUI_PJ063'
    if directory.startswith('Connector_USB'):
        return 'KiCad:' + name.rsplit('_', 1)[0]
    if 'CuK_JS202011' in name:
        return 'KiCad:Switch_CuK_JS202011'
    if name.startswith('SW_DIP_'):
        return 'KiCad:Switch_DIP_SPST'
    if 'Schrack-RYII' in name:
        return 'KiCad:Relay_Schrack_RYII'
    if 'Finder_' in name:
        return 'KiCad:Relay_Finder'
    if name.startswith('Relay_'):
        return 'KiCad:' + name.rsplit('.', 1)[0]
    if 'Fastron_' in name:
        return 'KiCad:AxialInductor_Fastron'
    return 'KiCad:' + name.rsplit('.', 1)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--extra-category', nargs=3, action='append', default=[],
                        metavar=('DIRECTORY', 'COUNT', 'FAMILY_LABEL'),
                        help='Add replacement assets with one declared related-family group')
    args = parser.parse_args()
    raw = args.output / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    license_path, license_hash = fetch_asset(api('LICENSE.md'), args.output)
    categories = [(directory, count, None) for directory, count in CATEGORIES]
    for directory, count, family in args.extra_category:
        if int(count) < 1 or not family:
            parser.error('Replacement category requires a positive count and family label')
        categories.append((directory, int(count), family))
    catalog = {}
    aliases = []
    for directory, count, declared_family in categories:
        entries = sorted((row for row in api(directory)
                          if row['name'].lower().endswith(('.step', '.stp'))),
                         key=lambda row: row['name'])[:count]
        if len(entries) != count:
            raise ValueError('Pinned category has too few requested STEP assets')
        for info in entries:
            path, sha = fetch_asset(info, raw)
            if b'ISO-10303-21;' not in path.read_bytes()[:8192]:
                raise ValueError('Expected genuine STEP exchange content')
            family = declared_family or family_label(directory, info['name'])
            family_hash = hashlib.sha256(family.encode()).hexdigest()
            number = int(family_hash[:8], 16) % 10
            source = {'path': str(path.resolve()), 'asset': directory + '/' + info['name'],
                      'source_url': info['download_url'], 'source_commit': REVISION,
                      'git_blob_sha1': info['sha'], 'source_sha256': sha,
                      'source_license': 'CC-BY-SA-4.0 with KiCad library exception',
                      'license_sha256': license_hash, 'license_file': str(license_path.resolve()),
                      'family_label': family, 'geometry_family': family_hash,
                      'split': ('development' if number < 6 else 'validation' if number < 8
                                else 'holdout')}
            if sha in catalog:
                aliases.append(source)
            else:
                catalog[sha] = source
            print(f"Acquired and verified {directory}/{info['name']}", flush=True)
    index_path = args.output / 'source-index.json'
    report = {'revision': REVISION, 'complete': True, 'aliases': aliases,
              'selection': 'Fixed category quotas, alphabetical filenames, before solve outcomes; '
                           'related component families grouped conservatively',
              'sources': list(catalog.values())}
    if index_path.exists():
        previous_bytes = index_path.read_bytes()
        previous = json.loads(previous_bytes)
        current = {row['source_sha256']: row for row in report['sources']}
        if previous['revision'] != REVISION or any(
            current.get(row['source_sha256']) != row for row in previous['sources']
        ):
            raise ValueError('Existing source provenance/families changed; use a new output')
        if previous['sources'] == report['sources'] and previous.get('aliases', []) == aliases:
            print('Existing complete source index preserved byte for byte')
            return
        history = args.output / 'index-history'
        history.mkdir(exist_ok=True)
        previous_path = history / (hashlib.sha256(previous_bytes).hexdigest() + '.json')
        if previous_path.exists() and previous_path.read_bytes() != previous_bytes:
            raise ValueError('Existing source-index history changed')
        if not previous_path.exists():
            previous_path.write_bytes(previous_bytes)
    temporary = index_path.with_name('.source-index.' + uuid.uuid4().hex + '.partial')
    temporary.write_text(json.dumps(report, indent=2) + '\n')
    temporary.replace(index_path)
    (args.output / 'ATTRIBUTION.txt').write_text(
        'KiCad community, KiCad 3D component libraries.\n'
        'CC-BY-SA 4.0 with KiCad library exception; see LICENSE.md.\n'
        f'Pinned source: https://github.com/{REPOSITORY}/tree/{REVISION}\n'
        'Individual STEP headers remain intact. Geometry stays local.\n')
    print(f'{len(catalog)} pinned STEP assets acquired; native qualification pending')


if __name__ == '__main__':
    main()
