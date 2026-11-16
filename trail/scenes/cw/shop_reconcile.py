from __future__ import annotations

from collections.abc import Callable

from PIL import Image

from trail.core.errors import TrailError
from trail.runtime.ocr_config import OcrRequestConfig
from trail.scenes.cw.stage import _read_ocr_piece as read_ocr_piece

_SLOTS = (1, 2, 3, 4, 5)


def recover_missing_shop_items(runtime, image, items, *, check_cancelled: Callable[[], None]):
    """Retry only unconfirmed lanes using the already captured shop crop."""
    if not isinstance(image, Image.Image) or not callable(getattr(runtime, "ocr_image", None)):
        return items
    by_slot = {row.get("slot"): dict(row) for row in items if isinstance(row, dict) and row.get("slot") in _SLOTS}
    config = OcrRequestConfig(lang="ch", ocr_mode="high")
    for slot in _SLOTS:
        row = by_slot.get(slot, {"slot": slot, "name": None, "price": None})
        if row.get("name") and row.get("price") is not None:
            continue
        check_cancelled()
        left = round(image.width * (slot - 1) / len(_SLOTS))
        right = round(image.width * slot / len(_SLOTS))
        crop = image.crop((left, 0, right, image.height))
        try:
            pieces = runtime.ocr_image(crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS), ocr=config) or []
        except TrailError:
            continue
        check_cancelled()
        words = [read_ocr_piece(piece).strip() for piece in pieces]
        names = {word for word in words if word and not word.isdecimal() and "备" not in word}
        prices = {int(word) for word in words if word.isdecimal() and 1 <= int(word) <= 5}
        if len(names) == len(prices) == 1:
            by_slot[slot] = {"slot": slot, "name": names.pop(), "price": prices.pop()}
    return [by_slot.get(slot, {"slot": slot, "name": None, "price": None}) for slot in _SLOTS]
