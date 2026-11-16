from __future__ import annotations

import re
import math
from time import monotonic, sleep

from trail.core.errors import TrailError
from trail.runtime.ocr_config import OcrRequestConfig
from trail.scenes.cw.stage import read_cw_stage_text, _read_ocr_piece, _read_ocr_box


BOSS_DETAIL_POINTS = ((392, 293), (673, 293), (955, 293))
DIFFICULTY_POINT = (105, 994)
RANK_EFFECTS_POINT = (824, 271)
PLANES = ("第一位面", "第二位面", "第三位面")


def _in_region(entries, *, left, top, right, bottom):
    found = []
    for entry in entries:
        bounds = entry["bounds"]
        if bounds is None:
            continue
        x1, y1, x2, y2 = bounds
        if left <= (x1 + x2) / 2 <= right and top <= (y1 + y2) / 2 <= bottom:
            found.append(entry)
    return found




def _ambiguous_growth_name(name):
    return any(len(name) == len(expected) and 0 < sum(a != b for a, b in zip(name, expected)) <= 1
               for expected in ("成长的烦恼", "成长的快乐"))


def _read_affix_crop(runtime, image, *, check_cancelled):
    check_cancelled()
    try:
        pieces = runtime.ocr_image(image.crop((297, 956, 1190, 1024)),
                                   ocr=OcrRequestConfig(lang="ch", ocr_mode="high")) or []
    except TrailError:
        check_cancelled()
        return []
    check_cancelled()
    entries = []
    for piece in pieces:
        text = _read_ocr_piece(piece).strip()
        bounds = _read_ocr_box(piece)
        score = piece.get("score", piece.get("confidence")) if isinstance(piece, dict) else (piece[2] if isinstance(piece, (list, tuple)) and len(piece) > 2 else None)
        if text and bounds:
            entries.append({"text": text, "bounds": bounds, "score": score})
    return entries

def _affix_row_complete(image, labels):
    """Prove every visible chip has one well-placed, legible OCR label."""
    if getattr(image, "size", None) != (1920, 1080) or not labels:
        return False
    import numpy as np
    pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    contrast = np.median(pixels[959:968, 286:1205, 2], axis=0) - np.median(pixels[1002:1007, 286:1205, 2], axis=0)
    smooth = np.convolve(contrast, np.ones(5) / 5, mode="same")
    mask = smooth >= 27
    edges = np.diff(np.r_[False, mask, False].astype(int))
    spans = [(int(a + 286), int(b + 286)) for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0])]
    if not spans or len(spans) != len(labels):
        return False
    if any(a <= 291 or b >= 1190 or not 55 <= b - a <= 260 for a, b in spans):
        return False
    if any(b[0] - a[1] < 4 for a, b in zip(spans, spans[1:])):
        return False
    names = [label.get("text", "").strip() for label in labels]
    if len(set(names)) != len(names) or any(_ambiguous_growth_name(name) for name in names):
        return False
    for (left, right), label in zip(spans, labels):
        bounds = label.get("bounds")
        name = label.get("text", "").strip()
        score = label.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or score < 0.85:
            return False
        if not bounds or not re.fullmatch(r"[\u4e00-\u9fff·]+", name):
            return False
        x1, y1, x2, y2 = bounds
        if not (left <= x1 < x2 <= right and 966 <= y1 < y2 <= 1010):
            return False
    return True

def _detail_observation(runtime):
    # Capture once so page confirmation and the high-accuracy crops refer to
    # exactly the same frame. Lightweight test/legacy runtimes can still OCR.
    if callable(getattr(runtime, "capture_image", None)) and callable(getattr(runtime, "ocr_image", None)):
        image = runtime.capture_image()
        return read_cw_stage_text(runtime, image=image)["entries"], image
    return read_cw_stage_text(runtime)["entries"], None


