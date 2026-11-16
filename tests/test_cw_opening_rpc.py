from __future__ import annotations

from copy import deepcopy

import pytest

from trail.daemon.models import DaemonRequest, RequestCancelled, RequestControl
from trail.daemon.protocol import PROTOCOL_VERSION
from trail.scenes.cw.models import ensure_cw_state
from trail.scenes.cw import opening
from trail.scenes.cw.run_facts import start_cw_run_facts
from tests.test_cw_events import _build_cw_harness


def piece(text, x=0, y=0):
    return {"text": text, "box": {"left": x, "top": y, "width": 40, "height": 20}}


class OpeningRuntime:
    def __init__(self, *, page="overview", fail_page=None, cancel_on=None):
        self.page, self.fail_page, self.cancel_on = page, fail_page, cancel_on
        self.actions = []
        self.token = None

    def ocr(self, **kwargs):
        if self.cancel_on == "wait" and self.actions:
            self.token.cancel()
        if self.page == self.fail_page:
            return []
        if self.page == "overview":
            return [piece("本场对局首领"), piece("本局奇遇"), piece("下一步"),
                    piece("敌人难度+5"), piece("伙伴", 1200, 680), piece("词缀", 310, 980)]
        if self.page == "boss":
            return [piece("敌方信息"), piece("首领 等级 95", 1400, 210)]
        if self.page == "opponent":
            return [piece("对手信息"), piece("第一位面", 400, 330), piece("攻击提高10%", 400, 370)]
        if self.page == "scroll":
            return [piece("第三位面", 400, 330), piece("速度提高20%", 400, 370)]
        if self.page == "rank":
            return [piece("职级效果"), piece("增益", 500, 400)]
        return []

    def click_point(self, x, y):
        self.actions.append(("click", x, y))
        if (x, y) in opening.BOSS_DETAIL_POINTS:
            self.page = "boss"
        elif (x, y) == opening.DIFFICULTY_POINT:
            self.page = "opponent"
        elif (x, y) == opening.RANK_EFFECTS_POINT:
            self.page = "rank"
        else:
            self.page = "overview"
        if self.cancel_on == "click":
            self.token.cancel()

    def drag_to(self, x, y, x2, y2, *, duration):
        self.actions.append(("drag", x, y, x2, y2))
        self.page = "scroll" if (x, y) == (1543, 640) else "tooltip"

    def press_key(self, key):
        self.actions.append(("key", key))
        self.page = "overview"

    def capture_after_action(self, **kwargs):
        return None


def setup(tmp_path, monkeypatch, runtime):
    _, service, session, cw_service, command = _build_cw_harness(tmp_path)
    facts = start_cw_run_facts(session)
    facts["opening"] = {"old": True}
    ensure_cw_state(session)["equipment"] = {"stale": False, "items": [{"name": "旧装备"}]}
    service.save_session(session)
    cw_service.runtime_service.get_runtime = lambda **kwargs: runtime
    clock = iter(range(10000))
    monkeypatch.setattr(opening, "monotonic", lambda: next(clock) * 5)
    monkeypatch.setattr(opening, "sleep", lambda _: None)
    return service, session, command


def request(tmp_path, session, control=None):
    return DaemonRequest(request_id="opening-test", protocol_version=PROTOCOL_VERSION,
                         workspace_root=str(tmp_path), session_id=session.session_id,
                         method="cw.stage.opening", verbose=False, payload={"session_id": session.session_id},
                         control=control or RequestControl())


def test_opening_rpc_commits_only_after_confirmed_return(tmp_path, monkeypatch):
    runtime = OpeningRuntime()
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    result = command.handle(request(tmp_path, session))
    assert result["ok"] is True
    assert result["data"]["affixes"] == [{"name": "词缀", "description": None}]
    saved = ensure_cw_state(service.load_session(session.session_id))
    assert saved["run_facts"]["opening"] == result["data"]
    assert saved["equipment"] == {"stale": False, "items": [{"name": "旧装备"}]}
    assert runtime.actions == [("click", *opening.BOSS_DETAIL_POINTS[0]), ("key", "esc"),
        ("click", *opening.BOSS_DETAIL_POINTS[1]), ("key", "esc"),
        ("click", *opening.BOSS_DETAIL_POINTS[2]), ("key", "esc"),
        ("click", *opening.DIFFICULTY_POINT), ("drag", 1543, 640, 1543, 803),
        ("click", *opening.RANK_EFFECTS_POINT), ("key", "esc"),
        ("drag", 324, 990, 324, 990), ("click", 1060, 946)]


