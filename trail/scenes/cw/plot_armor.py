from __future__ import annotations

import math
import re

import numpy as np
from PIL import Image

from trail.core.errors import TrailError
from trail.runtime.ocr_config import OcrRequestConfig
from trail.scenes.cw.role_recognition import SLOT_TARGET_SIZE
from trail.scenes.cw.stage import _read_ocr_box, _read_ocr_piece


_CARD_LAYOUTS = ((784, 1136), (608, 960, 1312))
PLOT_ARMOR_CARD_X_TOLERANCE = 64
_LOCK_MARKERS = ("羁绊解锁", "节点解锁", "尚未解锁", "尚未选择")
_OCR = OcrRequestConfig(lang="ch", ocr_mode="high")


def _words_in(entries, left, top, right, bottom):
    words = []
    for entry in entries:
        if not entry["bounds"]:
            continue
        x1, y1, x2, y2 = entry["bounds"]
        if left <= (x1 + x2) / 2 <= right and top <= (y1 + y2) / 2 <= bottom:
            words.append(entry)
    return [row["text"].strip() for row in sorted(words, key=lambda row: row["bounds"][1])]


def resolve_cw_plot_armor_card_layout(observed_centers: list[float]) -> tuple[int, ...] | None:
    if not observed_centers:
        return None
    matches = [
        layout
        for layout in _CARD_LAYOUTS
        if all(any(abs(x - center) <= PLOT_ARMOR_CARD_X_TOLERANCE for center in layout) for x in observed_centers)
    ]
    return matches[0] if len(matches) == 1 else None


def _read_role(image, center, recognizer):
    if image is None or recognizer is None:
        return None
    portrait = image.crop((center - 53, 289, center + 53, 393)).resize(SLOT_TARGET_SIZE)
    match = recognizer.recognize_crop(portrait, "front")
    if not match.name or match.score is None:
        return None
    second = next((candidate.score for candidate in match.candidates if candidate.name != match.name), 0)
    # Circular portraits lack the border and the fee color used by the board recognizer.
    return match.name if match.score >= 0.65 and match.score - second >= 0.02 else None


def _read_reselect_count(runtime, image):
    """Read only the counter below the colored icon on this same details frame."""
    if image is None or image.size != (1920, 1080) or not callable(getattr(runtime, "ocr_image", None)):
        return None
    try:
        page = runtime.ocr_image(image, ocr=_OCR) or []
    except TrailError:
        return None
    texts = [(_read_ocr_piece(piece).strip(), _read_ocr_box(piece)) for piece in page]
    if not any("主角光环与奇遇详情" in text and box and 530 <= (box[0] + box[2]) / 2 <= 900
               and 25 <= (box[1] + box[3]) / 2 <= 100 for text, box in texts):
        return None
    combined = "".join(text for text, _ in texts)
    if any(marker in combined for marker in ("将消耗", "是否重新选择主角", "是否替换该角色原有光环", "重选次数")):
        return None
    icon = np.asarray(image.crop((1465, 29, 1506, 64)).convert("RGB"), dtype=np.int16)
    red, green, blue = icon[:, :, 0], icon[:, :, 1], icon[:, :, 2]
    cyan = (blue > 120) & (green > 100) & (blue > red + 30) & (green > red + 15)
    purple = (red > 95) & (blue > 110) & (red > green + 25) & (blue > green + 25)
    if int(cyan.sum()) < 50 or int(purple.sum()) < 50:
        return None
    crop = image.crop((1455, 64, 1515, 106)).resize((180, 126), Image.Resampling.LANCZOS)
    try:
        pieces = runtime.ocr_image(crop, ocr=_OCR) or []
    except TrailError:
        return None
    if len(pieces) != 1:
        return None
    piece = pieces[0]
    text, box = _read_ocr_piece(piece).strip(), _read_ocr_box(piece)
    score = piece[2] if isinstance(piece, (list, tuple)) and len(piece) >= 3 else None
    if (not re.fullmatch(r"\d{1,2}", text) or box is None or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0.8):
        return None
    x, y = (box[0] + box[2]) / 6 + 1455, (box[1] + box[3]) / 6 + 64
    if not (1477 <= x <= 1495 and 70 <= y <= 98):
        return None
    return int(text)


