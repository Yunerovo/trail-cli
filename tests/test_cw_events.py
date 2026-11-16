from __future__ import annotations

from copy import deepcopy
import importlib
from itertools import chain, repeat
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from trail.core.errors import TrailError
from trail.scenes.cw.models import ensure_cw_state
from trail.session.store import SessionStore


def result_text(result) -> str:
    return result.stdout + getattr(result, "stderr", "")


def load_cw_events_module():
    try:
        return importlib.import_module("trail.scenes.cw.events")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing trail.scenes.cw.events: {exc}")


def _ocr_piece(text: str, *, left: int = 400, top: int = 500, width: int = 120, height: int = 36) -> dict[str, object]:
    return {
        "text": text,
        "box": {"left": left, "top": top, "width": width, "height": height},
    }


class EventRouterRuntime:
    def __init__(self, pieces: list[dict[str, object]] | None = None):
        self.pieces: list[dict[str, object]] = pieces or []
        self.clicks: list[tuple[int, int]] = []
        self.locate_calls: list[str] = []
        self.wait_calls: list[str] = []
        self.locate_result: dict[str, int] | None = None
        self.wait_result: dict[str, int] | None = None

    def ocr(self, **_kwargs: object) -> list[dict[str, object]]:
        return self.pieces

    def locate(self, path: str) -> dict[str, int] | None:
        self.locate_calls.append(path)
        return self.locate_result

    def wait_img(self, path: str, **_kwargs: object) -> dict[str, int] | None:
        self.wait_calls.append(path)
        return self.wait_result

    def click_point(self, x: int, y: int) -> None:
        self.clicks.append((x, y))


@pytest.mark.parametrize(("labels", "option", "target"), [
    ([835], 1, (835, 715)),
    ([1184], 1, None),
    ([1184], 2, (1184, 715)),
    ([656, 1011], 2, (1011, 715)),
    ([835], 3, None),
    ([835, 1011], 1, None),
])
def test_aura_reselect_clicks_only_the_requested_physical_card(monkeypatch, labels, option, target):
    events = load_cw_events_module()
    monkeypatch.setattr(events, "sleep", lambda seconds: None)
    clicks = []

    def ocr(*, capture=None, **kwargs):
        if capture is not None:
            return [
                _ocr_piece("重选主角", left=x - capture["from_x"] - 44,
                           top=715 - capture["from_y"] - 12, width=88, height=24)
                for x in labels
            ]
        if clicks:
            return [_ocr_piece("是否重新选择主角和光环的效果，将消耗1次重选次数。")]
        return [_ocr_piece("主角光环与奇遇详情")]

    from PIL import Image
    region = events._AURA_RESELECT_ROW
    runtime = SimpleNamespace(
        ocr=ocr, capture_image=lambda **kwargs: Image.new("RGB", (1920, 1080)),
        ocr_image=lambda image, **kwargs: ocr(capture=region),
        click_point=lambda x, y: clicks.append((x, y)),
    )
    if target is None:
        with pytest.raises(TrailError, match="未识别到可用的重选按钮") as failure:
            events.open_cw_plot_armor_reselect(runtime, option=option)
        assert failure.value.code == "CW_AURA_OPTION_UNAVAILABLE"
        assert clicks == []
    else:
        assert events.open_cw_plot_armor_reselect(runtime, option=option)["value"] == "plot_armor"
        assert clicks == [target]

@pytest.mark.parametrize("native_size", [(1280, 720), (1344, 756)])
def test_aura_reselect_uses_normalized_frame_for_scaled_clients(monkeypatch, native_size):
    from PIL import Image
    events = load_cw_events_module()
    monkeypatch.setattr(events, "sleep", lambda seconds: None)
    clicks = []
    native = Image.new("RGB", native_size)
    def capture_image(*, normalize):
        assert normalize is True
        return native.resize((1920, 1080))
    def ocr_image(image, **kwargs):
        assert image.size == (1000, 76)
        return [_ocr_piece("重选主角", left=1008 - 460 - 44, top=715 - 680 - 12, width=88, height=24)]
    def ocr(**kwargs):
        return [_ocr_piece("是否重新选择主角和光环的效果，将消耗1次重选次数。" if clicks else "主角光环与奇遇详情")]
    runtime = SimpleNamespace(capture_image=capture_image, ocr_image=ocr_image, ocr=ocr,
                              click_point=lambda x, y: clicks.append((x, y)))
    assert events.open_cw_plot_armor_reselect(runtime, option=2)["value"] == "plot_armor"
    assert clicks == [(1008, 715)]


EVENT_NEXT_ACTIONS = {
    "replenish": "cw.replenish.choose",
    "invest": "cw.invest.choose",
    "encounter": "cw.encounter.choose",
    "fortune": "cw.fortune.choose",
}


@pytest.mark.parametrize(
    ("method_name", "expected_options"),
    [
        ("read_cw_replenish", [1, 2, 3, 4, 5]),
        ("read_cw_invest", [1, 2, 3]),
        ("read_cw_encounter", [1, 2]),
        ("read_cw_fortune", [1, 2]),
    ],
)
def test_event_reads_return_expected_options(tmp_path, method_name, expected_options):
    events_module = load_cw_events_module()
    method = getattr(events_module, method_name, None)
    assert method is not None

    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)

    assert method(session) == {"options": expected_options}


@pytest.mark.parametrize(
    ("method_name", "option"),
    [
        ("choose_cw_replenish", 1),
        ("choose_cw_invest", 2),
        ("choose_cw_encounter", 1),
        ("choose_cw_fortune", 2),
    ],
)
def test_event_chooses_call_chooser_and_invalidate_stage_snapshot(tmp_path, method_name, option):
    events_module = load_cw_events_module()
    method = getattr(events_module, method_name, None)
    assert method is not None

    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    ensure_cw_state(session)["stage"] = {"value": "shop", "stale": False}
    session.last_stage = {"scene": "cw", "value": "shop"}
    chosen: list[int] = []

    refreshed = method(session, option=option, chooser=lambda selected: chosen.append(selected))

    assert refreshed.scene_state["cw"]["stage"] == {"stale": True}
    assert refreshed.last_stage is None
    assert chosen == [option]


def test_season_two_replenish_chooses_fifth_card_and_uses_visible_confirm(monkeypatch):
    events = load_cw_events_module()
    supply = [_ocr_piece("补给阶段"), _ocr_piece("返回备战界面")]
    readings = iter([
        supply,
        [*supply, _ocr_piece("已选择1/1"), _ocr_piece("确认", left=1659, top=978, width=91)],
        [_ocr_piece("备战阶段")],
    ])
    runtime = EventRouterRuntime()
    runtime.ocr = lambda **_: next(readings)
    monkeypatch.setattr(events, "sleep", lambda _: None)

    events.build_cw_replenish_chooser(runtime)(5)

    assert runtime.clicks == [(1671, 422), (1704, 996)]


def test_season_two_invest_checks_the_page_before_clicking():
    events = load_cw_events_module()
    runtime = EventRouterRuntime([_ocr_piece("奇遇伙伴")])

    with pytest.raises(TrailError) as error:
        events.build_cw_invest_chooser(runtime)(1)

    assert error.value.code == "CW_EVENT_PAGE_MISMATCH"
    assert runtime.clicks == []


@pytest.mark.parametrize("option", [1, 2, 3])
def test_invest_requires_selected_outline_and_stable_transition(monkeypatch, option):
    from PIL import Image, ImageDraw
    events = load_cw_events_module()
    image = Image.new("RGB", (1920, 1080))
    left, right = ((244, 671), (747, 1174), (1250, 1677))[option - 1]
    ImageDraw.Draw(image).rectangle((left, 195, right, 822), outline="white", width=5)
    assert events._selected_invest_card(image) == option
    button = {"text": "确认", "bounds": (940, 975, 1010, 1005)}
    title = {"text": "五百强", "bounds": (left + 160, 480, left + 250, 505)}
    prep = {"value": "preparation", "entries": [
        {"text": "备战阶段", "bounds": (425, 30, 505, 55)},
        {"text": "1-3", "bounds": (440, 65, 485, 90)},
        {"text": "出战", "bounds": (1770, 740, 1850, 770)}]}
    readings = iter([({"value": "invest", "entries": []}, image),
                     ({"value": "invest", "entries": [button, title]}, image),
                     (prep, image), (prep, image)])
    monkeypatch.setattr(events, "_invest_observation", lambda *args: next(readings))
    monkeypatch.setattr(events, "sleep", lambda _: None)
    runtime = EventRouterRuntime()
    events.build_cw_invest_chooser(runtime)(option)
    assert runtime.clicks == [((460, 960, 1460)[option - 1], 400), (975, 990)]


@pytest.mark.parametrize("reason", ["wrong_card", "no_outline", "off_region", "duplicate", "overlay"])
def test_invest_never_confirms_ambiguous_selection(monkeypatch, reason):
    from PIL import Image, ImageDraw
    events = load_cw_events_module()
    image = Image.new("RGB", (1920, 1080))
    if reason != "no_outline":
        left, right = (747, 1174) if reason == "wrong_card" else (244, 671)
        ImageDraw.Draw(image).rectangle((left, 195, right, 822), outline="white", width=5)
    button = {"text": "确认", "bounds": (940, 975, 1010, 1005)}
    if reason == "off_region":
        button["bounds"] = (1400, 975, 1470, 1005)
    title = {"text": "五百强", "bounds": (404, 480, 494, 505)}
    entries = [button, button, title] if reason == "duplicate" else [button, title]
    readings = iter([({"value": "invest", "entries": []}, image),
                     ({"value": None if reason == "overlay" else "invest", "entries": entries}, image)])
    monkeypatch.setattr(events, "_invest_observation", lambda *args: next(readings))
    monkeypatch.setattr(events, "sleep", lambda _: None)
    runtime = EventRouterRuntime()
    with pytest.raises(TrailError):
        events.build_cw_invest_chooser(runtime)(1)
    assert runtime.clicks == [(460, 400)]


def test_invest_refuses_stale_snapshot_title(monkeypatch):
    from PIL import Image, ImageDraw
    events = load_cw_events_module()
    image = Image.new("RGB", (1920, 1080))
    ImageDraw.Draw(image).rectangle((244, 195, 671, 822), outline="white", width=5)
    panel = {"value": "invest", "entries": [
        {"text": "新卡", "bounds": (404, 480, 494, 505)},
        {"text": "确认", "bounds": (940, 975, 1010, 1005)}]}
    readings = iter([({"value": "invest", "entries": []}, image), (panel, image)])
    monkeypatch.setattr(events, "_invest_observation", lambda *args: next(readings))
    monkeypatch.setattr(events, "sleep", lambda _: None)
    runtime = EventRouterRuntime()
    with pytest.raises(TrailError) as error:
        events.build_cw_invest_chooser(runtime, expected_title="旧卡")(1)
    assert error.value.code == "CW_EVENT_OPTION_UNCONFIRMED"
    assert runtime.clicks == [(460, 400)]


def test_invest_successor_requires_header_node_and_board_anchor():
    events = load_cw_events_module()
    entries = [{"text": "备战阶段", "bounds": (425, 30, 505, 55)},
               {"text": "1-3", "bounds": (440, 65, 485, 90)},
               {"text": "出战", "bounds": (1770, 740, 1850, 770)}]
    panel = {"value": "preparation", "entries": entries}
    assert events._confirmed_invest_successor(panel)["node"] == "1-3"
    for missing in range(3):
        assert events._confirmed_invest_successor({"value": "preparation", "entries": [e for i, e in enumerate(entries) if i != missing]}) is None


@pytest.mark.parametrize("code,stale", [("CW_EVENT_PAGE_MISMATCH", False), ("CW_EVENT_OPTION_UNCONFIRMED", True), ("CW_EVENT_CONFIRM_UNCONFIRMED", True)])
def test_invest_failed_after_click_invalidates_strategy(tmp_path, code, stale):
    events = load_cw_events_module()
    from tests.conftest import build_fake_cw_session
    session = build_fake_cw_session(tmp_path)
    state = ensure_cw_state(session)
    state["strategy"] = {"cards": [{"card_idx": 1}], "stale": False}
    state["stage"] = {"value": "invest", "stale": False}
    def fail(_):
        raise TrailError(code, "failure")
    with pytest.raises(TrailError):
        events.choose_cw_invest(session, option=1, chooser=fail)
    assert state["strategy"]["stale"] is stale
    assert state["stage"]["stale"] is stale


def test_event_handle_returns_type_and_action(tmp_path):
    events_module = load_cw_events_module()
    handle_cw_event = getattr(events_module, "handle_cw_event", None)
    assert handle_cw_event is not None

    from tests.conftest import build_fake_cw_session, fake_event_handler

    session = build_fake_cw_session(tmp_path)
    ensure_cw_state(session)["stage"] = {"value": "event", "stale": False}
    session.last_stage = {"scene": "cw", "value": "event"}
    result = handle_cw_event(session, handler=fake_event_handler)

    assert result == {"event_type": "special", "handled": True, "handled_action": "confirm"}
    assert session.scene_state["cw"]["stage"] == {"stale": True}
    assert session.last_stage is None


def test_detect_cw_event_unknown_does_not_click_runtime():
    events_module = load_cw_events_module()
    runtime = EventRouterRuntime([_ocr_piece("无法识别的新事件")])

    result = events_module.build_cw_event_router(runtime)(None)

    assert result["event_type"] == "unknown"
    assert result["handled"] is False
    assert result["next_action"] == "manual"
    assert runtime.clicks == []


def test_detect_cw_event_unknown_ignores_stage_event_template_hit(monkeypatch):
    events_module = load_cw_events_module()
    monkeypatch.setattr(events_module, "_asset", lambda alias: alias, raising=False)
    runtime = EventRouterRuntime([_ocr_piece("无法识别的新事件")])
    runtime.locate_result = {"left": 100, "top": 100, "width": 20, "height": 20}

    result = events_module.build_cw_event_router(runtime)(None)

    assert result["event_type"] == "unknown"
    assert result["handled"] is False
    assert result["next_action"] == "manual"
    assert runtime.locate_calls == []
    assert runtime.clicks == []


@pytest.mark.parametrize(
    ("text", "event_type", "next_action"),
    [
        ("补给", "replenish", "cw.replenish.choose"),
        ("投资事件", "invest", "cw.invest.choose"),
        ("遭遇事件", "encounter", "cw.encounter.choose"),
        ("命运卜者", "fortune", "cw.fortune.choose"),
    ],
)
def test_detect_cw_event_recognized_unhandled_is_no_click(text: str, event_type: str, next_action: str):
    events_module = load_cw_events_module()
    runtime = EventRouterRuntime([_ocr_piece(text)])

    result = events_module.build_cw_event_router(runtime)(None)

    assert result["event_type"] == event_type
    assert result["handled"] is False
    assert result["next_action"] == next_action
    assert runtime.clicks == []


