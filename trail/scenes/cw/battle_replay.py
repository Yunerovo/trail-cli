from __future__ import annotations

from pathlib import Path

import cv2
from PIL import Image

from trail.core.errors import TrailError
from trail.runtime.ocr_config import OcrRequestConfig
from trail.runtime.operator import RuntimeOperator, build_ocr_engine
from trail.scenes.cw.battle import _has_settlement_continue, _is_settlement_animation, observe_cw_battle_page, parse_cw_settlement_summary
from trail.scenes.cw.battle_report import BattleReport


class _ReplayRuntime:
    def __init__(self) -> None:
        self._ocr = RuntimeOperator(None, None, build_ocr_engine(), None)
        self.frame: Image.Image

    def screenshot(self):
        return self.frame

    def ocr_image(self, image, *, ocr=None):
        return self._ocr.ocr_image(image, ocr=ocr)

    def ocr(self, *, capture=None):
        image = self.frame
        if capture is not None:
            width, height = image.size
            image = image.crop((
                round(width * capture["from_x"]), round(height * capture["from_y"]),
                round(width * capture["to_x"]), round(height * capture["to_y"]),
            ))
        return self.ocr_image(image, ocr=OcrRequestConfig(lang="ch", ocr_mode="high"))


def _frame_image(frame) -> Image.Image:
    height, width = frame.shape[:2]
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    # A clipped lower edge can remove the UID while keeping the HUD coordinates.
    if 1.78 < width / height <= 1.85:
        padded = Image.new("RGB", (width, round(width * 9 / 16)))
        padded.paste(image, (0, 0))
        image = padded
    return image.resize((1920, 1080), Image.Resampling.BILINEAR)


def replay_cw_battle(path: Path) -> dict:
    """Read an exported single-battle recording in order, with no game input."""
    video = cv2.VideoCapture(str(path))
    if not video.isOpened():
        video.release()
        raise TrailError("CW_BATTLE_REPLAY_UNREADABLE", "无法打开战斗录像")
    runtime = _ReplayRuntime()
    report = BattleReport()
    summary = None
    fps = video.get(cv2.CAP_PROP_FPS)
    stride = max(1, round(fps / 4)) if fps > 0 else 1
    index = 0
    try:
        while video.grab():
            index += 1
            if (index - 1) % stride:
                continue
            available, frame = video.retrieve()
            if not available:
                continue
            runtime.frame = _frame_image(frame)
            observation = observe_cw_battle_page(runtime, detected_stage=None)
            text = observation.page_text
            if any(heading in text for heading in ("挑战成功", "挑战结束", "挑战失败")) and _has_settlement_continue(observation):
                report.observe(runtime, runtime.frame, observation.page_ocr_pieces, phase="settlement", stable_settlement=True)
                summary = parse_cw_settlement_summary(runtime, observation)
                rows = report.state.get("team_hp_breakdown")
                if "hp" in summary and (summary["result"] != "ended" or (rows and all(row["delta"] is not None for row in rows))):
                    break
            elif _is_settlement_animation(observation):
                report.observe(runtime, runtime.frame, observation.page_ocr_pieces, phase="battle")
                report.observe(runtime, runtime.frame, observation.page_ocr_pieces, phase="settlement")
            else:
                report.observe(runtime, runtime.frame, observation.page_ocr_pieces, phase="battle")
    finally:
        video.release()
    if summary is None and "round" not in report.state and "action_value_penalties" not in report.state:
        raise TrailError("CW_BATTLE_REPLAY_UNREADABLE", "录像中未识别到货币战争战斗或结算")
    return report.apply({"status": "completed" if summary else "partial", **(summary or {})})