@pytest.mark.parametrize("page,fail_page", [("wrong", None), ("overview", "boss"), ("overview", "rank")])
def test_opening_rpc_failure_preserves_previous_run(tmp_path, monkeypatch, page, fail_page):
    runtime = OpeningRuntime(page=page, fail_page=fail_page)
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    before = deepcopy(ensure_cw_state(service.load_session(session.session_id))["run_facts"])
    result = command.handle(request(tmp_path, session))
    assert result["ok"] is False
    assert ensure_cw_state(service.load_session(session.session_id))["run_facts"] == before
    if page == "wrong":
        assert not runtime.actions and result["error"]["code"] == "CW_OPENING_OVERVIEW_REQUIRED"
    else:
        assert result["error"]["code"] == "DAEMON_UNAVAILABLE"
        assert runtime.actions[-1][0] == "click"
        assert service.request_status("opening-test")["tainted"] is True


@pytest.mark.parametrize("cancel_on", ["preflight", "click", "wait"])
def test_opening_cancellation_marks_control_only_after_input(tmp_path, monkeypatch, cancel_on):
    runtime = OpeningRuntime(cancel_on=cancel_on)
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    control = RequestControl()
    runtime.token = control.cancellation_token
    if cancel_on == "preflight":
        runtime.token.cancel()
    with pytest.raises(RequestCancelled):
        command.execute_business_request(request(tmp_path, session, control))
    assert control.side_effect_stage == ("none" if cancel_on == "preflight" else "side_effect_applied")
    assert ensure_cw_state(service.load_session(session.session_id))["run_facts"]["opening"] == {"old": True}
    assert len(runtime.actions) == (0 if cancel_on == "preflight" else 1)


def test_opening_cancellation_after_click_is_unknown_in_executor_journal(tmp_path, monkeypatch):
    from trail.daemon.request_executor import RequestExecutor
    runtime = OpeningRuntime(cancel_on="click")
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    control = RequestControl(wait_timeout=2.0)
    runtime.token = control.cancellation_token
    executor = RequestExecutor(command_service=command, session_service=command.session_service)
    result = executor.handle(request(tmp_path, session, control))
    assert result["error"]["code"] == "REQUEST_CANCEL_UNKNOWN"
    status = service.request_status("opening-test")
    assert status["state"] == "cancel_unknown" and status["tainted"] is True
    assert ensure_cw_state(service.load_session(session.session_id))["run_facts"]["opening"] == {"old": True}
    assert len(runtime.actions) == 1


def test_opening_rpc_reads_four_preview_affixes_without_next_button(tmp_path, monkeypatch):
    class FourAffixRuntime(OpeningRuntime):
        def ocr(self, **kwargs):
            if self.page == "overview":
                return [piece("本场对局首领"), piece("本局奇遇"), piece("下一步", 1360, 980),
                        piece("敌人难度89", 130, 980), piece("伙伴", 1200, 680),
                        piece("侵蚀·第一位面强化", 350, 980), piece("霸占舞台", 605, 980),
                        piece("成长的烦恼", 782, 980), piece("永久创伤", 967, 980)]
            return super().ocr(**kwargs)
    runtime = FourAffixRuntime()
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    result = command.handle(request(tmp_path, session))
    assert result["ok"] is True and runtime.page == "overview"
    expected = ["侵蚀·第一位面强化", "霸占舞台", "成长的烦恼", "永久创伤"]
    assert result["data"]["affixes"] == [{"name": name, "description": None} for name in expected]
    saved = ensure_cw_state(service.load_session(session.session_id))["run_facts"]
    assert saved["opening"]["affixes"] == result["data"]["affixes"]
    hovers = [action for action in runtime.actions if action[0] == "drag" and action[1] == action[3] and action[2] == action[4]]
    assert hovers == [("drag", x, 990, x, 990) for x in (364, 619, 796, 981)]
    assert ("click", 1360, 980) not in runtime.actions