def _fill_fresh_event_session_facts(session) -> dict[str, object]:
    cw_state = ensure_cw_state(session)
    cw_state["stage"] = {
        "value": "event",
        "stale": False,
        "status": {"stale": False, "level": 7, "exp": "4/52", "team_size": "3/3"},
    }
    cw_state["slots"] = {"stale": False, "front": [{"name": "希儿"}], "back": [], "hand": [{"name": "佩拉"}]}
    cw_state["shop"] = {"opened": True, "stale": False, "items": [{"name": "银狼", "cost": 3}], "coins": 40}
    cw_state["equipment"] = {"stale": False, "items": [{"name": "风暴"}], "uncertain": 0}
    cw_state["strategy"] = {"stale": False, "cards": [{"strategy_title": "快攻"}]}
    cw_state["sell_plan"] = {"stale": False, "items": [{"name": "佩拉", "priority": 10}]}
    cw_state["metrics"] = {"crystals": 2}
    session.last_stage = {"scene": "cw", "value": "event"}
    return deepcopy(cw_state)


@pytest.mark.parametrize(
    ("event_type", "next_action"),
    [(event_type, next_action) for event_type, next_action in EVENT_NEXT_ACTIONS.items()],
)
def test_handle_cw_event_recognized_unhandled_does_not_mark_stale_or_click(tmp_path, event_type: str, next_action: str):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    before_cw_state = _fill_fresh_event_session_facts(session)
    before_last_stage = deepcopy(session.last_stage)
    runtime = EventRouterRuntime()

    result = events_module.handle_cw_event(
        session,
        router=lambda _choice: {"event_type": event_type, "handled": False, "next_action": next_action},
    )

    assert result["event_type"] == event_type
    assert result["handled"] is False
    assert result["next_action"] == next_action
    assert session.scene_state["cw"] == before_cw_state
    assert session.last_stage == before_last_stage
    assert ensure_cw_state(session)["metrics"] == before_cw_state["metrics"]
    assert runtime.clicks == []


def test_handle_cw_event_lv999_without_choice_does_not_mark_stale(tmp_path):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    before_cw_state = _fill_fresh_event_session_facts(session)
    before_last_stage = deepcopy(session.last_stage)
    runtime = EventRouterRuntime([_ocr_piece("银狼 LV.999 选择强化方向")])

    result = events_module.handle_cw_event(session, router=events_module.build_cw_event_router(runtime))

    assert result["event_type"] == "lv999_choice"
    assert result["handled"] is False
    assert result["next_action"] == "manual"
    assert session.scene_state["cw"] == before_cw_state
    assert session.last_stage == before_last_stage
    assert runtime.clicks == []


def test_handle_cw_event_unhandled_with_variable_cost_choice_does_not_mutate_session(tmp_path):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    before_cw_state = _fill_fresh_event_session_facts(session)
    cw_state = ensure_cw_state(session)
    cw_state["variable_cost_roles"] = {"银狼LV.999": {"cost": 3, "star": 2, "choice_available": True}}
    cw_state["shop"] = {"opened": True, "stale": False, "items": [{"name": "银狼LV.999", "cost": 3}]}
    before_cw_state = deepcopy(cw_state)
    before_last_stage = deepcopy(session.last_stage)
    runtime = EventRouterRuntime()

    with pytest.raises(ValueError, match="unhandled cw event cannot apply variable cost choice"):
        events_module.handle_cw_event(
            session,
            router=lambda _choice: {"event_type": "replenish", "handled": False, "next_action": "cw.replenish.choose"},
            variable_cost_choice={"role_name": "银狼LV.999", "choice": "cost_up"},
        )

    assert session.scene_state["cw"] == before_cw_state
    assert session.last_stage == before_last_stage
    assert ensure_cw_state(session)["shop"] == before_cw_state["shop"]
    assert runtime.clicks == []


def test_handle_cw_event_unknown_marks_readable_facts_stale_without_touching_metrics(tmp_path):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    _fill_fresh_event_session_facts(session)
    cw_state = ensure_cw_state(session)
    cw_state["variable_cost_roles"] = {"银狼LV.999": {"cost": 4, "star": 1}}
    before_metrics = deepcopy(cw_state["metrics"])
    before_last_result = deepcopy(session.last_result)
    before_last_screenshot = deepcopy(session.last_screenshot)

    result = events_module.handle_cw_event(
        session,
        router=lambda _choice: {"event_type": "unknown", "handled": False, "next_action": "manual"},
    )

    assert result == {"event_type": "unknown", "handled": False, "next_action": "manual"}
    assert cw_state["stage"]["stale"] is True
    assert cw_state["stage"]["status"]["stale"] is True
    assert cw_state["slots"]["stale"] is True
    assert cw_state["shop"]["stale"] is True
    assert cw_state["equipment"]["stale"] is True
    assert cw_state["sell_plan"]["stale"] is True
    assert cw_state["strategy"]["stale"] is True
    assert cw_state["variable_cost_roles_stale"] is True
    assert "stale" not in cw_state["variable_cost_roles"]
    assert "stale" not in cw_state["variable_cost_roles"]["银狼LV.999"]
    assert cw_state["metrics"] == before_metrics
    assert session.last_result == before_last_result
    assert session.last_screenshot == before_last_screenshot


def test_handle_cw_event_unknown_creates_missing_readable_subtrees(tmp_path):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    cw_state = ensure_cw_state(session)
    for key in ("slots", "shop", "equipment", "sell_plan", "strategy"):
        cw_state.pop(key, None)

    events_module.handle_cw_event(
        session,
        router=lambda _choice: {"event_type": "unknown", "handled": False, "next_action": "manual"},
    )

    assert cw_state["slots"] == {"stale": True}
    assert cw_state["shop"] == {"stale": True}
    assert cw_state["equipment"] == {"stale": True}
    assert cw_state["sell_plan"] == {"stale": True}
    assert cw_state["strategy"] == {"cards": [], "stale": True}
    assert cw_state["variable_cost_roles_stale"] is True


def test_handle_cw_event_rejects_variable_choice_on_non_lv999_detection_without_click(tmp_path):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    ensure_cw_state(session)["variable_cost_roles"] = {"银狼LV.999": {"cost": 3, "star": 2, "choice_available": True}}
    runtime = EventRouterRuntime([_ocr_piece("特殊事件确认")])

    with pytest.raises(TrailError) as exc_info:
        events_module.handle_cw_event(
            session,
            router=events_module.build_cw_event_router(runtime),
            variable_cost_choice={"role_name": "银狼LV.999", "choice": "cost_up"},
        )

    assert exc_info.value.code == "CW_EVENT_CHOICE_MISMATCH"
    assert runtime.clicks == []


def test_handle_cw_event_rejects_lv999_missing_option_center_without_click(tmp_path):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    ensure_cw_state(session)["variable_cost_roles"] = {"银狼LV.999": {"cost": 3, "star": 2, "choice_available": True}}
    runtime = EventRouterRuntime([_ocr_piece("银狼 LV.999 选择强化方向")])

    with pytest.raises(TrailError) as exc_info:
        events_module.handle_cw_event(
            session,
            router=events_module.build_cw_event_router(runtime),
            variable_cost_choice={"role_name": "银狼LV.999", "choice": "cost_up"},
        )

    assert exc_info.value.code == "CW_EVENT_CHOICE_TARGET_MISSING"
    assert runtime.clicks == []


def test_handle_cw_event_rejects_lv999_stale_state_before_click_or_apply(tmp_path):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    cw_state = ensure_cw_state(session)
    cw_state["variable_cost_roles_stale"] = True
    cw_state["variable_cost_roles"] = {"银狼LV.999": {"cost": 3, "star": 2, "choice_available": True}}
    cw_state["shop"] = {"opened": True, "stale": False, "items": [{"name": "银狼LV.999", "cost": 3}]}
    before_cw_state = deepcopy(cw_state)
    runtime = EventRouterRuntime([_ocr_piece("银狼 LV.999 选择强化方向"), _ocr_piece("提升费用")])

    with pytest.raises(TrailError) as exc_info:
        events_module.handle_cw_event(
            session,
            router=events_module.build_cw_event_router(runtime),
            variable_cost_choice={"role_name": "银狼LV.999", "choice": "cost_up"},
        )

    assert exc_info.value.code == "CW_EVENT_LV999_STATE_STALE"
    assert str(exc_info.value) == "银狼LV.999 状态已过期，请先运行 cw.event.reconcile"
    assert cw_state == before_cw_state
    assert runtime.clicks == []


def test_handle_cw_event_rejects_lv999_cost_up_at_max_cost_before_click_or_mutation(tmp_path):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    cw_state = ensure_cw_state(session)
    cw_state["variable_cost_roles"] = {"银狼LV.999": {"cost": 5, "star": 2, "choice_available": True}}
    cw_state["shop"] = {"opened": True, "stale": False, "items": [{"name": "银狼LV.999", "cost": 5}]}
    before_cw_state = deepcopy(cw_state)
    runtime = EventRouterRuntime([_ocr_piece("银狼 LV.999 选择强化方向"), _ocr_piece("提升费用")])

    with pytest.raises(TrailError) as exc_info:
        events_module.handle_cw_event(
            session,
            router=events_module.build_cw_event_router(runtime),
            variable_cost_choice={"role_name": "银狼LV.999", "choice": "cost_up"},
        )

    assert exc_info.value.code == "CW_EVENT_CHOICE_MISMATCH"
    assert cw_state == before_cw_state
    assert runtime.clicks == []


@pytest.mark.parametrize(
    "role_state",
    [
        {"cost": 3, "star": 2, "choice_available": False},
        {"cost": 3, "star": 1, "choice_available": True},
        {"star": 2, "choice_available": True},
        {"cost": 3, "star": 2, "choice_available": True, "confirmed_choices_by_cost": {3: "equipment"}},
    ],
)
def test_handle_cw_event_rejects_invalid_lv999_session_state_before_click_or_mutation(tmp_path, role_state):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    cw_state = ensure_cw_state(session)
    cw_state["variable_cost_roles"] = {"银狼LV.999": dict(role_state)}
    cw_state["shop"] = {"opened": True, "stale": False, "items": [{"name": "银狼LV.999", "cost": 3}]}
    before_cw_state = deepcopy(cw_state)
    runtime = EventRouterRuntime([_ocr_piece("银狼 LV.999 选择强化方向"), _ocr_piece("提升费用")])

    with pytest.raises(TrailError) as exc_info:
        events_module.handle_cw_event(
            session,
            router=events_module.build_cw_event_router(runtime),
            variable_cost_choice={"role_name": "银狼LV.999", "choice": "cost_up"},
        )

    assert exc_info.value.code == "CW_EVENT_CHOICE_MISMATCH"
    assert cw_state == before_cw_state
    assert runtime.clicks == []


@pytest.mark.parametrize(
    "payload",
    [
        {"role_name": "银狼", "choice": "cost_up"},
        {"role_name": "银狼LV.999", "choice": "reroll"},
        {"role_name": "银狼LV.999"},
    ],
)
def test_handle_cw_event_rejects_invalid_lv999_choice_payload_before_click(tmp_path, payload):
    events_module = load_cw_events_module()
    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    ensure_cw_state(session)["variable_cost_roles"] = {"银狼LV.999": {"cost": 3, "star": 2, "choice_available": True}}
    runtime = EventRouterRuntime([_ocr_piece("银狼 LV.999 选择强化方向"), _ocr_piece("提升费用")])

    with pytest.raises(TrailError) as exc_info:
        events_module.handle_cw_event(session, router=events_module.build_cw_event_router(runtime), variable_cost_choice=payload)

    assert exc_info.value.code == "CW_EVENT_CHOICE_MISMATCH"
    assert runtime.clicks == []


def test_special_confirm_router_uses_legacy_confirm_flow(monkeypatch):
    events_module = load_cw_events_module()
    monkeypatch.setattr(events_module, "_asset", lambda alias: alias, raising=False)
    runtime = EventRouterRuntime([_ocr_piece("特殊事件确认")])

    result = events_module.build_cw_event_router(runtime)(None)

    assert result["event_type"] == "special_confirm"
    assert result["handled"] is True
    assert result["handled_action"] == "confirm"
    assert runtime.locate_calls == []
    assert runtime.clicks == [events_module.SPECIAL_EVENT_OPTION_POINT, events_module.SPECIAL_EVENT_CONFIRM_POINT]


def test_lv999_choice_router_reports_confirm_action_with_explicit_choice():
    events_module = load_cw_events_module()
    runtime = EventRouterRuntime([
        _ocr_piece("银狼 LV.999 选择强化方向"),
        _ocr_piece("提升费用", left=700, top=520, width=160, height=40),
    ])

    result = events_module.build_cw_event_router(runtime)({"role_name": "银狼LV.999", "choice": "cost_up"})

    assert result == {"event_type": "lv999_choice", "handled": True, "handled_action": "confirm"}
    assert runtime.clicks == [(780, 540)]


@pytest.mark.parametrize(
    ("method_name", "callback_name"),
    [
        ("confirm_cw_boss_preview", "confirmer"),
        ("settle_cw_next", "continuer"),
        ("start_cw_battle", "starter"),
        ("continue_cw_battle", "continuer"),
    ],
)
def test_boss_preview_settle_and_battle_actions_invalidate_stage_snapshot(tmp_path, method_name, callback_name):
    events_module = load_cw_events_module()
    method = getattr(events_module, method_name, None)
    assert method is not None

    from tests.conftest import build_fake_cw_session

    session = build_fake_cw_session(tmp_path)
    ensure_cw_state(session)["stage"] = {"value": "battle", "stale": False}
    session.last_stage = {"scene": "cw", "value": "battle"}
    calls: list[str] = []

    refreshed = method(session, **{callback_name: lambda: calls.append("called")})

    assert refreshed.scene_state["cw"]["stage"] == {"stale": True}
    assert refreshed.last_stage is None
    assert calls == ["called"]


def test_build_cw_battle_starter_waits_for_template_before_clicking_center(monkeypatch, fake_runtime):
    events_module = load_cw_events_module()
    monkeypatch.setattr(events_module, "_asset", lambda alias: alias, raising=False)
    fake_runtime.wait_result = {"left": 100, "top": 200, "width": 60, "height": 20}

    events_module.build_cw_battle_starter(fake_runtime)()

    assert fake_runtime.wait_calls == ["action.battle_start"]
    assert fake_runtime.locate_calls == []
    assert fake_runtime.clicks == [(130, 210)]


