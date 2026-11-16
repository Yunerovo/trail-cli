from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from time import monotonic, sleep

from PIL import Image

from trail.core.errors import TrailError
from trail.runtime.ocr_config import OcrRequestConfig
from trail.runtime.resources import resolve_scene_asset
from trail.scenes.cw.battle_report import (
    BattleReport, CONTINUE_BUTTON, HP_DETAILS_POINT, REPORT_FRAME_SIZE, _ocr_region, battle_hud_from_page,
)
from trail.scenes.cw.events import _fail_if_battle_team_count_confirm_dialog, build_cw_battle_continuer, build_cw_battle_starter, build_cw_settle_continuer
from trail.scenes.cw.models import ensure_cw_state
from trail.scenes.cw.stage import _read_ocr_box, _replace_stage_fields, _season_two_decision_stage, build_cw_stage_detector, mark_cw_stage_stale, project_cw_preparation_header
from trail.session.models import SessionModel

SETTLEMENT_HEADLINE_CAPTURE = {
    "from_x": 0.30,
    "from_y": 0.10,
    "to_x": 0.70,
    "to_y": 0.28,
}
SETTLEMENT_ROUND_CAPTURE = {
    "from_x": 0.22,
    "from_y": 0.22,
    "to_x": 0.48,
    "to_y": 0.42,
}
SETTLEMENT_STATS_CAPTURE = {
    "from_x": 0.20,
    "from_y": 0.22,
    "to_x": 0.60,
    "to_y": 0.52,
}
_CW_REFERENCE_WIDTH = 1920
_CW_REFERENCE_HEIGHT = 1080
_CW_ACTION_OCR_REGION = {
    "from_x": 0.30,
    "from_y": 0.72,
    "to_x": 0.70,
    "to_y": 0.92,
}
LAYER_TRANSITION_CONTINUE_POINT = (960, 903)
GAME_OVER_RETURN_TEXT = "返回货币战争"
GAME_OVER_RETURN_POINT = (960, 908)
GAME_OVER_RETURN_SETTLE_SECONDS = 1.0
GAME_OVER_RETURN_HOME_WAIT_SECONDS = 3
GAME_OVER_RETURN_HOME_WAIT_INTERVAL_SECONDS = 0.5
GAME_OVER_RETURN_STILL_PAGE_KEYWORDS = (GAME_OVER_RETURN_TEXT, "对局未完成", "挑战失败", "伤害统计", "小队生命值")
GAME_OVER_HP_REGION = (1038, 119, 1077, 153)

STABLE_BATTLE_RETURN_STAGES: frozenset[str] = frozenset(
    {
        "encounter",
        "event",
        "fortune",
        "invest",
        "replenish",
        "shop",
        "voyage_companion",
        "plot_armor",
        "plot_armor_details",
        "weapon_box",
        "chimera_pick",
        "ranger_pick",
        "confirm_leave",
    }
)
_BATTLE_START_KEYWORDS: tuple[str, ...] = ("开始战斗", "开始挑战", "出战")
_SETTLEMENT_ENTRY_KEYWORDS: tuple[str, ...] = ("挑战成功", "挑战失败", "继续挑战")
_SETTLEMENT_FOLLOWUP_KEYWORDS: tuple[str, ...] = ("下一步", "下一页", "前往结算")
_GAME_OVER_KEYWORDS: tuple[str, ...] = ("游戏结束", "本局结束", "对局未完成", "返回货币战争")
_BATTLE_PROGRESS_KEYWORDS: tuple[str, ...] = ("自动战斗", "倍速", "暂停")
CW_BATTLE_RUN_POLL_INTERVAL_SECONDS = 0.20
_DETECTED_STAGE_UNSET = object()


def _asset(alias: str) -> str:
    return str(resolve_scene_asset("cw", alias))


def _read_ocr_piece(item: object) -> str:
    if isinstance(item, dict):
        for key in ("text", "value", "name"):
            value = item.get(key)
            if isinstance(value, str | int | float):
                return str(value)
        return ""

    if isinstance(item, (list, tuple)):
        if len(item) >= 2 and isinstance(item[1], str | int | float):
            return str(item[1])
        for value in item:
            if isinstance(value, str | int | float):
                return str(value)
        return ""

    if isinstance(item, str | int | float):
        return str(item)
    return ""


