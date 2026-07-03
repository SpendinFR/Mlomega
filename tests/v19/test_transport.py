import pytest
pytestmark = pytest.mark.transport

import asyncio, importlib.util, sys
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path)); mod = importlib.util.module_from_spec(spec); sys.modules[name]=mod; spec.loader.exec_module(mod); return mod

gateway = load('gateway', 'services/live-pc/gateway.py')
fake = load('fake_xr_device', 'simulators/fake_xr_device.py')


def test_webrtc_frame_queue_bounded_drops_old_frames():
    async def run():
        q = gateway.LatestFrameQueue()
        dev = fake.FakeXrDevice(frames=3, fps=0)
        await gateway.pump_latest(gateway.IterableIngress(dev.stream()), q, limit=3)
        assert q.stats()['queue_size'] == 1
        assert q.stats()['dropped_frames'] == 2
        _frame, envelope = await q.get_latest()
        assert envelope.frame_id.endswith('000002')
    asyncio.run(run())


def test_fake_xr_device_frame_ids_monotonic_and_rotation():
    async def run():
        dev = fake.FakeXrDevice(frames=4, fps=0, rotation=90)
        frames = [env async for _frame, env in dev.stream()]
        assert [f.frame_id for f in frames] == sorted(f.frame_id for f in frames)
        assert all(f.rotation == 90 for f in frames)
    asyncio.run(run())