def test_build_cw_battle_starter_cancels_team_count_confirm_dialog(monkeypatch, fake_runtime):
    events_module = load_cw_events_module()
    monkeypatch.setattr(events_module, "_asset", lambda alias: alias, raising=False)
    fake_runtime.wait_result = {"left": 100, "top": 200, "width": 60, "height": 20}
    ocr_calls: list[dict[str, object]] = []

    def fake_ocr(**kwargs):
        ocr_calls.append(kwargs)
        return [
            _ocr_piece("提示", left=820, top=300, width=100, height=36),
            _ocr_piece("可出战角色人数未达上限，是否确认出战？", left=620, top=430, width=680, height=36),
            _ocr_piece("取消", left=380, top=600, width=100, height=36),
            _ocr_piece("确认", left=1160, top=600, width=100, height=36),
        ]

    monkeypatch.setattr(fake_runtime, "ocr", fake_ocr)

    with pytest.raises(TrailError) as exc_info:
        events_module.build_cw_battle_starter(fake_runtime)()

    assert exc_info.value.code == "CW_BATTLE_TEAM_COUNT_INSUFFICIENT"
    assert "可出战角色人数未达上限" in str(exc_info.value)
    assert "检查场上人数" in str(exc_info.value)
    assert fake_runtime.clicks == [(130, 210), (430, 618)]
    assert ocr_calls


def test_cw_event_handle_flows_through_command_service_journal(tmp_path: Path, monkeypatch):
    registry, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    del registry, cw_service
    monkeypatch.setattr("trail.daemon.cw_service.event_router_factory", lambda runtime: object())
    monkeypatch.setattr(
        "trail.daemon.cw_service.handle_cw_event",
        lambda session, router, variable_cost_choice=None: _set_event_result(
            session, {"event_type": "special", "handled": True, "handled_action": "confirm"}
        ),
    )

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-handle-1",
        method="cw.event.handle",
        payload={},
    )
    status = service.request_status("req-event-handle-1")
    persisted = service.load_session(session.session_id)

    assert envelope["ok"] is True
    assert envelope["data"] == {"event_type": "special", "handled": True, "handled_action": "confirm"}
    assert status["final_state"] == "completed"
    assert persisted.scene_state["cw"]["stage"] == {"stale": True}


def test_cw_event_handle_unknown_daemon_response_includes_recovery_facts(tmp_path: Path, monkeypatch):
    registry, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    del registry, cw_service
    before_cw_state = _fill_fresh_event_session_facts(session)
    cw_state = ensure_cw_state(session)
    cw_state["variable_cost_roles"] = {"银狼LV.999": {"cost": 3, "star": 2, "choice_available": True}}
    cw_state["variable_cost_roles_stale"] = False
    before_cw_state = deepcopy(cw_state)
    service.save_session(session)
    monkeypatch.setattr(
        "trail.daemon.cw_service.event_router_factory",
        lambda runtime: lambda choice: {"event_type": "unknown", "handled": False, "next_action": "manual"},
    )

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-handle-unknown-real",
        method="cw.event.handle",
        payload={},
    )
    persisted_cw_state = ensure_cw_state(service.load_session(session.session_id))
    data = cast(dict[str, object], envelope.get("data"))

    assert envelope["ok"] is True
    assert data["event_type"] == "unknown"
    assert data["handled"] is False
    assert data["next_action"] == "manual"
    assert data["stale"] is True
    assert data["stale_facts"] == "stage/status|slots|shop|equipment|strategy|sell_plan|variable_cost_roles"
    assert data["crystals_stale"] is False
    assert data["reconcile_action"] == "cw.event.reconcile"
    assert cast(dict[str, object], persisted_cw_state["stage"])["stale"] is True
    assert cast(dict[str, object], persisted_cw_state["slots"])["stale"] is True
    assert cast(dict[str, object], persisted_cw_state["shop"])["stale"] is True
    assert cast(dict[str, object], persisted_cw_state["equipment"])["stale"] is True
    assert cast(dict[str, object], persisted_cw_state["strategy"])["stale"] is True
    assert cast(dict[str, object], persisted_cw_state["sell_plan"])["stale"] is True
    assert persisted_cw_state["variable_cost_roles_stale"] is True
    assert persisted_cw_state["metrics"] == before_cw_state["metrics"]


def test_cw_event_handle_payload_confirmed_lv999_choice_updates_persisted_shop_snapshot(tmp_path: Path, monkeypatch):
    registry, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    del registry, cw_service
    cw_state = ensure_cw_state(session)
    cw_state["variable_cost_roles"] = {
        "银狼LV.999": {"cost": 3, "star": 2, "choice_available": True, "confirmed_choices_by_cost": {}}
    }
    cw_state["shop"] = {"opened": True, "stale": False, "items": [{"name": "银狼LV.999", "cost": 3}]}
    service.save_session(session)
    monkeypatch.setattr(
        "trail.daemon.cw_service.event_router_factory",
        lambda runtime: lambda choice: {"event_type": "lv999_choice", "handled": True, "handled_action": "confirm"},
    )

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-handle-choice",
        method="cw.event.handle",
        payload={"variable_cost_choice": {"role_name": "银狼LV.999", "choice": "cost_up"}},
    )
    persisted = service.load_session(session.session_id)

    data = envelope.get("data")
    assert isinstance(data, dict)
    assert data["handled_action"] == "confirm"
    assert data["variable_cost_choice"] == {"role_name": "银狼LV.999", "choice": "cost_up"}
    persisted_cw_state = cast(dict[str, object], persisted.scene_state["cw"])
    persisted_roles = cast(dict[str, object], persisted_cw_state["variable_cost_roles"])
    persisted_role_state = cast(dict[str, object], persisted_roles["银狼LV.999"])
    persisted_shop = cast(dict[str, object], persisted_cw_state["shop"])
    persisted_items = cast(list[dict[str, object]], persisted_shop["items"])
    assert persisted_role_state["cost"] == 4
    assert persisted_role_state["star"] == 1
    assert persisted_shop["stale"] is False
    assert persisted_items[0]["cost"] == 4


def test_cw_event_handle_payload_without_variable_choice_keeps_legacy_behavior(tmp_path: Path, monkeypatch):
    registry, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    del registry, cw_service
    cw_state = ensure_cw_state(session)
    cw_state["variable_cost_roles"] = {"银狼LV.999": {"cost": 3, "star": 2, "choice_available": True}}
    cw_state["shop"] = {"opened": True, "stale": False, "items": [{"name": "银狼LV.999", "cost": 3}]}
    service.save_session(session)
    monkeypatch.setattr(
        "trail.daemon.cw_service.event_router_factory",
        lambda runtime: lambda choice: {"event_type": "special_confirm", "handled": True, "handled_action": "confirm"},
    )

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-handle-no-choice",
        method="cw.event.handle",
        payload={},
    )
    persisted = service.load_session(session.session_id)

    assert envelope.get("data") == {"event_type": "special_confirm", "handled": True, "handled_action": "confirm"}
    persisted_cw_state = cast(dict[str, object], persisted.scene_state["cw"])
    persisted_roles = cast(dict[str, object], persisted_cw_state["variable_cost_roles"])
    persisted_role_state = cast(dict[str, object], persisted_roles["银狼LV.999"])
    persisted_shop = cast(dict[str, object], persisted_cw_state["shop"])
    persisted_items = cast(list[dict[str, object]], persisted_shop["items"])
    assert persisted_role_state["cost"] == 3
    assert persisted_items[0]["cost"] == 3


@pytest.mark.parametrize(
    ("builder_name", "expected_wait_calls", "expected_locate_calls", "ocr_text", "expected_click"),
    [
        (
            "build_cw_battle_starter",
            ["action.battle_start"],
            [],
            "开始挑战",
            (460, 518),
        ),
        (
            "build_cw_battle_starter",
            ["action.battle_start"],
            [],
            "出战",
            (460, 518),
        ),
        (
            "build_cw_battle_continuer",
            [],
            ["action.battle_continue"],
            "继续挑战",
            (460, 518),
        ),
        (
            "build_cw_settle_continuer",
            [],
            ["stage.settle", "action.settle_next_page"],
            "下一页",
            (460, 518),
        ),
    ],
)
def test_cw_action_buttons_use_ocr_box_when_template_misses(
    monkeypatch,
    fake_runtime,
    builder_name,
    expected_wait_calls,
    expected_locate_calls,
    ocr_text,
    expected_click,
):
    events_module = load_cw_events_module()
    monkeypatch.setattr(events_module, "_asset", lambda alias: alias, raising=False)
    ocr_calls: list[dict[str, object]] = []

    def fake_ocr(**kwargs):
        ocr_calls.append(kwargs)
        return [_ocr_piece(ocr_text)]

    fake_runtime.wait_result = None
    fake_runtime.locate_result = None
    monkeypatch.setattr(fake_runtime, "ocr", fake_ocr)

    getattr(events_module, builder_name)(fake_runtime)()

    assert fake_runtime.wait_calls == expected_wait_calls
    assert fake_runtime.locate_calls == expected_locate_calls
    expected_ocr_calls = [{"capture": events_module.CW_ACTION_OCR_REGION}]
    if builder_name == "build_cw_battle_starter":
        expected_ocr_calls.append({"capture": events_module.CW_BATTLE_TEAM_COUNT_CONFIRM_REGION})
    assert ocr_calls == expected_ocr_calls
    assert fake_runtime.clicks == [expected_click]


@pytest.mark.parametrize(
    ("builder_name", "point_name"),
    [
        ("build_cw_battle_starter", "BATTLE_START_POINT"),
        ("build_cw_battle_continuer", "BATTLE_CONTINUE_POINT"),
        ("build_cw_settle_continuer", "SETTLE_NEXT_POINT"),
    ],
)
def test_cw_action_buttons_fall_back_to_existing_points_when_template_and_ocr_miss(
    monkeypatch,
    fake_runtime,
    builder_name,
    point_name,
):
    events_module = load_cw_events_module()
    monkeypatch.setattr(events_module, "_asset", lambda alias: alias, raising=False)
    fake_runtime.wait_result = None
    fake_runtime.locate_result = None
    fake_runtime.ocr_result = []

    getattr(events_module, builder_name)(fake_runtime)()

    assert fake_runtime.clicks == [getattr(events_module, point_name)]


def test_cw_action_buttons_fall_back_to_existing_points_when_ocr_raises(monkeypatch, fake_runtime):
    events_module = load_cw_events_module()
    monkeypatch.setattr(events_module, "_asset", lambda alias: alias, raising=False)
    fake_runtime.wait_result = None
    fake_runtime.locate_result = None
    monkeypatch.setattr(fake_runtime, "ocr", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("ocr boom")))

    events_module.build_cw_battle_continuer(fake_runtime)()

    assert fake_runtime.clicks == [events_module.BATTLE_CONTINUE_POINT]


def test_cw_action_buttons_accept_tuple_ocr_piece(monkeypatch, fake_runtime):
    events_module = load_cw_events_module()
    monkeypatch.setattr(events_module, "_asset", lambda alias: alias, raising=False)
    fake_runtime.wait_result = None
    fake_runtime.locate_result = None
    fake_runtime.ocr_result = [([(400, 500), (520, 500), (520, 536), (400, 536)], "下一页", 0.99)]

    events_module.build_cw_settle_continuer(fake_runtime)()

    assert fake_runtime.clicks == [(460, 518)]


def _build_cw_harness(tmp_path: Path):
    from trail.daemon.command_service import CommandService
    from trail.daemon.cw_service import CwService
    from trail.daemon.session_service import SessionServiceRegistry

    registry = SessionServiceRegistry()
    service = registry.for_workspace(str(tmp_path))
    session = service.create_session(window_binding={"title": "崩坏：星穹铁道", "hwnd": 1})
    runtime_service = SimpleNamespace(get_runtime=lambda **kwargs: SimpleNamespace())
    cw_service = CwService(runtime_service=runtime_service)
    command_service = CommandService(runtime_service=runtime_service, session_service=registry, cw_service=cw_service)
    return registry, service, session, cw_service, command_service


def _run_cw_mutation(*, command_service, session, workspace_root: Path, request_id: str, method: str, payload: dict[str, object]) -> dict[str, Any]:
    from trail.daemon.models import DaemonRequest
    from trail.daemon.protocol import PROTOCOL_VERSION

    return cast(
        dict[str, Any],
        command_service.handle(
            DaemonRequest(
                request_id=request_id,
                protocol_version=PROTOCOL_VERSION,
                workspace_root=str(workspace_root),
                session_id=session.session_id,
                verbose=False,
                method=method,
                payload={"session_id": session.session_id, **payload},
            )
        )
    )



@pytest.mark.parametrize(("method", "did_click"), [
    ("cw.shop.plan_exp", False),
    ("cw.shop.buy_exp_to_level", False),
    ("cw.shop.buy_exp_to_level", True),
])
def test_experience_rpc_returns_the_evidence_frame_and_invalidates_only_after_input(
    tmp_path, monkeypatch, method, did_click,
):
    from PIL import Image
    from trail.daemon import cw_service as module
    from trail.scenes.cw.run_facts import start_cw_run_facts

    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    facts = start_cw_run_facts(session)
    facts["opening"] = {"affixes_status": "partial", "affixes": [{"name": "成长的烦恼"}]}
    initial_state = ensure_cw_state(session)
    initial_state["shop"] = {"stale": False, "opened": False}
    initial_state["slots"] = {"stale": False}
    service.save_session(session)
    frame = Image.new("RGB", (8, 8))
    saved = []
    clicks = []

    def save_image(image, *, request_id):
        assert request_id == "exp-rpc"
        saved.append(image)
        return tmp_path / "exp.jpg"

    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(
        save_capture_image_to_workspace=save_image,
        capture_after_action=lambda **kwargs: pytest.fail("unexpected replacement capture"),
        click_point=lambda x, y: clicks.append((x, y)),
    )

    def quote(runtime, *, opening, target_level):
        assert opening == facts["opening"] and target_level == 9
        return {"status": "ready"}, frame

    def buy(runtime, *, opening, target_level, max_gold, check_cancelled):
        assert max_gold == 85
        check_cancelled()
        if did_click:
            runtime.click_point(320, 992)
        result, evidence = quote(runtime, opening=opening, target_level=target_level)
        return {**result, "status": "reached" if did_click else "insufficient_gold"}, evidence

    monkeypatch.setattr(module, "quote_cw_experience", quote)
    monkeypatch.setattr(module, "buy_cw_experience_to_level", buy)
    payload = {"target_level": 9}
    if method == "cw.shop.buy_exp_to_level":
        payload["max_gold"] = 85
    response = _run_cw_mutation(
        command_service=command, session=session, workspace_root=tmp_path,
        request_id="exp-rpc", method=method, payload=payload,
    )
    assert response["ok"] and response["screenshot"] == "exp.jpg" and saved == [frame]
    assert bool(clicks) == did_click
    current = ensure_cw_state(service.load_session(session.session_id))
    assert current["shop"]["stale"] == current["slots"]["stale"] == did_click
    assert current["run_facts"] == facts


