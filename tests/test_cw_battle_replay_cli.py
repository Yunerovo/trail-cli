from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from trail.cli import app
from trail.scenes.cw import battle_replay


def _piece(text: str, x: int = 0, y: int = 0, width: int = 80, height: int = 20):
    return {"text": text, "box": {"left": x, "top": y, "width": width, "height": height}}


def _video(path: Path, shades: list[int]) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 8.0, (160, 90))
    assert writer.isOpened(), "OpenCV MJPG video writer unavailable"
    try:
        for shade in shades:
            for _ in range(2):
                writer.write(np.full((90, 160, 3), shade, dtype=np.uint8))
    finally:
        writer.release()
    return path


@pytest.fixture
def fake_video_ocr(monkeypatch):
    # Test real video writing, decoding, sampling and crop selection; stub only OCR.
    monkeypatch.setattr(battle_replay._ReplayRuntime, "__init__", lambda self: None)
    def ocr(self, image, *, ocr=None):
        shade = image.getpixel((image.width // 2, image.height // 2))[0]
        if image.size == (1920, 1080):
            if shade < 80:
                return [_piece("2-3 100%", 650, 10), _piece("受到致命攻击，战斗倒计时减少！"),
                        _piece("-12", 160, 110)]
            if shade < 160:
                return [_piece("2-3 64%", 650, 10), _piece("受到致命攻击，战斗倒计时减少！")]
            if shade < 230:
                return [_piece("挑战成功"), _piece("继续挑战"), _piece("小队生命值"),
                        _piece("小队生命值结算说明")]
            return []
        if 160 <= shade < 230 and image.size == (510, 177):
            return [_piece("小队生命值82")]
        if 160 <= shade < 230 and image.size == (608, 244):
            return [_piece("小队生命值结算说明", 0, 0), _piece("战斗伤害", 0, 40),
                    _piece("-8", 500, 40)]
        return []
    monkeypatch.setattr(battle_replay._ReplayRuntime, "ocr_image", ocr)


def _run(cli_runner, path: Path, *, structured: bool = False):
    args = (["--format", "yaml"] if structured else []) + ["cw", "battle", "replay", str(path)]
    return cli_runner.invoke(app, args)


def _yaml_after_text(output: str):
    lines = output.splitlines()
    offset = next(i for i, line in enumerate(lines) if line.startswith("status:"))
    return yaml.safe_load("\n".join(lines[offset:]))


@pytest.mark.parametrize("structured", [False, True])
def test_replay_complete_video_cli(tmp_path, cli_runner, fake_video_ocr, structured):
    path = _video(tmp_path / "complete.avi", [40, 210])
    result = _run(cli_runner, path, structured=structured)
    assert result.exit_code == 0, result.output
    assert "ok cw.battle.replay status=completed result=win" in result.output
    assert "round=2-3 hp=82" in result.output
    assert "progress_pct=100 minimum_progress_met=1" in result.output
    lines = result.output.splitlines()
    assert any("kind=action_value_penalty" in line and "delta=-12" in line for line in lines)
    assert any("kind=team_hp_change" in line and "label=战斗伤害" in line and "delta=-8" in line for line in lines)
    if structured:
        data = _yaml_after_text(result.output)
        assert (data["status"], data["result"], data["round"], data["hp"]) == ("completed", "win", "2-3", 82)
        assert data["battle_report"]["progress_pct"] == 100
        assert data["battle_report"]["minimum_progress_met"] is True
        assert data["battle_report"]["action_value_penalties"][0]["delta"] == -12
        assert data["battle_report"]["team_hp_breakdown"] == [{"label": "战斗伤害", "delta": -8}]


@pytest.mark.parametrize("structured", [False, True])
def test_replay_truncated_video_cli(tmp_path, cli_runner, fake_video_ocr, structured):
    path = _video(tmp_path / "partial.avi", [125])
    result = _run(cli_runner, path, structured=structured)
    assert result.exit_code == 0, result.output
    assert "ok cw.battle.replay status=partial" in result.output
    assert "round=2-3" in result.output and "result=" not in result.output and " hp=" not in result.output
    assert "progress_pct=64 minimum_progress_met=unknown" in result.output
    assert any("kind=action_value_penalty" in line and "delta=unknown" in line for line in result.output.splitlines())
    assert "hp_breakdown_items=unknown" in result.output
    if structured:
        data = _yaml_after_text(result.output)
        assert data["status"] == "partial" and data["round"] == "2-3"
        assert "result" not in data and "hp" not in data
        assert data["battle_report"]["progress_pct"] == 64
        assert data["battle_report"]["minimum_progress_met"] is None
        assert data["battle_report"]["action_value_penalties"][0]["delta"] is None
        assert data["battle_report"]["team_hp_breakdown"] is None


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("corrupt", [True, False])
def test_replay_unreadable_video_cli(tmp_path, cli_runner, fake_video_ocr, corrupt, structured):
    path = tmp_path / "unreadable.avi"
    if corrupt:
        path.write_bytes(b"not a video")
    else:
        _video(path, [250])
    result = _run(cli_runner, path, structured=structured)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "fail cw.battle.replay code=CW_BATTLE_REPLAY_UNREADABLE"
    if structured:
        assert result.output.splitlines()[-1] == "{}"
    assert ("无法打开战斗录像" if corrupt else "录像中未识别到货币战争战斗或结算") in result.output
    assert "battle_report" not in result.output and "progress_pct=" not in result.output
    assert "shot " not in result.output and "recover " not in result.output