def test_opening_preserves_observed_affixes_on_incomplete_read(tmp_path, monkeypatch):
    from trail.scenes.cw.run_facts import record_cw_opening
    runtime = OpeningRuntime()
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    prior = {"affixes": [{"name": "可靠词缀", "description": None}], "affixes_status": "observed"}
    record_cw_opening(session, prior)
    service.save_session(session)
    result = command.handle(request(tmp_path, session))
    assert result["ok"] and result["data"]["affixes_status"] == "partial"
    saved = ensure_cw_state(service.load_session(session.session_id))["run_facts"]["opening"]
    assert saved["affixes"] == prior["affixes"] and saved["affixes_status"] == "observed"


def preview_pieces():
    return [piece("本场对局首领", 550, 190), piece("本局奇遇", 1180, 620),
            piece("下一步", 1380, 980),
            *[piece("阵营", x, 675) for x in (280, 560, 840)],
            *[piece(name, x, 730) for name, x in (("甲", 280), ("乙", 560), ("丙", 840))]]


@pytest.mark.parametrize("behavior,expected_clicks,ok", [
    ("ignored_once", 2, True), ("ignored_twice", 2, False),
    ("unknown", 1, False), ("modal", 1, False), ("changed", 1, False),
])
def test_boss_retry_requires_same_full_preview(tmp_path, monkeypatch, behavior, expected_clicks, ok):
    class RetryRuntime(OpeningRuntime):
        def __init__(self):
            super().__init__()
            self.first_clicks = 0
            self.post_click_reads = 0

        def click_point(self, x, y):
            if (x, y) == opening.BOSS_DETAIL_POINTS[0]:
                self.first_clicks += 1
                self.actions.append(("click", x, y))
                if self.first_clicks == 2 and behavior == "ignored_once":
                    self.page = "boss"
                return
            super().click_point(x, y)

        def ocr(self, **kwargs):
            if self.page == "overview":
                pieces = preview_pieces()
                if self.first_clicks:
                    self.post_click_reads += 1
                    # The first read is the timeout poll; subsequent reads are fresh checks.
                    if self.post_click_reads > 1:
                        if behavior == "unknown":
                            return []
                        if behavior == "modal":
                            pieces.append(piece("确认", 950, 500))
                        if behavior == "changed":
                            pieces[-1] = piece("不同阵营", 840, 730)
                return pieces
            return super().ocr(**kwargs)

    runtime = RetryRuntime()
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    before = deepcopy(ensure_cw_state(service.load_session(session.session_id))["run_facts"])
    result = command.handle(request(tmp_path, session))
    assert result["ok"] is ok
    assert runtime.first_clicks == expected_clicks
    if not ok:
        assert runtime.actions == [("click", *opening.BOSS_DETAIL_POINTS[0])] * expected_clicks
        assert ensure_cw_state(service.load_session(session.session_id))["run_facts"] == before


def test_boss_retry_cancellation_before_second_click(tmp_path, monkeypatch):
    class CancelRuntime(OpeningRuntime):
        def __init__(self):
            super().__init__()
            self.reads = 0
        def click_point(self, x, y):
            self.actions.append(("click", x, y))
        def ocr(self, **kwargs):
            self.reads += 1
            if self.reads == 4:
                self.token.cancel()
            return preview_pieces()

    runtime = CancelRuntime()
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    control = RequestControl()
    runtime.token = control.cancellation_token
    with pytest.raises(RequestCancelled):
        command.execute_business_request(request(tmp_path, session, control))
    assert runtime.actions == [("click", *opening.BOSS_DETAIL_POINTS[0])]
    assert ensure_cw_state(service.load_session(session.session_id))["run_facts"]["opening"] == {"old": True}


