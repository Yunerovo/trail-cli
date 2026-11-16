from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from trail.daemon.command_service import CommandService
from trail.daemon.models import DaemonRequest
from trail.daemon.protocol import PROTOCOL_VERSION
from trail.daemon.session_service import SessionServiceRegistry
from trail.output.rendering import render_output
from trail.scenes.cw.plot_armor import read_cw_plot_armor
from trail.scenes.cw.run_facts import record_cw_plot_armor, start_cw_run_facts, summarize_cw_run_facts


def piece(text, box, score=.99):
    x1, y1, x2, y2 = box
    return ([[x1,y1],[x2,y1],[x2,y2],[x1,y2]], text, score)


def frame(name="aura-header-3-1.png"):
    image = Image.new("RGB", (1920, 1080), "black")
    header = Image.open(Path(__file__).parent / "fixtures" / "cw" / name).convert("RGB")
    image.paste(header, (380, 20))
    return image


class Runtime:
    def __init__(self, image, page, digits):
        self.image, self.page, self.digits = image, page, digits
        self.captures = 0
        self.ocr_calls = []
    def capture_image(self, **kwargs):
        self.captures += 1
        return self.image
    def ocr_image(self, image, *, ocr):
        self.ocr_calls.append((image.size, ocr.ocr_mode))
        if image.size == (3840, 2160):
            return []
        if image.size == (1920, 1080):
            return self.page
        expected = self.image.crop((1455, 64, 1515, 106)).resize((180, 126), Image.Resampling.LANCZOS)
        assert image.tobytes() == expected.tobytes()
        return self.digits


TITLE = piece("主角光环与奇遇详情", (557, 52, 788, 77))
READING = {"value": "plot_armor_details", "entries": [{"text": "70", "bounds": (1470, 70, 1490, 90)}]}


@pytest.mark.parametrize(("header", "value"), [("aura-header-3-1.png", 1), ("aura-header-1-4.png", 0), ("aura-header-3-1.png", 3)])
def test_read_same_frame_counter_with_real_header_and_confident_digit(header, value):
    runtime = Runtime(frame(header), [TITLE], [piece(str(value), (70, 15, 110, 100))])
    result = read_cw_plot_armor(runtime, READING, spotlight_list=[])
    assert result["status"] == "unverified" and result["reselect_remaining"] == value
    assert result["_reselect_verified"] is True
    assert runtime.captures == 1 and runtime.ocr_calls == [((1920,1080), "high"), ((180,126), "high"), ((3840,2160), "high")]


def test_two_card_details_keep_first_aura_and_count_locked_slot():
    runtime = Runtime(frame(), [TITLE], [piece("1", (70, 15, 110, 100))])
    reading = {"value": "plot_armor_details", "entries": [
        {"text": "花团锦簇", "bounds": (732, 441, 837, 469)},
        {"text": "2-7节点解锁该主角光环", "bounds": (1009, 569, 1265, 596)},
    ]}
    result = read_cw_plot_armor(runtime, reading, spotlight_list=[{"name": "花团锦簇", "desc": "生命增幅"}])

    assert result["status"] == "observed"
    assert [(card["slot"], card["name"]) for card in result["cards"]] == [(1, "花团锦簇")]
    assert result["reselect_remaining"] == 1


def test_enlarged_same_frame_recovers_title_and_locked_card():
    class EnlargedRuntime(Runtime):
        def ocr_image(self, image, *, ocr):
            if image.size == (3840, 2160):
                return [piece("主角光环与奇遇详情", (1114, 104, 1576, 154)),
                        piece("花团锦簇", (1456, 876, 1680, 944)),
                        piece("2-7节点解锁该主角光环", (2016, 1140, 2532, 1196))]
            return super().ocr_image(image, ocr=ocr)
    runtime = EnlargedRuntime(frame(), [TITLE], [piece("1", (70, 15, 110, 100))])
    result = read_cw_plot_armor(runtime, {"value": "plot_armor_details", "entries": []},
                                spotlight_list=[{"name": "花团锦簇", "desc": "生命增幅"}])
    assert result["status"] == "observed"
    assert [(card["slot"], card["name"]) for card in result["cards"]] == [(1, "花团锦簇")]


