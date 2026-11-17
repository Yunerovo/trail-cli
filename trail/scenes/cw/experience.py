from __future__ import annotations

import re
from time import sleep
from pathlib import Path
from typing import Any

from trail.runtime.batch_ocr import BatchOcrTarget, run_batch_ocr
from trail.runtime.ocr_config import OcrRequestConfig
from trail.scenes.cw import stage
from trail.scenes.cw.opening import _ambiguous_growth_name
from trail.scenes.cw.shop import SHOP_COINS_REGION, _batch_ocr_items, _parse_first_int, build_cw_shop_exp_buyer


def _crop(frame, region):
    return frame.crop(tuple(region[k] for k in ("from_x", "from_y", "to_x", "to_y")))



def _has_gold_price_icon(frame):
    """Recognize the fixed purchase-control gold medal, not its text or hue alone."""
    import numpy as np
    from PIL import Image
    reference_path = Path(__file__).with_name("assets") / "experience_gold.png"
    with Image.open(reference_path) as reference:
        expected = np.asarray(reference.convert("RGB"), dtype=np.float32).reshape(-1)
    expected_centered = expected - expected.mean()
    expected_norm = float(np.linalg.norm(expected_centered))
    # The purchase control moves by a few pixels between UI states. Keep the
    # search tightly bounded around the known price icon, never the balance.
    for y in range(980, 991):
        for x in range(263, 274):
            actual = np.asarray(frame.crop((x, y, x + 34, y + 33)).convert("RGB"), dtype=np.float32).reshape(-1)
            actual_centered = actual - actual.mean()
            denominator = float(np.linalg.norm(actual_centered)) * expected_norm
            similarity = float(np.dot(actual_centered, expected_centered) / denominator) if denominator else 0.0
            error = float(np.mean(np.abs(actual - expected)))
            if similarity >= 0.80 and error <= 32.0:
                return True
    return False

def _observe(runtime):
    """Read all decision facts from one normalized, freshly captured frame."""
    frame = runtime.capture_image()
    if frame.size != (1920, 1080):
        return {"valid": False}, frame
    words = runtime.ocr_image(frame, ocr=OcrRequestConfig(lang="ch")) or []
    page = stage._stage_from_ocr_pieces(words)
    texts = [stage._read_ocr_piece(w).strip() for w in words]
    # A matching label must be in the expected control area, not in a tooltip.
    anchor = any("购买经验" in stage._read_ocr_piece(w) and (b := stage._read_ocr_box(w)) is not None
                 and 180 <= b[0] <= 360 and 800 <= b[1] <= 930 for w in words)
    max_anchor = any(stage._read_ocr_piece(w).strip() == "满级" and (b := stage._read_ocr_box(w)) is not None
                     and 180 <= b[0] <= 360 and 800 <= b[1] <= 930 for w in words)
    overlays = ("确认", "取消", "退出", "是否", "选择奇美拉", "简易武装箱", "选择主角光环")
    blocked = any(any(token in text for token in overlays) for text in texts)
    if page != "preparation" or not (anchor or max_anchor) or blocked:
        return {"valid": False, "stage": page}, frame
    targets = [
        BatchOcrTarget(("stage_status", "level"), _crop(frame, stage.CW_STATUS_LEVEL_REGION)),
        BatchOcrTarget(("stage_status", "exp"), _crop(frame, stage.CW_STATUS_EXP_REGION)),
        BatchOcrTarget("coins", _crop(frame, SHOP_COINS_REGION)),
        BatchOcrTarget("exp_price", frame.crop((300, 980, 347, 1022))),
    ]
    batch = run_batch_ocr(runtime, targets, trace_prefix="cw_experience_batch_ocr")
    status = stage.parse_cw_stage_status(batch.by_key)
    return {"valid": True, "stage": page, "level": status.get("level"), "exp": status.get("exp"),
            "coins": _parse_first_int(_batch_ocr_items(batch.by_key, "coins"), default=None),
            "max_level": max_anchor and status.get("level") == 10,
            "price_currency": "gold" if _has_gold_price_icon(frame) else None,
            "displayed_price": _parse_first_int(_batch_ocr_items(batch.by_key, "exp_price"), default=None)}, frame


def _price(opening: Any, level: int, displayed_price: int = 4) -> int | None:
    if level < 8:
        return displayed_price
    if not isinstance(opening, dict):
        return None
    rows = opening.get("affixes")
    if not isinstance(rows, list):
        return None
    names = [row.get("name") for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)]
    if len(names) != len(set(names)):
        return None
    if any(isinstance(row, dict) and isinstance(row.get("name"), str) and _ambiguous_growth_name(row["name"]) for row in rows):
        return None
    if any(isinstance(row, dict) and row.get("name") == "成长的烦恼" and row.get("uncertain") is not True for row in rows):
        return displayed_price + 1
    complete = opening.get("affixes_status") == "observed"
    if not complete or (not rows and opening.get("affixes_empty_verified") is not True):
        return None
    if any(not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip()
           or row.get("uncertain") is True for row in rows):
        return None
    return displayed_price


