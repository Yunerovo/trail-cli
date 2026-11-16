from pathlib import Path

import pytest
from PIL import Image

from trail.core.errors import TrailError
from trail.scenes.cw import aura_drag as module

SHOTS = Path(__file__).resolve().parents[1] / ".trail" / "shots"

def frame(prefix):
    files = list(SHOTS.glob(prefix + "*.jpg"))
    if not files:
        pytest.skip("local recorded screenshots unavailable")
    return Image.open(files[0]).convert("RGB")

def reading(*extra):
    return {"value": "plot_armor", "entries": [{"text": "选择一个角色获得主角光环", "bounds": (800, 60, 1100, 90)}, {"text": "返回备战界面", "bounds": (1700, 40, 1850, 80)}] + [{"text": t} for t in extra]}

class Runtime:
    def __init__(self, before, after):
        self.frames = iter([before, after])
        self.drags = []
    def capture_image(self, normalize=True):
        return next(self.frames)
    def drag_to(self, *args):
        self.drags.append(args)

def test_recorded_drag(monkeypatch):
    before, after = frame("2a4fb3"), frame("441e2893")
    runtime = Runtime(before, after)
    monkeypatch.setattr(module, "read_cw_stage_text", lambda *a, **k: reading())
    monkeypatch.setattr(module, "sleep", lambda _: None)
    result, image = module.drag_cw_aura_role(runtime, source="front:0")
    assert result["verified"] and result["source"] == "front:1" and image.size == after.size
    assert runtime.drags == [(804, 622, 957, 451)]

def test_overlay_and_empty_never_drag(monkeypatch):
    before = frame("2a4fb3")
    runtime = Runtime(before, before)
    monkeypatch.setattr(module, "read_cw_stage_text", lambda *a, **k: reading("是否重新选择主角和光环"))
    with pytest.raises(TrailError):
        module.drag_cw_aura_role(runtime, source="front:0")
    assert not runtime.drags
    assert not module._occupied(before, (1208, 764))

def test_bad_source_rejected():
    with pytest.raises(TrailError):
        module.drag_cw_aura_role(None, source="front:4")
    with pytest.raises(TrailError):
        module.drag_cw_aura_role(None, source=None)


def test_post_drag_failure_and_cancellation(monkeypatch):
    from trail.daemon.models import RequestCancelled
    before = frame("2a4fb3")
    monkeypatch.setattr(module, "sleep", lambda _: None)
    calls = 0
    def read(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return reading()
        raise ValueError("OCR failed")
    monkeypatch.setattr(module, "read_cw_stage_text", read)
    runtime = Runtime(before, before)
    with pytest.raises(TrailError) as exc:
        module.drag_cw_aura_role(runtime, source="front:0")
    assert exc.value.code == "CW_AURA_DRAG_UNCONFIRMED"
    assert exc.value.known_failure_after_save and len(runtime.drags) == 1

    runtime = Runtime(before, before)
    monkeypatch.setattr(module, "read_cw_stage_text", lambda *a, **k: reading())
    def cancel():
        if runtime.drags:
            raise RequestCancelled()
    with pytest.raises(RequestCancelled):
        module.drag_cw_aura_role(runtime, source="front:0", check_cancelled=cancel)
    assert len(runtime.drags) == 1


def test_empty_source_never_drags(monkeypatch):
    before = frame("2a4fb3")
    runtime = Runtime(before, before)
    monkeypatch.setattr(module, "read_cw_stage_text", lambda *a, **k: reading())
    with pytest.raises(TrailError) as exc:
        module.drag_cw_aura_role(runtime, source="back:5")
    assert exc.value.code == "CW_AURA_SOURCE_UNCONFIRMED" and not runtime.drags


def test_synthetic_duplicate_portrait_does_not_block(monkeypatch):
    import numpy as np
    rng = np.random.default_rng(42)
    canvas = np.full((1080, 1920, 3), 80, dtype=np.uint8)
    portrait = rng.integers(20, 230, (48, 48, 3), dtype=np.uint8)
    for x in (804, 909):
        canvas[598:646, x-24:x+24] = portrait
        canvas[578:605, x+23:x+45] = 0
        canvas[580:587, x+27:x+34] = 255
    before = Image.fromarray(canvas.copy())
    canvas[598:646, 780:828] = (canvas[598:646, 780:828].astype(float) * .6).astype(np.uint8)
    canvas[427:475, 933:981] = portrait
    after = Image.fromarray(canvas)
    runtime = Runtime(before, after)
    monkeypatch.setattr(module, "read_cw_stage_text", lambda *a, **k: reading())
    monkeypatch.setattr(module, "sleep", lambda _: None)
    result, _ = module.drag_cw_aura_role(runtime, source="front:0")
    assert result["verified"] and runtime.drags == [(804, 622, 957, 451)]


def test_synthetic_empty_slot_never_drags(monkeypatch):
    blank = Image.new("RGB", (1920, 1080), (80, 80, 80))
    runtime = Runtime(blank, blank)
    monkeypatch.setattr(module, "read_cw_stage_text", lambda *a, **k: reading())
    with pytest.raises(TrailError) as exc:
        module.drag_cw_aura_role(runtime, source="front:0")
    assert exc.value.code == "CW_AURA_SOURCE_UNCONFIRMED" and not runtime.drags
