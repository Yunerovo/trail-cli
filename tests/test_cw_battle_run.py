from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from trail.core.errors import TrailError
from trail.scenes.cw.models import ensure_cw_state
from trail.session.models import SessionModel


def load_cw_battle_module():
    try:
        return importlib.import_module("trail.scenes.cw.battle")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing trail.scenes.cw.battle: {exc}")


def _ocr_piece(text: str) -> dict[str, str]:
    return {"text": text}


def _ocr_button_piece(text: str, *, left: int, top: int, width: int = 120, height: int = 36) -> dict[str, object]:
    return {"text": text, "box": {"left": left, "top": top, "width": width, "height": height}}


CAPTURE_KEYS = ("from_x", "from_y", "to_x", "to_y")
HEADLINE_CAPTURE_KEY = (0.30, 0.10, 0.70, 0.28)
ROUND_CAPTURE_KEY = (0.22, 0.22, 0.48, 0.42)
STATS_CAPTURE_KEY = (0.20, 0.22, 0.60, 0.52)


def _capture_key(capture: object) -> object:
    if isinstance(capture, dict):
        unexpected_keys = set(capture) - set(CAPTURE_KEYS)
        if unexpected_keys:
            raise TypeError(f"unexpected capture keys: {sorted(unexpected_keys)}")
        return tuple(capture.get(key) for key in CAPTURE_KEYS)
    return capture


class FakeRuntime:
    def __init__(self, *, ocr_map: dict[object, list[object]] | None = None, locate_map: dict[str, object] | None = None):
        self.ocr_map = ocr_map or {}
        self.locate_map = locate_map or {}
        self.ocr_calls: list[object] = []
        self.locate_calls: list[str] = []

    def ocr(self, **kwargs) -> list[object]:
        capture = kwargs.get("capture")
        self.ocr_calls.append(_capture_key(capture))
        return list(self.ocr_map.get(_capture_key(capture), []))

    def locate(self, template: str, **kwargs):
        self.locate_calls.append(str(template))
        return self.locate_map.get(str(template))


class ScriptedBattleRuntime:
    def __init__(
        self,
        states: list[str],
        *,
        stable_stage: str = "shop",
        settle_text: str = "挑战成功",
        settle_headline: str | None = None,
        settle_page_texts: list[str] | None = None,
        settle_detect_stage: str | None = "settle",
        battle_start_text: str = "开始战斗",
        sleep_advances_from: tuple[str, ...] = ("battle_progress",),
    ):
        self.states = list(states)
        self.index = 0
        self.stable_stage = stable_stage
        self.settle_text = settle_text
        self.settle_headline = settle_headline or settle_text
        self.settle_page_texts = settle_page_texts
        self.settle_detect_stage = settle_detect_stage
        self.battle_start_text = battle_start_text
        self.sleep_advances_from = set(sleep_advances_from)
        self.actions: list[str] = []
        self.clicks: list[tuple[int, int]] = []

    @property
    def state(self) -> str:
        return self.states[min(self.index, len(self.states) - 1)]

    def _settle_result(self) -> str:
        return "lose" if self.settle_text == "挑战失败" else "win"

    def _ocr_map_for_state(self) -> dict[object, list[object]]:
        if self.state == "battle_start":
            return {None: [_ocr_piece(self.battle_start_text)]}
        if self.state == "preparation":
            return {None: [_ocr_piece(self.battle_start_text)] if self.battle_start_text else []}
        if self.state == "battle_progress":
            return {None: [_ocr_piece("自动战斗"), _ocr_piece("暂停")]}
        if self.state == "settle_entry":
            page_texts = self.settle_page_texts or [self.settle_text]
            return {
                None: [_ocr_piece(text) for text in page_texts],
                HEADLINE_CAPTURE_KEY: [_ocr_piece(self.settle_headline)],
                ROUND_CAPTURE_KEY: [_ocr_piece("第1-1回合")],
                STATS_CAPTURE_KEY: [_ocr_piece("生命 82"), _ocr_piece("金币 4"), _ocr_piece("经验 2")],
            }
        if self.state == "settle_followup":
            return {None: [_ocr_piece("下一步")]}
        if self.state == "game_over_followup":
            return {None: [_ocr_piece("对局未完成"), _ocr_piece("3-1 战斗"), _ocr_piece("小队生命值 0"), _ocr_piece("下一页")]}
        if self.state == "game_over_summary":
            return {None: [_ocr_piece("对局未完成"), _ocr_piece("3-1 战斗"), _ocr_piece("小队生命值 0")]}
        if self.state == "game_over_return":
            return {
                None: [
                    _ocr_piece("小队生命值"),
                    _ocr_piece("0"),
                    _ocr_piece("总经济"),
                    _ocr_piece("215"),
                    _ocr_piece("阵容价值"),
                    _ocr_piece("46"),
                    _ocr_piece("伤害统计"),
                    _ocr_piece("返回货币战争"),
                ]
            }
        if self.state == "home":
            return {None: [_ocr_piece("货币战争"), _ocr_piece("开始游戏")]}
        if self.state == "game_over":
            return {None: [_ocr_piece("游戏结束")]}
        return {None: []}

    def ocr(self, **kwargs) -> list[object]:
        capture = _capture_key(kwargs.get("capture"))
        return list(self._ocr_map_for_state().get(capture, []))

    def locate(self, template: str, **kwargs):
        del template, kwargs
        return None

    def wait_img(self, template: str, timeout: int = 3, interval: float = 0.5):
        del timeout, interval
        if self.state == "home" and str(template):
            return {"left": 84, "top": 96, "width": 40, "height": 20}
        return None

    def click_point(self, x: int, y: int):
        self.clicks.append((x, y))
        if self.state == "game_over_return":
            self.actions.append("return_home")
            self.advance()

    def advance(self) -> None:
        if self.index < len(self.states) - 1:
            self.index += 1

    def tick(self) -> None:
        if self.state in self.sleep_advances_from:
            self.advance()

    def detect_stage(self) -> str | None:
        if self.state == "stable_stage":
            return self.stable_stage
        if self.state == "game_over":
            return "game_over"
        if self.state == "game_over_return":
            return None
        if self.state == "layer_transition":
            return "layer_transition"
        if self.state == "settle_entry":
            return self.settle_detect_stage
        if self.state == "preparation":
            return "preparation"
        return None

    def run_action(self, name: str, *, advance: bool = True) -> None:
        self.actions.append(name)
        self.click_point(640, 360)
        if advance:
            self.advance()


class TeamCountConfirmRuntime(ScriptedBattleRuntime):
    def __init__(self):
        super().__init__(["battle_start"], sleep_advances_from=())
        self.wait_calls: list[str] = []

    def wait_img(self, template: str, timeout: int = 3, interval: float = 0.5):
        del timeout, interval
        self.wait_calls.append(str(template))
        return {"left": 100, "top": 200, "width": 60, "height": 20}

    def ocr(self, **kwargs) -> list[object]:
        capture = _capture_key(kwargs.get("capture"))
        if capture is None:
            return [_ocr_piece("开始战斗")]
        return [
            {"text": "提示", "box": {"left": 820, "top": 300, "width": 100, "height": 36}},
            {"text": "可出战角色人数未达上限，是否确认出战？", "box": {"left": 620, "top": 430, "width": 680, "height": 36}},
            {"text": "取消", "box": {"left": 380, "top": 600, "width": 100, "height": 36}},
            {"text": "确认", "box": {"left": 1160, "top": 600, "width": 100, "height": 36}},
        ]


class FakeClock:
    def __init__(self, *, step: float = 1.0):
        self.current = 0.0
        self.step = step
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        value = self.current
        self.current += self.step
        return value

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current += seconds


class CostlySettleRuntime(ScriptedBattleRuntime):
    def __init__(
        self,
        states: list[str],
        *,
        clock: FakeClock,
        page_ocr_cost: float,
        capture_ocr_cost: float,
        **kwargs,
    ):
        super().__init__(states, **kwargs)
        self.clock = clock
        self.page_ocr_cost = page_ocr_cost
        self.capture_ocr_cost = capture_ocr_cost
        self.page_ocr_calls = 0
        self.capture_ocr_calls: list[object] = []

    def ocr(self, **kwargs):
        capture = _capture_key(kwargs.get("capture"))
        if capture is None:
            self.page_ocr_calls += 1
            self.clock.current += self.page_ocr_cost
        else:
            self.capture_ocr_calls.append(capture)
            self.clock.current += self.capture_ocr_cost
        return super().ocr(**kwargs)

    def _ocr_map_for_state(self) -> dict[object, list[object]]:
        mapping = super()._ocr_map_for_state()
        if self.state == "settle_entry":
            page_texts = self.settle_page_texts or [self.settle_text]
            page_pieces: list[object] = []
            for text in page_texts:
                if text == "继续挑战":
                    page_pieces.append(_ocr_button_piece("继续挑战", left=900, top=884))
                elif text == "下一步":
                    page_pieces.append(_ocr_button_piece("下一步", left=900, top=884))
                else:
                    page_pieces.append(_ocr_piece(text))
            mapping[None] = page_pieces
        if self.state == "settle_followup":
            mapping[None] = [_ocr_button_piece("下一步", left=900, top=884)]
        return mapping

    def click_point(self, x: int, y: int):
        super().click_point(x, y)
        if self.state == "settle_entry":
            self.actions.append("continue")
            self.advance()
        elif self.state == "settle_followup":
            self.actions.append("next")
            self.advance()


class LayerTransitionRuntime(ScriptedBattleRuntime):
    def _ocr_map_for_state(self) -> dict[object, list[object]]:
        mapping = super()._ocr_map_for_state()
        if self.state == "layer_transition":
            mapping[None] = [_ocr_piece("点击空白处继续"), _ocr_piece("位面")]
        return mapping

    def detect_stage(self) -> str | None:
        if self.state == "layer_transition":
            return "layer_transition"
        return super().detect_stage()

    def click_point(self, x: int, y: int):
        super().click_point(x, y)
        if self.state == "layer_transition":
            self.actions.append("blank_continue")
            self.advance()


def _patch_stage_and_clock_only(monkeypatch, battle_scene, runtime: ScriptedBattleRuntime, *, clock: FakeClock):
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: runtime.detect_stage)
    monkeypatch.setattr(battle_scene, "monotonic", clock.monotonic, raising=False)

    def fake_sleep(seconds: float) -> None:
        clock.sleep(seconds)
        runtime.tick()

    monkeypatch.setattr(battle_scene, "sleep", fake_sleep, raising=False)


def _patch_run_loop(
    monkeypatch,
    battle_scene,
    runtime: ScriptedBattleRuntime,
    *,
    clock: FakeClock | None = None,
    start_advances: bool = True,
    continue_advances: bool = True,
    settle_advances: bool = True,
):
    active_clock = clock or FakeClock()
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: runtime.detect_stage)
    monkeypatch.setattr(
        battle_scene,
        "_start_battle",
        lambda _runtime: runtime.run_action("start", advance=start_advances),
        raising=False,
    )
    monkeypatch.setattr(
        battle_scene,
        "_continue_after_settlement",
        lambda _runtime, observation=None: runtime.run_action("continue", advance=continue_advances),
        raising=False,
    )
    monkeypatch.setattr(
        battle_scene,
        "_advance_settlement_page",
        lambda _runtime, observation=None: runtime.run_action("next", advance=settle_advances),
        raising=False,
    )

    def fake_sleep(seconds: float) -> None:
        active_clock.sleep(seconds)
        runtime.tick()

    monkeypatch.setattr(battle_scene, "sleep", fake_sleep, raising=False)
    monkeypatch.setattr(battle_scene, "monotonic", active_clock.monotonic, raising=False)
    return active_clock


def build_session(tmp_path: Path) -> SessionModel:
    return SessionModel(
        session_id="session-cw-battle-run",
        workspace=tmp_path,
        window_binding={"title": "崩坏：星穹铁道", "hwnd": 1},
        created_at="2026-04-21T00:00:00Z",
    )


def test_classify_cw_battle_page_requires_positive_battle_anchor(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [_ocr_piece("随机 OCR 文本")], HEADLINE_CAPTURE_KEY: []})

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path), detected_stage=None)

    assert result == "unknown"


