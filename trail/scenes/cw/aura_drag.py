from __future__ import annotations

import re
from time import sleep
from typing import Any

import cv2
import numpy as np
from PIL import Image

from trail.core.errors import TrailError
from trail.daemon.models import RequestCancelled
from trail.scenes.cw.slots import _runtime_slots_image
from trail.scenes.cw.stage import read_cw_stage_text

_POINTS = {"front": [(x, 622) for x in (804, 909, 1011, 1114)],
           "back": [(x, 764) for x in (712, 810, 910, 1010, 1110, 1208)]}


def _page(reading):
    entries = reading.get("entries", [])
    texts = [str(e.get("text", "")) for e in entries]
    def located(entry, labels, x_min, x_max, y_max):
        box = entry.get("bounds")
        return (isinstance(box, (tuple, list)) and len(box) == 4
                and x_min <= (box[0] + box[2]) / 2 <= x_max
                and 0 <= (box[1] + box[3]) / 2 <= y_max
                and any(label in str(entry.get("text", "")) for label in labels))
    return (reading.get("value") == "plot_armor"
            and any(located(e, ("选择一个角色获得主角光环",), 600, 1300, 180) for e in entries)
            and any(located(e, ("返回备战界面",), 1550, 1920, 180) for e in entries)
            and not any(any(s in t for s in ("主角光环与奇遇详情", "是否重新选择", "是否替换", "重选次数", "将消耗")) for t in texts))


def _occupied(image, point):
    x, y = point
    rgb = np.asarray(image.convert("RGB"))
    roi = rgb[y-44:y-17, x+23:x+45].astype(int)
    neutral = np.ptp(roi, axis=2) < 35
    white = np.all(roi > 190, axis=2) & neutral
    black = np.all(roi < 55, axis=2) & neutral
    return white.sum() >= 25 and black.sum() >= 100 and np.asarray(image.convert("L"))[y-25:y+26, x-24:x+24].std() > 12


def _score(before, after, point):
    x, y = point
    src = np.asarray(before.convert("L"))[y-24:y+24, x-24:x+24]
    dst = np.asarray(after.convert("L"))[400:500, 905:1010]
    return max(float(cv2.matchTemplate(dst, cv2.resize(src, None, fx=s, fy=s), cv2.TM_CCOEFF_NORMED).max()) for s in (1.0, 1.05, 1.1))


def drag_cw_aura_role(runtime, *, source: str, check_cancelled=lambda: None) -> tuple[dict[str, Any], Image.Image]:
    match = re.fullmatch(r"(front|back):([0-9]+)", source) if isinstance(source, str) else None
    if not match or int(match.group(2)) >= len(_POINTS[match.group(1)]):
        raise TrailError("CW_AURA_SOURCE_INVALID", "invalid aura role source")
    point = _POINTS[match.group(1)][int(match.group(2))]
    check_cancelled()
    before = _runtime_slots_image(runtime)
    reading = read_cw_stage_text(runtime, image=before)
    check_cancelled()
    if not _page(reading):
        raise TrailError("CW_AURA_PAGE_UNCONFIRMED", "aura selection page is not confirmed")
    if not _occupied(before, point):
        raise TrailError("CW_AURA_SOURCE_UNCONFIRMED", "source slot is not confirmed occupied")
    check_cancelled()
    runtime.drag_to(*point, 957, 451)
    try:
        for _ in range(8):
            check_cancelled()
            sleep(0.3)
            after = _runtime_slots_image(runtime)
            result = read_cw_stage_text(runtime, image=after)
            check_cancelled()
            if not _page(result):
                continue
            def darkened(p):
                px, py = p
                old = np.asarray(before.convert("L"))[py-24:py+24, px-24:px+24]
                new = np.asarray(after.convert("L"))[py-24:py+24, px-24:px+24]
                correlation = cv2.matchTemplate(old, new, cv2.TM_CCOEFF_NORMED)[0, 0]
                return new.mean() < old.mean() * .88 and correlation > .95
            others = [_score(before, after, p) for points in _POINTS.values() for p in points
                      if p != point and _occupied(before, p) and darkened(p)]
            score = _score(before, after, point)
            before_center = np.asarray(before.convert("L"))[410:490, 915:1000].astype(float)
            after_center = np.asarray(after.convert("L"))[410:490, 915:1000].astype(float)
            changed = np.abs(before_center - after_center).mean() > 10
            if darkened(point) and changed and score >= .70 and (not others or score > max(others) + .03):
                return {"value": "plot_armor", "entries": result["entries"], "source": f"{match.group(1)}:{int(match.group(2)) + 1}", "verified": True}, after
        raise TrailError("CW_AURA_DRAG_UNCONFIRMED", "aura role drag could not be confirmed")
    except RequestCancelled:
        raise
    except Exception as exc:
        error = TrailError("CW_AURA_DRAG_UNCONFIRMED", "aura role drag could not be confirmed")
        error.known_failure_after_save = True
        raise error from exc
