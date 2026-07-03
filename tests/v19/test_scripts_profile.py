import pytest
pytestmark = pytest.mark.transport

from pathlib import Path


def test_v19_scripts_and_profiles_exist():
    for path in [
        'scripts/INSTALL_MLOMEGA_V19_WINDOWS.ps1','scripts/setup_profile.ps1','scripts/RUN_MLOMEGA_V19.ps1',
        'scripts/DOCTOR_MLOMEGA_V19.ps1','scripts/BENCH_V19.ps1','configs/MODEL_MANIFEST.yaml','configs/profiles/rtx3070.yaml'
    ]:
        assert Path(path).exists(), path


def test_ports_use_v19_87xx_prefix_not_legacy_8766():
    text = Path('configs/profiles/rtx3070.yaml').read_text() + Path('scripts/RUN_MLOMEGA_V19.ps1').read_text()
    assert '8766' not in text
    assert '8704' in text and '8706' in text