def _wait_for_entries(runtime, condition, *, ocr=None, check_cancelled=lambda: None, with_image=False):
    deadline = monotonic() + 4
    while True:
        check_cancelled()
        if with_image:
            entries, image = _detail_observation(runtime)
        else:
            entries, image = read_cw_stage_text(runtime, ocr=ocr)["entries"], None
        check_cancelled()
        if condition(entries):
            return (entries, image) if with_image else entries
        if monotonic() >= deadline:
            raise TrailError("CW_OPENING_PAGE_UNCONFIRMED", "无法确认目标开局页面，请重新读取当前画面")
        sleep(0.18)


def _has(entries, text):
    return any(text in re.sub(r"[\s.·。]", "", entry["text"]) for entry in entries)


def _overview_ready(entries):
    return all(_has(entries, text) for text in ("本场对局首领", "本局奇遇", "下一步"))


def _other_modal_visible(entries):
    return any(_has(entries, text) for text in ("对手信息", "职级效果", "确认", "取消"))


def _retry_overview_signature(entries):
    """Identify the full preview using positioned labels and all three card names."""
    regions = (
        ("本场对局首领", (450, 160, 710, 235)),
        ("本局奇遇", (1110, 590, 1320, 660)),
        ("下一步", (1200, 950, 1600, 1030)),
    )
    if any(not _has(_in_region(entries, left=l, top=t, right=r, bottom=b), text)
           for text, (l, t, r, b) in regions):
        return None
    # A visible modal may leave some of the preview's labels underneath it.
    if _has(entries, "敌方信息") or _other_modal_visible(entries):
        return None
    cards = []
    for x, _ in BOSS_DETAIL_POINTS:
        labels = _in_region(entries, left=x - 220, top=660, right=x + 30, bottom=710)
        if not _has(labels, "阵营"):
            return None
        words = _in_region(entries, left=x - 220, top=716, right=x + 30, bottom=768)
        if len(words) != 1:
            return None
        name = re.sub(r"\s", "", words[0]["text"])
        if not name or name == "阵营":
            return None
        cards.append(name)
    return tuple(cards)


def _read_boss_detail(runtime, point, overview, *, check_cancelled, with_image=False):
    blocked = False

    def detail_or_blocked(words):
        nonlocal blocked
        if _other_modal_visible(words):
            blocked = True
            raise TrailError("CW_OPENING_PAGE_UNCONFIRMED", "无法确认目标开局页面，请重新读取当前画面")
        return _has(words, "敌方信息")

    runtime.click_point(*point)
    try:
        return _wait_for_entries(runtime, detail_or_blocked, check_cancelled=check_cancelled, with_image=with_image)
    except TrailError as error:
        if error.code != "CW_OPENING_PAGE_UNCONFIRMED" or blocked:
            raise
        original_error = error

    signature = _retry_overview_signature(overview)
    if signature is None:
        raise original_error
    # Require two fresh matching observations. A late-arriving detail is already
    # the desired result and must never cause another click.
    for observation in range(2):
        check_cancelled()
        if with_image:
            entries, image = _detail_observation(runtime)
        else:
            entries, image = read_cw_stage_text(runtime)["entries"], None
        check_cancelled()
        if detail_or_blocked(entries):
            return (entries, image) if with_image else entries
        if _retry_overview_signature(entries) != signature:
            raise original_error
        if observation == 0:
            sleep(0.18)
    check_cancelled()
    runtime.click_point(*point)
    return _wait_for_entries(runtime, detail_or_blocked, check_cancelled=check_cancelled, with_image=with_image)


def _read_boss_name(entries):
    words = _in_region(entries, left=1270, top=190, right=1890, bottom=254)
    words.sort(key=lambda word: word["bounds"][0])
    text = "".join(word["text"].strip() for word in words)
    name = re.split(r"\s*[/／]?\s*等级\s*\d+", text, maxsplit=1)[0].strip(" /／")
    return name if re.search(r"[^\W\d_]", name) else None


def _read_detail_crop(runtime, image, box, *, check_cancelled):
    if image is None:
        return []
    check_cancelled()
    try:
        entries = read_cw_stage_text(
            runtime, image=image.crop(box), ocr=OcrRequestConfig(lang="ch", ocr_mode="high")
        )["entries"]
    except TrailError:
        check_cancelled()
        return []
    check_cancelled()
    return entries