@pytest.mark.parametrize("case", ["no_icon", "no_title", "modal", "wrong_page", "none", "conflict", "cost", "off_axis", "low_confidence"])
def test_unconfirmed_counter_never_uses_full_page_number(case):
    image = frame()
    page = [TITLE, piece("70", (1470, 70, 1500, 95))]
    digits = [piece("1", (70, 15, 110, 100))]
    if case == "no_icon":
        image.paste("black", (1465,29,1506,64))
    elif case in {"no_title", "wrong_page"}:
        page = [piece("备战阶段", (557,52,788,77)), piece("70", (1470,70,1500,95))]
    elif case == "modal":
        page.append(piece("是否重新选择主角和光环，将消耗1次重选次数", (700,400,1200,450)))
    elif case == "none":
        digits = []
    elif case == "conflict":
        digits = [piece("0", (30,13,60,91)), piece("1", (60,13,90,91))]
    elif case == "cost":
        digits = [piece("×1", (70,15,110,100))]
    elif case == "off_axis":
        digits = [piece("70", (0,13,18,91))]
    elif case == "low_confidence":
        digits = [piece("1", (70,15,110,100), .2)]
    result = read_cw_plot_armor(Runtime(image, page, digits), READING, spotlight_list=[])
    assert result["reselect_remaining"] is None and result["_reselect_verified"] is False


def test_current_frame_title_required_even_when_previous_reading_claims_details():
    runtime = Runtime(frame(), [piece("选择主角", (557,52,788,77))], [piece("1", (70,15,110,100))])
    assert read_cw_plot_armor(runtime, READING, spotlight_list=[])["reselect_remaining"] is None
    assert len(runtime.ocr_calls) == 2


@pytest.mark.parametrize("status", ["observed", "unverified"])
def test_legacy_counter_is_hidden_in_summaries_and_both_state_exports(tmp_path, status):
    registry = SessionServiceRegistry()
    service = registry.for_workspace(str(tmp_path))
    session = service.create_session(window_binding={"title": "崩坏：星穹铁道"})
    facts = start_cw_run_facts(session)
    legacy = {"status": status, "cards": [], "reselect_remaining": 70}
    facts["plot_armor"] = deepcopy(legacy)
    facts["plot_armor_history"] = [deepcopy(legacy)]
    service.save_session(session)
    summary = summarize_cw_run_facts(session)
    assert summary["plot_armor"]["reselect_remaining"] is None
    text = render_output("cw.battle.run", {"ok": True, "data": {"status": "completed", "run_facts": summary}})
    assert "剩余重选=unknown" in text
    command = CommandService(runtime_service=SimpleNamespace(), session_service=registry)
    for cw_run in (False, True):
        payload = {"session_id": session.session_id, **({"cw_run": True} if cw_run else {})}
        result = command.handle(DaemonRequest(request_id="dump", protocol_version=PROTOCOL_VERSION,
            workspace_root=str(tmp_path), session_id=session.session_id, verbose=False, method="state.dump", payload=payload))
        data = result["data"]["cw_run"] if cw_run else result["data"]["scene_state"]["cw"]["run_facts"]
        assert data["plot_armor"]["reselect_remaining"] is None
        assert "_reselect_verified" not in data["plot_armor"] and data["plot_armor_history"] == [legacy]
        output = render_output("state.dump", result, output_format="yaml")
        assert "reselect_remaining: null" in output and "_reselect_verified" not in output
    assert service.load_session(session.session_id).scene_state["cw"]["run_facts"]["plot_armor"] == legacy


def test_verified_zero_survives_unverified_cards_and_exports_without_private_marker(tmp_path):
    registry = SessionServiceRegistry()
    service = registry.for_workspace(str(tmp_path))
    session = service.create_session(window_binding={"title": "崩坏：星穹铁道"})
    start_cw_run_facts(session)
    reading = read_cw_plot_armor(Runtime(frame("aura-header-1-4.png"), [TITLE], [piece("0", (70,15,110,100))]), READING, spotlight_list=[])
    record_cw_plot_armor(session, reading)
    service.save_session(session)
    assert summarize_cw_run_facts(session)["plot_armor"]["reselect_remaining"] == 0
    data = service.dump_state(session_id=session.session_id)["scene_state"]["cw"]["run_facts"]
    assert data["plot_armor"]["status"] == "unverified" and data["plot_armor"]["reselect_remaining"] == 0
    assert "_reselect_verified" not in data["plot_armor"]
    assert service.load_session(session.session_id).scene_state["cw"]["run_facts"]["plot_armor"]["_reselect_verified"] is True
    command = CommandService(runtime_service=SimpleNamespace(), session_service=registry)
    for cw_run in (False, True):
        payload = {"session_id": session.session_id, **({"cw_run": True} if cw_run else {})}
        result = command.handle(DaemonRequest(request_id="dump-zero", protocol_version=PROTOCOL_VERSION,
            workspace_root=str(tmp_path), session_id=session.session_id, verbose=False, method="state.dump", payload=payload))
        run = result["data"]["cw_run"] if cw_run else result["data"]["scene_state"]["cw"]["run_facts"]
        assert run["plot_armor"]["reselect_remaining"] == 0
        assert "reselect_remaining: 0" in render_output("state.dump", result, output_format="yaml")