@pytest.mark.parametrize(
    "method, handler, payload, stage, missing_shot",
    [
        ("cw.event.drag_aura_role", "drag_cw_aura_role", {"source": "front:0"}, "plot_armor", False),
        ("cw.replenish.refresh", "refresh_cw_replenish", {}, "replenish", False),
        ("cw.replenish.refresh", "refresh_cw_replenish", {}, "replenish", True),
    ],
)
def test_cw_verified_page_action_persists_stage_and_saves_the_observed_frame(
    tmp_path, monkeypatch, method, handler, payload, stage, missing_shot,
):
    from PIL import Image
    from trail.daemon import cw_service as module

    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    expected_frame = Image.new("RGB", (8, 8), "blue")
    saved_frames = []
    shot = tmp_path / "confirmed.jpg"

    def save_image(image, *, request_id):
        assert request_id == "verified-page-action"
        saved_frames.append(image)
        if missing_shot:
            raise OSError("cannot save the observed frame")
        return shot

    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(
        save_capture_image_to_workspace=save_image,
        capture_after_action=lambda **kwargs: pytest.fail("unexpected replacement capture"),
    )

    def action(runtime, *, check_cancelled, **options):
        assert options == payload
        check_cancelled()
        return {"value": stage, "entries": []}, expected_frame

    monkeypatch.setattr(module, handler, action)
    response = _run_cw_mutation(
        command_service=command, session=session, workspace_root=tmp_path,
        request_id="verified-page-action", method=method, payload=payload,
    )

    if missing_shot:
        assert not response["ok"]
        assert service.request_status("verified-page-action")["final_state"] == "persisted_but_response_unknown"
    else:
        assert response["ok"] and response["screenshot"] == "confirmed.jpg"
        assert response["data"] == {"value": stage, "entries": []}
    assert saved_frames == [expected_frame]
    assert service.load_session(session.session_id).last_stage == {"scene": "cw", "value": stage}


def _fact_set(value: object) -> set[str]:
    if not isinstance(value, str) or value == "none":
        return set()
    return {part for part in value.split("|") if part}


def _seed_reconcile_stale_state(session) -> dict[str, object]:
    cw_state = ensure_cw_state(session)
    cw_state["stage"] = {"value": "event", "stale": True, "status": {"stale": True, "level": 7}}
    cw_state["slots"] = {"stale": True, "front": [{"name": "希儿"}], "back": [], "hand": []}
    cw_state["shop"] = {"opened": True, "stale": True, "items": [{"name": "银狼LV.999", "cost": 3}], "coins": 40}
    cw_state["equipment"] = {"stale": True, "items": [{"name": "风暴"}]}
    cw_state["strategy"] = {"stale": True, "cards": [{"strategy_title": "快攻"}]}
    cw_state["sell_plan"] = {"stale": True, "items": [{"name": "佩拉", "priority": 10}]}
    cw_state["metrics"] = {"crystals": 7, "bonus": 2}
    cw_state["variable_cost_roles"] = {"银狼LV.999": {"cost": 3, "star": 2}}
    cw_state["variable_cost_roles_stale"] = True
    session.last_stage = {"scene": "cw", "value": "event"}
    return deepcopy(cw_state)


def _patch_reconcile_stage(monkeypatch, stage_value: str | None) -> None:
    monkeypatch.setattr("trail.daemon.cw_service.stage_detector_factory", lambda runtime: lambda: stage_value)


def test_cw_event_reconcile_unknown_stage_keeps_stale_and_metrics(tmp_path: Path, monkeypatch):
    registry, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    del registry, cw_service
    before_cw_state = _seed_reconcile_stale_state(session)
    cw_state = ensure_cw_state(session)
    cw_state["stage"] = {"value": "event", "stale": True, "status": {"stale": False, "level": 7}}
    before_cw_state = deepcopy(cw_state)
    service.save_session(session)
    _patch_reconcile_stage(monkeypatch, None)

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-unknown",
        method="cw.event.reconcile",
        payload={},
    )
    persisted = service.load_session(session.session_id)
    persisted_cw_state = cast(dict[str, object], persisted.scene_state["cw"])
    data = cast(dict[str, object], envelope.get("data"))

    assert envelope["ok"] is True
    assert data["stage"] == "unknown"
    assert data["stale"] is True
    assert data["reconciled"] == "none"
    assert data["next_action"] == "manual"
    assert {"stage/status", "slots", "shop", "equipment", "strategy", "sell_plan", "variable_cost_roles"}.issubset(
        _fact_set(data["stale_facts"])
    )
    persisted_stage = cast(dict[str, object], persisted_cw_state["stage"])
    persisted_status = cast(dict[str, object], persisted_stage["status"])
    assert persisted_status["stale"] is True
    assert "crystals" not in data
    assert persisted_cw_state["metrics"] == before_cw_state["metrics"]
    assert persisted.last_stage is None


def test_collect_cw_preparation_facts_include_crystals_false_omits_crystals_key(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module

    _registry, service, session, _cw_service, _command_service = _build_cw_harness(tmp_path)
    cw_state = ensure_cw_state(session)
    cw_state["metrics"] = {"crystals": 5}
    before_metrics = deepcopy(cw_state["metrics"])
    monkeypatch.setattr(
        cw_service_module,
        "collect_cw_crystals",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("crystals collector must not run")),
    )

    data = cw_service_module._collect_cw_preparation_facts(
        session,
        runtime=SimpleNamespace(),
        workspace_root=str(tmp_path),
        include_crystals=False,
        order=(),
    )

    assert "crystals" not in data
    assert ensure_cw_state(session)["metrics"] == before_metrics


def test_cw_event_reconcile_preparation_restores_stale_sell_plan_after_reader_side_effect(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module

    _registry, service, session, _cw_service, command_service = _build_cw_harness(tmp_path)
    before_cw_state = _seed_reconcile_stale_state(session)
    previous_sell_plan = cast(dict[str, object], deepcopy(before_cw_state["sell_plan"]))
    service.save_session(session)
    _patch_reconcile_stage(monkeypatch, "preparation")

    def fake_collect(session_arg, **kwargs):
        assert kwargs["include_crystals"] is False
        assert kwargs["order"] == ("slots", "shop", "equipment")
        cw_state = ensure_cw_state(session_arg)
        cw_state["slots"] = {"front": [{"name": "希儿"}], "back": [], "hand": [], "stale": False}
        cw_state["sell_plan"] = {}
        return {"slots": deepcopy(cw_state["slots"])}

    monkeypatch.setattr(cw_service_module, "_collect_cw_preparation_facts", fake_collect)

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-prep-sell-plan",
        method="cw.event.reconcile",
        payload={},
    )
    persisted = service.load_session(session.session_id)
    persisted_cw_state = cast(dict[str, object], persisted.scene_state["cw"])
    data = cast(dict[str, object], envelope.get("data"))

    assert envelope["ok"] is True
    assert "crystals" not in data
    assert persisted_cw_state["sell_plan"] == {**previous_sell_plan, "stale": True}
    assert cast(dict[str, object], persisted_cw_state["strategy"])["stale"] is True
    assert {"strategy", "sell_plan"}.issubset(_fact_set(data["stale_facts"]))
    assert {"strategy", "sell_plan"}.issubset(_fact_set(data["todo"]))


def test_cw_event_reconcile_preparation_collects_no_crystals_and_only_fresh_reconciled(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module

    _registry, service, session, _cw_service, command_service = _build_cw_harness(tmp_path)
    before_cw_state = _seed_reconcile_stale_state(session)
    service.save_session(session)
    _patch_reconcile_stage(monkeypatch, "preparation")

    def fake_collect(session_arg, **kwargs):
        assert kwargs["include_crystals"] is False
        cw_state = ensure_cw_state(session_arg)
        cw_state["slots"] = {"front": [{"name": "希儿"}], "back": [], "hand": [], "stale": False}
        cw_state["shop"] = {"opened": False, "stale": True, "items": []}
        cw_state["equipment"] = {"stale": False, "items": [{"name": "风暴"}]}
        cw_state["sell_plan"] = {}
        return {
            "slots": deepcopy(cw_state["slots"]),
            "shop": deepcopy(cw_state["shop"]),
            "equipment": deepcopy(cw_state["equipment"]),
        }

    monkeypatch.setattr(cw_service_module, "_collect_cw_preparation_facts", fake_collect)

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-prep-fresh",
        method="cw.event.reconcile",
        payload={},
    )
    persisted = service.load_session(session.session_id)
    data = cast(dict[str, object], envelope.get("data"))

    assert envelope["ok"] is True
    assert "crystals" not in data
    assert ensure_cw_state(persisted)["metrics"] == before_cw_state["metrics"]
    assert data["reconciled"] == "stage|slots|equipment"
    assert "shop" not in _fact_set(data["reconciled"])
    assert {"shop", "strategy", "sell_plan"}.issubset(_fact_set(data["stale_facts"]))


def test_cw_event_reconcile_refreshes_plot_armor_and_keeps_run_id(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module
    from trail.scenes.cw.run_facts import record_cw_plot_armor, start_cw_run_facts

    _, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    run_id = start_cw_run_facts(session)["run_id"]
    original = {"slot": 1, "name": "遇强则强", "description": "旧效果", "role": "海瑟音", "status": "active"}
    updated = {"slot": 1, "name": "脆弱杀手", "description": "新效果", "role": "昔涟", "status": "active"}
    record_cw_plot_armor(session, {"status": "observed", "node": "3-1", "reselect_remaining": 1, "cards": [original]})
    service.save_session(session)
    keys = []
    runtime = SimpleNamespace(capture_image=lambda **kwargs: None, press_key=keys.append)
    cw_service.runtime_service.get_runtime = lambda **kwargs: runtime
    _patch_reconcile_stage(monkeypatch, "preparation")
    monkeypatch.setattr(cw_service_module, "sleep", lambda duration: None)
    monkeypatch.setattr(cw_service_module, "open_cw_plot_armor_reselect", lambda runtime: {})
    monkeypatch.setattr(cw_service_module, "read_cw_stage_text", lambda *args, **kwargs: {"value": "plot_armor_details"})
    monkeypatch.setattr(cw_service_module, "_slots_read_resources", lambda **kwargs: ({}, {}, None))
    monkeypatch.setattr(
        cw_service_module, "read_cw_plot_armor",
        lambda *args, **kwargs: {"status": "observed", "node": "3-1", "reselect_remaining": 0, "_reselect_verified": True, "cards": [updated]},
    )
    stages = iter(["plot_armor_details", "preparation"])
    monkeypatch.setattr(cw_service_module, "read_cw_preparation_header", lambda runtime: {"stage": next(stages)})
    monkeypatch.setattr(cw_service_module, "_collect_cw_preparation_facts", lambda *args, **kwargs: {})

    envelope = _run_cw_mutation(
        command_service=command_service, session=session, workspace_root=tmp_path,
        request_id="req-cw-reconcile-aura", method="cw.event.reconcile", payload={},
    )

    assert envelope["ok"] is True
    assert keys == ["esc"]
    assert envelope["data"]["run_facts"]["run_id"] == run_id
    assert envelope["data"]["run_facts"]["investment_history_incomplete"] is True
    assert envelope["data"]["run_facts"]["plot_armor"]["reselect_remaining"] == 0
    saved = service.load_session(session.session_id).scene_state["cw"]["run_facts"]
    assert [entry["cards"] for entry in saved["plot_armor_history"]] == [[original], [updated]]


def test_cw_event_reconcile_shop_uses_current_page_reader_without_reset_or_open(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module
    from trail.scenes.cw import shop as shop_module

    class Runtime:
        def __init__(self):
            self.clicks: list[tuple[int, int]] = []

        def click_point(self, x: int, y: int) -> None:
            point = (x, y)
            self.clicks.append(point)
            if point in {shop_module.SHOP_SCAN_RESET_POINT, shop_module.SHOP_OPEN_POINT}:
                raise AssertionError(f"shop reconcile must not reset/open already-open shop: {point}")

    runtime = Runtime()
    registry, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    del registry
    runtime_service = SimpleNamespace(get_runtime=lambda **kwargs: runtime)
    cw_service.runtime_service = runtime_service
    command_service.runtime_service = runtime_service
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    stages = iter(["shop", "preparation"])
    monkeypatch.setattr(cw_service_module, "stage_detector_factory", lambda runtime: lambda: next(stages))
    calls: list[str] = []

    def current_page_reader_factory(runtime_arg):
        del runtime_arg

        def reader():
            calls.append("page-reader")
            return {
                "opened": True,
                "stale": False,
                "items": [{"slot": i, "name": "希儿", "price": 3} for i in range(1, 6)],
                "coins": 40,
                "reserve_full": False,
            }

        return reader

    monkeypatch.setattr(cw_service_module, "fetch_cw_guide_config", lambda **kwargs: {})
    monkeypatch.setattr(cw_service_module, "shop_page_snapshot_reader_factory", current_page_reader_factory)
    monkeypatch.setattr(cw_service_module, "shop_closer_factory", lambda runtime_arg: lambda: calls.append("close"))

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-current-page-shop",
        method="cw.event.reconcile",
        payload={},
    )
    data = cast(dict[str, object], envelope.get("data"))
    persisted_shop = cast(dict[str, object], ensure_cw_state(service.load_session(session.session_id))["shop"])

    assert envelope["ok"] is True
    assert calls == ["page-reader", "close"]
    assert runtime.clicks == []
    assert persisted_shop["opened"] is False
    assert persisted_shop["stale"] is False
    assert "shop" in _fact_set(data["reconciled"])
    assert "shop" not in _fact_set(data["stale_facts"])
    assert "shop" not in _fact_set(data.get("todo"))
    assert "shop" not in _fact_set(data["stale_facts"])


def test_cw_event_reconcile_shop_scans_current_shop_before_close_without_open_shop(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module

    _registry, service, session, _cw_service, command_service = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    stages = iter(["shop", "preparation"])
    monkeypatch.setattr(cw_service_module, "stage_detector_factory", lambda runtime: lambda: next(stages))
    calls: list[str] = []

    monkeypatch.setattr(
        cw_service_module,
        "open_cw_shop",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reconcile must not open an already-open shop")),
    )
    monkeypatch.setattr(cw_service_module, "fetch_cw_guide_config", lambda **kwargs: {})
    monkeypatch.setattr(cw_service_module, "shop_scan_snapshot_reader_factory", lambda *args, **kwargs: object())
    monkeypatch.setattr(cw_service_module, "shop_closer_factory", lambda runtime: object())

    def fake_scan(session_arg, *, scanner, guide_config=None):
        del scanner, guide_config
        calls.append("scan")
        cw_state = ensure_cw_state(session_arg)
        cw_state["shop"] = {"opened": True, "stale": False, "items": [{"slot": i, "name": "希儿", "cost": 3} for i in range(1, 6)], "coins": 40}
        return SimpleNamespace(session=session_arg, response_snapshot=deepcopy(cw_state["shop"]))

    def fake_close(session_arg, *, closer=None):
        del closer
        calls.append("close")
        shop = dict(ensure_cw_state(session_arg).get("shop") or {})
        shop["opened"] = False
        shop["stale"] = True
        ensure_cw_state(session_arg)["shop"] = shop
        return session_arg

    monkeypatch.setattr(cw_service_module, "scan_cw_shop", fake_scan)
    monkeypatch.setattr(cw_service_module, "close_cw_shop", fake_close)
    monkeypatch.setattr(cw_service_module, "project_cw_shop_snapshot", lambda session_arg, **kwargs: deepcopy(ensure_cw_state(session_arg)["shop"]))

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-shop",
        method="cw.event.reconcile",
        payload={},
    )
    persisted_shop = cast(dict[str, object], ensure_cw_state(service.load_session(session.session_id))["shop"])
    data = cast(dict[str, object], envelope.get("data"))

    assert envelope["ok"] is True
    assert calls == ["scan", "close"]
    assert persisted_shop["opened"] is False
    assert persisted_shop["stale"] is False
    assert "shop" in _fact_set(data["reconciled"])


def test_cw_event_reconcile_shop_scan_failure_keeps_command_ok_and_shop_stale(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module

    _registry, service, session, _cw_service, command_service = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    _patch_reconcile_stage(monkeypatch, "shop")

    monkeypatch.setattr(cw_service_module, "fetch_cw_guide_config", lambda **kwargs: {})
    monkeypatch.setattr(cw_service_module, "shop_scan_snapshot_reader_factory", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        cw_service_module,
        "scan_cw_shop",
        lambda *args, **kwargs: (_ for _ in ()).throw(TrailError("SHOP_SCAN_FAILED", "shop scan failed")),
    )
    monkeypatch.setattr(
        cw_service_module,
        "close_cw_shop",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("close must not run after scan failure")),
    )

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-shop-scan-failure",
        method="cw.event.reconcile",
        payload={},
    )
    persisted_shop = cast(dict[str, object], ensure_cw_state(service.load_session(session.session_id))["shop"])
    data = cast(dict[str, object], envelope.get("data"))

    assert envelope["ok"] is True
    assert "shop" in _fact_set(data["attempted"])
    assert "shop" not in _fact_set(data["reconciled"])
    assert "shop" in _fact_set(data["todo"])
    assert "shop" in _fact_set(data["stale_facts"])
    assert persisted_shop.get("stale") is not False