def test_classify_cw_battle_page_detects_positive_battle_anchor(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [_ocr_piece("自动战斗"), _ocr_piece("2倍速"), _ocr_piece("暂停")]})

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path), detected_stage=None)

    assert result == "battle_progress"


def test_classify_cw_battle_page_detects_battle_start(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [_ocr_piece("开始战斗")]})

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path))

    assert result == "battle_start"


def test_classify_cw_battle_page_treats_chuzhan_preparation_page_as_battle_start(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [_ocr_piece("出战")]})
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda runtime: lambda: "preparation")

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path))

    assert result == "battle_start"


def test_classify_cw_battle_page_treats_detected_preparation_as_board_when_ocr_misses(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [], HEADLINE_CAPTURE_KEY: []})
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda runtime: lambda: "preparation")

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path))

    assert result == "preparation"


def test_classify_cw_battle_page_prefers_settlement_entry_over_battle_hud(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(
        ocr_map={
            None: [_ocr_piece("自动战斗"), _ocr_piece("挑战成功")],
            HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战成功")],
        }
    )

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path))

    assert result == "settle_entry"


def test_classify_cw_battle_page_treats_continue_challenge_as_settlement_entry(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [_ocr_piece("继续挑战")], HEADLINE_CAPTURE_KEY: [_ocr_piece("继续挑战")]})

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path))

    assert result == "settle_entry"


def test_classify_cw_battle_page_treats_challenge_end_with_continue_as_settlement_entry(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(
        ocr_map={
            None: [_ocr_piece("挑战结束"), _ocr_piece("继续挑战")],
            HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战结束")],
        }
    )

    result = battle_scene.classify_cw_battle_page(
        runtime,
        session=build_session(tmp_path),
        detected_stage=None,
    )

    assert result == "settle_entry"


def test_classify_cw_battle_page_uses_single_page_ocr_snapshot(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()

    class RuntimeSpy(FakeRuntime):
        def __init__(self):
            super().__init__(ocr_map={None: [_ocr_piece("挑战成功"), _ocr_piece("继续挑战")]})
            self.page_ocr_calls = 0

        def ocr(self, **kwargs):
            capture = _capture_key(kwargs.get("capture"))
            if capture is None:
                self.page_ocr_calls += 1
            return super().ocr(**kwargs)

    runtime = RuntimeSpy()
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: lambda: None)

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path), detected_stage=None)

    assert result == "settle_entry"
    assert runtime.page_ocr_calls == 1


def test_classify_cw_battle_page_uses_observation_headline_when_page_ocr_misses(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [], HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战成功")]})

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path), detected_stage=None)

    assert result == "settle_entry"
    assert runtime.ocr_calls == [None, HEADLINE_CAPTURE_KEY]


def test_continue_after_settlement_observation_uses_observed_button_without_fallback(monkeypatch):
    battle_scene = load_cw_battle_module()
    runtime = CostlySettleRuntime(
        ["settle_entry"],
        clock=FakeClock(step=0.0),
        page_ocr_cost=0.0,
        capture_ocr_cost=0.0,
        settle_text="挑战成功",
        settle_page_texts=["挑战成功", "继续挑战"],
        settle_detect_stage=None,
        sleep_advances_from=(),
    )
    observation = battle_scene.observe_cw_battle_page(runtime, detected_stage=None)

    monkeypatch.setattr(
        battle_scene,
        "build_cw_battle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback battle continuer used"),
    )

    battle_scene._continue_after_settlement(runtime, observation=observation)

    assert runtime.clicks[-1] == (960, 902)
    assert runtime.page_ocr_calls == 1


def test_continue_after_settlement_observation_ignores_off_region_button_and_uses_fallback(monkeypatch):
    battle_scene = load_cw_battle_module()
    runtime = CostlySettleRuntime(
        ["settle_entry"],
        clock=FakeClock(step=0.0),
        page_ocr_cost=0.0,
        capture_ocr_cost=0.0,
        settle_text="挑战成功",
        settle_detect_stage=None,
        sleep_advances_from=(),
    )
    monkeypatch.setattr(
        runtime,
        "_ocr_map_for_state",
        lambda: {None: [_ocr_button_piece("继续挑战", left=100, top=100)]},
    )
    observation = battle_scene.observe_cw_battle_page(runtime, detected_stage=None)
    monkeypatch.setattr(
        battle_scene,
        "build_cw_battle_continuer",
        lambda _runtime: lambda: runtime.actions.append("fallback"),
    )

    battle_scene._continue_after_settlement(runtime, observation=observation)

    assert runtime.clicks == []
    assert runtime.actions == ["fallback"]


def test_advance_settlement_page_observation_uses_observed_button_without_fallback(monkeypatch):
    battle_scene = load_cw_battle_module()
    runtime = CostlySettleRuntime(
        ["settle_followup"],
        clock=FakeClock(step=0.0),
        page_ocr_cost=0.0,
        capture_ocr_cost=0.0,
        sleep_advances_from=(),
    )
    observation = battle_scene.observe_cw_battle_page(runtime, detected_stage=None)

    monkeypatch.setattr(
        battle_scene,
        "build_cw_settle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback settle continuer used"),
    )

    battle_scene._advance_settlement_page(runtime, observation=observation)

    assert runtime.clicks[-1] == (960, 902)
    assert runtime.page_ocr_calls == 1


def test_continue_after_settlement_observation_accepts_tuple_ocr_piece_without_fallback(monkeypatch):
    battle_scene = load_cw_battle_module()
    runtime = CostlySettleRuntime(
        ["settle_entry"],
        clock=FakeClock(step=0.0),
        page_ocr_cost=0.0,
        capture_ocr_cost=0.0,
        settle_text="挑战成功",
        settle_page_texts=["挑战成功"],
        settle_detect_stage=None,
        sleep_advances_from=(),
    )
    monkeypatch.setattr(
        runtime,
        "_ocr_map_for_state",
        lambda: {
            None: [([[900, 884], [1020, 884], [1020, 920], [900, 920]], "继续挑战", 0.99)],
            HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战成功")],
        },
    )
    observation = battle_scene.observe_cw_battle_page(runtime, detected_stage=None)

    monkeypatch.setattr(
        battle_scene,
        "build_cw_battle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback battle continuer used"),
    )

    battle_scene._continue_after_settlement(runtime, observation=observation)

    assert runtime.clicks[-1] == (960, 902)
    assert runtime.page_ocr_calls == 1


def test_classify_cw_battle_page_does_not_treat_challenge_end_without_continue_as_settlement_entry(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(
        ocr_map={
            None: [_ocr_piece("挑战结束")],
            HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战结束")],
        }
    )

    result = battle_scene.classify_cw_battle_page(
        runtime,
        session=build_session(tmp_path),
        detected_stage=None,
    )

    assert result == "unknown"


def test_classify_cw_battle_page_treats_go_settle_button_as_settlement_followup(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(
        ocr_map={
            None: [_ocr_piece("挑战结束"), _ocr_piece("前往结算")],
            HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战结束")],
        }
    )

    result = battle_scene.classify_cw_battle_page(
        runtime,
        session=build_session(tmp_path),
        detected_stage=None,
    )

    assert result == "settle_followup"


def test_classify_cw_battle_page_treats_game_over_with_next_page_as_game_over_followup(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(
        ocr_map={
            None: [_ocr_piece("对局未完成"), _ocr_piece("前往结算")],
            HEADLINE_CAPTURE_KEY: [_ocr_piece("对局未完成")],
        }
    )

    result = battle_scene.classify_cw_battle_page(
        runtime,
        session=build_session(tmp_path),
        detected_stage=None,
    )

    assert result == "game_over_followup"


def test_classify_cw_battle_page_treats_game_over_summary_as_game_over(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(
        ocr_map={
            None: [_ocr_piece("对局未完成"), _ocr_piece("小队生命值 0")],
            HEADLINE_CAPTURE_KEY: [_ocr_piece("对局未完成")],
        }
    )

    result = battle_scene.classify_cw_battle_page(
        runtime,
        session=build_session(tmp_path),
        detected_stage=None,
    )

    assert result == "game_over"


def test_classify_cw_battle_page_detects_settlement_followup(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [_ocr_piece("下一步")]})

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path))

    assert result == "settle_followup"


def test_advance_settlement_page_observation_uses_go_settle_button_without_fallback(monkeypatch):
    battle_scene = load_cw_battle_module()
    runtime = CostlySettleRuntime(
        ["settle_followup"],
        clock=FakeClock(step=0.0),
        page_ocr_cost=0.0,
        capture_ocr_cost=0.0,
        sleep_advances_from=(),
    )
    monkeypatch.setattr(
        runtime,
        "_ocr_map_for_state",
        lambda: {None: [_ocr_button_piece("前往结算", left=900, top=884)]},
    )
    observation = battle_scene.observe_cw_battle_page(runtime, detected_stage=None)

    monkeypatch.setattr(
        battle_scene,
        "build_cw_settle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback settle continuer used"),
    )

    battle_scene._advance_settlement_page(runtime, observation=observation)

    assert runtime.clicks[-1] == (960, 902)
    assert runtime.page_ocr_calls == 1


def test_run_cw_battle_advances_game_over_followup_then_reports_game_over(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["game_over_followup", "game_over_summary"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15)

    assert result == {
        "status": "completed",
        "result": "lose",
        "stage": "game_over",
        "stale": False,
        "in_battle": False,
        "round": "3-1",
        "hp": 0,
        "settle_text": "对局未完成",
        "game_over": True,
        "end_reason": "global_battle_failed",
        "restart_candidate": True,
    }
    assert runtime.actions == ["next"]
    assert session.scene_state["cw"]["stage"] == {"value": "game_over", "stale": False}


def test_run_cw_battle_clicks_return_currency_wars_and_reports_final_failure(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["game_over_return", "home"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15)

    assert result == {
        "status": "completed",
        "result": "lose",
        "stage": "game_over",
        "stale": False,
        "in_battle": False,
        "hp": 0,
        "settle_text": "返回货币战争",
        "game_over": True,
        "end_reason": "global_battle_failed",
        "restart_candidate": True,
        "returned_home": True,
    }
    assert runtime.actions == ["return_home"]
    assert runtime.clicks == [(960, 908)]
    assert session.scene_state["cw"]["entry"] == {"page": "home"}
    assert session.scene_state["cw"]["stage"] == {"stale": True}
    assert session.last_stage is None


def test_run_cw_battle_does_not_read_economy_as_hp_on_final_failure(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["game_over_return", "home"], sleep_advances_from=())

    def ocr(**kwargs) -> list[object]:
        capture = _capture_key(kwargs.get("capture"))
        if capture is None:
            if runtime.state == "home":
                return [_ocr_piece("货币战争"), _ocr_piece("开始游戏")]
            return [
                _ocr_piece("小队生命值"),
                _ocr_piece("总经济"),
                _ocr_piece("215"),
                _ocr_piece("阵容价值"),
                _ocr_piece("46"),
                _ocr_piece("伤害统计"),
                _ocr_piece("返回货币战争"),
            ]
        return []

    monkeypatch.setattr(runtime, "ocr", ocr)
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15)

    assert result == {
        "status": "completed",
        "result": "lose",
        "stage": "game_over",
        "stale": False,
        "in_battle": False,
        "settle_text": "返回货币战争",
        "game_over": True,
        "end_reason": "global_battle_failed",
        "restart_candidate": True,
        "returned_home": True,
    }
    assert runtime.actions == ["return_home"]
    assert session.scene_state["cw"]["entry"] == {"page": "home"}
    assert session.scene_state["cw"]["stage"] == {"stale": True}


def test_run_cw_battle_does_not_mark_returned_home_when_game_over_template_still_matches(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["game_over_return"], sleep_advances_from=())
    monkeypatch.setattr(
        runtime,
        "wait_img",
        lambda template, timeout=3, interval=0.5: {"left": 84, "top": 96, "width": 40, "height": 20},
    )
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15)

    assert result == {
        "status": "completed",
        "result": "lose",
        "stage": "game_over",
        "stale": False,
        "in_battle": False,
        "hp": 0,
        "settle_text": "返回货币战争",
        "game_over": True,
        "end_reason": "global_battle_failed",
        "restart_candidate": True,
        "returned_home": False,
    }
    assert runtime.actions == ["return_home"]
    assert "entry" not in session.scene_state["cw"]
    assert session.scene_state["cw"]["stage"] == {"stale": True}


