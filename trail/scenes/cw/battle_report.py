from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from PIL import Image, ImageOps

from trail.runtime.ocr_config import OcrRequestConfig
from trail.scenes.cw.stage import _read_ocr_box, _read_ocr_piece


REPORT_FRAME_SIZE = (1280, 720)
LETHAL_BANNER = (429, 102, 853, 171)
HUD_TEXT = (390, 1, 550, 33)
COUNTDOWN_DELTA = (104, 64, 178, 111)
SUMMARY_HP_WITH_DETAILS = (515, 318, 555, 361)
SUMMARY_HP_PLAIN = (535, 326, 580, 352)
REWARD_HP_WITH_DETAILS = (520, 321, 555, 361)
SUMMARY_HP_LABEL = (420, 309, 590, 368)
ANIMATED_HP_BREAKDOWN = (820, 320, 1147, 454)
ANIMATED_HP_INSURANCE_BREAKDOWN = (820, 294, 1147, 485)
SUMMARY_HP_BREAKDOWN = (606, 280, 910, 402)
SUMMARY_HP_BREAKDOWN_VALUES = (858, 308, 912, 400)
CONTINUE_BUTTON = (515, 569, 770, 627)
HP_DETAILS_POINT = (854, 510)
HIGH_OCR = OcrRequestConfig(lang="ch", ocr_mode="high")
_SIGNED_NUMBER = re.compile(r"^[+\-−–－＋]?\d{1,3}$")
_ROUND_PROGRESS = re.compile(r"(?<!\d)([1-3]-[1-9]\d?)\D{1,5}(\d{1,3})[%％]")
_MERGED_ROUND_PROGRESS = re.compile(r"(?<!\d)([1-3]-[1-9])(100|[1-9]?\d)[%％]")
_HP_BREAKDOWN_TITLE = "小队生命值结算说明"
_INCOMPLETE_DAMAGE = "未完成进度伤害"
_LETHAL_HIT_TEXT = "受到致命攻击，战斗倒计时减少！"
_NEGATIVE_DELTA = re.compile(r"^[-−–－]([1-9]\d{0,2})(?:[xX×])?$")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _ocr_region(runtime, frame: Image.Image, box: tuple[int, int, int, int], *, upscale: int = 1) -> list[Any]:
    image = frame.crop(box)
    if upscale > 1:
        image = image.resize((image.width * upscale, image.height * upscale), Image.Resampling.LANCZOS)
    return list(runtime.ocr_image(image, ocr=HIGH_OCR) or [])


def _parse_hud(pieces: list[Any]) -> tuple[str, int] | None:
    text = " ".join(_read_ocr_piece(piece).strip() for piece in pieces)
    match = _ROUND_PROGRESS.search(text) or _MERGED_ROUND_PROGRESS.search(text)
    if match and int(match[2]) <= 100:
        return match[1], int(match[2])
    return None


def battle_hud_from_page(pieces: list[Any]) -> tuple[str, int] | None:
    top_left = []
    for piece in pieces:
        box = _read_ocr_box(piece)
        if box and box[1] < 50 and 575 < (box[0] + box[2]) / 2 < 835:
            top_left.append((box[0], _read_ocr_piece(piece)))
    return _parse_hud([text for _, text in sorted(top_left)])


def _parse_hp_breakdown(
    pieces: list[Any], *, separate_values: list[tuple[str, tuple[float, float, float, float]]] | None = None,
    read_missing_value=None,
) -> list[dict[str, Any]] | None:
    fields = []
    heading_bottom = None
    for piece in pieces:
        box = _read_ocr_box(piece)
        if box is None:
            continue
        text = _compact(_read_ocr_piece(piece))
        if _HP_BREAKDOWN_TITLE in text:
            heading_bottom = box[3]
        fields.append((text, box))
    if heading_bottom is None:
        return None

    labels = []
    values = []
    for text, box in fields:
        if not heading_bottom < (box[1] + box[3]) / 2 <= heading_bottom + 270:
            continue
        if box[0] < 95 and re.fullmatch(r"[\u4e00-\u9fff·]{2,20}", text):
            labels.append((text, box))
        elif box[0] >= 435 and _SIGNED_NUMBER.fullmatch(text):
            values.append((text.translate(str.maketrans("−–－＋", "---+")), box))
    if separate_values is not None:
        values.extend(
            (text.translate(str.maketrans("−–－＋", "---+")), box)
            for text, box in separate_values if _SIGNED_NUMBER.fullmatch(text)
        )
    rows = []
    for label, label_box in sorted(labels, key=lambda row: row[1][1]):
        y = (label_box[1] + label_box[3]) / 2
        nearby = {value for value, box in values if abs((box[1] + box[3]) / 2 - y) <= 17}
        signed = {value for value in nearby if value[0] in "+-"}
        if signed:
            nearby = signed
        if not signed and nearby != {"0"} and read_missing_value is not None:
            recovered = read_missing_value(y)
            if recovered:
                nearby = recovered
        delta = None
        if len(nearby) == 1:
            value = next(iter(nearby))
            if value[0] in "+-" or value == "0":
                delta = int(value)
            elif label.endswith(("伤害", "扣除")):
                delta = -int(value)
        rows.append({"label": label, "delta": delta})
    return rows or None


