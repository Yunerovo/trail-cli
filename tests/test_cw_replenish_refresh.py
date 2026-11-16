from pathlib import Path

import pytest
from PIL import Image

from trail.core.errors import TrailError
from trail.daemon.models import RequestCancelled
from trail.scenes.cw import replenish_refresh as refresh

SHOT = Path(__file__).resolve().parents[1] / '.trail/shots/e0333e71b0fa4f9ba9d3d78afd549e70-aabd56513b.jpg'


def entry(text, bounds):
    return {'text': text, 'bounds': bounds}


def reading(count='1', title='补给阶段'):
    return {'entries': [entry(title, (900, 140, 1020, 175)),
                        entry('返回备战界面', (1730, 50, 1870, 80)),
                        entry('剩余次数：' + count, (1360, 978, 1490, 1010))]}


class Runtime:
    def __init__(self):
        self.clicks = []
    def click_point(self, *point):
        self.clicks.append(point)


def test_real_icon_and_positioned_ocr():
    if not SHOT.exists():
        pytest.skip('local screenshot unavailable')
    assert refresh._icon(Image.open(SHOT))
    assert refresh._title(reading()['entries'])
    assert refresh._count(reading()['entries']) == 1
    assert refresh._count([entry('剩余次数：1', (20, 20, 120, 50))]) is None


def test_success_uses_final_frame(monkeypatch):
    runtime = Runtime()
    image = Image.new('RGB', (1920, 1080))
    observations = iter([(reading('1'), image), (reading('0'), image)])
    monkeypatch.setattr(refresh, '_observe', lambda _: next(observations))
    monkeypatch.setattr(refresh, '_icon', lambda _: True)
    monkeypatch.setattr(refresh, 'sleep', lambda _: None)
    result, final = refresh.refresh_cw_replenish(runtime)
    assert runtime.clicks == [(1331, 995)]
    assert result['refresh_remaining'] == 0 and final is image


@pytest.mark.parametrize('initial,icon', [(reading('0'), True), (reading('?'), True), (reading('1', '备战阶段'), True), (reading(), False)])
def test_refuses_unverified_precondition(monkeypatch, initial, icon):
    runtime = Runtime()
    monkeypatch.setattr(refresh, '_observe', lambda _: (initial, None))
    monkeypatch.setattr(refresh, '_icon', lambda _: icon)
    with pytest.raises(TrailError):
        refresh.refresh_cw_replenish(runtime)
    assert not runtime.clicks


def test_unconfirmed_and_cancellation(monkeypatch):
    runtime = Runtime()
    monkeypatch.setattr(refresh, '_observe', lambda _: (reading(), None))
    monkeypatch.setattr(refresh, '_icon', lambda _: True)
    monkeypatch.setattr(refresh, 'sleep', lambda _: None)
    with pytest.raises(TrailError) as caught:
        refresh.refresh_cw_replenish(runtime)
    assert caught.value.code == 'CW_REPLENISH_REFRESH_UNCONFIRMED'
    assert caught.value.known_failure_after_save and len(runtime.clicks) == 1
    runtime = Runtime()
    def cancel():
        raise RuntimeError('cancel')
    with pytest.raises(RuntimeError):
        refresh.refresh_cw_replenish(runtime, check_cancelled=cancel)
    assert not runtime.clicks


def test_cancel_after_click_propagates(monkeypatch):
    runtime = Runtime()
    monkeypatch.setattr(refresh, '_observe', lambda _: (reading(), None))
    monkeypatch.setattr(refresh, '_icon', lambda _: True)
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if runtime.clicks:
            raise RequestCancelled()
    with pytest.raises(RequestCancelled):
        refresh.refresh_cw_replenish(runtime, check_cancelled=cancel)
    assert len(runtime.clicks) == 1


def test_icon_rejects_dim_overlay():
    from PIL import ImageEnhance
    image = Image.new('RGB', (1920, 1080))
    with Image.open(refresh._ICON_PATH) as icon:
        image.paste(icon, (1307, 970))
    assert refresh._icon(image)
    assert not refresh._icon(ImageEnhance.Brightness(image).enhance(0.5))


def test_missing_return_is_rejected():
    assert not refresh._page([entry('补给阶段', (900, 140, 1020, 175))])