def test_run_cw_battle_reads_game_over_return_summary_when_stage_detector_hits_game_over(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["game_over_return", "home"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(
        battle_scene,
        "build_cw_stage_detector",
        lambda _runtime: lambda: "game_over" if runtime.state == "game_over_return" else runtime.detect_stage(),
    )

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15)

    assert result == {
        "status": "completed",
        "result": "lose",
        "stage": "game_over",
        "stale": False,
        "in_battle": False,
        "hp": 0,
        "settle_text": "返回货币战争",
        "game_over": True,
        "end_reason": "global_battle_failed",
        "restart_candidate": True,
        "returned_home": True,
    }
    assert runtime.actions == ["return_home"]
    assert session.scene_state["cw"]["entry"] == {"page": "home"}
    assert session.scene_state["cw"]["stage"] == {"stale": True}


def test_classify_cw_battle_page_detects_game_over(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [_ocr_piece("游戏结束")], HEADLINE_CAPTURE_KEY: []})

    result = battle_scene.classify_cw_battle_page(runtime, session=build_session(tmp_path))

    assert result == "game_over"


def test_classify_cw_battle_page_maps_stable_stage_detector_hits(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda runtime: lambda: "shop")

    result = battle_scene.classify_cw_battle_page(FakeRuntime(), session=build_session(tmp_path))

    assert result == "stable_stage"


def test_classify_cw_battle_page_does_not_treat_boss_preview_as_stable_stage(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda runtime: lambda: "boss_preview")

    result = battle_scene.classify_cw_battle_page(FakeRuntime(), session=build_session(tmp_path))

    assert result != "stable_stage"


def test_classify_cw_battle_page_treats_layer_transition_as_battle_flow_state(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: lambda: "layer_transition")

    result = battle_scene.classify_cw_battle_page(FakeRuntime(), session=build_session(tmp_path))

    assert result == "layer_transition"


def test_classify_cw_battle_page_trusts_detected_layer_transition_without_ocr(tmp_path: Path):
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={None: [], HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战成功")]})

    result = battle_scene.classify_cw_battle_page(
        runtime,
        session=build_session(tmp_path),
        detected_stage="layer_transition",
    )

    assert result == "layer_transition"
    assert runtime.ocr_calls == []


def test_classify_cw_battle_page_maps_detector_settle_to_settlement_entry(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda runtime: lambda: "settle")

    result = battle_scene.classify_cw_battle_page(FakeRuntime(), session=build_session(tmp_path))

    assert result == "settle_entry"


def test_parse_cw_settlement_summary_reads_stable_fields():
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(
        ocr_map={
            HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战成功")],
            ROUND_CAPTURE_KEY: [_ocr_piece("第1-1回合")],
            STATS_CAPTURE_KEY: [_ocr_piece("生命 82"), _ocr_piece("金币 4"), _ocr_piece("经验 2")],
        }
    )

    result = battle_scene.parse_cw_settlement_summary(runtime)

    assert result == {
        "result": "win",
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
    }


def test_parse_cw_settlement_summary_retains_challenge_end_without_calling_it_win():
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(
        ocr_map={
            None: [_ocr_piece("挑战结束"), _ocr_piece("继续挑战")],
            HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战结束")],
            ROUND_CAPTURE_KEY: [_ocr_piece("1-4 X战斗")],
            STATS_CAPTURE_KEY: [_ocr_piece("小队生命值82"), _ocr_piece("获得金币总览")],
        }
    )

    result = battle_scene.parse_cw_settlement_summary(runtime)

    assert result == {
        "result": "ended",
        "round": "1-4",
        "hp": 82,
        "settle_text": "挑战结束",
    }


def test_parse_cw_settlement_summary_omits_missing_secondary_fields():
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战失败")]})

    result = battle_scene.parse_cw_settlement_summary(runtime)

    assert result == {"result": "lose", "settle_text": "挑战失败"}


def test_parse_cw_settlement_summary_raises_when_headline_unreadable():
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime(ocr_map={HEADLINE_CAPTURE_KEY: [_ocr_piece("结算奖励")], None: [_ocr_piece("下一步")]})

    with pytest.raises(TrailError) as exc_info:
        battle_scene.parse_cw_settlement_summary(runtime)

    assert exc_info.value.code == "CW_SETTLEMENT_UNREADABLE"


def test_battle_report_reads_lethal_penalty_and_hp_rows_without_mixing_them():
    from PIL import Image
    from trail.scenes.cw.battle_report import BattleReport

    class ReportRuntime(FakeRuntime):
        def ocr_image(self, image, *, ocr=None):
            assert image.size == (654, 268)
            return [
                _ocr_button_piece("小队生命值结算说明", left=220, top=65),
                _ocr_button_piece("基础伤害", left=70, top=130),
                _ocr_button_piece("-5", left=600, top=130),
                _ocr_button_piece("未完成进度伤害", left=70, top=174),
                _ocr_button_piece("10", left=600, top=174),
                _ocr_button_piece("长线作战", left=70, top=220),
                _ocr_button_piece("+2", left=600, top=220),
            ]

    runtime = ReportRuntime()
    image = Image.new("RGB", (1920, 1080))
    report = BattleReport()
    event = [
        _ocr_button_piece("1-4", left=690, top=0),
        _ocr_button_piece("0%", left=755, top=0),
        _ocr_button_piece("-35X", left=185, top=120),
        _ocr_piece("受到致命攻击，战斗倒计时减少！"),
    ]
    report.observe(runtime, image, event, phase="battle")
    report.observe(runtime, image, event, phase="battle")
    report = BattleReport(report.state)
    report.observe(runtime, image, event, phase="battle")
    report.observe(runtime, image, [_ocr_piece("小队生命值结算说明")], phase="settlement")

    data = report.apply({"result": "ended", "hp": 38})
    assert data["result"] == "lose"
    assert data["round"] == "1-4"
    assert data["hp"] == 38
    assert data["battle_report"] == {
        "progress_pct": 0,
        "latest_hud_progress_pct": 0,
        "latest_hud_source": "battle_hud",
        "settlement_progress_pct": None,
        "settlement_hp": 38,
        "minimum_progress_met": False,
        "action_value_penalties": [{"kind": "lethal_hit", "delta": -35, "text": "受到致命攻击，战斗倒计时减少！"}],
        "team_hp_breakdown": [
            {"label": "基础伤害", "delta": -5},
            {"label": "未完成进度伤害", "delta": -10},
            {"label": "长线作战", "delta": 2},
        ],
    }


def test_battle_hud_preserves_the_boundary_between_round_and_progress():
    from trail.scenes.cw.battle_report import _parse_hud

    assert _parse_hud([_ocr_piece("1-4"), _ocr_piece("45%")]) == ("1-4", 45)
    assert _parse_hud([_ocr_piece("1-445%")]) == ("1-4", 45)
    assert _parse_hud([_ocr_piece("1-10"), _ocr_piece("45%")]) == ("1-10", 45)


def test_settlement_hp_uses_cropped_digits_instead_of_info_icon():
    from PIL import Image

    class SettlementRuntime(FakeRuntime):
        def ocr_image(self, image, *, ocr=None):
            assert image.width < 130
            return [_ocr_piece("38")]

    battle = load_cw_battle_module()
    runtime = SettlementRuntime(ocr_map={
        HEADLINE_CAPTURE_KEY: [_ocr_piece("挑战结束")],
        STATS_CAPTURE_KEY: [_ocr_piece("小队生命值381")],
    })
    observation = battle.BattleObservation(
        runtime, "settle", [], "挑战结束1-4X战斗小队生命值381继续挑战",
        frame=Image.new("RGB", (1920, 1080)),
    )
    assert battle.parse_cw_settlement_summary(runtime, observation) == {
        "round": "1-4", "hp": 38, "result": "ended", "settle_text": "挑战结束",
    }


@pytest.mark.parametrize("node", ["1-2奖励", "1-6战斗"])
def test_successful_settlement_hp_reads_the_label_and_value_together(node):
    from PIL import Image

    class SettlementRuntime(FakeRuntime):
        def ocr_image(self, image, *, ocr=None):
            assert image.width > 400
            return [_ocr_piece("小队生命值 84"), _ocr_piece("1")]

    battle = load_cw_battle_module()
    runtime = SettlementRuntime(ocr_map={STATS_CAPTURE_KEY: [_ocr_piece("小队生命值 41")]})
    observation = battle.BattleObservation(
        runtime, "settle", [], f"挑战成功{node}小队生命值841继续挑战",
        frame=Image.new("RGB", (1920, 1080)),
    )
    assert battle.parse_cw_settlement_summary(runtime, observation)["hp"] == 84


def test_mark_cw_stage_stale_clears_last_stage(tmp_path: Path):
    from trail.scenes.cw.models import ensure_cw_state
    from trail.scenes.cw.stage import mark_cw_stage_stale

    session = build_session(tmp_path)
    ensure_cw_state(session)["stage"] = {"value": "shop", "stale": False, "status": {"stale": False, "level": 7}}
    session.last_stage = {"scene": "cw", "value": "shop"}

    mark_cw_stage_stale(session)

    assert session.scene_state["cw"]["stage"] == {"stale": True, "status": {"stale": False, "level": 7}}
    assert session.last_stage is None


def test_run_cw_battle_clicks_start_then_returns_completed_summary(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_start", "battle_progress", "settle_entry", "settle_followup", "stable_stage"])
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)

    assert result == {
        "status": "completed",
        "result": "win",
        "stage": "shop",
        "stale": False,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
    }
    assert runtime.actions == ["start", "continue", "next"]
    assert session.scene_state["cw"]["stage"] == {"value": "shop", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "shop"}


def test_run_cw_battle_clicks_chuzhan_start_then_returns_completed_summary(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(
        ["battle_start", "battle_progress", "settle_entry", "settle_followup", "stable_stage"],
        battle_start_text="出战",
    )
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(
        battle_scene,
        "build_cw_stage_detector",
        lambda _runtime: lambda: "preparation" if runtime.state == "battle_start" else runtime.detect_stage(),
    )

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)

    assert result == {
        "status": "completed",
        "result": "win",
        "stage": "shop",
        "stale": False,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
    }
    assert runtime.actions == ["start", "continue", "next"]
    assert session.scene_state["cw"]["stage"] == {"value": "shop", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "shop"}


def test_run_cw_battle_does_not_start_without_start_text(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(
        ["battle_start", "battle_progress", "settle_entry", "settle_followup", "stable_stage"],
        battle_start_text="",
    )
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(
        battle_scene,
        "build_cw_stage_detector",
        lambda _runtime: lambda: "preparation" if runtime.state == "battle_start" else runtime.detect_stage(),
    )

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)

    assert result == {"status": "observed", "stage": "preparation", "stale": False, "in_battle": False}
    assert runtime.actions == []


def test_run_cw_battle_stops_on_next_round_preparation_after_settle(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(
        ["battle_start", "battle_progress", "settle_entry", "settle_followup", "preparation"],
        battle_start_text="出战",
    )
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)

    assert result == {
        "status": "completed",
        "result": "win",
        "stage": "preparation",
        "stale": False,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
    }
    assert runtime.actions == ["start", "continue", "next"]
    assert session.scene_state["cw"]["stage"] == {"value": "preparation", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "preparation"}


@pytest.mark.parametrize(
    ("states", "expected_action"),
    [
        (["battle_start", "battle_progress"], "start"),
        (["settle_entry", "settle_followup"], "continue"),
        (["settle_followup", "stable_stage"], "next"),
        (["game_over_followup", "stable_stage"], "next"),
        (["layer_transition", "stable_stage"], "blank_continue"),
    ],
)
def test_run_cw_battle_marks_side_effect_before_post_action_cancel(
    tmp_path: Path,
    monkeypatch,
    states: list[str],
    expected_action: str,
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(states, sleep_advances_from=())
    if states[0] == "layer_transition":
        ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    token = type("Token", (), {"side_effect_stage": "none"})()
    cancelled = {"value": False}

    def throw_if_cancelled():
        if getattr(token, "side_effect_stage", "none") != "none":
            cancelled["value"] = True
            raise RuntimeError("cancel after action")

    token.throw_if_cancelled = throw_if_cancelled
    _patch_run_loop(
        monkeypatch,
        battle_scene,
        runtime,
        start_advances=False,
        continue_advances=False,
        settle_advances=False,
    )
    if states[0] == "layer_transition":
        monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: runtime.detect_stage, raising=False)
        monkeypatch.setattr(battle_scene, "_advance_layer_transition", lambda _runtime: runtime.run_action("blank_continue", advance=False), raising=False)

    with pytest.raises(RuntimeError, match="cancel after action"):
        battle_scene.run_cw_battle(session, runtime=runtime, timeout=30, cancellation_token=token)

    assert cancelled["value"] is True
    assert token.side_effect_stage == "side_effect_applied"
    assert runtime.actions == [expected_action]


def test_run_cw_battle_stops_on_detected_next_round_preparation_when_ocr_misses_start_text(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(
        ["battle_start", "battle_progress", "settle_entry", "settle_followup", "preparation"],
        battle_start_text="出战",
    )
    original_ocr = runtime.ocr
    runtime.ocr = lambda **kwargs: [] if runtime.state == "preparation" else original_ocr(**kwargs)
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)

    assert result == {
        "status": "completed",
        "result": "win",
        "stage": "preparation",
        "stale": False,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
    }
    assert runtime.actions == ["start", "continue", "next"]
    assert session.scene_state["cw"]["stage"] == {"value": "preparation", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "preparation"}


def test_run_cw_battle_keeps_first_preparation_detector_hit_when_second_read_misses(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(
        ["settle_entry", "settle_followup", "preparation"],
        battle_start_text="",
    )
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    preparation_reads = 0

    def build_racy_stage_detector(_runtime):
        def detect_stage():
            nonlocal preparation_reads
            if runtime.state != "preparation":
                return runtime.detect_stage()
            preparation_reads += 1
            return "preparation" if preparation_reads == 1 else None

        return detect_stage

    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", build_racy_stage_detector)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)

    assert result == {
        "status": "completed",
        "result": "win",
        "stage": "preparation",
        "stale": False,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
    }
    assert runtime.actions == ["continue", "next"]
    assert preparation_reads == 1
    assert session.scene_state["cw"]["stage"] == {"value": "preparation", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "preparation"}


def test_run_cw_battle_does_not_recheck_detector_when_first_preparation_read_misses(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(
        ["settle_entry", "settle_followup", "preparation"],
        battle_start_text="",
    )
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    preparation_reads = 0

    def build_racy_stage_detector(_runtime):
        def detect_stage():
            nonlocal preparation_reads
            if runtime.state != "preparation":
                return runtime.detect_stage()
            preparation_reads += 1
            return None if preparation_reads == 1 else "preparation"

        return detect_stage

    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", build_racy_stage_detector)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)

    assert result == {
        "status": "completed",
        "result": "win",
        "stage": "preparation",
        "stale": False,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
    }
    assert runtime.actions == ["continue", "next"]
    assert preparation_reads == 2
    assert session.scene_state["cw"]["stage"] == {"value": "preparation", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "preparation"}


def test_run_cw_battle_raises_when_start_shows_team_count_confirm_dialog(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = TeamCountConfirmRuntime()
    clock = FakeClock()
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: runtime.detect_stage)
    monkeypatch.setattr(battle_scene, "monotonic", clock.monotonic, raising=False)

    with pytest.raises(TrailError) as exc_info:
        battle_scene.run_cw_battle(session, runtime=runtime, timeout=2)

    assert exc_info.value.code == "CW_BATTLE_TEAM_COUNT_INSUFFICIENT"
    assert "可出战角色人数未达上限" in str(exc_info.value)
    assert "检查场上人数" in str(exc_info.value)
    assert runtime.clicks == [(130, 210), (430, 618)]


def test_run_cw_battle_returns_settle_timeout_summary_when_budget_exhausted(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_entry", "settle_followup"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime, settle_advances=False)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=2)

    assert result == {
        "status": "in_progress",
        "result": "win",
        "stage": "settle",
        "stale": True,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
        "timeout_seconds": 2,
    }
    assert session.scene_state["cw"]["stage"] == {"stale": True}
    assert session.last_stage is None


def test_run_cw_battle_persists_last_battle_round(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_entry", "settle_followup", "stable_stage"])
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=10)

    assert result["round"] == "1-1"
    assert ensure_cw_state(session)["metrics"]["last_battle_round"] == "1-1"


def test_run_cw_battle_persists_last_battle_round_for_settle_timeout(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_entry", "settle_followup", "unknown"], sleep_advances_from=("unknown",))

    _patch_run_loop(monkeypatch, battle_scene, runtime, clock=FakeClock(step=0.1))
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: lambda: None)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=1)

    assert result["status"] == "in_progress"
    assert result["stage"] == "settle"
    assert ensure_cw_state(session)["metrics"]["last_battle_round"] == "1-1"
    assert ensure_cw_state(session)["stage"] == {"stale": True}
    assert session.last_stage is None


def test_run_cw_battle_finishes_when_starting_from_settle_followup(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_followup", "stable_stage"])
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=10)

    assert result == {
        "status": "completed",
        "stage": "shop",
        "stale": False,
        "in_battle": False,
    }
    assert runtime.actions == ["next"]
    assert session.scene_state["cw"]["stage"] == {"value": "shop", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "shop"}


def test_run_cw_battle_finishes_from_continue_only_settle_entry_without_summary(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_entry", "stable_stage"], settle_text="继续挑战")
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=10)

    assert result == {
        "status": "completed",
        "stage": "shop",
        "stale": False,
        "in_battle": False,
    }
    assert runtime.actions == ["continue"]
    assert session.scene_state["cw"]["stage"] == {"value": "shop", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "shop"}


def test_run_cw_battle_resumes_challenge_end_settlement_entry_when_detector_misses(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(
        ["settle_entry", "settle_followup", "stable_stage"],
        settle_headline="挑战结束",
        settle_page_texts=["挑战结束", "继续挑战"],
        settle_detect_stage=None,
    )
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=10)

    assert result == {
        "status": "completed",
        "result": "ended",
        "stage": "shop",
        "stale": False,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战结束",
    }
    assert runtime.actions == ["continue", "next"]
    assert session.scene_state["cw"]["stage"] == {"value": "shop", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "shop"}


def test_run_cw_battle_resumes_from_settle_into_layer_transition_then_shop(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = LayerTransitionRuntime(["settle_entry", "layer_transition", "stable_stage"])
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: runtime.detect_stage)
    monkeypatch.setattr(
        battle_scene,
        "_continue_after_settlement",
        lambda _runtime, observation=None: runtime.run_action("continue"),
    )
    monkeypatch.setattr(
        battle_scene,
        "build_cw_battle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback battle continuer used"),
    )
    monkeypatch.setattr(
        battle_scene,
        "build_cw_settle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback settle continuer used"),
    )

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15)

    assert result["status"] == "completed"
    assert result["stage"] == "shop"
    assert runtime.actions == ["continue", "blank_continue"]
    assert runtime.clicks[-1] == (960, 903)


