import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location('sessionhub', Path('services/live-pc/sessionhub.py'))
sessionhub = importlib.util.module_from_spec(spec); sys.modules['sessionhub'] = sessionhub; spec.loader.exec_module(sessionhub)

def test_two_clients_get_unique_sessions_and_ephemeral_tokens():
    hub = sessionhub.SessionHub()
    a = hub.create_session('s25-a'); b = hub.create_session('s25-b')
    assert a.session_id != b.session_id
    assert a.token != b.token
    assert hub.authenticate(a.token).device_id == 's25-a'

def test_clock_offsets_are_coherent_for_simulated_clients():
    hub = sessionhub.SessionHub(); a = hub.create_session('a'); b = hub.create_session('b')
    # client clocks are respectively 5ms ahead and 8ms behind server; symmetric 1ms network legs.
    sa = hub.complete_clock_sync(a.session_id, client_send_ns=6_000_000, server_recv_ns=1_000_000, server_send_ns=1_100_000, client_recv_ns=6_100_000)
    sb = hub.complete_clock_sync(b.session_id, client_send_ns=-7_000_000, server_recv_ns=1_000_000, server_send_ns=1_100_000, client_recv_ns=-6_900_000)
    assert abs(sa.offset_ns + 5_000_000) < 100_000
    assert abs(sb.offset_ns - 8_000_000) < 100_000
    assert hub.current_offset_ns(a.session_id) == sa.offset_ns