def test_cw_event_reconcile_shop_close_failure_keeps_command_ok_and_shop_stale(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module

    _registry, service, session, _cw_service, command_service = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    _patch_reconcile_stage(monkeypatch, "shop")

    monkeypatch.setattr(cw_service_module, "fetch_cw_guide_config", lambda **kwargs: {})
    monkeypatch.setattr(cw_service_module, "shop_scan_snapshot_reader_factory", lambda *args, **kwargs: object())
    monkeypatch.setattr(cw_service_module, "shop_closer_factory", lambda runtime: object())

    def fake_scan(session_arg, *, scanner, guide_config=None):
        del scanner, guide_config
        cw_state = ensure_cw_state(session_arg)
        cw_state["shop"] = {"opened": True, "stale": False, "items": [{"name": "希儿", "cost": 3}], "coins": 40}
        return SimpleNamespace(session=session_arg, response_snapshot=deepcopy(cw_state["shop"]))

    monkeypatch.setattr(cw_service_module, "scan_cw_shop", fake_scan)
    monkeypatch.setattr(
        cw_service_module,
        "close_cw_shop",
        lambda *args, **kwargs: (_ for _ in ()).throw(TrailError("SHOP_CLOSE_FAILED", "shop close failed")),
    )

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-shop-close-failure",
        method="cw.event.reconcile",
        payload={},
    )
    persisted_shop = cast(dict[str, object], ensure_cw_state(service.load_session(session.session_id))["shop"])
    data = cast(dict[str, object], envelope.get("data"))

    assert envelope["ok"] is True
    assert "shop" in _fact_set(data["attempted"])
    assert "shop" not in _fact_set(data["reconciled"])
    assert "shop" in _fact_set(data["todo"])
    assert "shop" in _fact_set(data["stale_facts"])
    assert persisted_shop.get("stale") is not False


def test_cw_event_reconcile_reader_failure_goes_to_attempted_todo_not_reconciled(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module

    _registry, service, session, _cw_service, command_service = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    _patch_reconcile_stage(monkeypatch, "preparation")

    def fail_collect(*args, **kwargs):
        raise TrailError("SLOTS_READ_EMPTY", "slots unavailable")

    monkeypatch.setattr(cw_service_module, "_collect_cw_preparation_facts", fail_collect)

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-reader-failure",
        method="cw.event.reconcile",
        payload={},
    )
    data = cast(dict[str, object], envelope.get("data"))

    assert envelope["ok"] is True
    assert "slots" in _fact_set(data["attempted"])
    assert "slots" not in _fact_set(data["reconciled"])
    assert "slots" in _fact_set(data["todo"])
    assert "slots" in _fact_set(data["stale_facts"])
    assert data["why"] == "stale_after_reconcile"


def test_cw_event_reconcile_keeps_fresh_equipment_after_mutation_wrapper(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module

    _registry, service, session, _cw_service, command_service = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    _patch_reconcile_stage(monkeypatch, "preparation")

    def fake_collect(session_arg, **kwargs):
        cw_state = ensure_cw_state(session_arg)
        cw_state["equipment"] = {"stale": False, "items": [{"name": "风暴"}]}
        return {"equipment": deepcopy(cw_state["equipment"])}

    monkeypatch.setattr(cw_service_module, "_collect_cw_preparation_facts", fake_collect)

    envelope = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-equipment-fresh",
        method="cw.event.reconcile",
        payload={},
    )
    persisted_equipment = cast(dict[str, object], ensure_cw_state(service.load_session(session.session_id))["equipment"])

    assert envelope["ok"] is True
    assert persisted_equipment["stale"] is False


def test_cw_event_reconcile_variable_cost_roles_stale_rules(tmp_path: Path, monkeypatch):
    from trail.daemon import cw_service as cw_service_module
    from trail.scenes.cw.slots import CwSlotsReadResult

    _registry, service, session, _cw_service, command_service = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    _patch_reconcile_stage(monkeypatch, "preparation")
    snapshots = [
        CwSlotsReadResult(front=[{"name": "希儿"}], back=[], hand=[], stage="preparation", stage_status={"stale": False, "level": 7}),
        CwSlotsReadResult(
            front=[{"name": "银狼LV.999", "cost": 4, "star": 2}],
            back=[],
            hand=[],
            stage="preparation",
            stage_status={"stale": False, "level": 7},
        ),
    ]

    def fake_slots_reader_factory(runtime, targets=None, request_id=None, dismiss_initial_overlay=False):
        del runtime, targets, request_id, dismiss_initial_overlay
        return lambda: snapshots.pop(0)

    def fake_scan(session_arg, *, scanner, guide_config=None):
        del scanner, guide_config
        cw_state = ensure_cw_state(session_arg)
        cw_state["shop"] = {"opened": True, "stale": False, "items": []}
        return SimpleNamespace(session=session_arg, response_snapshot=deepcopy(cw_state["shop"]))

    def fake_close(session_arg, *, closer=None):
        del closer
        shop = dict(ensure_cw_state(session_arg).get("shop") or {})
        shop["opened"] = False
        shop["stale"] = False
        ensure_cw_state(session_arg)["shop"] = shop
        return session_arg

    monkeypatch.setattr(cw_service_module, "dismiss_cw_slots_overlay", lambda runtime: None)
    monkeypatch.setattr(cw_service_module, "slots_reader_factory", fake_slots_reader_factory)
    monkeypatch.setattr(cw_service_module, "fetch_cw_guide_config", lambda **kwargs: {})
    monkeypatch.setattr(cw_service_module, "open_cw_shop", lambda session_arg, opener=None: session_arg)
    monkeypatch.setattr(cw_service_module, "scan_cw_shop", fake_scan)
    monkeypatch.setattr(cw_service_module, "project_cw_shop_snapshot", lambda session_arg, **kwargs: deepcopy(ensure_cw_state(session_arg)["shop"]))
    monkeypatch.setattr(cw_service_module, "close_cw_shop", fake_close)
    monkeypatch.setattr(cw_service_module, "shop_opener_factory", lambda runtime: object())
    monkeypatch.setattr(cw_service_module, "shop_page_snapshot_reader_factory", lambda runtime: object())
    monkeypatch.setattr(cw_service_module, "shop_closer_factory", lambda runtime: object())
    monkeypatch.setattr(cw_service_module, "_apply_cw_equipment_read_with_resources", lambda *args, **kwargs: {"stale": False, "items": []})
    monkeypatch.setattr(cw_service_module, "sleep", lambda seconds: None)

    first = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-lv999-absent",
        method="cw.event.reconcile",
        payload={},
    )
    first_data = cast(dict[str, object], first.get("data"))
    first_cw_state = ensure_cw_state(service.load_session(session.session_id))
    assert first_cw_state["variable_cost_roles_stale"] is True
    assert "variable_cost_roles" in _fact_set(first_data["stale_facts"])

    second = _run_cw_mutation(
        command_service=command_service,
        session=session,
        workspace_root=tmp_path,
        request_id="req-event-reconcile-lv999-seen",
        method="cw.event.reconcile",
        payload={},
    )
    second_data = cast(dict[str, object], second.get("data"))
    second_cw_state = ensure_cw_state(service.load_session(session.session_id))
    assert second_cw_state["variable_cost_roles_stale"] is False
    assert second_cw_state["variable_cost_roles"]["银狼LV.999"]["cost"] == 4
    assert "variable_cost_roles" in _fact_set(second_data["reconciled"])
    assert "variable_cost_roles" not in _fact_set(second_data["stale_facts"])


def _set_stage(session, stage: dict[str, object]):
    session.scene_state.setdefault("cw", {})["stage"] = dict(stage)
    session.last_stage = None
    return session


def _set_event_result(session, result: dict[str, object]):
    session.scene_state.setdefault("cw", {})["stage"] = {"stale": True}
    session.last_stage = None
    return result


@pytest.mark.parametrize(
    ("method", "patch_name", "expected_options"),
    [
        ("cw.replenish.read", "read_cw_replenish", [1, 2, 3, 4, 5]),
        ("cw.invest.read", "read_cw_invest", [1, 2, 3]),
        ("cw.encounter.read", "read_cw_encounter", [1, 2]),
        ("cw.fortune.read", "read_cw_fortune", [1, 2]),
    ],
)
def test_cw_event_read_services_return_options(tmp_path: Path, monkeypatch, method: str, patch_name: str, expected_options: list[int]):
    registry, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    monkeypatch.setattr(f"trail.daemon.cw_service.{patch_name}", lambda session: {"options": expected_options})

    result = cw_service.handle(
        method=method,
        payload={"session_id": session.session_id},
        workspace_root=str(tmp_path),
        session_service=service,
    )

    assert result == {"options": expected_options}


@pytest.mark.parametrize(
    ("method", "request_id", "payload", "setup_patches", "expected_data"),
    [
        (
            "cw.event.handle",
            "req-event-handle-1",
            {},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.event_router_factory", lambda runtime: object()),
                monkeypatch.setattr(
                    "trail.daemon.cw_service.handle_cw_event",
                    lambda session, router, variable_cost_choice=None: _set_event_result(
                        session, {"event_type": "special", "handled": True, "handled_action": "confirm"}
                    ),
                ),
            ),
            {"event_type": "special", "handled": True, "handled_action": "confirm"},
        ),
        (
            "cw.replenish.choose",
            "req-replenish-choose-1",
            {"option": 2},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.replenish_chooser_factory", lambda runtime: object()),
                monkeypatch.setattr("trail.daemon.cw_service.choose_cw_replenish", lambda session, option, chooser: _set_stage(session, {"stale": True})),
            ),
            {"stale": True},
        ),
        (
            "cw.invest.choose",
            "req-invest-choose-1",
            {"option": 1},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.invest_chooser_factory", lambda runtime: object()),
                monkeypatch.setattr("trail.daemon.cw_service.choose_cw_invest", lambda session, option, chooser: _set_stage(session, {"stale": True})),
            ),
            {"stale": True},
        ),
        (
            "cw.encounter.choose",
            "req-encounter-choose-1",
            {"option": 1},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.encounter_chooser_factory", lambda runtime: object()),
                monkeypatch.setattr("trail.daemon.cw_service.choose_cw_encounter", lambda session, option, chooser: _set_stage(session, {"stale": True})),
            ),
            {"stale": True},
        ),
        (
            "cw.fortune.choose",
            "req-fortune-choose-1",
            {"option": 2},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.fortune_chooser_factory", lambda runtime: object()),
                monkeypatch.setattr("trail.daemon.cw_service.choose_cw_fortune", lambda session, option, chooser: _set_stage(session, {"stale": True})),
            ),
            {"stale": True},
        ),
        (
            "cw.boss_preview.confirm",
            "req-boss-confirm-1",
            {},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.boss_preview_confirmer_factory", lambda runtime: object()),
                monkeypatch.setattr("trail.daemon.cw_service.confirm_cw_boss_preview", lambda session, confirmer: _set_stage(session, {"stale": True})),
            ),
            {"stale": True},
        ),
        (
            "cw.battle.start",
            "req-battle-start-1",
            {},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.battle_starter_factory", lambda runtime: object()),
                monkeypatch.setattr("trail.daemon.cw_service.start_cw_battle", lambda session, starter: _set_stage(session, {"stale": True})),
                monkeypatch.setattr("trail.daemon.cw_service.sleep", lambda seconds: None),
            ),
            {"stale": True},
        ),
        (
            "cw.battle.continue",
            "req-battle-continue-1",
            {},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.battle_continuer_factory", lambda runtime: object()),
                monkeypatch.setattr("trail.daemon.cw_service.continue_cw_battle", lambda session, continuer: _set_stage(session, {"stale": True})),
            ),
            {"stale": True},
        ),
        (
            "cw.settle.next",
            "req-settle-next-1",
            {},
            lambda monkeypatch: (
                monkeypatch.setattr("trail.daemon.cw_service.settle_continuer_factory", lambda runtime: object()),
                monkeypatch.setattr("trail.daemon.cw_service.settle_cw_next", lambda session, continuer: _set_stage(session, {"stale": True})),
            ),
            {"stale": True},
        ),
    ],
)
def test_cw_event_mutation_services_return_expected_data(
    tmp_path: Path,
    monkeypatch,
    method: str,
    request_id: str,
    payload: dict[str, object],
    setup_patches,
    expected_data: dict[str, object],
):
    registry, service, session, cw_service, command_service = _build_cw_harness(tmp_path)
    del registry, command_service, request_id
    setup_patches(monkeypatch)

    result = cw_service.handle(
        method=method,
        payload={"session_id": session.session_id, **payload},
        workspace_root=str(tmp_path),
        session_service=service,
    )
    persisted = service.load_session(session.session_id)

    assert result == expected_data
    assert persisted.scene_state["cw"]["stage"] == {"stale": True}