def test_run_cw_battle_timeout_on_layer_transition_keeps_in_progress(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = LayerTransitionRuntime(["layer_transition", "layer_transition"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: runtime.detect_stage)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=1)

    assert result["status"] == "in_progress"
    assert result["stage"] == "layer_transition"
    assert result["stale"] is True
    assert result["in_battle"] is False


def test_run_cw_battle_keeps_report_and_stops_at_requested_layer_boundary(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = LayerTransitionRuntime(["settle_entry", "layer_transition", "stable_stage"])
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _: runtime.detect_stage)
    monkeypatch.setattr(
        battle_scene, "_continue_after_settlement",
        lambda _runtime, observation=None: runtime.run_action("continue"),
    )

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15, stop_at_plane=True)

    assert result["status"] == "completed"
    assert result["stage"] == "layer_transition"
    assert "result" in result
    assert runtime.actions == ["continue"]


def test_run_cw_battle_advances_detected_layer_transition_without_ocr(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)

    class NoOcrLayerTransitionRuntime(LayerTransitionRuntime):
        def __init__(self, states: list[str], *, sleep_advances_from: tuple[str, ...]):
            super().__init__(states, sleep_advances_from=sleep_advances_from)
            self.ocr_calls = 0

        def ocr(self, **kwargs):
            del kwargs
            self.ocr_calls += 1
            return []

    runtime = NoOcrLayerTransitionRuntime(["layer_transition", "stable_stage"], sleep_advances_from=())
    _patch_stage_and_clock_only(monkeypatch, battle_scene, runtime=runtime, clock=FakeClock(step=0.0))

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)

    assert result == {"status": "completed", "stage": "shop", "stale": False, "in_battle": False}
    assert runtime.actions == ["blank_continue"]
    assert runtime.ocr_calls == 0


def test_run_cw_battle_resumes_settle_entry_before_short_deadline(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    clock = FakeClock(step=0.0)
    runtime = CostlySettleRuntime(
        ["settle_entry", "settle_followup", "stable_stage"],
        clock=clock,
        page_ocr_cost=3.0,
        capture_ocr_cost=1.0,
        settle_text="挑战成功",
        settle_page_texts=["挑战成功", "继续挑战"],
        settle_detect_stage=None,
        sleep_advances_from=(),
    )
    _patch_stage_and_clock_only(monkeypatch, battle_scene, runtime, clock=clock)
    monkeypatch.setattr(
        battle_scene,
        "build_cw_battle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback battle continuer used"),
    )
    monkeypatch.setattr(
        battle_scene,
        "build_cw_settle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback settle continuer used"),
    )

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15)

    assert result["status"] == "completed"
    assert result["stage"] == "shop"
    assert runtime.actions == ["continue", "next"]
    assert clock.current < 15


def test_run_cw_battle_short_deadline_resume_hint_then_settle_still_advances_continue_and_next(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    clock = FakeClock(step=0.0)
    runtime = CostlySettleRuntime(
        ["settle_entry", "settle_followup", "stable_stage"],
        clock=clock,
        page_ocr_cost=3.0,
        capture_ocr_cost=1.0,
        settle_text="挑战成功",
        settle_page_texts=["挑战成功", "继续挑战"],
        settle_detect_stage=None,
        sleep_advances_from=(),
    )
    _patch_stage_and_clock_only(monkeypatch, battle_scene, runtime, clock=clock)
    monkeypatch.setattr(
        battle_scene,
        "build_cw_battle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback battle continuer used"),
    )
    monkeypatch.setattr(
        battle_scene,
        "build_cw_settle_continuer",
        lambda _runtime: lambda: pytest.fail("fallback settle continuer used"),
    )

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=15)

    assert result["status"] == "completed"
    assert result["stage"] == "shop"
    assert runtime.actions == ["continue", "next"]
    assert ensure_cw_state(session)["battle_resume"] == {}


