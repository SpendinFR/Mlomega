from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_phone_session_stop_binds_close_day_to_explicit_runtime_service_id_v18_8():
    source = (ROOT / 'MLOmega_Phone_Bridge_V18_8' / 'pc' / 'brainlive_phone_receiver.py').read_text(encoding='utf-8')
    assert 'def _active_service_for_explicit_stop()' in source
    assert '["brainlive-stop-service", "--service-run-id", service_run_id, "--close-day"]' in source
    assert 'explicit_service_identity_unavailable' in source
    assert 'request = run_mlomega_command(["brainlive-stop-service", "--close-day"])' not in source


def test_legacy_doctor_is_a_core_profile_compatibility_alias_v18_8():
    source = (ROOT / 'src' / 'mlomega_audio_elite' / 'cli.py').read_text(encoding='utf-8')
    marker = 'def cmd_doctor_elite(args) -> None:'
    block = source[source.index(marker):source.index('\ndef cmd_v13_autonomous', source.index(marker))]
    assert 'doctor-elite compatibility alias -> doctor-core-v18-8' in block
    assert 'core_doctor(check_services=True' in block