def read_cw_plot_armor(runtime, reading, *, spotlight_list, recognizer=None) -> dict:
    entries = reading["entries"]
    catalog = {row["name"]: row.get("desc") for row in spotlight_list if row.get("name")}
    captured = runtime.capture_image(normalize=True) if callable(getattr(runtime, "capture_image", None)) else None
    image = captured.convert("RGB") if isinstance(captured, Image.Image) else None
    node = next(
        (word for word in _words_in(entries, 470, 40, 539, 90) if re.fullmatch(r"[1-3]-[1-9]\d?", word)),
        None,
    )
    remaining = _read_reselect_count(runtime, image)
    # A small title can disappear in the first page OCR. Re-read the same
    # normalized frame at higher resolution and retain only bounded evidence.
    if image is not None and image.size == (1920, 1080) and callable(getattr(runtime, "ocr_image", None)):
        try:
            enlarged = runtime.ocr_image(image.resize((3840, 2160), Image.Resampling.LANCZOS), ocr=_OCR) or []
            rows = [(_read_ocr_piece(piece).strip(), _read_ocr_box(piece)) for piece in enlarged]
            if (reading.get("value") == "plot_armor_details"
                    and not any(marker in "".join(text for text, _ in rows) for marker in
                                ("将消耗", "是否重新选择主角", "是否替换该角色原有光环"))
                    and any("主角光环与奇遇详情" in text and box and 1060 <= (box[0] + box[2]) / 2 <= 1800
                            and 50 <= (box[1] + box[3]) / 2 <= 200 for text, box in rows)):
                entries = list(entries) + [{"text": text, "bounds": tuple(v / 2 for v in box)}
                                           for text, box in rows if box and (text in catalog or any(m in text for m in _LOCK_MARKERS))]
        except TrailError:
            pass
    cards = []
    parsed = 0
    observed_centers = []
    for entry in entries:
        if not entry["bounds"]:
            continue
        left, top, right, bottom = entry["bounds"]
        text = entry["text"].strip()
        y = (top + bottom) / 2
        if (431 <= y <= 480 and text in catalog) or (419 <= y <= 744 and any(
            marker in "".join(text.split()) for marker in _LOCK_MARKERS
        )):
            observed_centers.append((left + right) / 2)
    centers = resolve_cw_plot_armor_card_layout(observed_centers) or ()
    for slot, center in enumerate(centers, start=1):
        titles = _words_in(entries, center - 150, 431, center + 150, 480)
        name = next((title for title in titles if title in catalog), None)
        card_text = "".join(_words_in(entries, center - 155, 419, center + 155, 744))
        if name is None:
            if any(marker in "".join(card_text.split()) for marker in _LOCK_MARKERS):
                parsed += 1
            continue
        description = catalog[name] or "".join(_words_in(entries, center - 152, 514, center + 152, 638))
        description = re.sub(r"<[^>]*>", "", description).replace("\\n", "\n")
        role = _read_role(image, center, recognizer)
        red, green, blue = image.getpixel((center - 95, 248))[:3] if image is not None else (0, 0, 0)
        labels = _words_in(entries, center - 155, 260, center + 155, 516)
        labels += _words_in(entries, center - 155, 641, center + 155, 744)
        if any(label in {"已失效", "光环失效", "未生效", "未激活", "已出售"} for label in labels):
            status = "inactive"
        elif red > 130 and red - max(green, blue) > 50:
            status = "active"
        else:
            status = "unknown"
        cards.append({"slot": slot, "name": name, "description": description or None, "role": role, "status": status})
        parsed += 1
    return {
        "status": "observed" if centers and parsed == len(centers) else "partial" if parsed else "unverified",
        "node": node,
        "reselect_remaining": remaining,
        "_reselect_verified": remaining is not None,
        "cards": cards,
    }