def _read_detail_faction(entries):
    if len(entries) != 1:
        return None
    text = entries[0]["text"].strip(" :：")
    return text or None


def _read_plane_stats(entries):
    plane_stats = {}
    current = None
    fields = {"攻击": "attack_pct", "生命值": "hp_pct", "速度": "speed_pct"}
    words = _in_region(entries, left=385, top=316, right=740, bottom=820)
    for entry in sorted(words, key=lambda value: value["bounds"][1]):
        text = entry["text"].replace(" ", "")
        if text in PLANES:
            current = text
            plane_stats[current] = {}
        elif current and (match := re.search(r"(攻击|生命值|速度)提高(\d+)%", text)):
            plane_stats[current][fields[match[1]]] = int(match[2])
    return [{"plane": plane, **plane_stats[plane]} for plane in PLANES if plane in plane_stats]


def _read_affix_tooltip(entries, name):
    words = sorted(
        _in_region(entries, left=70, top=843, right=1370, bottom=963),
        key=lambda entry: entry["bounds"][1],
    )
    for idx, word in enumerate(words):
        if word["text"].strip(" .·。") != name:
            continue
        left = word["bounds"][0]
        return "".join(body["text"] for body in words[idx + 1:] if abs(body["bounds"][0] - left) <= 24) or None
    return None