@pytest.mark.parametrize(("phase", "observations", "expected_stage", "esc_count"), [
    ("before", ["preparation", "plot_armor_details", "unknown"], "unknown", 1),
    ("before", ["preparation", "plot_armor"], "plot_armor", 0),
    ("after", ["battle", "plot_armor_details", "plot_armor_details"], "plot_armor_details", 1),
])
def test_battle_stops_if_aura_panel_exit_is_unconfirmed(
    tmp_path, monkeypatch, phase, observations, expected_stage, esc_count
):
    from trail.daemon import cw_service as module
    monkeypatch.setattr(module, "_observe_battle_collection_page", lambda runtime: ({"value": "preparation", "entries": []}, None))
    from trail.scenes.cw.run_facts import start_cw_run_facts

    _, service, session, cw_service, _ = _build_cw_harness(tmp_path)
    facts = start_cw_run_facts(session)
    run_id = facts["run_id"]
    facts["plot_armor_history"].append({"cards": [{"name": "旧光环"}]})
    ensure_cw_state(session)["stage"] = {"value": "preparation", "stale": False, "status": {"level": 7, "stale": False}}
    service.save_session(session)
    keys, battles, collects = [], [], []
    runtime = SimpleNamespace(capture_image=lambda **kwargs: None, press_key=keys.append)
    cw_service.runtime_service.get_runtime = lambda **kwargs: runtime
    monkeypatch.setattr(module, "sleep", lambda duration: None)
    stages = iter(observations)
    monkeypatch.setattr(module, "read_cw_preparation_header", lambda runtime: {"stage": next(stages), "node": "1-1", "hp": 99})
    def fail_open(runtime):
        raise TrailError("CW_AURA_DETAILS_UNCONFIRMED", "unconfirmed")
    monkeypatch.setattr(module, "open_cw_plot_armor_reselect", fail_open)
    def battle(*args, **kwargs):
        battles.append(True)
        return {"status": "completed", "stage": "preparation", "result": "win",
                "battle_report": {"progress_pct": 80}, "decision": {"entries": ["old"]},
                "current_node": {"stage": "preparation", "node": "1-1", "hp": 99}, "hp": 99}
    monkeypatch.setattr(module, "run_cw_battle", battle)
    monkeypatch.setattr(module, "_collect_cw_preparation_facts", lambda *a, **k: collects.append(True))
    result = cw_service.handle(method="cw.battle.run", payload={"session_id": session.session_id},
                               workspace_root=str(tmp_path), session_service=service)
    saved = service.load_session(session.session_id)
    assert len(battles) == (phase == "after")
    assert not collects and len(keys) == esc_count
    assert result["stage"] == expected_stage and result["stale"] is True
    assert result["status"] == ("blocked" if phase == "before" else "completed")
    assert result["next_action"] == "cw.event.reconcile"
    assert result["current_node"] == {"stage": expected_stage, "node": None, "hp": None}
    assert "decision" not in result and "hp" not in result
    assert result["run_facts"]["run_id"] == run_id
    assert result["run_facts"]["plot_armor"]["status"] == "unverified"
    assert saved.scene_state["cw"]["run_facts"]["plot_armor_history"] == [{"cards": [{"name": "旧光环"}]}]
    assert saved.scene_state["cw"]["stage"].get("value") == (None if expected_stage == "unknown" else expected_stage)
    assert saved.scene_state["cw"]["stage"]["status"]["stale"] is True
    if phase == "after":
        assert result["battle_report"] == {"progress_pct": 80} and result["result"] == "win"