def test_run_cw_battle_returns_settle_timeout_when_starting_from_settle_followup(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_followup"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=1)

    assert result == {
        "status": "in_progress",
        "stage": "settle",
        "stale": True,
        "in_battle": False,
        "timeout_seconds": 1,
    }
    assert session.scene_state["cw"]["stage"] == {"stale": True}
    assert session.last_stage is None


def test_run_cw_battle_returns_settle_timeout_for_continue_only_settle_entry_without_summary(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_entry"], settle_text="继续挑战", sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=1)

    assert result == {
        "status": "in_progress",
        "stage": "settle",
        "stale": True,
        "in_battle": False,
        "timeout_seconds": 1,
    }
    assert session.scene_state["cw"]["stage"] == {"stale": True}
    assert session.last_stage is None


def test_run_cw_battle_returns_in_battle_timeout_when_only_progress_seen(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_progress"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=2)

    assert result == {
        "status": "in_progress",
        "stale": True,
        "in_battle": True,
        "timeout_seconds": 2,
    }
    assert session.scene_state["cw"]["stage"] == {"stale": True}
    assert session.last_stage is None


def test_run_cw_battle_tolerates_unknown_opening_when_resume_hint_is_set(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    runtime = ScriptedBattleRuntime(["unknown", "battle_progress"], sleep_advances_from=("unknown",))

    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: lambda: None)
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=5)

    assert result["status"] == "in_progress"
    assert result["in_battle"] is True


def test_run_cw_battle_resume_hint_completes_when_opening_on_preparation(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    runtime = ScriptedBattleRuntime(["preparation"], battle_start_text="出战")
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=5)

    assert result == {
        "status": "completed",
        "stage": "preparation",
        "stale": False,
        "in_battle": False,
    }
    assert runtime.actions == []
    assert ensure_cw_state(session).get("battle_resume") == {}


def test_run_cw_battle_resume_hint_ocr_only_preparation_completes_when_detector_misses(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    runtime = ScriptedBattleRuntime(["preparation"], battle_start_text="出战")

    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: lambda: None)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=5)

    assert result == {
        "status": "completed",
        "stage": "preparation",
        "stale": False,
        "in_battle": False,
    }
    assert runtime.actions == []
    assert ensure_cw_state(session).get("battle_resume") == {}


def test_run_cw_battle_completes_after_layer_transition_when_ocr_sees_preparation(
    tmp_path: Path, monkeypatch
):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(
        ["layer_transition", "preparation"],
        battle_start_text="备战阶段",
        sleep_advances_from=(),
    )

    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(
        battle_scene,
        "build_cw_stage_detector",
        lambda _runtime: lambda: "layer_transition" if runtime.state == "layer_transition" else None,
    )
    monkeypatch.setattr(battle_scene, "_advance_layer_transition", lambda _runtime: runtime.run_action("blank_continue"))

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=5)

    assert result == {
        "status": "completed",
        "stage": "preparation",
        "stale": False,
        "in_battle": False,
    }
    assert runtime.actions == ["blank_continue"]
    assert ensure_cw_state(session).get("battle_resume") == {}


def test_run_cw_battle_sets_resume_hint_after_in_battle_timeout(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_progress"])

    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: lambda: None)
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=1)

    assert result["status"] == "in_progress"
    assert result["in_battle"] is True
    assert ensure_cw_state(session)["battle_resume"] == {"in_battle_hint": True}


def test_run_cw_battle_does_not_set_resume_hint_for_settle_timeout(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_entry", "settle_followup", "unknown"], sleep_advances_from=("unknown",))

    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: lambda: None)
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=1)

    assert result["status"] == "in_progress"
    assert result["stage"] == "settle"
    assert result["in_battle"] is False
    assert ensure_cw_state(session).get("battle_resume") == {}


def test_run_cw_battle_clears_resume_hint_after_completed_result(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    runtime = ScriptedBattleRuntime(["battle_progress", "stable_stage"])

    monkeypatch.setattr(battle_scene, "build_cw_stage_detector", lambda _runtime: lambda: "shop")
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=5)

    assert result["status"] == "completed"
    assert ensure_cw_state(session).get("battle_resume") == {}


def test_run_cw_battle_preserves_resume_hint_when_settlement_parse_failure_raises(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    runtime = ScriptedBattleRuntime(["settle_entry"])
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    def fail_settlement_summary(_runtime, observation=None):
        del observation
        raise TrailError("CW_SETTLEMENT_UNREADABLE", "无法识别货币战争结算结果")

    monkeypatch.setattr(battle_scene, "parse_cw_settlement_summary", fail_settlement_summary)

    with pytest.raises(TrailError) as exc_info:
        battle_scene.run_cw_battle(session, runtime=runtime, timeout=5)

    assert exc_info.value.code == "CW_SETTLEMENT_UNREADABLE"
    assert ensure_cw_state(session)["battle_resume"]["in_battle_hint"] is True


def test_run_cw_battle_returns_game_over_when_chain_ends(tmp_path: Path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["settle_entry", "game_over"])
    _patch_run_loop(monkeypatch, battle_scene, runtime)

    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=10)

    assert result == {
        "status": "completed",
        "result": "win",
        "stage": "game_over",
        "stale": False,
        "in_battle": False,
        "round": "1-1",
        "hp": 82,
        "coins": 4,
        "exp": 2,
        "settle_text": "挑战成功",
    }
    assert session.scene_state["cw"]["stage"] == {"value": "game_over", "stale": False}
    assert session.last_stage == {"scene": "cw", "value": "game_over"}


def test_battle_start_delayed_page_clicks_only_once_and_times_out(tmp_path, monkeypatch):
    scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_start"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, scene, runtime, start_advances=False)
    result = scene.run_cw_battle(session, runtime=runtime, timeout=2)
    assert runtime.actions == ["start"]
    assert result["status"] == "in_progress" and result["timeout_seconds"] == 2


def test_battle_start_delayed_page_then_progress_does_not_click_twice(tmp_path, monkeypatch):
    scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_start", "battle_progress", "settle_entry", "settle_followup", "stable_stage"],
                                    sleep_advances_from=("battle_start", "battle_progress"))
    _patch_run_loop(monkeypatch, scene, runtime, start_advances=False)
    result = scene.run_cw_battle(session, runtime=runtime, timeout=30)
    assert runtime.actions.count("start") == 1
    assert result["status"] == "completed" and result["result"] == "win"


@pytest.mark.parametrize("old_hint", [False, True])
def test_special_board_from_same_frame_never_starts(tmp_path, monkeypatch, old_hint):
    from PIL import Image
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    if old_hint:
        ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    runtime = ScriptedBattleRuntime(["preparation"])
    runtime.screenshot = lambda: Image.new("RGB", (1920, 1080))
    runtime.ocr_image = lambda image, **kwargs: [
        _ocr_button_piece("备战阶段", left=427, top=36),
        _ocr_button_piece("幕后茶歇", left=1764, top=740),
        _ocr_button_piece("1-8", left=440, top=65, width=50, height=25),
        _ocr_button_piece("32", left=1440, top=65, width=40, height=25),
        _ocr_button_piece("购买经验", left=244, top=855),
    ]
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=1)
    assert result["status"] == "observed"
    assert result["stage"] == "preparation" and result["in_battle"] is False
    assert result["current_node"] == {"stage": "preparation", "node": "1-8", "hp": 32}
    assert result["next_action"] == "cw.stage.read"
    assert runtime.clicks == []
    assert not ensure_cw_state(session)["battle_resume"].get("in_battle_hint")


def test_special_text_outside_right_control_does_not_confirm_special(tmp_path):
    from PIL import Image
    battle_scene = load_cw_battle_module()
    runtime = FakeRuntime()
    pieces = [_ocr_button_piece("备战阶段", left=427, top=36),
              _ocr_button_piece("幕后茶歇", left=100, top=740)]
    observation = battle_scene.BattleObservation(runtime, None, pieces, "备战阶段幕后茶歇", Image.new("RGB", (1920, 1080)))
    assert not battle_scene._is_special_preparation(observation)


def test_same_frame_right_hand_battle_start_clicks_once(tmp_path, monkeypatch):
    from PIL import Image
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_start", "battle_progress", "stable_stage"])
    runtime.screenshot = lambda: Image.new("RGB", (1920, 1080))
    runtime.ocr_image = lambda image, **kwargs: ([_ocr_button_piece("出战", left=1770, top=740)]
        if runtime.state == "battle_start" else [_ocr_piece("自动战斗")])
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    monkeypatch.setattr(battle_scene, "_fail_if_battle_team_count_confirm_dialog", lambda runtime: None)
    def click(x, y):
        runtime.clicks.append((x, y))
        runtime.advance()
    runtime.click_point = click
    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=30)
    assert result["status"] == "completed" and result["stage"] == "shop"
    assert runtime.clicks == [(1830, 758)]


def test_unknown_with_old_hint_does_not_claim_combat_on_timeout(tmp_path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"in_battle_hint": True}
    runtime = ScriptedBattleRuntime(["unknown"])
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    result = battle_scene.run_cw_battle(session, runtime=runtime, timeout=0)
    assert result["in_battle"] is None
    assert runtime.clicks == []
    assert ensure_cw_state(session)["battle_resume"].get("in_battle_hint")


def test_unconfirmed_start_is_not_clicked_again_on_next_request(tmp_path, monkeypatch):
    battle_scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_start"], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle_scene, runtime)
    first = battle_scene.run_cw_battle(session, runtime=runtime, timeout=2)
    assert first["in_battle"] is None
    assert runtime.actions == ["start"]
    second = battle_scene.run_cw_battle(session, runtime=runtime, timeout=2)
    assert second["status"] == "in_progress" and second["in_battle"] is None
    assert runtime.actions == ["start"]


def test_positioned_transition_outranks_stale_preparation(tmp_path):
    from PIL import Image
    battle = load_cw_battle_module()
    runtime = FakeRuntime()
    pieces = [_ocr_button_piece("战斗阶段", left=840, top=340, width=165),
              _ocr_button_piece("3-1", left=1010, top=340, width=65),
              _ocr_button_piece("备战阶段", left=425, top=36),
              _ocr_button_piece("出战", left=1770, top=740)]
    obs = battle.BattleObservation(runtime, "preparation", pieces, "战斗阶段3-1备战阶段出战", Image.new("RGB", (1920, 1080)))
    assert battle.classify_cw_battle_page(runtime, session=build_session(tmp_path), detected_stage="preparation", observation=obs) == "battle_transition"


def test_transition_then_combat_timeout_discards_banner_frame(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_transition", "battle_progress"], sleep_advances_from=("battle_transition",))
    runtime.screenshot = lambda: Image.new("RGB", (1920, 1080))
    runtime.ocr_image = lambda image, **kwargs: []
    _patch_run_loop(monkeypatch, battle, runtime)
    monkeypatch.setattr(battle, "classify_cw_battle_page", lambda runtime, **kwargs: runtime.state)
    monkeypatch.setattr(battle, "_battle_screen_evidence", lambda runtime, observation: ("3-1", False))
    result = battle.run_cw_battle(session, runtime=runtime, timeout=2)
    assert result["status"] == "in_progress" and result["in_battle"] is True
    assert result.get("round") == "3-1"
    assert result.get("stage") != "battle_transition"
    assert "_reconcile_frame" not in result


@pytest.mark.parametrize("text,y,blocked", [
    ("备战席已满，请出售角色或提升等级", 525, True),
    ("备战席已满，需要售卖备战区角色才可开启晶矿", 290, False),
    ("备战席已满，请出售角色或提升等级", 290, False),
])
def test_positioned_reserve_overflow_is_distinct_from_crystal_notice(tmp_path, text, y, blocked):
    from PIL import Image
    battle = load_cw_battle_module()
    runtime = FakeRuntime()
    pieces = [_ocr_button_piece(text, left=800, top=y, width=350, height=25),
              _ocr_button_piece("出战", left=1770, top=740)]
    obs = battle.BattleObservation(runtime, "preparation", pieces, text, Image.new("RGB", (1920, 1080)))
    state = battle.classify_cw_battle_page(runtime, session=build_session(tmp_path), detected_stage="preparation", observation=obs)
    assert (state == "reserve_overflow") is blocked


def test_reserve_overflow_blocks_before_start(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_start"])
    runtime.screenshot = lambda: Image.new("RGB", (1920, 1080))
    runtime.ocr_image = lambda image, **kwargs: [_ocr_button_piece("备战席已满，请出售角色或提升等级", left=800, top=525, width=350, height=25)] if image.size == (1920, 1080) else []
    _patch_run_loop(monkeypatch, battle, runtime)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=2)
    assert result["status"] == "blocked" and result["reason"] == "reserve_overflow"
    assert not runtime.clicks


def test_overflow_after_click_allows_new_attempt_when_cleared(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_start", "blocked", "battle_start", "stable_stage"])
    runtime.screenshot = lambda: Image.new("RGB", (1920, 1080))
    def ocr(image, **kwargs):
        if image.size != (1920, 1080):
            return []
        if runtime.state == "blocked":
            return [_ocr_button_piece("备战席已满，请出售角色或提升等级", left=800, top=525, width=350, height=25)]
        return [_ocr_button_piece("出战", left=1770, top=740)]
    runtime.ocr_image = ocr
    _patch_run_loop(monkeypatch, battle, runtime)
    monkeypatch.setattr(battle, "_fail_if_battle_team_count_confirm_dialog", lambda runtime: None)
    def click(x, y):
        runtime.clicks.append((x, y))
        runtime.advance()
    runtime.click_point = click
    first = battle.run_cw_battle(session, runtime=runtime, timeout=2)
    assert first["reason"] == "reserve_overflow" and first["_reconcile_frame"] is not None
    assert not ensure_cw_state(session)["battle_resume"].get("start_pending")
    runtime.advance()
    second = battle.run_cw_battle(session, runtime=runtime, timeout=2)
    assert second["status"] == "completed" and len(runtime.clicks) == 2


def test_start_point_and_special_header_scale_to_reference(tmp_path):
    from PIL import Image
    scene = load_cw_battle_module()
    runtime = FakeRuntime()
    pieces = [_ocr_button_piece("出战", left=1180, top=490, width=80, height=24),
              _ocr_button_piece("1-8", left=293, top=43, width=34, height=16),
              _ocr_button_piece("32", left=960, top=43, width=26, height=16)]
    observation = scene.BattleObservation(runtime, None, pieces, "出战1-832", Image.new("RGB", (1280, 720)))
    assert scene._battle_start_point(observation) == (1830, 753)
    assert scene._special_preparation_header(observation) == {"stage": "preparation", "node": "1-8", "hp": 32}


@pytest.mark.parametrize("state", ["stable_stage", "game_over", "preparation"])
def test_completed_stage_clears_pending_start(tmp_path, monkeypatch, state):
    scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"start_pending": True, "in_battle_hint": True}
    runtime = ScriptedBattleRuntime([state], battle_start_text="出战")
    _patch_run_loop(monkeypatch, scene, runtime)
    result = scene.run_cw_battle(session, runtime=runtime, timeout=1)
    assert result["status"] == "completed"
    assert not ensure_cw_state(session)["battle_resume"].get("start_pending")


def test_pending_start_unknown_waits_without_click_or_claiming_combat(tmp_path, monkeypatch):
    scene = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)["battle_resume"] = {"start_pending": True}
    runtime = ScriptedBattleRuntime(["unknown"])
    _patch_run_loop(monkeypatch, scene, runtime)
    result = scene.run_cw_battle(session, runtime=runtime, timeout=0)
    assert result["in_battle"] is None
    assert runtime.clicks == []
    assert ensure_cw_state(session)["battle_resume"]["start_pending"] is True