def collect_cw_opening(runtime, *, check_cancelled=lambda: None):
    """Inspect the current Season 2 overview and return to it after reading."""
    entries = read_cw_stage_text(runtime)["entries"]
    if not _overview_ready(entries):
        raise TrailError("CW_OPENING_OVERVIEW_REQUIRED", "请先进入本场对局首领与本局奇遇同屏的开局预览")

    # Sparse OCR remains acceptable for the ordinary first click, but cannot
    # authorize a retry. Try one fresh read to obtain a stronger baseline.
    retry_baseline = entries
    if _retry_overview_signature(retry_baseline) is None:
        check_cancelled()
        retry_baseline = read_cw_stage_text(runtime)["entries"]
        check_cancelled()
        if not _overview_ready(retry_baseline):
            raise TrailError("CW_OPENING_PAGE_UNCONFIRMED", "无法确认目标开局页面，请重新读取当前画面")
    if _has(retry_baseline, "敌方信息") or _other_modal_visible(retry_baseline):
        raise TrailError("CW_OPENING_PAGE_UNCONFIRMED", "无法确认目标开局页面，请重新读取当前画面")

    companions = _in_region(entries, left=1140, top=660, right=1380, bottom=722)
    companion = companions[0]["text"] if len(companions) == 1 else None
    difficulty = next(
        (int(match[1]) for entry in entries if (match := re.search(r"敌人难度\s*\+?\s*(\d+)", entry["text"]))),
        None,
    )
    affix_labels = sorted(
        _in_region(entries, left=290, top=956, right=1160, bottom=1024),
        key=lambda entry: entry["bounds"][0],
    )
    affix_labels = [word for word in affix_labels if "敌人难度" not in word["text"]]
    bosses = []
    for idx, (x, y) in enumerate(BOSS_DETAIL_POINTS, start=1):
        check_cancelled()
        faction_words = _in_region(entries, left=x - 220, top=716, right=x + 30, bottom=768)
        detail, image = _read_boss_detail(runtime, (x, y), retry_baseline,
                                          check_cancelled=check_cancelled, with_image=True)
        title = _read_detail_crop(runtime, image, (1270, 180, 1875, 265), check_cancelled=check_cancelled)
        # Crop coordinates are local; translate them for the existing title parser.
        for word in title:
            if word["bounds"] is not None:
                x1, y1, x2, y2 = word["bounds"]
                word["bounds"] = (x1 + 1270, y1 + 180, x2 + 1270, y2 + 180)
        faction = _read_detail_faction(_read_detail_crop(
            runtime, image, (15, 108, 350, 178), check_cancelled=check_cancelled
        )) or _read_detail_faction(_in_region(detail, left=15, top=108, right=350, bottom=178))
        bosses.append({
            "plane": PLANES[idx - 1],
            "faction": faction or (faction_words[0]["text"] if len(faction_words) == 1 else None),
            "name": _read_boss_name(title) or _read_boss_name(detail),
        })
        check_cancelled()
        runtime.press_key("esc")
        _wait_for_entries(runtime, _overview_ready, check_cancelled=check_cancelled)

    check_cancelled()
    runtime.click_point(*DIFFICULTY_POINT)
    opponent = _wait_for_entries(runtime, lambda words: _has(words, "对手信息") and _has(words, "第一位面"), check_cancelled=check_cancelled)
    check_cancelled()
    runtime.drag_to(1543, 640, 1543, 803, duration=0.4)
    scrolled = _wait_for_entries(
        runtime,
        lambda words: any(
            row["plane"] == PLANES[-1] and "speed_pct" in row for row in _read_plane_stats(words)
        ),
        check_cancelled=check_cancelled,
    )
    plane_stats = {row["plane"]: row for row in _read_plane_stats(opponent)}
    for row in _read_plane_stats(scrolled):
        plane_stats.setdefault(row["plane"], {}).update(row)
    check_cancelled()
    runtime.click_point(*RANK_EFFECTS_POINT)
    rank = _wait_for_entries(runtime, lambda words: _has(words, "职级效果") and not _has(words, "第一位面"), check_cancelled=check_cancelled)
    rank_effects = [
        entry["text"] for entry in _in_region(rank, left=420, top=315, right=1520, bottom=820)
    ]
    check_cancelled()
    runtime.press_key("esc")
    preview, preview_image = _wait_for_entries(runtime, _overview_ready, check_cancelled=check_cancelled, with_image=True)

    # The full-frame OCR can merge difficulty and the first chip. Read the
    # complete chip row from the same confirmed preview frame instead.
    affixes_status = "partial" if affix_labels else "unknown"
    if preview_image is not None:
        crop = _read_affix_crop(runtime, preview_image, check_cancelled=check_cancelled)
        labels = []
        for word in crop:
            if word["bounds"] is None or not word["text"].strip():
                continue
            x1, y1, x2, y2 = word["bounds"]
            labels.append({**word, "bounds": (x1 + 297, y1 + 956, x2 + 297, y2 + 956)})
        affix_labels = sorted(labels, key=lambda word: word["bounds"][0])
        affixes_status = "partial" if labels else "unknown"
        if _affix_row_complete(preview_image, affix_labels):
            affixes_status = "observed"

    affixes = []
    for entry in affix_labels:
        check_cancelled()
        left, top, right, bottom = entry["bounds"]
        x = max(300, round(left + min(14, (right - left) / 2)))
        y = round((top + bottom) / 2)
        name = entry["text"].strip(" .·。")
        # The game exposes the tooltip after the pointer settles on the chip.
        runtime.drag_to(x, y, x, y, duration=0.35)
        sleep(0.8)
        check_cancelled()
        try:
            tooltip = _wait_for_entries(
                runtime, lambda words: bool(_read_affix_tooltip(words, name)),
                ocr=OcrRequestConfig(lang="ch", ocr_mode="high"),
                check_cancelled=check_cancelled,
            )
        except TrailError as error:
            if error.code != "CW_OPENING_PAGE_UNCONFIRMED":
                raise
            description = None
        else:
            description = _read_affix_tooltip(tooltip, name)
        affix = {"name": name, "description": description}
        score = entry.get("score")
        if score is not None:
            if not isinstance(score, bool) and isinstance(score, (int, float)) and 0 <= score <= 1:
                affix["score"] = score
            if "score" not in affix or score < 0.85:
                affix["uncertain"] = True
        affixes.append(affix)
    if affixes:
        check_cancelled()
        runtime.click_point(1060, 946)
        _wait_for_entries(runtime, _overview_ready, check_cancelled=check_cancelled)
    return {
        "companion": companion,
        "enemy_difficulty": difficulty,
        "bosses": bosses,
        "affixes": affixes,
        "affixes_status": affixes_status,
        "enemy_plane_stats": list(plane_stats.values()),
        "rank_effects": rank_effects,
    }