def test_battle_continues_when_aura_read_fails_but_preparation_is_confirmed(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    monkeypatch.setattr(module, "_observe_battle_collection_page", lambda runtime: ({"value": "battle", "entries": []}, None))
    from trail.scenes.cw.run_facts import start_cw_run_facts

    _, service, session, cw_service, _ = _build_cw_harness(tmp_path)
    start_cw_run_facts(session)
    service.save_session(session)
    keys = []
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(
        capture_image=lambda **kwargs: None, press_key=keys.append
    )
    monkeypatch.setattr(module, "read_cw_preparation_header", lambda runtime: {"stage": "preparation"})
    def fail_open(runtime):
        raise TrailError("CW_AURA_DETAILS_UNCONFIRMED", "unconfirmed")
    monkeypatch.setattr(module, "open_cw_plot_armor_reselect", fail_open)
    calls = []
    def battle(*args, **kwargs):
        calls.append(True)
        return {"status": "completed", "stage": "battle", "result": "win"}
    monkeypatch.setattr(module, "run_cw_battle", battle)
    result = cw_service.handle(method="cw.battle.run", payload={"session_id": session.session_id},
                               workspace_root=str(tmp_path), session_service=service)
    assert calls and not keys and result["stage"] == "battle"
    assert result["run_facts"]["plot_armor"]["status"] == "unverified"


def test_aura_failed_open_can_confirm_details_and_restore_preparation(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    monkeypatch.setattr(module, "_observe_battle_collection_page", lambda runtime: ({"value": "battle", "entries": []}, None))
    from trail.scenes.cw.run_facts import start_cw_run_facts
    _, service, session, cw_service, _ = _build_cw_harness(tmp_path)
    start_cw_run_facts(session)
    service.save_session(session)
    keys = []
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(
        capture_image=lambda **kwargs: None, press_key=keys.append
    )
    stages = iter(["preparation", "plot_armor_details", "preparation"])
    monkeypatch.setattr(module, "read_cw_preparation_header", lambda runtime: {"stage": next(stages)})
    monkeypatch.setattr(module, "sleep", lambda duration: None)
    def fail_open(runtime):
        raise TrailError("CW_AURA_DETAILS_UNCONFIRMED", "unconfirmed")
    monkeypatch.setattr(module, "open_cw_plot_armor_reselect", fail_open)
    calls = []
    monkeypatch.setattr(module, "run_cw_battle", lambda *a, **k: calls.append(True) or {"status": "completed", "stage": "battle"})
    result = cw_service.handle(method="cw.battle.run", payload={"session_id": session.session_id},
                               workspace_root=str(tmp_path), session_service=service)
    assert keys == ["esc"] and calls and result["stage"] == "battle"
    assert result["run_facts"]["plot_armor"]["status"] == "unverified"


def test_aura_interrupted_open_persists_stale_state_without_escape(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    from trail.daemon.models import RequestCancelled
    from trail.scenes.cw.run_facts import start_cw_run_facts
    _, service, session, cw_service, _ = _build_cw_harness(tmp_path)
    run_id = start_cw_run_facts(session)["run_id"]
    ensure_cw_state(session)["stage"] = {"value": "preparation", "stale": False}
    service.save_session(session)
    keys = []
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(
        capture_image=lambda **kwargs: None, press_key=keys.append
    )
    monkeypatch.setattr(module, "read_cw_preparation_header", lambda runtime: {"stage": "preparation"})
    def cancelled(runtime):
        raise RequestCancelled()
    monkeypatch.setattr(module, "open_cw_plot_armor_reselect", cancelled)
    with pytest.raises(RequestCancelled):
        cw_service.handle(method="cw.battle.run", payload={"session_id": session.session_id},
                          workspace_root=str(tmp_path), session_service=service)
    saved = service.load_session(session.session_id).scene_state["cw"]
    assert not keys and saved["stage"]["stale"] is True and "value" not in saved["stage"]
    assert saved["run_facts"]["run_id"] == run_id


@pytest.mark.parametrize("same_stage", [True, False])
def test_blocked_crystals_project_only_matching_decision_header(tmp_path, monkeypatch, same_stage):
    from trail.daemon import cw_service as module
    _, _, session, _, _ = _build_cw_harness(tmp_path)
    def collect(session, collector):
        ensure_cw_state(session)["metrics"] = {"last_crystal_collection": "blocked", "stage": "invest"}
    monkeypatch.setattr(module, "collect_cw_crystals", collect)
    monkeypatch.setattr(module, "crystal_collector_factory", lambda runtime: None)
    reading = {"value": "invest" if same_stage else "preparation", "entries": [
        {"text": "2-3", "bounds": [420, 60, 460, 90]},
        {"text": "85", "bounds": [1430, 60, 1460, 90]},
    ]}
    monkeypatch.setattr(module, "read_cw_stage_text", lambda runtime: reading)
    result = module._collect_cw_preparation_facts(
        session, runtime=SimpleNamespace(ocr=lambda: []), workspace_root=str(tmp_path),
        response_data={"status": "completed", "stage": "preparation", "round": "2-2",
                       "battle_report": {"progress_pct": 80}}, order=("crystals", "slots"),
    )
    assert result["current_node"] == {"stage": "invest", "node": "2-3" if same_stage else None,
                                      "hp": 85 if same_stage else None}
    assert result["round"] == "2-2" and result["battle_report"] == {"progress_pct": 80}
    assert "slots" not in result


@pytest.mark.parametrize(("code", "after_click"), [("CW_AURA_RESELECT_UNCONFIRMED", True), ("CW_AURA_OPTION_UNAVAILABLE", False)])
def test_aura_reselect_failure_only_invalidates_after_click(tmp_path, monkeypatch, code, after_click):
    from trail.daemon import cw_service as module
    from trail.scenes.cw.run_facts import start_cw_run_facts, record_cw_plot_armor
    _, service, session, cw_service, _ = _build_cw_harness(tmp_path)
    run_id = start_cw_run_facts(session)["run_id"]
    reading = {"status": "observed", "node": "3-1", "reselect_remaining": 1, "cards": []}
    record_cw_plot_armor(session, reading)
    service.save_session(session)
    def fail(runtime, *, option):
        error = TrailError(code, "unconfirmed")
        error.known_failure_after_save = True
        raise error
    monkeypatch.setattr(module, "open_cw_plot_armor_reselect", fail)
    loaded, _, _, handlers, *_ = cw_service._context(
        method="cw.event.open_aura_reselect", payload={"session_id": session.session_id, "option": 1},
        workspace_root=str(tmp_path), session_service=service,
    )
    with pytest.raises(TrailError):
        module._handle_and_save_session(handlers["cw.event.open_aura_reselect"], service, loaded)
    saved = service.load_session(session.session_id).scene_state["cw"]["run_facts"]
    assert saved["run_id"] == run_id and saved["plot_armor_history"] == [reading]
    assert saved["plot_armor"]["status"] == ("unverified" if after_click else "observed")


def test_battle_without_auto_collect_keeps_report_when_current_header_read_fails(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    from trail.scenes.cw.run_facts import start_cw_run_facts
    _, service, session, cw_service, _ = _build_cw_harness(tmp_path)
    facts = start_cw_run_facts(session)
    facts["plot_armor"]["status"] = "observed"
    service.save_session(session)
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(capture_image=lambda **kwargs: None)
    report = {"progress_pct": 80}
    monkeypatch.setattr(module, "run_cw_battle", lambda *a, **k: {
        "status": "completed", "stage": "preparation", "result": "win",
        "round": "2-2", "battle_report": report,
    })
    def fail_header(runtime):
        raise TrailError("OCR_FAILED", "无法读取当前节点")
    monkeypatch.setattr(module, "read_cw_preparation_header", fail_header)
    result = cw_service.handle(
        method="cw.battle.run", payload={"session_id": session.session_id, "collect_crystals": False},
        workspace_root=str(tmp_path), session_service=service,
    )
    assert result["status"] == "completed" and result["result"] == "win"
    assert result["round"] == "2-2" and result["battle_report"] == report
    assert result["current_node"] == {"stage": "preparation", "node": None, "hp": None}
    assert result["next_action"] == "cw.stage.read"
    assert result["warnings"][0]["code"] == "CW_CURRENT_NODE_READ_FAILED"


def test_reconcile_unknown_after_aura_refresh_keeps_stage_stale_and_requests_manual(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    from trail.scenes.cw.run_facts import start_cw_run_facts
    _, service, session, cw_service, _ = _build_cw_harness(tmp_path)
    start_cw_run_facts(session)
    ensure_cw_state(session)["stage"] = {"value": "preparation", "stale": False}
    service.save_session(session)
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(capture_image=lambda **kwargs: None)
    _patch_reconcile_stage(monkeypatch, "preparation")
    def fail_open(runtime):
        raise TrailError("CW_AURA_DETAILS_UNCONFIRMED", "unconfirmed")
    monkeypatch.setattr(module, "open_cw_plot_armor_reselect", fail_open)
    monkeypatch.setattr(module, "read_cw_preparation_header", lambda runtime: {"stage": "unknown"})
    result = cw_service.handle(method="cw.event.reconcile", payload={"session_id": session.session_id},
                               workspace_root=str(tmp_path), session_service=service)
    saved_stage = service.load_session(session.session_id).scene_state["cw"]["stage"]
    assert result["stage"] == "unknown" and result["next_action"] == "manual"
    assert saved_stage["stale"] is True and "value" not in saved_stage


@pytest.mark.parametrize(("after", "opened", "stale", "next_action", "save_failed"), [
    ("preparation", False, False, None, False), ("shop", True, True, None, False),
    (None, None, True, "manual", False), ("invest", False, True, "cw.event.handle", False),
    ("preparation", False, False, None, True),
])
def test_reconcile_shop_verifies_close_with_new_stage(tmp_path, monkeypatch, after, opened, stale, next_action, save_failed):
    from trail.daemon import cw_service as module
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    frames = []
    classified = []
    saved_frames = []
    def capture_image(*, normalize):
        assert normalize is True
        frame = object()
        frames.append(frame)
        return frame
    def save_frame(frame, *, request_id):
        assert request_id == "shop-close-verify"
        saved_frames.append(frame)
        if save_failed:
            raise OSError("cannot save screenshot")
        return tmp_path / "final-page.png"
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(
        ocr=lambda: [], capture_image=capture_image, save_capture_image_to_workspace=save_frame,
        capture_after_action=lambda **kwargs: pytest.fail("do not replace the verified frame with a new shot"),
    )
    monkeypatch.setattr(module, "read_cw_stage_text", lambda runtime: {"value": "invest", "entries": [{"text": "请选择投资策略"}]})
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    calls = []
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(module, "sleep", sleep)
    # The first frame still shows the shop while its close animation plays.
    intermediate = ["shop", None] if after == "preparation" else []
    stages = chain(["shop", *intermediate], repeat(after))
    def detector_factory(runtime):
        def detect(*, image=None):
            calls.append("detect")
            if image is not None:
                classified.append(image)
            return next(stages)
        return detect
    monkeypatch.setattr(module, "stage_detector_factory", detector_factory)
    monkeypatch.setattr(module, "fetch_cw_guide_config", lambda **kwargs: {})
    def scan(session, **kwargs):
        calls.append("scan")
        prices = [1, 2, 3, 3, 1]
        ensure_cw_state(session)["shop"] = {
            "opened": True,
            "stale": False,
            "items": [{"slot": slot, "name": "希儿", "price": price} for slot, price in enumerate(prices, 1)],
        }
        return SimpleNamespace(response_snapshot=deepcopy(ensure_cw_state(session)["shop"]))
    monkeypatch.setattr(module, "scan_cw_shop", scan)
    monkeypatch.setattr(module, "shop_page_snapshot_reader_factory", lambda runtime: None)
    monkeypatch.setattr(module, "shop_closer_factory", lambda runtime: lambda: calls.append("close"))
    response = _run_cw_mutation(command_service=command, session=session, workspace_root=tmp_path,
        request_id="shop-close-verify", method="cw.event.reconcile", payload={})
    data = response["data"]
    saved = service.load_session(session.session_id)
    assert calls[:4] == ["detect", "scan", "close", "detect"]
    assert all(call == "detect" for call in calls[4:])
    assert response["ok"] is True
    assert response["screenshot"] == (None if save_failed else "final-page.png")
    assert classified == frames and saved_frames == [classified[-1]]
    if after == "preparation":
        assert calls[4:] == ["detect", "detect"]
        assert [row["price"] for row in data["shop"]["items"]] == [1, 2, 3, 3, 1]
    assert data["stage"] == (after or "unknown") and data.get("next_action") == next_action
    assert saved.scene_state["cw"]["shop"]["opened"] is opened
    assert saved.scene_state["cw"]["shop"]["stale"] is stale
    if after is None:
        assert saved.scene_state["cw"]["stage"]["stale"] and "value" not in saved.scene_state["cw"]["stage"]
    else:
        assert saved.last_stage["value"] == after
    assert ("shop" in _fact_set(data["reconciled"])) is (not stale)
    if after == "invest":
        assert data["decision"]["entries"][0]["text"] == "请选择投资策略"


def test_battle_no_auto_collect_uses_decision_page_in_session(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    from trail.scenes.cw.run_facts import start_cw_run_facts
    _, service, session, cw_service, _ = _build_cw_harness(tmp_path)
    start_cw_run_facts(session)["plot_armor"]["status"] = "observed"
    service.save_session(session)
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(capture_image=lambda **kwargs: None)
    monkeypatch.setattr(module, "run_cw_battle", lambda *a, **k: {"status": "completed", "stage": "preparation",
        "round": "2-2", "battle_report": {"progress_pct": 100}})
    monkeypatch.setattr(module, "read_cw_preparation_header", lambda runtime: {"stage": "invest", "node": "2-3", "hp": 85})
    result = cw_service.handle(method="cw.battle.run", payload={"session_id": session.session_id, "collect_crystals": False},
                               workspace_root=str(tmp_path), session_service=service)
    saved = service.load_session(session.session_id)
    assert result["stage"] == "invest" and result["current_node"] == {"stage": "invest", "node": "2-3", "hp": 85}
    assert result["battle_report"] == {"progress_pct": 100} and result["round"] == "2-2"
    assert saved.last_stage["value"] == "invest" and result["next_action"] == "cw.stage.read"


@pytest.mark.parametrize(("failure", "after"), [
    ("scan", "shop"), ("close", "shop"), ("scan", "invest"),
    ("wait", "shop"), ("wait", "invest"), ("wait", "unknown"),
])
def test_reconcile_partial_preparation_shop_failure_preserves_slots_and_stops(tmp_path, monkeypatch, failure, after):
    from trail.daemon import cw_service as module
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(ocr=lambda: [], capture_after_action=lambda **kwargs: "final-page.png")
    monkeypatch.setattr(module, "read_cw_stage_text", lambda runtime: {"value": "invest", "entries": [{"text": "请选择投资策略"}]})
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    actions = []
    stages = iter(["preparation", None if after == "unknown" else after])
    if failure == "wait":
        clock = iter([0.0, 2.0])
        monkeypatch.setattr(module, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module, "stage_detector_factory", lambda runtime: lambda: actions.append("detect") or next(stages))
    monkeypatch.setattr(module, "dismiss_cw_slots_overlay", lambda runtime: actions.append("dismiss"))
    monkeypatch.setattr(module, "_build_slots_reader_for_service", lambda *a, **k: (None, {}))
    def read_slots(session, **kwargs):
        actions.append("slots")
        slots = {"stale": False, "front": [{"name": "新角色"}], "back": [], "hand": [], "_screenshot": "old-slots.png"}
        ensure_cw_state(session)["slots"] = deepcopy(slots)
        return SimpleNamespace(response_snapshot=slots)
    monkeypatch.setattr(module, "read_cw_slots", read_slots)
    monkeypatch.setattr(module, "sleep", lambda duration: None)
    monkeypatch.setattr(module, "shop_opener_factory", lambda runtime: lambda: actions.append("open"))
    monkeypatch.setattr(module, "shop_page_snapshot_reader_factory", lambda runtime: None)
    def scan(session, **kwargs):
        actions.append("scan")
        if failure == "scan":
            raise TrailError("SCAN_FAILED", "scan failed")
        ensure_cw_state(session)["shop"] = {"opened": True, "stale": False, "items": [{"name": "新商品"}]}
        return SimpleNamespace(response_snapshot=deepcopy(ensure_cw_state(session)["shop"]))
    monkeypatch.setattr(module, "scan_cw_shop", scan)
    def close():
        actions.append("close")
        if failure == "close":
            raise TrailError("CLOSE_FAILED", "close failed")
    monkeypatch.setattr(module, "shop_closer_factory", lambda runtime: close)
    monkeypatch.setattr(module, "_apply_cw_equipment_read_with_resources", lambda *a, **k: pytest.fail("equipment must not run"))
    response = _run_cw_mutation(command_service=command, session=session, workspace_root=tmp_path,
        request_id="partial-shop-failure", method="cw.event.reconcile", payload={})
    data = response["data"]
    saved = service.load_session(session.session_id)
    assert response["screenshot"] == "final-page.png"
    assert response["ok"] and data["attempted"] == "slots|shop" and data["stage"] == after
    if after == "invest":
        assert data["next_action"] == "cw.event.handle"
        assert data["decision"]["entries"][0]["text"] == "请选择投资策略"
    if after == "unknown":
        assert data["next_action"] == "manual"
    assert data["slots"]["front"] == [{"name": "新角色"}]
    assert saved.scene_state["cw"]["slots"]["stale"] is False
    assert saved.scene_state["cw"]["shop"]["stale"] is True and "shop" not in _fact_set(data["reconciled"])
    if failure == "wait":
        assert saved.scene_state["cw"]["shop"]["opened"] is (None if after == "unknown" else after == "shop")
    assert "shop" in _fact_set(data["todo"]) and "shop" not in data
    assert actions == (["detect", "dismiss", "slots", "open", "scan", "detect"] if failure == "scan" else
                       ["detect", "dismiss", "slots", "open", "scan", "close", "detect"])


@pytest.mark.parametrize("after", ["preparation", "shop", None])
def test_reconcile_shop_close_error_rechecks_page_and_keeps_shop_stale(tmp_path, monkeypatch, after):
    from trail.daemon import cw_service as module
    _, service, session, _, command = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    actions = []
    stages = iter(["shop", after])
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(module, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module, "stage_detector_factory", lambda runtime: lambda: actions.append("detect") or next(stages))
    monkeypatch.setattr(module, "fetch_cw_guide_config", lambda **kwargs: {})
    def scan(session, **kwargs):
        actions.append("scan")
        ensure_cw_state(session)["shop"] = {"opened": True, "stale": False, "items": [{"name": "希儿"}]}
        return SimpleNamespace(response_snapshot=deepcopy(ensure_cw_state(session)["shop"]))
    monkeypatch.setattr(module, "scan_cw_shop", scan)
    monkeypatch.setattr(module, "shop_page_snapshot_reader_factory", lambda runtime: None)
    def close():
        actions.append("close")
        raise TrailError("INPUT_UNCONFIRMED", "close failed")
    monkeypatch.setattr(module, "shop_closer_factory", lambda runtime: close)
    response = _run_cw_mutation(command_service=command, session=session, workspace_root=tmp_path,
        request_id="shop-close-error", method="cw.event.reconcile", payload={})
    assert response["ok"] and response["data"]["stage"] == (after or "unknown")
    assert actions == ["detect", "scan", "close", "detect"]
    assert service.load_session(session.session_id).scene_state["cw"]["shop"]["stale"] is True
    assert "shop" not in _fact_set(response["data"]["reconciled"])
    if after is None:
        assert response["data"]["next_action"] == "manual"


def test_reconcile_cancel_during_shop_scan_stops_before_close_and_marks_side_effect(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    from trail.daemon.models import DaemonRequest, RequestCancelled, RequestControl
    from trail.daemon.protocol import PROTOCOL_VERSION
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    control = RequestControl()
    actions = []
    runtime = SimpleNamespace(click_point=lambda x, y: actions.append("click"))
    cw_service.runtime_service.get_runtime = lambda **kwargs: runtime
    _patch_reconcile_stage(monkeypatch, "shop")
    monkeypatch.setattr(module, "fetch_cw_guide_config", lambda **kwargs: {})
    def scanner_factory(runtime):
        def scanner():
            runtime.click_point(1, 2)
            control.cancellation_token.cancel()
        return scanner
    monkeypatch.setattr(module, "shop_page_snapshot_reader_factory", scanner_factory)
    def scan(session, *, scanner, **kwargs):
        scanner()
        return SimpleNamespace(response_snapshot={"opened": True, "stale": False, "items": []})
    monkeypatch.setattr(module, "scan_cw_shop", scan)
    monkeypatch.setattr(module, "shop_closer_factory", lambda runtime: lambda: actions.append("close"))
    req = DaemonRequest(request_id="cancel-scan", protocol_version=PROTOCOL_VERSION,
        workspace_root=str(tmp_path), session_id=session.session_id, verbose=False,
        method="cw.event.reconcile", payload={"session_id": session.session_id}, control=control)
    with pytest.raises(RequestCancelled):
        command.execute_business_request(req)
    assert actions == ["click"] and control.side_effect_stage == "side_effect_applied"


def test_battle_auto_collect_cancel_after_slots_stops_before_shop(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    monkeypatch.setattr(module, "_observe_battle_collection_page", lambda runtime: ({"value": "preparation", "entries": []}, None))
    from trail.daemon.models import DaemonRequest, RequestCancelled, RequestControl
    from trail.daemon.protocol import PROTOCOL_VERSION
    from trail.scenes.cw.run_facts import start_cw_run_facts
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    start_cw_run_facts(session)["plot_armor"]["status"] = "observed"
    service.save_session(session)
    control, actions = RequestControl(), []
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(capture_image=lambda **kwargs: None)
    monkeypatch.setattr(module, "run_cw_battle", lambda *a, **k: {"status": "completed", "stage": "preparation"})
    monkeypatch.setattr(module, "collect_cw_crystals", lambda *a, **k: actions.append("crystals"))
    monkeypatch.setattr(module, "crystal_collector_factory", lambda runtime: None)
    monkeypatch.setattr(module, "dismiss_cw_slots_overlay", lambda runtime: actions.append("dismiss"))
    monkeypatch.setattr(module, "_build_slots_reader_for_service", lambda *a, **k: (None, {}))
    def read_slots(session, **kwargs):
        actions.append("slots")
        control.cancellation_token.cancel()
        return SimpleNamespace(response_snapshot={"stale": False, "front": [], "back": [], "hand": []})
    monkeypatch.setattr(module, "read_cw_slots", read_slots)
    monkeypatch.setattr(module, "shop_opener_factory", lambda runtime: lambda: actions.append("shop"))
    req = DaemonRequest(request_id="cancel-battle-collect", protocol_version=PROTOCOL_VERSION,
        workspace_root=str(tmp_path), session_id=session.session_id, verbose=False,
        method="cw.battle.run", payload={"session_id": session.session_id}, control=control)
    with pytest.raises(RequestCancelled):
        command.execute_business_request(req)
    assert actions == ["crystals", "slots"]


@pytest.mark.parametrize("auto_collect", [True, False])
@pytest.mark.parametrize("outcome", ["invest", "stable", "timeout"])
def test_battle_level_up_transition_waits_before_any_preparation_input(tmp_path, monkeypatch, auto_collect, outcome):
    from trail.daemon import cw_service as module
    from trail.scenes.cw.run_facts import start_cw_run_facts
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    start_cw_run_facts(session)["plot_armor"]["status"] = "observed"
    service.save_session(session)
    calls = []
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(
        capture_image=lambda **kwargs: None, ocr=lambda: [],
        capture_after_action=lambda **kwargs: "final-page.png",
    )
    monkeypatch.setattr(module, "run_cw_battle", lambda *a, **k: {
        "status": "completed", "stage": "preparation", "round": "1-2",
        "battle_report": {"progress_pct": 100},
    })
    level = {"value": "preparation", "entries": [{"text": "等级提升", "bounds": (907, 195, 1015, 227)},
                                                 {"text": "1-3", "bounds": (420, 60, 460, 90)}]}
    normal = {"value": "preparation", "entries": []}
    invest = {"value": "invest", "entries": [{"text": "请选择投资策略", "bounds": (500, 250, 700, 300)}]}
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def reading(runtime):
        calls.append("read")
        if len([x for x in calls if x == "read"]) == 1:
            return level
        return invest if outcome == "invest" else normal if outcome == "stable" else level
    monkeypatch.setattr(module, "read_cw_stage_text", reading)
    monkeypatch.setattr(module, "read_cw_preparation_header", lambda runtime: {"stage": "preparation", "node": None, "hp": None})
    monkeypatch.setattr(module, "_collect_cw_preparation_facts", lambda *a, **k: calls.append("collect") or k["response_data"])
    envelope = _run_cw_mutation(command_service=command, session=session, workspace_root=tmp_path,
        request_id="level-transition", method="cw.battle.run", payload={"collect_crystals": auto_collect})
    result = envelope["data"]
    assert result["round"] == "1-2" and result["battle_report"] == {"progress_pct": 100}
    if outcome == "invest":
        assert result["stage"] == "invest" and result["current_node"]["node"] == "1-3"
        assert result["next_action"] == "cw.stage.read" and result["decision"]["entries"][0]["text"] == "请选择投资策略"
        assert service.load_session(session.session_id).last_stage["value"] == "invest"
        assert "collect" not in calls and envelope["screenshot"] == "final-page.png"
    elif outcome == "timeout":
        assert result["stage"] == "unknown" and result["stale"] is True
        assert result["current_node"] == {"stage": "unknown", "node": None, "hp": None}
        assert result["next_action"] == "cw.event.reconcile" and "collect" not in calls
    else:
        assert clock[0] >= module.CW_POST_BATTLE_TRANSITION_MIN_SECONDS
        assert result["stage"] == "preparation" and ("collect" in calls) is auto_collect


@pytest.mark.parametrize("present", [0, 4])
def test_reconcile_open_shop_uncertain_cards_keep_old_or_partial_and_are_stale(tmp_path, monkeypatch, present):
    from trail.daemon import cw_service as module
    from trail.scenes.cw import stage as stage_module
    from trail.runtime.resources import resolve_scene_asset
    from PIL import Image
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    actions = []
    class Runtime:
        closed = False
        def __init__(self):
            self.matcher = SimpleNamespace(locate=self.locate)
        def screenshot(self, **kwargs):
            image = Image.new("RGB", (1920, 1080), "black")
            if not self.closed:
                image.paste("white", (1553, 453, 1689, 580))
            return image
        def ocr_image(self, image, **kwargs):
            return [_ocr_piece("备战阶段"), *([] if self.closed else [_ocr_piece("刷新")])]
        def locate(self, template, image):
            if str(template) == str(resolve_scene_asset("cw", "action.battle_start")):
                return {"left": 1650, "top": 720, "width": 50, "height": 20}
            return None
        def capture_after_action(self, **kwargs):
            return "final-page.png"
    runtime = Runtime()
    cw_service.runtime_service.get_runtime = lambda **kwargs: runtime
    monkeypatch.setattr(module, "stage_detector_factory", stage_module.build_cw_stage_detector)
    monkeypatch.setattr(module, "fetch_cw_guide_config", lambda **kwargs: {})
    monkeypatch.setattr(module, "shop_page_snapshot_reader_factory", lambda runtime: None)
    def scan(session, **kwargs):
        actions.append("scan")
        rows = [{"slot": i, "name": "希儿" if i <= present else None, "price": 1 if i <= present else None} for i in range(1, 6)]
        ensure_cw_state(session)["shop"] = {"opened": True, "stale": False, "items": rows}
        return SimpleNamespace(response_snapshot=deepcopy(ensure_cw_state(session)["shop"]))
    monkeypatch.setattr(module, "scan_cw_shop", scan)
    def close():
        actions.append("close")
        runtime.closed = True
    monkeypatch.setattr(module, "shop_closer_factory", lambda runtime: close)
    monkeypatch.setattr(module, "_collect_cw_preparation_facts", lambda *a, **k: pytest.fail("must not collect prep"))
    response = _run_cw_mutation(command_service=command, session=session, workspace_root=tmp_path,
        request_id="overlay-shop-guard", method="cw.event.reconcile", payload={})
    data = response["data"]
    saved = service.load_session(session.session_id).scene_state["cw"]["shop"]
    assert response["ok"] and response["screenshot"] == "final-page.png"
    assert data["stage"] == "preparation" and data["attempted"] == "shop"
    assert actions == ["scan", "close"] and saved["stale"] is True
    assert "shop" not in _fact_set(data["reconciled"]) and "shop" in _fact_set(data["todo"])
    if present == 0:
        assert saved["items"] == [{"name": "银狼LV.999", "cost": 3}]
    else:
        assert sum(bool(row.get("name")) for row in saved["items"]) == 4
        assert saved["items"][4]["name"] is None


@pytest.mark.parametrize("present", [0, 4])
def test_reconcile_preparation_shop_scan_uncertain_is_stale_and_uses_closed_page_shot(tmp_path, monkeypatch, present):
    from trail.daemon import cw_service as module
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    _seed_reconcile_stale_state(session)
    service.save_session(session)
    actions = []
    cw_service.runtime_service.get_runtime = lambda **kwargs: SimpleNamespace(capture_after_action=lambda **kwargs: "closed-page.png")
    _patch_reconcile_stage(monkeypatch, "preparation")
    monkeypatch.setattr(module, "dismiss_cw_slots_overlay", lambda runtime: None)
    monkeypatch.setattr(module, "_build_slots_reader_for_service", lambda *a, **k: (None, {}))
    def slots(session, **kwargs):
        data = {"stale": False, "front": [], "back": [], "hand": [], "_screenshot": "old-slots.png"}
        ensure_cw_state(session)["slots"] = deepcopy(data)
        return SimpleNamespace(response_snapshot=data)
    monkeypatch.setattr(module, "read_cw_slots", slots)
    monkeypatch.setattr(module, "sleep", lambda duration: None)
    monkeypatch.setattr(module, "shop_opener_factory", lambda runtime: lambda: actions.append("open"))
    monkeypatch.setattr(module, "shop_closer_factory", lambda runtime: lambda: actions.append("close"))
    monkeypatch.setattr(module, "shop_page_snapshot_reader_factory", lambda runtime: None)
    def scan(session, **kwargs):
        actions.append("scan")
        rows = [{"slot": i, "name": "希儿" if i <= present else None, "price": 1 if i <= present else None} for i in range(1, 6)]
        ensure_cw_state(session)["shop"] = {"opened": True, "stale": False, "items": rows}
        return SimpleNamespace(response_snapshot=deepcopy(ensure_cw_state(session)["shop"]))
    monkeypatch.setattr(module, "scan_cw_shop", scan)
    monkeypatch.setattr(module, "_apply_cw_equipment_read_with_resources", lambda *a, **k: {"stale": False, "items": []})
    response = _run_cw_mutation(command_service=command, session=session, workspace_root=tmp_path,
        request_id="partial-prep-shop", method="cw.event.reconcile", payload={})
    data = response["data"]
    saved = service.load_session(session.session_id).scene_state["cw"]["shop"]
    assert response["ok"] and response["screenshot"] == "closed-page.png"
    assert actions == ["open", "scan", "close"] and data["stage"] == "preparation"
    assert saved["stale"] is True and "shop" in _fact_set(data["todo"])
    assert "shop" not in _fact_set(data["reconciled"])
    if present == 0:
        assert saved["items"] == [{"name": "银狼LV.999", "cost": 3}]
    else:
        assert sum(bool(row.get("name")) for row in saved["items"]) == 4
        assert saved["items"][4]["name"] is None


def test_battle_waits_even_when_initial_level_up_ocr_misses(monkeypatch):
    from trail.daemon import cw_service as module
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def read(runtime):
        if clock[0] >= 5:
            return {"value": "invest", "entries": []}
        entries = [{"text": "1-3", "bounds": (420, 60, 460, 90)}]
        if 1 <= clock[0] < 3:
            entries.append({"text": "等级提升", "bounds": (906, 194, 1015, 230)})
        return {"value": "preparation", "entries": entries}
    monkeypatch.setattr(module, "read_cw_stage_text", read)
    result = module._read_post_battle_transition(object(), check_cancelled=lambda: None, budget=15)
    assert result["value"] == "invest" and result["_transition_node"] == "1-3"
    assert clock[0] == 5


def test_battle_collection_stops_when_shop_changes_during_scan(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    _, service, session, _, _ = _build_cw_harness(tmp_path)
    actions = []
    observations = iter([
        ({"value": "preparation", "entries": [{"text": "商店", "bounds": (1603, 975, 1653, 1002)}]}, None),
        ({"value": "invest", "entries": []}, None),
    ])
    monkeypatch.setattr(module, "_observe_battle_collection_page", lambda runtime: next(observations))
    monkeypatch.setattr(module, "_build_slots_reader_for_service", lambda *a, **k: (None, {}))
    monkeypatch.setattr(module, "sleep", lambda seconds: None)
    monkeypatch.setattr(module, "shop_opener_factory", lambda runtime: lambda: actions.append("open"))
    monkeypatch.setattr(module, "shop_closer_factory", lambda runtime: lambda: actions.append("close"))
    monkeypatch.setattr(module, "shop_page_snapshot_reader_factory", lambda runtime: None)
    monkeypatch.setattr(module, "scan_cw_shop", lambda *a, **k: actions.append("scan") or SimpleNamespace(response_snapshot={}))
    runtime = SimpleNamespace(click_point=lambda x, y: actions.append("open"))
    result = module._collect_cw_preparation_facts(session, runtime=runtime, order=("shop",),
        response_data={"status": "completed", "stage": "preparation", "round": "1-2",
                       "current_node": {"stage": "preparation", "node": "1-3", "hp": None}},
        guard_battle_inputs=True)
    assert actions == ["open", "scan"]
    assert result["stage"] == "invest" and result["current_node"]["node"] == "1-3"
    assert "shop" not in result and service is not None
    assert session.last_stage["value"] == "invest"


def test_battle_collection_unknown_frame_blocks_all_inputs(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    _, _, session, _, _ = _build_cw_harness(tmp_path)
    calls = []
    monkeypatch.setattr(module, "_observe_battle_collection_page", lambda runtime: ({"value": "unknown", "entries": []}, None))
    monkeypatch.setattr(module, "collect_cw_crystals", lambda *a, **k: calls.append("crystals"))
    result = module._collect_cw_preparation_facts(session, runtime=object(), order=("crystals",),
        response_data={"status": "completed", "stage": "preparation"}, guard_battle_inputs=True)
    assert calls == [] and result["stage"] == "unknown" and result["stale"] is True
    assert session.last_stage is None


def test_battle_shop_closes_only_the_observed_control(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    _, _, session, _, _ = _build_cw_harness(tmp_path)
    clicked = []
    runtime = SimpleNamespace(click_point=lambda x, y: clicked.append((x, y)))
    readings = iter([
        ({"value": "preparation", "entries": [{"text": "商店", "bounds": (1603, 975, 1653, 1002)}]}, None),
        ({"value": "shop", "entries": [{"text": "收起商店", "bounds": (1603, 975, 1653, 1002)}]}, None),
    ])
    monkeypatch.setattr(module, "_observe_battle_collection_page", lambda runtime: next(readings))
    monkeypatch.setattr(module, "_build_slots_reader_for_service", lambda *a, **k: (None, {}))
    monkeypatch.setattr(module, "sleep", lambda seconds: None)
    monkeypatch.setattr(module, "shop_opener_factory", lambda runtime: lambda: None)
    monkeypatch.setattr(module, "shop_page_snapshot_reader_factory", lambda runtime: None)
    monkeypatch.setattr(module, "scan_cw_shop", lambda *a, **k: SimpleNamespace(response_snapshot={}))
    result = module._collect_cw_preparation_facts(session, runtime=runtime, order=("shop",),
        response_data={"status": "completed", "stage": "preparation"}, guard_battle_inputs=True)
    assert clicked == [(1628, 988), (1628, 988)] and result["stage"] == "preparation"


def test_battle_crystal_drag_requires_stage_on_target_frame(monkeypatch):
    from trail.scenes.cw import slots as module
    frames = iter(["first", "changed"])
    dragged = []
    runtime = SimpleNamespace(drag_to=lambda *args, **kwargs: dragged.append(args))
    monkeypatch.setattr(module.stage, "build_cw_stage_detector", lambda runtime: lambda: "preparation")
    monkeypatch.setattr(module, "_runtime_slots_image", lambda runtime: next(frames))
    monkeypatch.setattr(module, "_visible_cw_crystals", lambda image: [(1400, 500)])
    monkeypatch.setattr(module, "_visible_cw_gold_crystals", lambda image: [])
    checks = []
    def verify(image):
        checks.append(image)
        return "invest" if image == "changed" else "preparation"
    monkeypatch.setattr(module, "sleep", lambda seconds: None)
    outcome = module.build_cw_crystal_collector(runtime, verify_frame=verify)()
    assert outcome["stage"] == "invest" and outcome["last_crystal_collection"] == "blocked"
    assert len(dragged) == 1 and checks[-1] == "changed"


def test_battle_input_guard_stops_after_deadline_or_level_overlay(tmp_path, monkeypatch):
    from trail.daemon import cw_service as module
    _, _, session, _, _ = _build_cw_harness(tmp_path)
    monkeypatch.setattr(module, "monotonic", lambda: 12.0)
    calls = []
    monkeypatch.setattr(module, "collect_cw_crystals", lambda *a, **k: calls.append("collect"))
    for reading, deadline in [
        ({"value": "preparation", "entries": []}, 10.0),
        ({"value": "preparation", "entries": [{"text": "等级提升", "bounds": (906, 194, 1015, 230)}]}, 20.0),
    ]:
        monkeypatch.setattr(module, "_observe_battle_collection_page", lambda runtime: (reading, None))
        result = module._collect_cw_preparation_facts(session, runtime=object(), order=("crystals",),
            response_data={"status": "completed", "stage": "preparation", "round": "1-2"},
            guard_battle_inputs=True, battle_deadline=deadline)
        assert result["stage"] == "unknown" and result["round"] == "1-2"
    assert calls == []


def test_battle_wait_ignores_previous_round_and_conflicting_node(monkeypatch):
    from trail.daemon import cw_service as module
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def read(runtime):
        if clock[0] >= 2:
            return {"value": "invest", "entries": []}
        node = "1-2" if clock[0] < 0.5 else "1-3" if clock[0] < 1.5 else "1-4"
        return {"value": "preparation", "entries": [{"text": node, "bounds": (420, 60, 460, 90)}]}
    monkeypatch.setattr(module, "read_cw_stage_text", read)
    result = module._read_post_battle_transition(object(), check_cancelled=lambda: None, budget=15, previous_round="1-2")
    assert result["value"] == "invest" and result["_transition_node"] is None


def test_battle_observation_preserves_frame_if_ocr_fails(monkeypatch):
    from trail.daemon import cw_service as module
    frame = object()
    runtime = SimpleNamespace(capture_image=lambda **kwargs: frame, ocr_image=lambda *a, **k: None)
    monkeypatch.setattr(module, "read_cw_stage_text", lambda *a, **k: (_ for _ in ()).throw(TrailError("OCR_FAILED", "failed")))
    reading, observed_frame = module._observe_battle_collection_page(runtime)
    assert reading["value"] == "unknown" and observed_frame is frame


def test_battle_header_conflicting_nodes_are_unknown():
    from trail.scenes.cw.stage import project_cw_preparation_header
    reading = {"value": "preparation", "entries": [
        {"text": "1-3", "bounds": (420, 60, 460, 90)},
        {"text": "1-4", "bounds": (420, 60, 460, 90)},
    ]}
    assert project_cw_preparation_header(reading)["node"] is None


def test_battle_unverified_armor_level_transition_has_no_preflight_input(tmp_path, monkeypatch):
    from PIL import Image
    from trail.daemon import cw_service as module
    from trail.scenes.cw.run_facts import start_cw_run_facts
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    start_cw_run_facts(session)
    service.save_session(session)
    clicks = []
    runtime = SimpleNamespace(
        capture_image=lambda **kwargs: None,
        screenshot=lambda: Image.new("RGB", (1920, 1080)),
        ocr_image=lambda image, **kwargs: [
            _ocr_piece("备战阶段", left=427, top=36),
            _ocr_piece("等级提升", left=907, top=195),
            _ocr_piece("出战", left=1770, top=740),
        ],
        click_point=lambda *point: clicks.append(point),
        capture_after_action=lambda **kwargs: "final-page.png",
    )
    cw_service.runtime_service.get_runtime = lambda **kwargs: runtime
    monkeypatch.setattr(module, "read_cw_preparation_header", lambda runtime: pytest.fail("preflight must not open armor"))
    monkeypatch.setattr(module, "run_cw_battle", lambda *a, **k: {
        "status": "observed", "stage": "preparation", "stale": False, "in_battle": False,
    })
    envelope = _run_cw_mutation(command_service=command, session=session, workspace_root=tmp_path,
        request_id="level-preflight", method="cw.battle.run", payload={"collect_crystals": False})
    assert envelope["data"]["status"] == "observed"
    assert clicks == []