def test_completed_battle_stops_at_special_board_with_report_without_clicking_it(tmp_path, monkeypatch):
    from PIL import Image
    scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["battle_start", "battle_progress", "settle_entry", "settle_followup", "preparation"])
    runtime.screenshot = lambda: Image.new("RGB", (1920, 1080))
    def image_ocr(image, **kwargs):
        if runtime.state == "preparation":
            return [_ocr_button_piece("备战阶段", left=427, top=36),
                    _ocr_button_piece("幕后茶歇", left=1764, top=740),
                    _ocr_button_piece("1-8", left=440, top=65, width=50, height=25)]
        if runtime.state == "battle_start":
            return [_ocr_button_piece("出战", left=1770, top=740)]
        if runtime.state == "settle_entry":
            return [_ocr_piece("挑战成功"), _ocr_button_piece("继续挑战", left=900, top=880)]
        if runtime.state == "settle_followup":
            return [_ocr_button_piece("下一步", left=900, top=880)]
        return runtime._ocr_map_for_state().get(None, [])
    runtime.ocr_image = image_ocr
    _patch_run_loop(monkeypatch, scene, runtime)
    monkeypatch.setattr(scene, "_fail_if_battle_team_count_confirm_dialog", lambda runtime: None)
    def click(x, y):
        runtime.clicks.append((x, y))
        if runtime.state == "battle_start":
            runtime.advance()
    runtime.click_point = click
    result = scene.run_cw_battle(session, runtime=runtime, timeout=30)
    assert result["status"] == "observed" and result["stage"] == "preparation"
    assert result["in_battle"] is False and result["current_node"]["node"] == "1-8"
    assert result["next_action"] == "cw.stage.read" and result["result"] == "win"
    assert runtime.clicks == [(1830, 758), (640, 360), (640, 360)]


@pytest.mark.parametrize("overlay", ["等级提升", "请选择投资策略"])
def test_board_overlay_vetoes_right_hand_battle_button(tmp_path, monkeypatch, overlay):
    from PIL import Image
    scene = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(["preparation"])
    runtime.screenshot = lambda: Image.new("RGB", (1920, 1080))
    runtime.ocr_image = lambda image, **kwargs: [
        _ocr_button_piece("备战阶段", left=427, top=36),
        _ocr_button_piece(overlay, left=900, top=194),
        _ocr_button_piece("出战", left=1770, top=740),
        *([_ocr_button_piece("返回备战界面", left=100, top=950)] if overlay == "请选择投资策略" else []),
    ]
    _patch_run_loop(monkeypatch, scene, runtime)
    if overlay == "等级提升":
        result = scene.run_cw_battle(session, runtime=runtime, timeout=1)
        assert result["status"] == "observed" and result["in_battle"] is False
    else:
        with pytest.raises(TrailError) as error:
            scene.run_cw_battle(session, runtime=runtime, timeout=1)
        assert error.value.code == "CW_BATTLE_STATE_UNKNOWN"
    assert runtime.clicks == []


def _terminal_evaluation_pieces(include_button=True):
    pieces = [_ocr_button_piece('挑战成功', left=837, top=206),
              _ocr_button_piece('小队生命值91', left=866, top=284),
              _ocr_button_piece('对局评价', left=906, top=361),
              _ocr_button_piece('SS', left=850, top=490)]
    if include_button:
        pieces.append(_ocr_button_piece('下一步', left=921, top=885, width=80))
    return pieces


def _terminal_detail_pieces():
    return [_ocr_button_piece('对局胜利', left=462, top=185),
            _ocr_button_piece('3-7首领', left=777, top=185),
            _ocr_button_piece('小队生命值91', left=1390, top=185, width=200),
            _ocr_button_piece('SS', left=420, top=280),
            _ocr_button_piece('标准博弈', left=340, top=468),
            _ocr_button_piece('A8财富造物主1', left=501, top=468, width=196),
            _ocr_button_piece('奖励信息', left=350, top=531),
            _ocr_button_piece('积分', left=303, top=668),
            _ocr_button_piece('1200', left=665, top=668, width=68),
            _ocr_button_piece('我的阵容', left=776, top=385),
            _ocr_button_piece('下一页', left=921, top=885, width=80)]


def test_terminal_evaluation_checkpoint_then_detail_without_extra_input(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['settle_entry'])
    runtime.page = 'evaluation'
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kwargs: (_terminal_evaluation_pieces() if runtime.page == 'evaluation' else _terminal_detail_pieces()) if image.size == frame.size else []
    checkpoints = []
    def checkpoint(data, observed, phase):
        from copy import deepcopy
        checkpoints.append(deepcopy(data))
        data[phase]['evidence'] = 'captured.jpg'
        assert observed is frame
    def click(x, y):
        assert checkpoints[-1]['next_attempted'] is True
        runtime.clicks.append((x, y))
        runtime.page = 'detail'
    runtime.click_point = click
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'settle')
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1, terminal_checkpoint=checkpoint)
    assert len(runtime.clicks) == 1
    assert result['_reconcile_frame'] is frame
    assert result['terminal_report']['detail'] == {'result': 'win', 'grade': 'SS', 'hp': 91, 'node': '3-7', 'boss': '首领', 'mode': '标准博弈', 'difficulty': 'A8-1', 'score': 1200, 'evidence': 'captured.jpg'}
    runtime.clicks.clear()
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1, terminal_checkpoint=checkpoint)
    assert result['status'] == 'completed' and runtime.clicks == []


def test_terminal_never_retries_or_clicks_without_checkpoint(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['settle_entry'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kwargs: _terminal_evaluation_pieces() if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'settle')
    result = battle.run_cw_battle(session, runtime=runtime, timeout=0)
    assert result['terminal_report']['evaluation']['hp'] == 91
    assert runtime.clicks == []
    checkpoints = []
    def checkpoint(data, image, phase):
        checkpoints.append(dict(data))
        data['evaluation']['evidence'] = 'captured.jpg'
    monkeypatch.setattr(battle, 'monotonic', iter([0, 0, 2]).__next__)
    monkeypatch.setattr(battle, 'sleep', lambda _: None)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1, terminal_checkpoint=checkpoint)
    assert len(runtime.clicks) == 1 and result['status'] == 'in_progress'
    monkeypatch.setattr(battle, 'monotonic', iter([0, 2]).__next__)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1, terminal_checkpoint=checkpoint)
    assert len(runtime.clicks) == 1


def test_terminal_button_must_be_unique_and_exact():
    from PIL import Image
    from trail.scenes.cw.terminal import next_point
    battle = load_cw_battle_module()
    frame = Image.new('RGB', (960, 540))
    def obs(pieces):
        return battle.BattleObservation(FakeRuntime(), None, pieces, '', frame)
    button = _ocr_button_piece('下一步', left=460, top=442, width=40, height=18)
    assert next_point(obs([button])) == (960, 902)
    assert next_point(obs([button, button])) is None
    assert next_point(obs([_ocr_button_piece('点击下一步', left=460, top=442, width=40)])) is None


def test_terminal_stale_run_is_not_merged_into_detail(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    state = ensure_cw_state(session)
    state['run_facts'] = {'run_id': 'new'}
    state['battle_resume'] = {'terminal': {'run_id': 'old', 'evaluation': {'result': 'win', 'grade': 'SS'}, 'next_attempted': True}}
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['settle_entry'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kwargs: _terminal_detail_pieces() if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'settle')
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1)
    assert result['terminal_report']['run_id'] == 'new'
    assert 'evaluation' not in result['terminal_report']
    assert not runtime.clicks


def test_starting_hud_precedes_stale_preparation_controls(tmp_path):
    from PIL import Image
    battle = load_cw_battle_module()
    runtime = FakeRuntime()
    pieces = [_ocr_button_piece('3-2', left=690, top=0, width=50, height=25),
              _ocr_button_piece('0%', left=755, top=0, width=40, height=25),
              _ocr_button_piece('战斗开始 1/2', left=800, top=350, width=200),
              _ocr_button_piece('出战', left=1770, top=740)]
    obs = battle.BattleObservation(runtime, 'preparation', pieces, '3-20%战斗开始1/2出战', Image.new('RGB', (1920,1080)))
    assert battle.classify_cw_battle_page(runtime, session=build_session(tmp_path), detected_stage='preparation', observation=obs) == 'battle_progress'


@pytest.mark.parametrize('with_board', [True, False])
def test_pending_start_on_old_board_or_black_never_reclicks(tmp_path, monkeypatch, with_board):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)['battle_resume'] = {'start_pending': True, 'start_node': '3-2', 'report': {'round': '3-2'}}
    frame = Image.new('RGB', (1920,1080))
    runtime = ScriptedBattleRuntime(['preparation' if with_board else 'unknown'])
    runtime.screenshot = lambda: frame
    pieces = [_ocr_button_piece('备战阶段', left=427, top=36), _ocr_button_piece('出战', left=1770, top=740)] if with_board else []
    runtime.ocr_image = lambda image, **kwargs: pieces if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'preparation' if with_board else None)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=0)
    assert result['status'] == 'in_progress' and not runtime.clicks
    resume = ensure_cw_state(session)['battle_resume']
    assert resume['start_pending'] and resume['start_node'] == '3-2' and resume['report']['round'] == '3-2'


def test_pending_start_new_confirmed_node_returns_without_click(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)['battle_resume'] = {'start_pending': True, 'start_node': '3-2'}
    frame = Image.new('RGB', (1920,1080))
    runtime = ScriptedBattleRuntime(['preparation'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kwargs: [_ocr_button_piece('备战阶段', left=427, top=36), _ocr_button_piece('3-3', left=440, top=65, width=50, height=25), _ocr_button_piece('出战', left=1770, top=740)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'preparation')
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1)
    assert result['status'] == 'completed' and result['round'] == '3-2'
    assert not runtime.clicks and 'start_pending' not in ensure_cw_state(session)['battle_resume']


def test_durable_settlement_attempt_is_not_repeated(tmp_path, monkeypatch):
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(['settle_entry'], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle, runtime)
    snapshots = []
    from copy import deepcopy
    def checkpoint():
        snapshots.append(deepcopy(ensure_cw_state(session)['battle_resume']))
    result = battle.run_cw_battle(session, runtime=runtime, timeout=10, battle_checkpoint=checkpoint)
    assert result['status'] == 'in_progress'
    assert runtime.actions.count('continue') == 1
    assert any(s.get('settlement_actions', {}).get('entry') for s in snapshots)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=2, battle_checkpoint=checkpoint)
    assert runtime.actions.count('continue') == 1


