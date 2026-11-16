from __future__ import annotations

from pathlib import Path
import re
from time import sleep
from typing import Any

from PIL import Image, ImageStat

from trail.core.errors import TrailError
from trail.daemon.models import RequestCancelled
from trail.scenes.cw.stage import read_cw_stage_text

# Cropped from a canonical frame; contains only the refresh control.
_ICON_PATH = Path(__file__).parent / 'assets' / 'replenish_refresh.png'


def _inside(entry, box):
    bounds = entry.get('bounds')
    if bounds is None:
        return False
    x1, y1, x2, y2 = bounds
    l, t, r, b = box
    return l <= (x1 + x2) / 2 <= r and t <= (y1 + y2) / 2 <= b


def _count(entries):
    pieces = [e for e in entries if _inside(e, (1355, 960, 1540, 1030))]
    pieces.sort(key=lambda e: e['bounds'][0])
    if any(b['bounds'][0] - a['bounds'][2] > 24 or abs((a['bounds'][1] + a['bounds'][3]) - (b['bounds'][1] + b['bounds'][3])) > 24
           for a, b in zip(pieces, pieces[1:])):
        return None
    text = ''.join(re.sub(r'\s+', '', e['text']) for e in pieces)
    match = re.fullmatch(r'剩余次数[:：](\d+)', text)
    return int(match.group(1)) if match else None


def _title(entries):
    return any(re.sub(r'\s+', '', e['text']) == '补给阶段'
               and _inside(e, (820, 110, 1100, 200)) for e in entries)


def _page(entries):
    return _title(entries) and any(
        re.sub(r'\s+', '', e['text']) == '返回备战界面'
        and _inside(e, (1690, 30, 1919, 100)) for e in entries
    )


def _icon(image):
    if not isinstance(image, Image.Image) or image.size != (1920, 1080):
        return False
    crop = image.convert('L').crop((1307, 970, 1355, 1020))
    stats = ImageStat.Stat(crop)
    if stats.mean[0] < 125 or stats.stddev[0] < 55:
        return False
    sample = list(crop.resize((16, 16)).tobytes())
    with Image.open(_ICON_PATH) as reference:
        b = list(reference.convert('L').resize((16, 16)).tobytes())
    a = sample
    am, bm = sum(a) / len(a), sum(b) / len(b)
    numerator = sum((x-am)*(y-bm) for x, y in zip(a, b))
    denominator = (sum((x-am)**2 for x in a) * sum((y-bm)**2 for y in b)) ** .5
    return denominator > 0 and numerator / denominator >= .88


def _observe(runtime):
    image = runtime.capture_image(normalize=True)
    reading = read_cw_stage_text(runtime, image=image)
    return reading, image


def refresh_cw_replenish(runtime, *, check_cancelled=lambda: None) -> tuple[dict[str, Any], Image.Image]:
    check_cancelled()
    reading, image = _observe(runtime)
    check_cancelled()
    count = _count(reading['entries'])
    if not _page(reading['entries']) or count is None or count <= 0 or not _icon(image):
        raise TrailError('CW_REPLENISH_REFRESH_UNAVAILABLE', '无法确认补给页、可用刷新次数或刷新按钮')
    check_cancelled()
    runtime.click_point(1331, 995)
    try:
        for _ in range(12):
            check_cancelled()
            sleep(.2)
            check_cancelled()
            after, final_image = _observe(runtime)
            check_cancelled()
            remaining = _count(after['entries'])
            if _page(after['entries']) and remaining == count - 1:
                return {'value': 'replenish', 'entries': after['entries'], 'refresh_remaining': remaining}, final_image
        raise TrailError('CW_REPLENISH_REFRESH_UNCONFIRMED', '已发送刷新，但未能确认剩余次数下降')
    except RequestCancelled:
        raise
    except Exception as exc:
        if isinstance(exc, TrailError) and exc.code == 'CW_REPLENISH_REFRESH_UNCONFIRMED':
            error = exc
        else:
            error = TrailError('CW_REPLENISH_REFRESH_UNCONFIRMED', '已发送刷新，但未能确认结果')
        error.known_failure_after_save = True
        raise error from exc if error is not exc else None
