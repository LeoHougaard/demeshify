import hashlib
import io

import pytest

from tools import acquire_kicad_step as acquisition


def asset(data):
    return {'name': 'model.step', 'size': len(data),
            'sha': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest(),
            'download_url': f'https://raw.githubusercontent.com/{acquisition.REPOSITORY}/'
                            f'{acquisition.REVISION}/models/model.step'}


def test_verified_download_resumes_without_redownloading(tmp_path, monkeypatch):
    data = b'ISO-10303-21;\nprotocol-test'
    monkeypatch.setattr(acquisition.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(data))
    path, digest = acquisition.fetch_asset(asset(data), tmp_path)
    assert path.read_bytes() == data and digest == hashlib.sha256(data).hexdigest()

    def offline(*args, **kwargs):
        raise AssertionError('Cached asset must not be downloaded again')

    monkeypatch.setattr(acquisition.urllib.request, 'urlopen', offline)
    assert acquisition.fetch_asset(asset(data), tmp_path) == (path, digest)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='bytes preserved'):
        acquisition.fetch_asset(asset(data), tmp_path)
    assert path.read_bytes() == b'changed'


def test_bad_download_is_not_promoted_and_partial_evidence_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(acquisition.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(b'bad'))
    with pytest.raises(ValueError, match='bytes preserved'):
        acquisition.fetch_asset(asset(b'expected'), tmp_path)
    assert not (tmp_path / 'model.step').exists()
    assert [path.read_bytes() for path in tmp_path.glob('*.partial')] == [b'bad']


def test_component_variants_stay_in_one_family():
    assert acquisition.family_label('Inductor_THT', 'L_Axial_Horizontal_Fastron_MECC.step') == (
        acquisition.family_label('Inductor_THT', 'L_Axial_Vertical_Fastron_MISC.step'))
    assert acquisition.family_label('Button_Switch_THT', 'SW_CuK_JS202011AQN_Angled.step') == (
        acquisition.family_label('Button_Switch_THT', 'SW_CuK_JS202011CQN_Straight.step'))