@pytest.mark.parametrize('pieces', [
    [_ocr_button_piece('对局胜利', left=462, top=185), _ocr_button_piece('下一页', left=921, top=885, width=80)],
    [_ocr_button_piece('挑战成功', left=837, top=206), _ocr_button_piece('下一步', left=921, top=885, width=80)],
])
def test_incomplete_terminal_anchors_guard_against_generic_next(tmp_path, monkeypatch, pieces):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920,1080))
    runtime = ScriptedBattleRuntime(['settle_entry'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kwargs: pieces if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'settle')
    result = battle.run_cw_battle(session, runtime=runtime, timeout=0, battle_checkpoint=lambda: None)
    assert result['status'] == 'in_progress' and runtime.clicks == [] and runtime.actions == []


def test_terminal_checkpoint_deadline_passes_before_click(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920,1080))
    runtime = ScriptedBattleRuntime(['settle_entry'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kwargs: _terminal_evaluation_pieces() if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'settle')
    times = iter([0, 2, 2])
    monkeypatch.setattr(battle, 'monotonic', lambda: next(times))
    def checkpoint(data, image, phase):
        data[phase]['evidence'] = 'captured.jpg'
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1, terminal_checkpoint=checkpoint)
    assert result['status'] == 'in_progress' and not result['terminal_report'].get('next_attempted')
    assert not runtime.clicks
    runtime.page = 'evaluation'
    runtime.ocr_image = lambda image, **kwargs: (_terminal_evaluation_pieces() if runtime.page == 'evaluation' else _terminal_detail_pieces()) if image.size == frame.size else []
    def click(x, y):
        runtime.clicks.append((x, y))
        runtime.page = 'detail'
    runtime.click_point = click
    monkeypatch.setattr(battle, 'monotonic', lambda: 0)
    resumed = battle.run_cw_battle(session, runtime=runtime, timeout=1, terminal_checkpoint=checkpoint)
    assert resumed['status'] == 'completed' and len(runtime.clicks) == 1


def test_known_team_count_rejection_clears_durable_pending(tmp_path, monkeypatch):
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(['battle_start'])
    _patch_run_loop(monkeypatch, battle, runtime)
    def fail(runtime):
        raise TrailError('CW_BATTLE_TEAM_COUNT_INSUFFICIENT', 'too few')
    monkeypatch.setattr(battle, '_start_battle', fail)
    snapshots = []
    def checkpoint():
        snapshots.append(dict(ensure_cw_state(session)['battle_resume']))
    with pytest.raises(TrailError):
        battle.run_cw_battle(session, runtime=runtime, timeout=30, battle_checkpoint=checkpoint)
    assert snapshots[0]['start_pending'] is True and 'start_pending' not in snapshots[-1]


def test_run_facts_new_run_clears_old_resume_but_legacy_initialization_preserves_it(tmp_path):
    from trail.scenes.cw.run_facts import start_cw_run_facts, cw_run_facts
    session = build_session(tmp_path)
    state = ensure_cw_state(session)
    state['battle_resume'] = {'start_pending': True}
    cw_run_facts(session)
    assert state['battle_resume']['start_pending'] is True
    start_cw_run_facts(session)
    assert 'battle_resume' not in state