def _read_stable_hp_row(runtime, frame: Image.Image, label_y: float) -> set[str]:
    _, y1, x2, _ = SUMMARY_HP_BREAKDOWN
    center = round(y1 + label_y / 2)
    crop = frame.crop((x2 - 47, center - 16, x2, center + 17)).convert("L")
    image = ImageOps.autocontrast(crop.resize((crop.width * 5, crop.height * 5), Image.Resampling.LANCZOS)).convert("RGB")
    return {
        text.translate(str.maketrans("−–－＋", "---+"))
        for piece in runtime.ocr_image(image, ocr=HIGH_OCR) or []
        if _SIGNED_NUMBER.fullmatch(text := _compact(_read_ocr_piece(piece)))
    }


class BattleReport:
    """Collect HUD facts and the settlement's signed HP changes for one node."""

    def __init__(self, previous: dict[str, Any] | None = None):
        self.state = deepcopy(previous) if previous else {}

    def observe(self, runtime, frame: Image.Image, page_pieces: list[Any], *, phase: str, stable_settlement: bool = False) -> None:
        text = _compact("".join(_read_ocr_piece(piece) for piece in page_pieces))
        if phase == "battle":
            self._observe_hud(runtime, frame, page_pieces, text)
        elif phase == "settlement":
            match = re.search(r"挑战进度\D{0,8}(100|[1-9]?\d)\s*[%％]", text)
            if match:
                self.state["settlement_progress_pct"] = int(match[1])
            # The same panel can appear during the animation and beside the final HP total.
            stable_settlement = stable_settlement or "继续挑战" in text
            if stable_settlement:
                region = SUMMARY_HP_BREAKDOWN
            else:
                region = ANIMATED_HP_INSURANCE_BREAKDOWN if "保险机制" in text else ANIMATED_HP_BREAKDOWN
            if stable_settlement or _HP_BREAKDOWN_TITLE in text or "挑战进度" in text:
                normalized = frame.resize(REPORT_FRAME_SIZE, Image.Resampling.BILINEAR)
                pieces = _ocr_region(runtime, normalized, region, upscale=2)
                separate_values = None
                if stable_settlement:
                    dy = 2 * (SUMMARY_HP_BREAKDOWN_VALUES[1] - region[1])
                    separate_values = [
                        (_compact(_read_ocr_piece(piece)), (box[0], box[1] + dy, box[2], box[3] + dy))
                        for piece in _ocr_region(runtime, normalized, SUMMARY_HP_BREAKDOWN_VALUES, upscale=2)
                        if (box := _read_ocr_box(piece)) is not None
                    ]
                breakdown = _parse_hp_breakdown(
                    pieces, separate_values=separate_values,
                    read_missing_value=(lambda y: _read_stable_hp_row(runtime, normalized, y)) if stable_settlement else None,
                )
                if breakdown:
                    previous = {row["label"]: row["delta"] for row in self.state.get("team_hp_breakdown", [])}
                    for row in breakdown:
                        if row["delta"] is not None or row["label"] not in previous:
                            previous[row["label"]] = row["delta"]
                    self.state["team_hp_breakdown"] = [
                        {"label": label, "delta": delta} for label, delta in previous.items()
                    ]

    def _observe_hud(self, runtime, frame, page_pieces, text):
        hud = battle_hud_from_page(page_pieces)
        if hud is None and (self.state.get("round") or ("%" in text and re.search(r"[1-3]-[1-9]", text))):
            normalized = frame.resize(REPORT_FRAME_SIZE, Image.Resampling.BILINEAR)
            hud = _parse_hud(_ocr_region(runtime, normalized, HUD_TEXT, upscale=3))
        if hud:
            round_text, progress = hud
            if self.state.get("round", round_text) == round_text:
                self.state["round"] = round_text
                self.state["latest_hud_progress_pct"] = progress
                self.state["progress_pct"] = max(progress, self.state.get("progress_pct", 0))
        has_banner = "受到致命攻击" in text and "倒计时减少" in text
        if not has_banner and ("致命攻击" in text or "倒计时减少" in text or self.state.get("lethal_banner_active")):
            normalized = frame.resize(REPORT_FRAME_SIZE, Image.Resampling.BILINEAR)
            banner = _compact("".join(_read_ocr_piece(piece) for piece in _ocr_region(runtime, normalized, LETHAL_BANNER)))
            has_banner = "受到致命攻击" in banner and "倒计时减少" in banner
        if has_banner:
            events = self.state.setdefault("action_value_penalties", [])
            previous_clear_frames = self.state.get("lethal_banner_clear_frames", 2)
            if not events or previous_clear_frames >= 2:
                events.append({"kind": "lethal_hit", "delta": None, "text": _LETHAL_HIT_TEXT})
            if events[-1]["delta"] is None:
                axis = [
                    piece for piece in page_pieces
                    if (box := _read_ocr_box(piece)) and 130 < box[0] < 275 and 95 < box[1] < 156
                ]
                delta = _read_negative_delta(axis)
                if delta is None:
                    normalized = frame.resize(REPORT_FRAME_SIZE, Image.Resampling.BILINEAR)
                    delta = _read_negative_delta(_ocr_region(runtime, normalized, COUNTDOWN_DELTA, upscale=2))
                events[-1]["delta"] = delta
        self.state["lethal_banner_active"] = has_banner
        self.state["lethal_banner_clear_frames"] = 0 if has_banner else min(
            2, self.state.get("lethal_banner_clear_frames", 0) + 1,
        )

    @staticmethod
    def read_final_hp(runtime, frame: Image.Image, *, has_details: bool) -> int | None:
        region = SUMMARY_HP_WITH_DETAILS if has_details else SUMMARY_HP_PLAIN
        normalized = frame.resize(REPORT_FRAME_SIZE, Image.Resampling.LANCZOS)
        if has_details:
            digits = BattleReport._read_hp_digits(runtime, normalized, region)
            return digits if digits is not None else BattleReport.read_labeled_hp(runtime, frame)
        return BattleReport._read_hp_digits(runtime, normalized, region)

    @staticmethod
    def _read_hp_digits(runtime, normalized: Image.Image, region: tuple[int, int, int, int]) -> int | None:
        for scale in (3, 2):
            pieces = _ocr_region(runtime, normalized, region, upscale=scale)
            text = _compact("".join(_read_ocr_piece(piece) for piece in pieces))
            if re.fullmatch(r"\d{1,3}", text) and 0 <= int(text) <= 100:
                return int(text)
        return None

    @staticmethod
    def read_labeled_hp(runtime, frame: Image.Image) -> int | None:
        normalized = frame.resize(REPORT_FRAME_SIZE, Image.Resampling.LANCZOS)
        # The HP label shifts horizontally with the number and the optional info icon.
        # Read the complete label first, and keep the icon outside the fallback crop.
        pieces = _ocr_region(runtime, normalized, SUMMARY_HP_LABEL, upscale=3)
        for piece in pieces:
            text = _compact(_read_ocr_piece(piece))
            match = re.fullmatch(r"小队生命值(100|[1-9]?\d)", text)
            if match:
                return int(match[1])
            if text.startswith("小队生命值"):
                hp = BattleReport._read_hp_digits(runtime, normalized, REWARD_HP_WITH_DETAILS)
                if hp is not None and text == f"小队生命值{hp}1":
                    return hp
        return None

    def apply(self, summary: dict[str, Any]) -> dict[str, Any]:
        summary = dict(summary)
        if "round" in self.state:
            summary["round"] = self.state["round"]
        breakdown = self.state.get("team_hp_breakdown")
        progress = self.state.get("progress_pct")
        events = self.state.get("action_value_penalties")
        failed_goal = any(
            row["label"] == _INCOMPLETE_DAMAGE and (row["delta"] is None or row["delta"] < 0)
            for row in breakdown or []
        )
        goal = False if failed_goal else None
        round_text = summary.get("round")
        if failed_goal:
            if summary.get("result") == "ended":
                summary["result"] = "lose"
        elif progress == 100 and round_text and round_text not in {"1-1", "1-2"}:
            goal = True
        summary["battle_report"] = {
            "progress_pct": progress,
            "latest_hud_progress_pct": self.state.get("latest_hud_progress_pct"),
            "latest_hud_source": "battle_hud" if self.state.get("latest_hud_progress_pct") is not None else None,
            "settlement_progress_pct": self.state.get("settlement_progress_pct"),
            "settlement_hp": summary.get("hp"),
            "minimum_progress_met": goal,
            "action_value_penalties": events,
            "team_hp_breakdown": breakdown,
        }
        return summary


def _read_negative_delta(pieces: list[Any]) -> int | None:
    values = {
        -int(match[1]) for piece in pieces if (match := _NEGATIVE_DELTA.fullmatch(_compact(_read_ocr_piece(piece))))
    }
    return values.pop() if len(values) == 1 else None