def _plan(observation, opening, target_level):
    result = {"status": "unknown", "target_level": target_level, "level": observation.get("level"),
              "exp": observation.get("exp"), "coins": observation.get("coins"),
              "cost_per_purchase": None, "purchases_required": None, "gold_required": None,
              "purchases_verified": 0, "purchases_attempted": 0, "gold_spent": 0, "stage": observation.get("stage"), "reason": None,
              "price_currency": observation.get("price_currency"), "displayed_price": observation.get("displayed_price"),
              "missing_fields": []}
    level, coins, exp = result["level"], result["coins"], result["exp"]
    result["missing_fields"] = [name for name, value in (
        ("level", level), ("exp", exp), ("coins", coins),
        ("price_currency", observation.get("price_currency")),
        ("displayed_price", observation.get("displayed_price"))) if value is None]
    if not observation.get("valid") or type(level) is not int or level < 1 or type(target_level) is not int or target_level < 1:
        result["reason"] = "page_or_level_unavailable"
        return result
    if target_level == level:
        result["status"] = "reached"
        return result
    if target_level != level + 1:
        result["status"] = "unsupported"
        return result
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", exp) if isinstance(exp, str) else None
    if match is None or type(coins) is not int or coins < 0:
        result["reason"] = "experience_unavailable" if match is None else "coins_unavailable"
        return result
    current, threshold = map(int, match.groups())
    if observation.get("price_currency") != "gold" or type(observation.get("displayed_price")) is not int or observation["displayed_price"] < 0:
        result["reason"] = "price_currency_or_amount_unavailable"
        return result
    cost = _price(opening, level, observation["displayed_price"])
    if threshold <= 0 or current >= threshold or cost is None:
        result["reason"] = "affix_unavailable" if cost is None else "experience_unavailable"
        return result
    count = (threshold - current + 3) // 4
    result.update(status="ready", cost_per_purchase=cost, purchases_required=count, gold_required=count * cost)
    if coins < result["gold_required"]:
        result["status"] = "insufficient_gold"
    return result


def quote_cw_experience(runtime, *, opening, target_level):
    observation, frame = _observe(runtime)
    return _plan(observation, opening, target_level), frame


def buy_cw_experience_to_level(runtime, *, opening, target_level, max_gold, check_cancelled):
    check_cancelled()
    observation, frame = _observe(runtime)
    result = _plan(observation, opening, target_level)
    if result["status"] != "ready":
        return result, frame
    if max_gold is not None and (type(max_gold) is not int or max_gold < 0 or result["gold_required"] > max_gold):
        result["status"] = "budget_exceeded"
        return result, frame
    buyer = build_cw_shop_exp_buyer(runtime)
    for _ in range(result["purchases_required"]):
        check_cancelled()
        # Confirm price and currency again before each individual action.
        if (observation.get("price_currency") != "gold" or
                type(observation.get("displayed_price")) is not int or observation["displayed_price"] < 0 or
                _price(opening, observation["level"], observation.get("displayed_price")) != result["cost_per_purchase"]):
            result["status"] = "partial" if result["purchases_verified"] else "unknown"
            result["reason"] = "price_currency_or_amount_unavailable"
            return result, frame
        before_level, before_exp, before_coins = observation["level"], observation["exp"], observation["coins"]
        before_current, threshold = map(int, before_exp.split("/"))
        result["purchases_attempted"] += 1
        buyer()
        check_cancelled()
        observation, frame = _observe(runtime)
        # Animation can briefly leave the old frame on screen. Only poll; never
        # issue another purchase until the previous one is fully verified.
        for _poll in range(2):
            unchanged = (observation.get("level") == before_level
                         and observation.get("exp") == before_exp
                         and observation.get("coins") == before_coins)
            if observation.get("valid") and not unchanged:
                break
            check_cancelled()
            sleep(0.25)
            observation, frame = _observe(runtime)
        after_level = before_level + (before_current + 4 >= threshold)
        after_current = (before_current + 4) % threshold
        after_exp = observation.get("exp")
        match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", after_exp) if isinstance(after_exp, str) else None
        max_level_verified = (after_level == 10 and before_level == 9
                              and before_current + 4 >= threshold and observation.get("max_level") is True)
        exp_verified = (match is not None and int(match.group(1)) == after_current
                        and (after_level != before_level or int(match.group(2)) == threshold))
        verified = (observation.get("valid") and observation.get("level") == after_level
                    and (exp_verified or max_level_verified)
                    and observation.get("coins") == before_coins - result["cost_per_purchase"])
        if not verified:
            result.update(status="unconfirmed", level=observation.get("level"), exp=after_exp, coins=observation.get("coins"))
            return result, frame
        result["purchases_verified"] += 1
        result["gold_spent"] += result["cost_per_purchase"]
        result.update(level=after_level, exp=after_exp, coins=observation["coins"])
    result["status"] = "reached"
    return result, frame