def test_combat_hint_survives_black_then_stale_old_board(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.page = 'hud'
    frame = Image.new('RGB', (1920,1080))
    runtime.screenshot = lambda: frame
    def ocr(image, **kwargs):
        if image.size != frame.size:
            return []
        if runtime.page == 'hud':
            return [_ocr_button_piece('3-4', left=690, top=0, width=50, height=25), _ocr_button_piece('67%', left=755, top=0, width=40, height=25), _ocr_piece('自动战斗')]
        if runtime.page == 'board':
            return [_ocr_button_piece('备战阶段', left=427, top=36), _ocr_button_piece('3-4', left=440, top=65, width=50, height=25), _ocr_button_piece('出战', left=1770, top=740)]
        return []
    runtime.ocr_image = ocr
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    first = battle.run_cw_battle(session, runtime=runtime, timeout=0)
    assert first['in_battle'] is True
    runtime.page = 'black'
    second = battle.run_cw_battle(session, runtime=runtime, timeout=0)
    assert second['in_battle'] is None
    runtime.page = 'board'
    third = battle.run_cw_battle(session, runtime=runtime, timeout=0)
    assert third['status'] == 'in_progress' and not runtime.clicks
    assert ensure_cw_state(session)['battle_resume']['in_battle_hint'] is True


@pytest.mark.parametrize('old_node', ['3-3', '3-4', '2-10'])
def test_pending_node_never_completes_on_older_or_equal_board(tmp_path, monkeypatch, old_node):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)['battle_resume'] = {'start_pending': True, 'start_node': '3-4'}
    frame = Image.new('RGB', (1920,1080))
    runtime = ScriptedBattleRuntime(['preparation'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kwargs: [_ocr_button_piece('备战阶段', left=427, top=36), _ocr_button_piece(old_node, left=440, top=65, width=50, height=25), _ocr_button_piece('出战', left=1770, top=740)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'preparation')
    for _ in range(2):
        result = battle.run_cw_battle(session, runtime=runtime, timeout=0)
        assert result['status'] == 'in_progress' and not runtime.clicks
        assert ensure_cw_state(session)['battle_resume']['start_pending']


def test_confirmed_settlement_does_not_finish_on_old_same_node_board(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)['battle_resume'] = {'report': {'round': '3-4', 'settlement': {'result': 'win', 'round': '3-4'}}, 'settlement_actions': {'entry': True}}
    frame = Image.new('RGB', (1920,1080))
    runtime = ScriptedBattleRuntime(['preparation'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kwargs: [_ocr_button_piece('备战阶段', left=427, top=36), _ocr_button_piece('3-4', left=440, top=65, width=50, height=25), _ocr_button_piece('出战', left=1770, top=740)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'preparation')
    for _ in range(2):
        result = battle.run_cw_battle(session, runtime=runtime, timeout=0, battle_checkpoint=lambda: None)
        assert result['status'] == 'in_progress' and result['stage'] == 'settle' and not runtime.clicks


def test_successful_start_publishes_side_effect_before_black_timeout(tmp_path, monkeypatch):
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(['battle_start', 'unknown'], sleep_advances_from=())
    _patch_run_loop(monkeypatch, battle, runtime)
    class Token:
        side_effect_stage = 'none'
        def throw_if_cancelled(self):
            pass
    token = Token()
    progress = []
    result = battle.run_cw_battle(session, runtime=runtime, timeout=5, cancellation_token=token,
        publish_progress=lambda data, frame: progress.append((data, frame, token.side_effect_stage)))
    assert result['status'] == 'in_progress'
    assert progress[-1][0]['phase'] == 'start_pending'
    assert progress[-1][1] is None and progress[-1][2] == 'side_effect_applied'

@pytest.mark.parametrize('old,new', [('2-6','2-7'), ('2-6','3-1')])
def test_later_battle_hud_replaces_previous_settlement(tmp_path, monkeypatch, old, new):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    resume = ensure_cw_state(session).setdefault('battle_resume', {})
    resume.update(report={'round': old, 'settlement': {'round': old, 'result': 'win', 'hp': 50}},
                  settlement_actions={'entry': True}, start_pending=True, start_node=old)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: [_ocr_button_piece(new, left=690, top=0, width=50, height=25),
        _ocr_button_piece('0%', left=755, top=0, width=40, height=25)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=0)
    assert result['status'] == 'in_progress' and result['in_battle'] is True
    assert result.get('result') is None and result.get('hp') is None
    assert resume['report']['round'] == new and resume['report']['progress_pct'] == 0
    assert 'settlement_actions' not in resume and 'start_pending' not in resume
    assert not runtime.clicks


def test_empty_front_denial_returns_blocked_and_clears_pending(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    resume = ensure_cw_state(session).setdefault('battle_resume', {})
    resume.update(start_pending=True, start_node='1-9')
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: [_ocr_button_piece('前台区域无角色，无法出战', left=750, top=510, width=420, height=40)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=10)
    assert result['status'] == 'blocked' and result['reason'] == 'empty_front'
    assert result['_reconcile_frame'] is frame and 'start_pending' not in resume
    assert not runtime.clicks


def test_fresh_empty_front_prevents_start(tmp_path, monkeypatch):
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)['slots'] = {'stale': False, 'front': [None, None, None]}
    runtime = ScriptedBattleRuntime(['battle_start'])
    _patch_run_loop(monkeypatch, battle, runtime)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=10)
    assert result['status'] == 'blocked' and result['reason'] == 'empty_front'
    assert not runtime.clicks


def test_current_empty_front_counter_blocks_start_even_without_slots(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    runtime = ScriptedBattleRuntime(['battle_start'])
    frame = Image.new('RGB', (1920, 1080))
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: [_ocr_button_piece('0/3', left=895, top=235, width=125, height=55),
        _ocr_button_piece('前台区域', left=930, top=305, width=80, height=25),
        _ocr_button_piece('出战', left=1770, top=740)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'preparation')
    result = battle.run_cw_battle(session, runtime=runtime, timeout=10)
    assert result['status'] == 'blocked' and result['reason'] == 'empty_front'
    assert not runtime.clicks


@pytest.mark.parametrize('label,node,old_key', [('继续挑战', '2-6', 'entry'), ('前往结算', '3-7', 'followup:前往结算')])
def test_new_positioned_result_page_advances_once_despite_older_checkpoint(tmp_path, monkeypatch, label, node, old_key):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    resume = ensure_cw_state(session).setdefault('battle_resume', {})
    resume.update(report={'round': '2-5', 'settlement': {'round': '2-5', 'result': 'win'}},
                  settlement_actions={old_key: True})
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: [_ocr_button_piece('挑战成功', left=835, top=200, width=250, height=55),
        _ocr_button_piece(node, left=790, top=275, width=45, height=25),
        _ocr_button_piece(label, left=900, top=885, width=120, height=35)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'settle')
    clock = [0.0]
    monkeypatch.setattr(battle, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(battle, 'sleep', lambda duration: clock.__setitem__(0, clock[0] + duration))
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1, battle_checkpoint=lambda: None)
    assert result['status'] == 'in_progress'
    assert runtime.clicks == [(960, 902)]


@pytest.mark.parametrize("visible_percentage", [True, False])
def test_final_result_through_black_evaluation_and_detail(tmp_path, monkeypatch, visible_percentage):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.page = 'result'
    runtime.screenshot = lambda: frame
    def ocr(image, **kw):
        if image.size != frame.size:
            return []
        if runtime.page == 'result':
            return [_ocr_button_piece('挑战成功', left=835, top=200, width=250, height=55),
                    _ocr_button_piece('3-7首领', left=790, top=275, width=110, height=25),
                    _ocr_button_piece('挑战进度100%' if visible_percentage else '挑战进度', left=850, top=380, width=200, height=30),
                    _ocr_button_piece('100', left=910, top=710, width=100, height=70),
                    _ocr_button_piece('前往结算', left=900, top=885, width=120, height=35)]
        if runtime.page == 'evaluation':
            return _terminal_evaluation_pieces()
        if runtime.page == 'detail':
            return _terminal_detail_pieces()
        return []
    runtime.ocr_image = ocr
    def click(x, y):
        runtime.clicks.append((x, y))
        runtime.page = 'black' if runtime.page == 'result' else 'detail'
    runtime.click_point = click
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    clock = [0.0]
    monkeypatch.setattr(battle, 'monotonic', lambda: clock[0])
    def tick(duration):
        clock[0] += duration
        if runtime.page == 'black':
            runtime.page = 'evaluation'
    monkeypatch.setattr(battle, 'sleep', tick)
    phases = []
    def checkpoint(data, image, phase):
        phases.append(phase)
        data[phase]['evidence'] = phase + '.jpg'
    result = battle.run_cw_battle(session, runtime=runtime, timeout=5,
        terminal_checkpoint=checkpoint, battle_checkpoint=lambda: None)
    assert result['status'] == 'completed' and result['stage'] == 'game_over'
    assert runtime.clicks == [(960, 902), (961, 903)]
    assert 'evaluation' in phases and 'detail' in phases
    assert result['terminal_report']['evaluation']['grade'] == 'SS'
    assert result['terminal_report']['detail']['node'] == '3-7'
    assert result['battle_report']['settlement_progress_pct'] == (100 if visible_percentage else None)
    assert result['battle_report']['settlement_hp'] == 100 and result['hp'] == 91



def test_older_hud_does_not_replace_later_round_state(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    resume = ensure_cw_state(session).setdefault('battle_resume', {})
    resume.update(report={'round': '3-1', 'settlement': {'round': '3-1', 'result': 'win'}}, settlement_actions={'entry': True})
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: [_ocr_button_piece('2-7', left=690, top=0, width=50, height=25),
        _ocr_button_piece('0%', left=755, top=0, width=40, height=25)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    battle.run_cw_battle(session, runtime=runtime, timeout=0)
    assert resume['report']['round'] == '3-1' and resume['settlement_actions']['entry']
    assert not runtime.clicks


def test_settlement_action_coordinates_are_normalized():
    from PIL import Image
    battle = load_cw_battle_module()
    runtime = FakeRuntime()
    pieces = [_ocr_button_piece('继续挑战', left=450, top=442, width=60, height=18)]
    obs = battle.BattleObservation(runtime, None, pieces, '继续挑战', Image.new('RGB', (960, 540)))
    box = battle._find_action_button_box_from_observation(obs, allowed_texts={'继续挑战'})
    assert battle._box_center(box) == (960, 902)


def test_live_board_does_not_obey_old_empty_front_snapshot(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)['slots'] = {'stale': False, 'front': [None, None]}
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: [_ocr_button_piece('出战', left=1770, top=740)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: 'preparation')
    monkeypatch.setattr(battle, '_fail_if_battle_team_count_confirm_dialog', lambda runtime: None)
    clock = [0.0]
    monkeypatch.setattr(battle, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(battle, 'sleep', lambda d: clock.__setitem__(0, clock[0] + d))
    result = battle.run_cw_battle(session, runtime=runtime, timeout=0.5)
    assert result['status'] == 'in_progress' and len(runtime.clicks) == 1


def test_terminal_detail_keeps_matching_node_battle_report(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    ensure_cw_state(session)['battle_resume'] = {'report': {'round': '3-7', 'progress_pct': 100,
        'settlement': {'round': '3-7', 'hp': 88}}}
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: _terminal_detail_pieces() if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1)
    assert result['hp'] == 91 and result['battle_report']['settlement_hp'] == 88
    assert result['battle_report']['progress_pct'] == 100


def test_settle_next_clicks_once_then_waits_without_clicking_evaluation(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.page = 'result'
    runtime.screenshot = lambda: frame
    def ocr(image, **kw):
        if image.size != frame.size:
            return []
        if runtime.page == 'result':
            return [_ocr_button_piece('挑战成功', left=835, top=200, width=250, height=55),
                    _ocr_button_piece('3-7', left=790, top=275, width=45, height=25),
                    _ocr_button_piece('前往结算', left=900, top=885, width=120, height=35)]
        return _terminal_evaluation_pieces() if runtime.page == 'evaluation' else (_terminal_detail_pieces() if runtime.page == 'detail' else [])
    runtime.ocr_image = ocr
    def click(x, y):
        runtime.clicks.append((x, y)); runtime.page = 'black' if runtime.page == 'result' else 'detail'
    runtime.click_point = click
    clock = [0.0]
    monkeypatch.setattr(battle, 'monotonic', lambda: clock[0])
    def tick(d):
        clock[0] += d
        if runtime.page == 'black': runtime.page = 'evaluation'
    monkeypatch.setattr(battle, 'sleep', tick)
    result = battle.advance_cw_settlement_once(session, runtime=runtime, checkpoint=lambda: None, timeout=1)
    assert result['status'] == 'completed' and runtime.clicks == [(960, 902)]
    again = battle.advance_cw_settlement_once(session, runtime=runtime, checkpoint=lambda: None, timeout=1)
    assert again['status'] == 'completed' and again['value'] == 'game_over'
    assert runtime.clicks == [(960, 902), (961, 903)]


def test_start_progress_after_dispatch_keeps_preparation_frame(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: [_ocr_button_piece('出战', left=1770, top=740)] if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    monkeypatch.setattr(battle, '_fail_if_battle_team_count_confirm_dialog', lambda runtime: None)
    clock = [0.0]
    monkeypatch.setattr(battle, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(battle, 'sleep', lambda d: clock.__setitem__(0, clock[0] + d))
    class Token:
        side_effect_stage = 'none'
        def throw_if_cancelled(self): pass
    progress = []
    battle.run_cw_battle(session, runtime=runtime, timeout=.5, cancellation_token=Token(),
        publish_progress=lambda data, captured: progress.append((data, captured)))
    assert progress[-1][0]['phase'] == 'start_pending' and progress[-1][1] is frame


def test_terminal_wait_preserves_pending_node_report(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    report = {'round': '3-7', 'progress_pct': 100}
    ensure_cw_state(session)['battle_resume'] = {'report': report}
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: _terminal_evaluation_pieces() if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=0)
    assert result['status'] == 'in_progress' and ensure_cw_state(session)['battle_resume']['report'] == report


def test_settle_next_evaluation_checkpoint_survives_battle_run_and_detail(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    state = ensure_cw_state(session)
    state['run_facts'] = {'run_id': 'run-1'}
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.page = 'evaluation'
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: (_terminal_evaluation_pieces() if runtime.page == 'evaluation' else _terminal_detail_pieces()) if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    clock = [0.0]
    monkeypatch.setattr(battle, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(battle, 'sleep', lambda d: clock.__setitem__(0, clock[0] + d))
    result = battle.advance_cw_settlement_once(session, runtime=runtime, checkpoint=lambda: None, timeout=.5)
    assert result['status'] == 'in_progress' and len(runtime.clicks) == 1
    assert state['battle_resume']['terminal']['run_id'] == 'run-1'
    battle.run_cw_battle(session, runtime=runtime, timeout=0, terminal_checkpoint=lambda *a: None)
    assert len(runtime.clicks) == 1
    runtime.page = 'detail'
    result = battle.run_cw_battle(session, runtime=runtime, timeout=1, terminal_checkpoint=lambda *a: None)
    assert result['terminal_report']['evaluation']['grade'] == 'SS'
    assert result['terminal_report']['detail']['node'] == '3-7'


def test_settle_next_on_terminal_detail_is_completed_without_click(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    runtime.ocr_image = lambda image, **kw: _terminal_detail_pieces() if image.size == frame.size else []
    result = battle.advance_cw_settlement_once(session, runtime=runtime, checkpoint=lambda: None)
    assert result['status'] == 'completed' and result['value'] == 'game_over' and not runtime.clicks


def test_settle_next_forward_preparation_clears_old_resume_and_allows_start(tmp_path, monkeypatch):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    resume = ensure_cw_state(session).setdefault('battle_resume', {})
    resume['report'] = {'round': '2-6', 'settlement': {'round': '2-6', 'result': 'win'}}
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.page = 'result'
    runtime.screenshot = lambda: frame
    def ocr(image, **kw):
        if image.size != frame.size: return []
        if runtime.page == 'result':
            return [_ocr_button_piece('挑战结束', left=835, top=200, width=250, height=55),
                    _ocr_button_piece('2-6', left=790, top=275, width=45, height=25),
                    _ocr_button_piece('继续挑战', left=900, top=885, width=120, height=35)]
        return [_ocr_button_piece('备战阶段', left=427, top=36),
                _ocr_button_piece('2-7', left=440, top=65, width=50, height=25),
                _ocr_button_piece('出战', left=1770, top=740)]
    runtime.ocr_image = ocr
    def click(x, y):
        runtime.clicks.append((x, y)); runtime.page = 'preparation'
    runtime.click_point = click
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    monkeypatch.setattr(battle, '_fail_if_battle_team_count_confirm_dialog', lambda runtime: None)
    clock = [0.0]
    monkeypatch.setattr(battle, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(battle, 'sleep', lambda d: clock.__setitem__(0, clock[0] + d))
    result = battle.advance_cw_settlement_once(session, runtime=runtime, checkpoint=lambda: None, timeout=1)
    assert result['status'] == 'completed' and not resume.get('settlement_actions') and not resume.get('report')
    result = battle.run_cw_battle(session, runtime=runtime, timeout=.5)
    assert result['status'] == 'in_progress' and len(runtime.clicks) == 2


@pytest.mark.parametrize('digits,expected_hp', [
    ([(910, 710, '100')], 100),
    ([(910, 510, '100')], None),
    ([(910, 710, '100'), (920, 720, '99')], None),
])
def test_terminal_result_timeout_reports_only_unambiguous_central_hp(tmp_path, monkeypatch, digits, expected_hp):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.screenshot = lambda: frame
    pieces = [_ocr_button_piece('挑战成功', left=835, top=200, width=250, height=55),
              _ocr_button_piece('3-7首领', left=790, top=275, width=110, height=25),
              _ocr_button_piece('挑战进度', left=850, top=380, width=200, height=30),
              _ocr_button_piece('前往结算', left=900, top=885, width=120, height=35)]
    pieces.extend(_ocr_button_piece(value, left=x, top=y, width=80, height=40) for x, y, value in digits)
    runtime.ocr_image = lambda image, **kw: pieces if image.size == frame.size else []
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    checkpoints = []
    result = battle.run_cw_battle(session, runtime=runtime, timeout=0,
        battle_checkpoint=lambda: checkpoints.append(dict(ensure_cw_state(session)['battle_resume']['report'])))
    assert result['status'] == 'in_progress' and result['stage'] == 'settle'
    assert result['round'] == '3-7' and result['result'] == 'win'
    assert result.get('hp') == expected_hp and result['battle_report']['settlement_hp'] == expected_hp
    assert result['battle_report']['settlement_progress_pct'] is None
    assert checkpoints[-1]['settlement'].get('hp') == expected_hp and not runtime.clicks


@pytest.mark.parametrize('label,headline', [('下一步', '挑战结束'), ('前往结算', '挑战成功')])
def test_followup_report_observed_once_for_legacy_and_final_pages(tmp_path, monkeypatch, label, headline):
    from PIL import Image
    battle = load_cw_battle_module()
    session = build_session(tmp_path)
    frame = Image.new('RGB', (1920, 1080))
    runtime = ScriptedBattleRuntime(['unknown'])
    runtime.page = 'result'
    runtime.screenshot = lambda: frame
    pieces = [_ocr_button_piece(headline, left=835, top=200, width=250, height=55),
              _ocr_button_piece('挑战进度100%', left=850, top=380, width=200, height=30),
              _ocr_button_piece(label, left=900, top=885, width=120, height=35)]
    if label == '前往结算':
        pieces.append(_ocr_button_piece('3-7首领', left=790, top=275, width=110, height=25))
    runtime.ocr_image = lambda image, **kw: pieces if image.size == frame.size and runtime.page == 'result' else []
    def click(x, y):
        runtime.clicks.append((x, y)); runtime.page = 'black'
    runtime.click_point = click
    monkeypatch.setattr(battle, 'build_cw_stage_detector', lambda runtime: lambda: None)
    clock = [0.0]
    monkeypatch.setattr(battle, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(battle, 'sleep', lambda d: clock.__setitem__(0, clock[0] + d))
    observations = []
    original = battle.BattleReport.observe
    def observe(self, *args, **kwargs):
        observations.append(kwargs.get('phase'))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(battle.BattleReport, 'observe', observe)
    result = battle.run_cw_battle(session, runtime=runtime, timeout=.2, battle_checkpoint=lambda: None)
    assert result['battle_report']['settlement_progress_pct'] == 100
    assert observations.count('settlement') == 1 and len(runtime.clicks) == 1