def test_boss_detail_arriving_during_retry_checks_does_not_click_again(monkeypatch):
    class LateDetail(OpeningRuntime):
        def __init__(self):
            super().__init__()
            self.reads = 0
        def click_point(self, x, y):
            self.actions.append(("click", x, y))
        def ocr(self, **kwargs):
            self.reads += 1
            if self.reads >= 2:
                return [piece("敌方信息"), piece("首领 等级 95", 1400, 210)]
            return preview_pieces()

    runtime = LateDetail()
    monkeypatch.setattr(opening, "monotonic", iter(range(0, 100, 5)).__next__)
    initial = opening.read_cw_stage_text(runtime)["entries"]
    # Force the original wait to time out; the next independent observation sees details.
    original_wait = opening._wait_for_entries
    def timeout(*args, **kwargs):
        raise opening.TrailError("CW_OPENING_PAGE_UNCONFIRMED", "timeout")
    monkeypatch.setattr(opening, "_wait_for_entries", timeout)
    detail = opening._read_boss_detail(runtime, opening.BOSS_DETAIL_POINTS[0], initial, check_cancelled=lambda: None)
    assert opening._has(detail, "敌方信息")
    assert runtime.actions == [("click", *opening.BOSS_DETAIL_POINTS[0])]



def test_transient_modal_during_first_wait_never_retries(monkeypatch):
    class TransientModal(OpeningRuntime):
        def __init__(self):
            super().__init__()
            self.reads = 0
        def click_point(self, x, y):
            self.actions.append(("click", x, y))
        def ocr(self, **kwargs):
            self.reads += 1
            return preview_pieces() + ([piece("确认", 950, 500)] if self.reads == 1 else [])

    runtime = TransientModal()
    overview = [{"text": p["text"], "bounds": (p["box"]["left"], p["box"]["top"],
                  p["box"]["left"] + 40, p["box"]["top"] + 20)} for p in preview_pieces()]
    with pytest.raises(opening.TrailError) as error:
        opening._read_boss_detail(runtime, opening.BOSS_DETAIL_POINTS[0], overview, check_cancelled=lambda: None)
    assert error.value.code == "CW_OPENING_PAGE_UNCONFIRMED"
    assert runtime.reads == 1
    assert runtime.actions == [("click", *opening.BOSS_DETAIL_POINTS[0])]


def test_sparse_first_ocr_refreshes_retry_baseline(tmp_path, monkeypatch):
    class SparseRuntime(OpeningRuntime):
        def __init__(self):
            super().__init__()
            self.reads = 0
            self.first_clicks = 0
        def click_point(self, x, y):
            if (x, y) == opening.BOSS_DETAIL_POINTS[0]:
                self.first_clicks += 1
                self.actions.append(("click", x, y))
                if self.first_clicks == 2:
                    self.page = "boss"
            else:
                super().click_point(x, y)
        def ocr(self, **kwargs):
            if self.page == "overview":
                self.reads += 1
                if self.reads == 1:
                    return [piece("本场对局首领"), piece("本局奇遇"), piece("下一步")]
                return preview_pieces()
            return super().ocr(**kwargs)

    runtime = SparseRuntime()
    service, session, command = setup(tmp_path, monkeypatch, runtime)
    result = command.handle(request(tmp_path, session))
    assert result["ok"] is True
    assert runtime.first_clicks == 2


@pytest.mark.parametrize("new_frame", [[], [piece("其他页面", 500, 200)]])
def test_sparse_preview_followed_by_unrecognized_frame_never_clicks(new_frame):
    class ChangedRuntime(OpeningRuntime):
        def __init__(self):
            super().__init__()
            self.reads = 0
        def ocr(self, **kwargs):
            self.reads += 1
            if self.reads == 1:
                return [piece("本场对局首领"), piece("本局奇遇"), piece("下一步")]
            return new_frame

    runtime = ChangedRuntime()
    with pytest.raises(opening.TrailError) as error:
        opening.collect_cw_opening(runtime)
    assert error.value.code == "CW_OPENING_PAGE_UNCONFIRMED"
    assert runtime.reads == 2
    assert runtime.actions == []