def _coerce_ocr_pieces(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _joined_ocr_text(runtime, *, capture=None) -> str:
    ocr = getattr(runtime, "ocr", None)
    if not callable(ocr):
        return ""
    try:
        pieces = _coerce_ocr_pieces(ocr(capture=capture))
    except Exception:
        return ""
    return "".join(_read_ocr_piece(piece).strip() for piece in pieces or [])


@dataclass
class BattleObservation:
    runtime: object
    detected_stage: object
    page_ocr_pieces: list[object]
    page_text: str
    frame: Image.Image | None = None
    _headline_text: str | None = None
    _round_text: str | None = None
    _stats_text: str | None = None

    def headline_text(self) -> str:
        if self._headline_text is None:
            self._headline_text = _joined_ocr_text(self.runtime, capture=SETTLEMENT_HEADLINE_CAPTURE)
        return self._headline_text

    def round_text(self) -> str:
        if self._round_text is None:
            self._round_text = _joined_ocr_text(self.runtime, capture=SETTLEMENT_ROUND_CAPTURE)
        return self._round_text

    def stats_text(self) -> str:
        if self._stats_text is None:
            self._stats_text = _joined_ocr_text(self.runtime, capture=SETTLEMENT_STATS_CAPTURE)
        return self._stats_text


def observe_cw_battle_page(runtime, *, detected_stage: object) -> BattleObservation:
    ocr = getattr(runtime, "ocr", None)
    pieces: list[object] = []
    frame = None
    if callable(getattr(runtime, "ocr_image", None)) and callable(getattr(runtime, "screenshot", None)):
        image = runtime.screenshot()
        if isinstance(image, Image.Image):
            frame = image
        elif isinstance(image, (bytes, bytearray)):
            with Image.open(BytesIO(image)) as decoded:
                frame = decoded.convert("RGB")
    if frame is not None:
        pieces = _coerce_ocr_pieces(runtime.ocr_image(frame, ocr=OcrRequestConfig(lang="ch", ocr_mode="fast")))
    elif callable(ocr):
        try:
            pieces = _coerce_ocr_pieces(ocr())
        except TypeError:
            pieces = _coerce_ocr_pieces(ocr(capture=None))
        except Exception:
            pieces = []
    page_text = "".join(_read_ocr_piece(piece).strip() for piece in pieces)
    return BattleObservation(
        runtime=runtime,
        detected_stage=detected_stage,
        page_ocr_pieces=pieces,
        page_text=page_text,
        frame=frame,
    )


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _page_text(runtime, observation: BattleObservation | None = None) -> str:
    if observation is not None:
        return observation.page_text
    return _joined_ocr_text(runtime)


def _piece_center_in_region(piece: object, frame: Image.Image, region: tuple[float, float, float, float]) -> bool:
    box = _read_ocr_box(piece)
    if box is None:
        return False
    x = (box[0] + box[2]) / (2 * frame.width)
    y = (box[1] + box[3]) / (2 * frame.height)
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def _battle_screen_evidence(runtime, observation: BattleObservation) -> tuple[str | None, bool]:
    """Read positioned transition and overflow labels, including same-frame crops."""
    frame = observation.frame
    if frame is None:
        return None, False
    pieces = list(observation.page_ocr_pieces)
    if callable(getattr(runtime, "ocr_image", None)):
        for region in ((700, 320, 1220, 405), (450, 510, 1470, 565)):
            box = tuple(round(v * (frame.width / 1920 if i % 2 == 0 else frame.height / 1080))
                        for i, v in enumerate(region))
            try:
                rows = _coerce_ocr_pieces(runtime.ocr_image(frame.crop(box), ocr=OcrRequestConfig(lang="ch", ocr_mode="high")))
            except Exception:
                continue
            for row in rows:
                bounds = _read_ocr_box(row)
                if bounds is not None:
                    x1, y1, x2, y2 = bounds
                    pieces.append({"text": _read_ocr_piece(row), "box": {"left": x1 + box[0], "top": y1 + box[1],
                                   "width": x2 - x1, "height": y2 - y1}})
    banner = [p for p in pieces if _piece_center_in_region(p, frame, (.36, .29, .64, .39))]
    banner_text = "".join(_read_ocr_piece(p).replace(" ", "") for p in banner)
    match = re.search(r"战斗阶段\D{0,4}([1-3]-\d{1,2})", banner_text)
    overflow = any("备战席已满" in _read_ocr_piece(p).replace(" ", "") and
                   "出售角色或提升等级" in _read_ocr_piece(p).replace(" ", "") and
                   _piece_center_in_region(p, frame, (.24, .47, .76, .53)) for p in pieces)
    return (match.group(1) if match else None), overflow


def _battle_start_point(observation: BattleObservation) -> tuple[int, int] | None:
    if observation.frame is None:
        return None
    for piece in observation.page_ocr_pieces:
        if _read_ocr_piece(piece).strip() in _BATTLE_START_KEYWORDS and any(
            _piece_center_in_region(piece, observation.frame, region)
            for region in ((0.30, 0.72, 0.70, 0.92), (0.86, 0.65, 1.0, 0.76))
        ):
            box = _read_ocr_box(piece)
            assert box is not None
            return (round((box[0] + box[2]) / 2 / observation.frame.width * _CW_REFERENCE_WIDTH),
                    round((box[1] + box[3]) / 2 / observation.frame.height * _CW_REFERENCE_HEIGHT))
    return None


def _has_battle_start(runtime, observation: BattleObservation | None = None) -> bool:
    if observation is not None and observation.frame is not None:
        return _battle_start_point(observation) is not None
    return _contains_any(_page_text(runtime, observation), _BATTLE_START_KEYWORDS)


def has_cw_preparation_board(observation: BattleObservation) -> bool:
    frame = observation.frame
    return frame is not None and any(
        "备战阶段" in _read_ocr_piece(piece) and _piece_center_in_region(piece, frame, (0.15, 0.0, 0.35, 0.12))
        for piece in observation.page_ocr_pieces
    )


def _is_special_preparation(observation: BattleObservation) -> bool:
    # Both the board heading and the right-hand control must come from this frame.
    frame = observation.frame
    if frame is None:
        return False
    pieces = observation.page_ocr_pieces
    board = has_cw_preparation_board(observation)
    special = any("幕后茶歇" in _read_ocr_piece(p) and _piece_center_in_region(p, frame, (0.80, 0.60, 1.0, 0.80)) for p in pieces)
    return board and special


def _special_preparation_header(observation: BattleObservation) -> dict[str, object]:
    frame = observation.frame
    assert frame is not None
    entries = []
    for piece in observation.page_ocr_pieces:
        box = _read_ocr_box(piece)
        if box is not None:
            box = (box[0] * _CW_REFERENCE_WIDTH / frame.width, box[1] * _CW_REFERENCE_HEIGHT / frame.height,
                   box[2] * _CW_REFERENCE_WIDTH / frame.width, box[3] * _CW_REFERENCE_HEIGHT / frame.height)
        entries.append({"text": _read_ocr_piece(piece).strip(), "bounds": box})
    return project_cw_preparation_header({"value": "preparation", "entries": entries})


def _has_settlement_entry(runtime, observation: BattleObservation | None = None) -> bool:
    if observation is not None:
        page_text = observation.page_text
        if "挑战结束" in page_text and "继续挑战" in page_text:
            return True
        if _contains_any(page_text, _SETTLEMENT_ENTRY_KEYWORDS):
            return True
        headline = observation.headline_text()
        if _contains_any(headline, ("挑战成功", "挑战失败", "继续挑战")):
            return True
        if "挑战结束" in headline and "继续挑战" in page_text:
            return True
        return False

    headline = _joined_ocr_text(runtime, capture=SETTLEMENT_HEADLINE_CAPTURE)
    if _contains_any(headline, _SETTLEMENT_ENTRY_KEYWORDS):
        return True
    page_text = _joined_ocr_text(runtime)
    if "挑战结束" in headline and "继续挑战" in page_text:
        return True
    if _contains_any(page_text, _SETTLEMENT_ENTRY_KEYWORDS):
        return True
    return False


def _has_settlement_continue(observation: BattleObservation) -> bool:
    if "继续挑战" in observation.page_text:
        return True
    if observation.frame is None or "小队生命值" not in observation.page_text:
        return False
    frame = observation.frame.resize(REPORT_FRAME_SIZE, Image.Resampling.BILINEAR)
    return any("继续挑战" in _read_ocr_piece(piece) for piece in _ocr_region(observation.runtime, frame, CONTINUE_BUTTON))


def _is_settlement_animation(observation: BattleObservation) -> bool:
    return (
        (
            "小队生命值结算说明" in observation.page_text
            or ("点击空白加速" in observation.page_text
                and _contains_any(observation.page_text, ("挑战进度", "挑战结束", "挑战成功")))
        )
        and not _has_settlement_continue(observation)
    )


def _is_continue_only_settlement_entry(runtime, observation: BattleObservation | None = None) -> bool:
    if observation is not None:
        headline = observation.headline_text() or observation.page_text
    else:
        headline = _joined_ocr_text(runtime, capture=SETTLEMENT_HEADLINE_CAPTURE) or _joined_ocr_text(runtime)
    return "继续挑战" in headline and "挑战成功" not in headline and "挑战失败" not in headline


def _has_settlement_followup(runtime, observation: BattleObservation | None = None) -> bool:
    return _contains_any(_page_text(runtime, observation), _SETTLEMENT_FOLLOWUP_KEYWORDS)


def _has_game_over(runtime, observation: BattleObservation | None = None) -> bool:
    return _contains_any(_page_text(runtime, observation), _GAME_OVER_KEYWORDS)


def _extract_game_over_hp(page_text: str) -> int | None:
    if "小队生命值" not in page_text:
        hp_match = re.search(r"(?:生命值|生命|血量|HP)\D{0,24}(\d+)", page_text, re.IGNORECASE)
        if hp_match is not None:
            return int(hp_match.group(1))
        return None
    before_economy = page_text.split("总经济", 1)[0]
    digits = re.findall(r"\d+", before_economy.split("小队生命值", 1)[1])
    if len(digits) == 1:
        return int(digits[0])
    return None


def _read_game_over_summary(runtime, observation: BattleObservation | None = None) -> dict[str, object]:
    page_text = _page_text(runtime, observation)
    summary: dict[str, object] = {
        "stage": "game_over",
        "game_over": True,
    }
    hp_value = _extract_game_over_hp(page_text)
    if hp_value is None and observation is not None and observation.frame is not None and "对局未完成" in page_text:
        frame = observation.frame.resize(REPORT_FRAME_SIZE, Image.Resampling.LANCZOS)
        hp_value = BattleReport._read_hp_digits(runtime, frame, GAME_OVER_HP_REGION)
    if "对局未完成" in page_text or "挑战失败" in page_text or "返回货币战争" in page_text:
        if "对局未完成" in page_text:
            settle_text = "对局未完成"
        elif "挑战失败" in page_text:
            settle_text = "挑战失败"
        else:
            settle_text = "返回货币战争"
        summary.update(
            {
                "result": "lose",
                "settle_text": settle_text,
                "end_reason": "global_battle_failed",
                "restart_candidate": True,
            }
        )
    elif "游戏结束" in page_text or "本局结束" in page_text:
        summary.update({"settle_text": "游戏结束", "end_reason": "game_over"})

    round_match = re.search(r"(\d+-\d+)", page_text)
    if round_match is not None:
        summary["round"] = round_match.group(1)
    if hp_value is not None:
        summary["hp"] = hp_value
    score_match = re.search(r"(?:积分|score)\D*(\d+)", page_text, re.IGNORECASE)
    if score_match is not None:
        summary["score"] = int(score_match.group(1))
    promotion_match = re.search(r"晋升点\D*([\d+]+)", page_text)
    if promotion_match is not None:
        summary["promotion_points"] = promotion_match.group(1)
    return summary


def _has_positive_battle_anchor(runtime, observation: BattleObservation | None = None) -> bool:
    text = _page_text(runtime, observation)
    return _contains_any(text, _BATTLE_PROGRESS_KEYWORDS) or bool(
        observation is not None and observation.frame is not None
        and (
            _contains_any(text, ("受到致命攻击", "战斗开始", "敌方行动中", "我方行动中"))
            or re.search(r"\d{1,2}-\d{1,2}\s*\d{1,3}[%％]", text)
        )
    )


def _classify_stage_without_ocr(detected_stage: object) -> str | None:
    if detected_stage == "layer_transition":
        return "layer_transition"
    if detected_stage in STABLE_BATTLE_RETURN_STAGES:
        return "stable_stage"
    return None


def _read_settle_headline(runtime, observation: BattleObservation | None = None) -> tuple[str, str]:
    if observation is not None:
        headline = observation.page_text
        if not _contains_any(headline, ("挑战成功", "挑战失败", "挑战结束")):
            headline = observation.headline_text()
    else:
        headline = _joined_ocr_text(runtime, capture=SETTLEMENT_HEADLINE_CAPTURE) or _joined_ocr_text(runtime)
    if "挑战成功" in headline:
        return "win", "挑战成功"
    if "挑战失败" in headline:
        return "lose", "挑战失败"
    if "挑战结束" in headline:
        return "ended", "挑战结束"
    raise TrailError("CW_SETTLEMENT_UNREADABLE", "无法识别货币战争结算结果")


def _read_optional_settlement_metrics(runtime, observation: BattleObservation | None = None) -> dict[str, str | int]:
    if observation is not None:
        round_text = observation.round_text()
        stats_text = observation.stats_text()
    else:
        round_text = _joined_ocr_text(runtime, capture=SETTLEMENT_ROUND_CAPTURE)
        stats_text = _joined_ocr_text(runtime, capture=SETTLEMENT_STATS_CAPTURE)
    combined_text = f"{round_text} {stats_text}".strip()
    metrics: dict[str, str | int] = {}

    page_text = observation.page_text if observation is not None else _page_text(runtime)
    round_match = re.search(r"(?<!\d)([1-3]-[1-9]\d?)\D{0,3}(?:战斗|奖励|遭遇|首领)", page_text)
    if round_match is None:
        round_match = re.search(r"(?<!\d)([1-3]-[1-9]\d?)(?!\d)", round_text or combined_text)
    if round_match is not None:
        metrics["round"] = round_match.group(1)

    for key, pattern in (
        ("coins", r"(?:金币|钱币|coin(?:s)?)\D*(\d+)"),
        ("exp", r"(?:经验|EXP|exp)\D*(\d+)"),
    ):
        match = re.search(pattern, combined_text)
        if match is not None:
            metrics[key] = int(match.group(1))
    if observation is not None and observation.frame is not None:
        reward = bool(re.search(r"[1-3]-[1-9]\d?\D{0,3}奖励", page_text) or "奖励" in round_text)
        hp = BattleReport.read_labeled_hp(runtime, observation.frame) if reward or "挑战成功" in page_text else None
        if hp is None and not reward and "小队生命值" in page_text:
            has_details = "挑战结束" in page_text or "小队生命值结算说明" in page_text
            hp = BattleReport.read_final_hp(runtime, observation.frame, has_details=has_details)
        if hp is not None:
            metrics["hp"] = hp
    else:
        hp = _unambiguous_hp(combined_text)
        if hp is not None:
            metrics["hp"] = hp

    return metrics


def _unambiguous_hp(text: str) -> int | None:
    match = re.search(r"(?:小队生命值|生命|血量|HP)\s*(\d{1,3})(?!\d)", text, re.IGNORECASE)
    if match and int(match[1]) <= 100:
        return int(match[1])
    return None


def classify_cw_battle_page(
    runtime,
    *,
    session: SessionModel,
    detected_stage: object = _DETECTED_STAGE_UNSET,
    observation: BattleObservation | None = None,
) -> str:
    del session

    detected_state = _classify_stage_without_ocr(detected_stage)
    if detected_state is not None:
        return detected_state

    if observation is None:
        observation = observe_cw_battle_page(runtime, detected_stage=detected_stage)

    if _is_settlement_animation(observation):
        return "settle_animation"
    banner_node, overflow = _battle_screen_evidence(runtime, observation)
    if overflow:
        return "reserve_overflow"
    if banner_node:
        return "battle_transition"
    if observation.frame is not None:
        # A decision overlay can coexist with the board's underlying action.
        normalized_pieces = []
        for piece in observation.page_ocr_pieces:
            box = _read_ocr_box(piece)
            if box is not None:
                box = {"left": box[0] * _CW_REFERENCE_WIDTH / observation.frame.width,
                       "top": box[1] * _CW_REFERENCE_HEIGHT / observation.frame.height,
                       "width": (box[2] - box[0]) * _CW_REFERENCE_WIDTH / observation.frame.width,
                       "height": (box[3] - box[1]) * _CW_REFERENCE_HEIGHT / observation.frame.height}
            normalized_pieces.append({"text": _read_ocr_piece(piece), "box": box})
        if _season_two_decision_stage(normalized_pieces):
            return "unknown"
        if any("等级提升" in _read_ocr_piece(piece) and _piece_center_in_region(
            piece, observation.frame, (0.35, 0.12, 0.65, 0.30)
        ) for piece in observation.page_ocr_pieces):
            return "preparation"
    if _is_special_preparation(observation):
        return "special_preparation"
    if _has_battle_start(runtime, observation):
        return "battle_start"
    if _has_game_over(runtime, observation):
        if _has_settlement_followup(runtime, observation):
            return "game_over_followup"
        return "game_over"
    if _has_settlement_followup(runtime, observation):
        return "settle_followup"
    if _has_settlement_entry(runtime, observation):
        return "settle_entry"

    if detected_stage is _DETECTED_STAGE_UNSET:
        detected_stage = build_cw_stage_detector(runtime)()
    detected_state = _classify_stage_without_ocr(detected_stage)
    if detected_state is not None:
        return detected_state
    if detected_stage == "game_over":
        return "game_over"
    if detected_stage == "settle":
        return "settle_entry"
    if detected_stage == "preparation" or "备战阶段" in observation.page_text:
        return "preparation"
    if detected_stage == "layer_transition":
        return "layer_transition"
    if detected_stage in STABLE_BATTLE_RETURN_STAGES:
        return "stable_stage"

    if _has_positive_battle_anchor(runtime, observation):
        return "battle_progress"
    return "unknown"


def parse_cw_settlement_summary(runtime, observation: BattleObservation | None = None) -> dict[str, object]:
    if observation is None and callable(getattr(runtime, "ocr_image", None)):
        observation = observe_cw_battle_page(runtime, detected_stage="settle")
    result, settle_text = _read_settle_headline(runtime, observation=observation)
    summary: dict[str, object] = {
        "result": result,
        "settle_text": settle_text,
    }
    summary.update(_read_optional_settlement_metrics(runtime, observation=observation))
    return summary


def _collect_stable_hp_details(runtime, observation: BattleObservation) -> BattleObservation:
    if not callable(getattr(runtime, "drag_to", None)) or "小队生命值" not in observation.page_text:
        return observation
    if "小队生命值结算说明" in observation.page_text:
        return observation
    x, y = HP_DETAILS_POINT
    try:
        runtime.drag_to(x, y, x, y, duration=0.35)
    except TrailError:
        return observation
    for _ in range(3):
        sleep(0.2)
        details = observe_cw_battle_page(runtime, detected_stage="settle")
        if "小队生命值结算说明" in details.page_text:
            return details
    return observation


def _start_battle(runtime) -> None:
    build_cw_battle_starter(runtime)()


def _normalized_action_text(text: object) -> str:
    return "".join(str(text or "").split())


def _normalize_observation_piece(piece: object) -> dict[str, float | str] | None:
    if isinstance(piece, dict):
        text = str(piece.get("text") or "").strip()
        box = piece.get("box")
        if not text or not isinstance(box, dict):
            return None
        try:
            left = float(box["left"])
            top = float(box["top"])
            width = float(box["width"])
            height = float(box["height"])
        except (KeyError, TypeError, ValueError):
            return None
        return {"text": text, "left": left, "top": top, "width": width, "height": height}

    if isinstance(piece, (list, tuple)) and len(piece) >= 2:
        polygon = piece[0]
        text = str(piece[1] or "").strip()
        if not text or not isinstance(polygon, (list, tuple)):
            return None
        try:
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
        except (TypeError, ValueError, IndexError):
            return None
        if not xs or not ys:
            return None
        left = min(xs)
        top = min(ys)
        return {"text": text, "left": left, "top": top, "width": max(xs) - left, "height": max(ys) - top}

    return None


def _box_center(box: dict[str, float | str]) -> tuple[int, int]:
    return (
        int(float(box["left"]) + float(box["width"]) / 2.0),
        int(float(box["top"]) + float(box["height"]) / 2.0),
    )


def _is_in_action_ocr_region(point: tuple[int, int]) -> bool:
    x, y = point
    left = _CW_REFERENCE_WIDTH * _CW_ACTION_OCR_REGION["from_x"]
    right = _CW_REFERENCE_WIDTH * _CW_ACTION_OCR_REGION["to_x"]
    top = _CW_REFERENCE_HEIGHT * _CW_ACTION_OCR_REGION["from_y"]
    bottom = _CW_REFERENCE_HEIGHT * _CW_ACTION_OCR_REGION["to_y"]
    return left <= x <= right and top <= y <= bottom


def _find_action_button_box_from_observation(
    observation: BattleObservation,
    *,
    allowed_texts: set[str],
) -> dict[str, float | str] | None:
    normalized_allowed = {_normalized_action_text(text) for text in allowed_texts}
    for piece in observation.page_ocr_pieces:
        normalized_piece = _normalize_observation_piece(piece)
        if normalized_piece is None:
            continue
        if _normalized_action_text(normalized_piece["text"]) not in normalized_allowed:
            continue
        if not _is_in_action_ocr_region(_box_center(normalized_piece)):
            continue
        return normalized_piece
    return None


def _continue_after_settlement(runtime, observation: BattleObservation | None = None) -> None:
    if observation is not None:
        button = _find_action_button_box_from_observation(observation, allowed_texts={"继续挑战", "继续"})
        if button is not None:
            runtime.click_point(*_box_center(button))
            return
    build_cw_battle_continuer(runtime)()


def _advance_settlement_page(runtime, observation: BattleObservation | None = None) -> None:
    if observation is not None:
        button = _find_action_button_box_from_observation(observation, allowed_texts={"下一步", "下一页", "前往结算"})
        if button is not None:
            runtime.click_point(*_box_center(button))
            return
    build_cw_settle_continuer(runtime)()


def _advance_layer_transition(runtime) -> None:
    runtime.click_point(*LAYER_TRANSITION_CONTINUE_POINT)


def _wait_for_game_over_return_home(runtime) -> bool:
    home_template_visible = False
    wait_img = getattr(runtime, "wait_img", None)
    if callable(wait_img):
        try:
            if wait_img(
                _asset("entry.start"),
                timeout=GAME_OVER_RETURN_HOME_WAIT_SECONDS,
                interval=GAME_OVER_RETURN_HOME_WAIT_INTERVAL_SECONDS,
            ) is not None:
                home_template_visible = True
        except Exception:
            pass

    if not home_template_visible:
        page_text = _joined_ocr_text(runtime)
        if "开始" in page_text and "货币战争" in page_text and "入画奇航" in page_text:
            return True
        locate = getattr(runtime, "locate", None)
        if not callable(locate):
            return False
        for alias in ("entry.start", "entry.continue"):
            try:
                if locate(_asset(alias)) is not None:
                    home_template_visible = True
                    break
            except Exception:
                continue
    if not home_template_visible:
        return False

    page_text = _joined_ocr_text(runtime)
    return bool(page_text) and not _contains_any(page_text, GAME_OVER_RETURN_STILL_PAGE_KEYWORDS)


def _return_from_game_over(runtime, observation: BattleObservation | None = None) -> bool:
    if observation is not None:
        button = _find_action_button_box_from_observation(observation, allowed_texts={GAME_OVER_RETURN_TEXT})
        if button is not None:
            runtime.click_point(*_box_center(button))
            sleep(GAME_OVER_RETURN_SETTLE_SECONDS)
            return _wait_for_game_over_return_home(runtime)
    runtime.click_point(*GAME_OVER_RETURN_POINT)
    sleep(GAME_OVER_RETURN_SETTLE_SECONDS)
    return _wait_for_game_over_return_home(runtime)


def _has_game_over_return(runtime, observation: BattleObservation | None = None) -> bool:
    return GAME_OVER_RETURN_TEXT in _page_text(runtime, observation)


def _mark_game_over_return_home(session: SessionModel, *, returned_home: bool) -> None:
    if returned_home:
        ensure_cw_state(session)["entry"] = {"page": "home"}
    mark_cw_stage_stale(session)


def _set_completed_stage(session: SessionModel, *, stage: str) -> None:
    _replace_stage_fields(session, value=stage, stale=False)
    session.last_stage = {"scene": "cw", "value": stage}


def _persist_battle_round(session: SessionModel, summary: dict[str, object]) -> None:
    battle_round = summary.get("round")
    if isinstance(battle_round, str) and battle_round:
        ensure_cw_state(session).setdefault("metrics", {})["last_battle_round"] = battle_round


def _battle_resume_state(session: SessionModel) -> dict[str, object]:
    cw_state = ensure_cw_state(session)
    resume = cw_state.get("battle_resume")
    if isinstance(resume, dict):
        return resume
    resume = {}
    cw_state["battle_resume"] = resume
    return resume


def _seed_resume_in_battle(session: SessionModel) -> bool:
    return bool(_battle_resume_state(session).get("in_battle_hint"))


def _store_resume_in_battle(session: SessionModel, *, enabled: bool) -> None:
    resume = _battle_resume_state(session)
    if enabled:
        resume["in_battle_hint"] = True
        return
    resume.pop("in_battle_hint", None)


def _finalize_battle_result(
    session: SessionModel, result: dict[str, object], report: BattleReport | None,
) -> dict[str, object]:
    if report is not None:
        result = report.apply(result)
    _persist_battle_round(session, result)
    _store_resume_in_battle(
        session,
        enabled=result.get("status") == "in_progress" and result.get("in_battle") is True,
    )
    resume = _battle_resume_state(session)
    if result.get("status") == "completed":
        resume.pop("start_pending", None)
    if report is not None and result.get("status") == "in_progress":
        resume["report"] = report.state
    else:
        resume.pop("report", None)
    return result


def _timeout_battle_run(
    session: SessionModel,
    *,
    timeout: int | float,
    summary: dict[str, object],
    settle_chain_seen: bool,
    combat_seen: bool,
) -> dict[str, object]:
    mark_cw_stage_stale(session)
    if settle_chain_seen:
        return {
            **summary,
            "status": "in_progress",
            "stage": "settle",
            "stale": True,
            "in_battle": False,
            "timeout_seconds": timeout,
        }
    return {
        **summary,
        "status": "in_progress",
        "stale": True,
        "in_battle": True if combat_seen else None,
        "timeout_seconds": timeout,
    }


def _unknown_battle_state(*, state: str, detail: object | None = None) -> TrailError:
    if isinstance(detail, str) and detail:
        message = f"无法识别货币战争 battle.run 状态: {detail}"
    else:
        message = f"无法识别货币战争 battle.run 状态: {state}"
    return TrailError("CW_BATTLE_STATE_UNKNOWN", message)


def _mark_cancel_side_effect_applied(cancellation_token) -> None:
    if cancellation_token is not None:
        setattr(cancellation_token, "side_effect_stage", "side_effect_applied")


def run_cw_battle(
    session: SessionModel, *, runtime, timeout: int | float, cancellation_token=None, stop_at_plane: bool = False,
) -> dict[str, object]:
    deadline = monotonic() + timeout
    started_chain = False
    start_attempted = False
    resume_in_battle = _seed_resume_in_battle(session)
    start_pending = bool(_battle_resume_state(session).get("start_pending"))
    settle_chain_seen = False
    summary: dict[str, object] = {}
    report_enabled = callable(getattr(runtime, "ocr_image", None)) and callable(getattr(runtime, "screenshot", None))
    previous_report = _battle_resume_state(session).get("report")
    report = BattleReport(previous_report if isinstance(previous_report, dict) else None) if report_enabled else None
    in_combat = False
    combat_seen = False
    transition_seen = False
    transition_frame = None
    stage_detector = build_cw_stage_detector(runtime)
    if isinstance(previous_report, dict) and isinstance(previous_report.get("settlement"), dict):
        summary = dict(previous_report["settlement"])
        started_chain = True
        settle_chain_seen = True
    elif report is not None and report.state.get("waiting_for_settlement_animation"):
        started_chain = True
        settle_chain_seen = True

    try:
        while True:
            if cancellation_token is not None:
                cancellation_token.throw_if_cancelled()
            observation: BattleObservation | None = None
            detected_stage = None
            state = None
            if report is not None and (in_combat or report.state.get("waiting_for_settlement_animation")):
                observation = observe_cw_battle_page(runtime, detected_stage=None)
                hud = battle_hud_from_page(observation.page_ocr_pieces)
                if _is_settlement_animation(observation):
                    state = "settle_animation"
                elif _has_settlement_followup(runtime, observation) and "挑战进度" in observation.page_text:
                    state = "settle_followup"
                elif hud and hud[0] == report.state.get("round"):
                    state = "battle_progress"
            if state is None:
                detected_stage = stage_detector()
                state = _classify_stage_without_ocr(detected_stage)
            if cancellation_token is not None:
                cancellation_token.throw_if_cancelled()
            if state is None:
                if observation is None:
                    observation = observe_cw_battle_page(runtime, detected_stage=detected_stage)
                state = classify_cw_battle_page(
                    runtime,
                    session=session,
                    detected_stage=detected_stage,
                    observation=observation,
                )
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
            if report is not None and observation is not None and observation.frame is not None:
                if state in {"battle_progress", "unknown", "settle_animation"} and not settle_chain_seen:
                    report.observe(runtime, observation.frame, observation.page_ocr_pieces, phase="battle")
                if state == "settle_animation":
                    report.state["waiting_for_settlement_animation"] = True
                    report.observe(runtime, observation.frame, observation.page_ocr_pieces, phase="settlement")
            in_combat = state == "battle_progress"
            combat_seen = combat_seen or in_combat
            if state in {"battle_progress", "settle_animation", "settle_entry", "settle_followup", "game_over_followup", "game_over"}:
                if summary.get("stage") == "battle_transition":
                    summary.pop("stage")
                transition_seen = False
                transition_frame = None
            if state == "reserve_overflow":
                _battle_resume_state(session).pop("start_pending", None)
                _set_completed_stage(session, stage="preparation")
                node = _special_preparation_header(observation) if observation is not None else None
                blocked = {"status": "blocked", "reason": "reserve_overflow",
                    "stage": "preparation", "stale": False, "in_battle": False, "current_node": node}
                if observation is not None and observation.frame is not None:
                    blocked["_reconcile_frame"] = observation.frame
                return _finalize_battle_result(session, blocked, report)
            if state == "battle_transition":
                transition_seen = True
                transition_frame = observation.frame if observation is not None else None
                _battle_resume_state(session)["start_pending"] = True
                start_pending = True
                if observation is not None:
                    node, _ = _battle_screen_evidence(runtime, observation)
                    if node:
                        summary["round"] = node
                        summary["stage"] = "battle_transition"
            if state in {"battle_start", "preparation"} and (combat_seen or settle_chain_seen or resume_in_battle or
                    (started_chain and not (start_pending or start_attempted or transition_seen))):
                completed_stage = detected_stage if detected_stage == "preparation" else "preparation"
                _set_completed_stage(session, stage=completed_stage)
                return _finalize_battle_result(
                    session,
                    {
                        **summary,
                        "status": "completed",
                        "stage": completed_stage,
                        "stale": False,
                        "in_battle": False,
                    },
                    report,
                )
            if state in {"battle_start", "preparation"} and (start_pending or start_attempted or transition_seen):
                state = "unknown"
            if state in {"preparation", "special_preparation"}:
                if state == "special_preparation":
                    _battle_resume_state(session).pop("start_pending", None)
                _set_completed_stage(session, stage="preparation")
                observed = {**summary, "status": "observed", "stage": "preparation", "stale": False, "in_battle": False}
                if state == "special_preparation" and observation is not None:
                    observed.update(current_node=_special_preparation_header(observation), next_action="cw.stage.read")
                return _finalize_battle_result(session, observed, report)
            if state in {"battle_progress", "settle_animation", "settle_entry", "settle_followup", "layer_transition"}:
                _battle_resume_state(session).pop("start_pending", None)
                started_chain = True
            if state == "game_over_followup":
                started_chain = True
            if state in {"settle_animation", "settle_entry", "settle_followup", "game_over_followup"}:
                settle_chain_seen = True

            if state == "stable_stage":
                stage = detected_stage
                if stage not in STABLE_BATTLE_RETURN_STAGES:
                    raise _unknown_battle_state(state=state, detail=stage)
                _set_completed_stage(session, stage=stage)
                return _finalize_battle_result(
                    session,
                    {
                        **summary,
                        "status": "completed",
                        "stage": stage,
                        "stale": False,
                        "in_battle": False,
                    },
                    report,
                )

            if state == "game_over":
                has_return_button = _has_game_over_return(runtime, observation=observation)
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                if observation is not None:
                    game_over_summary = _read_game_over_summary(runtime, observation=observation)
                    if "result" not in summary or game_over_summary.get("result") == "lose" or has_return_button:
                        summary.update(game_over_summary)
                if has_return_button:
                    returned_home = _return_from_game_over(runtime, observation=observation)
                    _mark_cancel_side_effect_applied(cancellation_token)
                    if cancellation_token is not None:
                        cancellation_token.throw_if_cancelled()
                    summary["returned_home"] = returned_home
                    _mark_game_over_return_home(session, returned_home=returned_home)
                else:
                    _set_completed_stage(session, stage="game_over")
                return _finalize_battle_result(
                    session,
                    {
                        **summary,
                        "status": "completed",
                        "stage": "game_over",
                        "stale": False,
                        "in_battle": False,
                    },
                    report,
                )

            if state == "unknown" and not (started_chain or start_attempted or start_pending or resume_in_battle):
                raise _unknown_battle_state(state=state)

            if state == "layer_transition" and stop_at_plane:
                _set_completed_stage(session, stage="layer_transition")
                return _finalize_battle_result(
                    session,
                    {**summary, "status": "completed", "stage": "layer_transition", "stale": False, "in_battle": False},
                    report,
                )

            remaining = deadline - monotonic()
            if remaining <= 0:
                if state == "layer_transition":
                    mark_cw_stage_stale(session)
                    return _finalize_battle_result(
                        session,
                        {
                            **summary,
                            "status": "in_progress",
                            "stage": "layer_transition",
                            "stale": True,
                            "in_battle": False,
                            "timeout_seconds": timeout,
                        },
                        report,
                    )
                timed_out = _timeout_battle_run(
                        session,
                        timeout=timeout,
                        summary=summary,
                        settle_chain_seen=settle_chain_seen,
                        combat_seen=combat_seen,
                    )
                if transition_seen and not combat_seen and transition_frame is not None:
                    timed_out["_reconcile_frame"] = transition_frame
                return _finalize_battle_result(session, timed_out, report)

            if state == "battle_start":
                if start_attempted:
                    sleep(min(CW_BATTLE_RUN_POLL_INTERVAL_SECONDS, remaining))
                    continue
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                if observation is not None and observation.frame is not None:
                    point = _battle_start_point(observation)
                    if point is None:
                        raise _unknown_battle_state(state=state)
                    runtime.click_point(*point)
                    _fail_if_battle_team_count_confirm_dialog(runtime)
                else:
                    _start_battle(runtime)
                _battle_resume_state(session)["start_pending"] = True
                start_pending = True
                _mark_cancel_side_effect_applied(cancellation_token)
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                start_attempted = True
                continue

            if state in {"battle_progress", "battle_transition", "settle_animation"}:
                sleep(min(CW_BATTLE_RUN_POLL_INTERVAL_SECONDS, remaining))
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                continue

            if state == "settle_entry":
                if report is not None and observation is not None and observation.frame is not None:
                    stable_settlement = _has_settlement_continue(observation)
                    if not stable_settlement and report.state.get("waiting_for_settlement_animation"):
                        sleep(min(CW_BATTLE_RUN_POLL_INTERVAL_SECONDS, remaining))
                        continue
                    rows = report.state.get("team_hp_breakdown")
                    should_read_details = "挑战结束" in observation.page_text or "小队生命值结算说明" in observation.page_text
                    details = observation
                    if (
                        stable_settlement and should_read_details and not report.state.get("settlement_details_attempted")
                        and (not rows or any(row["delta"] is None for row in rows))
                    ):
                        report.state["settlement_details_attempted"] = True
                        details = _collect_stable_hp_details(runtime, observation)
                    if details.frame is not None:
                        report.observe(
                            runtime, details.frame, details.page_ocr_pieces,
                            phase="settlement", stable_settlement=stable_settlement,
                        )
                try:
                    summary = parse_cw_settlement_summary(runtime, observation=observation)
                except TrailError as error:
                    if error.code != "CW_SETTLEMENT_UNREADABLE" or not _is_continue_only_settlement_entry(
                        runtime,
                        observation=observation,
                    ):
                        raise
                if report is not None and summary:
                    report.state["settlement"] = summary
                _continue_after_settlement(runtime, observation=observation)
                _mark_cancel_side_effect_applied(cancellation_token)
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                continue

            if state == "settle_followup":
                if report is not None and observation is not None and observation.frame is not None:
                    report.observe(runtime, observation.frame, observation.page_ocr_pieces, phase="settlement")
                _advance_settlement_page(runtime, observation=observation)
                _mark_cancel_side_effect_applied(cancellation_token)
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                continue

            if state == "game_over_followup":
                if observation is not None:
                    summary.update(_read_game_over_summary(runtime, observation=observation))
                _advance_settlement_page(runtime, observation=observation)
                _mark_cancel_side_effect_applied(cancellation_token)
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                continue

            if state == "layer_transition":
                _advance_layer_transition(runtime)
                _mark_cancel_side_effect_applied(cancellation_token)
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                continue

            if state == "unknown":
                sleep(min(CW_BATTLE_RUN_POLL_INTERVAL_SECONDS, remaining))
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                continue

            raise _unknown_battle_state(state=state)
    except Exception:
        _store_resume_in_battle(session, enabled=False)
        _battle_resume_state(session).pop("report", None)
        raise