def test_boss_detail_crops_use_confirmed_frame_and_do_not_mix_cards(tmp_path, monkeypatch):
    from PIL import Image

    class CroppedRuntime(OpeningRuntime):
        def __init__(self):
            super().__init__()
            self.card = -1
            self.crops = []

        def click_point(self, x, y):
            if (x, y) in opening.BOSS_DETAIL_POINTS:
                self.card = opening.BOSS_DETAIL_POINTS.index((x, y))
            super().click_point(x, y)

        def capture_image(self):
            return Image.new("RGB", (1920, 1080), (self.card + 1, 0, 0))

        def ocr_image(self, image, *, ocr=None):
            card = image.getpixel((0, 0))[0] - 1
            if image.size == (1920, 1080):
                if self.page == "overview":
                    return preview_pieces()
                return [piece("敌方信息"), piece("错误名字 等级 90", 1400, 210)]
            if image.size == (893, 68):
                return []
            self.crops.append((card, image.size, ocr.ocr_mode))
            if image.size == (605, 85):
                return [
                    [piece("第一名 / 等级 90", 60, 20)],
                    [piece("另一首领 等级 80", 60, 20)],
                    [piece("「石心十人」", 60, 20), piece("诡弈砂金/等级90", 200, 20)],
                ][card]
            if image.size == (335, 70):
                return [piece(name, 10, 20) for name in ["甲阵营", "", "金筹博弈资本"][card:card + 1] if name]
            raise AssertionError(image.size)

        def ocr(self, **kwargs):
            if self.page == "overview":
                return preview_pieces()
            return super().ocr(**kwargs)

    runtime = CroppedRuntime()
    _, session, command = setup(tmp_path, monkeypatch, runtime)
    result = command.handle(request(tmp_path, session))
    assert result["ok"] is True
    assert result["data"]["bosses"] == [
        {"plane": "第一位面", "faction": "甲阵营", "name": "第一名"},
        {"plane": "第二位面", "faction": "乙", "name": "另一首领"},
        {"plane": "第三位面", "faction": "金筹博弈资本", "name": "「石心十人」诡弈砂金"},
    ]
    assert runtime.crops == [(card, size, "high") for card in range(3)
                             for size in ((605, 85), (335, 70))]


def test_detail_crops_reject_empty_title_and_ambiguous_faction():
    assert opening._read_boss_name([{"text": "等级90", "bounds": (1300, 200, 1400, 240)}]) is None
    assert opening._read_detail_faction([{"text": "甲"}, {"text": "乙"}]) is None


def test_detail_crop_ocr_failure_falls_back_but_cancellation_propagates():
    from PIL import Image
    class Failed:
        def ocr_image(self, *args, **kwargs):
            raise opening.TrailError("OCR_BACKEND_UNAVAILABLE", "failure")
    image = Image.new("RGB", (1920, 1080))
    assert opening._read_detail_crop(Failed(), image, (15, 108, 350, 178), check_cancelled=lambda: None) == []
    def cancel():
        raise RequestCancelled()
    with pytest.raises(RequestCancelled):
        opening._read_detail_crop(Failed(), image, (15, 108, 350, 178), check_cancelled=cancel)


def test_affix_visual_completeness_requires_all_confident_labels():
    from PIL import Image, ImageDraw
    from trail.scenes.cw.opening import _affix_row_complete
    image = Image.new("RGB", (1920, 1080), (0, 0, 30))
    draw = ImageDraw.Draw(image)
    for left, right in ((300, 493), (507, 664), (679, 849), (862, 1056)):
        draw.rectangle((left, 959, right - 1, 967), fill=(0, 0, 75))
    labels = [
        {"score": 0.99, "text": "第三位面强化", "bounds": (343, 976, 486, 1001)},
        {"score": 0.99, "text": "决战在即", "bounds": (540, 975, 648, 1002)},
        {"score": 0.99, "text": "成长的烦恼", "bounds": (723, 977, 842, 1000)},
        {"score": 0.99, "text": "第一位面强化", "bounds": (906, 975, 1050, 1001)},
    ]
    assert _affix_row_complete(image, labels)
    assert not _affix_row_complete(image, labels[:-1])
    assert not _affix_row_complete(image, [])
    assert not _affix_row_complete(image, [*labels[:2], {**labels[2], "text": "?"}, labels[3]])
    draw.rectangle((1190, 959, 1204, 967), fill=(0, 0, 75))
    assert not _affix_row_complete(image, labels)
