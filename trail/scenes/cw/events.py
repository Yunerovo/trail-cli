from __future__ import annotations

from collections.abc import Callable, Mapping
from time import sleep
from typing import Literal, NoReturn, NotRequired, TypedDict, cast

from trail.core.errors import TrailError
from trail.runtime.ocr_config import OcrRequestConfig
from trail.runtime.resources import resolve_scene_asset
from trail.scenes.cw.models import ensure_cw_state
from trail.scenes.cw.plot_armor import PLOT_ARMOR_CARD_X_TOLERANCE, resolve_cw_plot_armor_card_layout
from trail.scenes.cw.stage import _replace_stage_fields, mark_cw_stage_stale, mark_cw_stage_status_stale, read_cw_stage_text, project_cw_preparation_header
from trail.scenes.cw.variable_cost import (
    VARIABLE_COST_ROLE_NAME,
    VARIABLE_COST_ROLE_COSTS,
    _choice_for_cost,
    _parse_star,
    _parse_variable_cost,
    apply_cw_variable_cost_choice,
    is_cw_variable_cost_role,
    is_variable_cost_roles_stale,
)
from trail.session.models import SessionModel

OptionChooser = Callable[[int], object]
EventHandler = Callable[[], tuple[str, str]]
SceneAction = Callable[[], object]
CwEventType = Literal["lv999_choice", "special_confirm", "replenish", "invest", "encounter", "fortune", "unknown"]


class CwEventOption(TypedDict):
    role_name: str
    choice: str


class CwEventDetection(TypedDict):
    event_type: CwEventType
    text: str


class CwEventResult(TypedDict):
    event_type: CwEventType
    handled: bool
    next_action: NotRequired[str]
    handled_action: NotRequired[str]
    variable_cost_choice: NotRequired[dict[str, object]]


CwEventRouter = Callable[[CwEventOption | None], CwEventResult]

LV999_EVENT_CHOICE_TEXTS: dict[str, set[str]] = {
    "cost_up": {"提升费用", "费用提升", "升费"},
    "equipment": {"获得装备", "选择装备", "装备"},
}

CW_WIDTH = 1920
CW_HEIGHT = 1080
CW_ACTION_OCR_REGION = {
    "from_x": 0.30,
    "from_y": 0.72,
    "to_x": 0.70,
    "to_y": 0.92,
}
CW_BATTLE_TEAM_COUNT_CONFIRM_REGION = {
    "from_x": 0.25,
    "from_y": 0.25,
    "to_x": 0.75,
    "to_y": 0.75,
}
_AURA_RESELECT_ROW = {"from_x": 460, "from_y": 680, "to_x": 1460, "to_y": 756}
_AURA_RESELECT_TEXT_OFFSET_X = 48
CW_EVENT_NEXT_ACTIONS: dict[CwEventType, str] = {
    "replenish": "cw.replenish.choose",
    "invest": "cw.invest.choose",
    "encounter": "cw.encounter.choose",
    "fortune": "cw.fortune.choose",
}


def _point(x_ratio: float, y_ratio: float) -> tuple[int, int]:
    return int(CW_WIDTH * x_ratio), int(CW_HEIGHT * y_ratio)


def _asset(alias: str) -> str:
    return str(resolve_scene_asset("cw", alias))


def _box_center(box: object) -> tuple[int, int]:
    center = getattr(box, "center", None)
    if isinstance(center, tuple) and len(center) == 2:
        return int(center[0]), int(center[1])
    if isinstance(box, Mapping):
        values = _extract_box_values(box)
        if values is None:
            raise ValueError("invalid box")
        left, top, width, height = values
        return (
            int(left + width / 2.0),
            int(top + height / 2.0),
        )
    return int(getattr(box, "left") + getattr(box, "width") / 2.0), int(getattr(box, "top") + getattr(box, "height") / 2.0)


def _normalized_action_text(text: object) -> str:
    return "".join(str(text or "").split())


def _read_ocr_piece_text(piece: object) -> str:
    if isinstance(piece, Mapping):
        for key in ("text", "ocr_text", "value", "name"):
            value = piece.get(key)
            if isinstance(value, str | int | float):
                return str(value)
        return ""

    if isinstance(piece, (list, tuple)):
        if len(piece) >= 2 and isinstance(piece[1], str | int | float):
            return str(piece[1])
        for value in piece:
            if isinstance(value, str | int | float):
                return str(value)
        return ""

    if isinstance(piece, str | int | float):
        return str(piece)
    return ""


def _joined_ocr_text(runtime, *, capture: Mapping[str, float] | None = None) -> str:
    try:
        pieces = runtime.ocr(capture=capture)
    except Exception:
        return ""
    return "".join(_read_ocr_piece_text(piece).strip() for piece in pieces or [])


def _extract_box_values(box: Mapping[object, object]) -> tuple[float, float, float, float] | None:
    try:
        left = float(str(box["left"]))
        top = float(str(box["top"]))
        width = float(str(box["width"]))
        height = float(str(box["height"]))
    except (KeyError, TypeError, ValueError):
        return None
    return left, top, width, height


def _extract_box_from_polygon(polygon: object) -> tuple[float, float, float, float] | None:
    if not isinstance(polygon, (list, tuple)):
        return None
    try:
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
    except (IndexError, TypeError, ValueError):
        return None
    if not xs or not ys:
        return None
    left = min(xs)
    top = min(ys)
    return left, top, max(xs) - left, max(ys) - top


def _extract_box_from_mapping(piece: Mapping[object, object]) -> tuple[float, float, float, float] | None:
    box = piece.get("box")
    if isinstance(box, Mapping):
        return _extract_box_values(box)

    polygon = piece.get("polygon") or piece.get("points")
    if polygon is not None:
        return _extract_box_from_polygon(polygon)

    center = piece.get("center")
    if isinstance(center, Mapping):
        try:
            center_x = float(str(center["x"]))
            center_y = float(str(center["y"]))
        except (KeyError, TypeError, ValueError):
            return None
        return center_x, center_y, 0.0, 0.0
    if isinstance(center, (list, tuple)) and len(center) == 2:
        try:
            center_x = float(center[0])
            center_y = float(center[1])
        except (TypeError, ValueError):
            return None
        return center_x, center_y, 0.0, 0.0

    return _extract_box_values(piece)


def _normalize_ocr_piece(piece: object) -> dict[str, float | str] | None:
    if isinstance(piece, Mapping):
        text = str(piece.get("text") or piece.get("ocr_text") or "").strip()
        box = _extract_box_from_mapping(piece)
    elif isinstance(piece, (list, tuple)) and len(piece) >= 2:
        text = str(piece[1] or "").strip()
        box = _extract_box_from_polygon(piece[0])
    else:
        return None
    if not text or box is None:
        return None
    left, top, width, height = box
    return {
        "text": text,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


def _find_ocr_button_box(
    runtime,
    *,
    allowed_texts: set[str],
    capture: Mapping[str, float] | None = CW_ACTION_OCR_REGION,
) -> dict[str, float | str] | None:
    try:
        pieces = runtime.ocr(capture=capture)
    except Exception:
        return None
    normalized_allowed_texts = {_normalized_action_text(text) for text in allowed_texts}
    for piece in pieces or []:
        normalized_piece = _normalize_ocr_piece(piece)
        if normalized_piece is None:
            continue
        if _normalized_action_text(normalized_piece["text"]) not in normalized_allowed_texts:
            continue
        return normalized_piece
    return None


def _click_cw_action_button(
    runtime,
    *,
    wait_alias: str | None,
    locate_aliases: tuple[str, ...],
    allowed_texts: set[str],
    fallback_point: tuple[int, int],
) -> None:
    if wait_alias is not None:
        match = runtime.wait_img(_asset(wait_alias), timeout=3, interval=0.5)
        if match is not None:
            runtime.click_point(*_box_center(match))
            return
    for alias in locate_aliases:
        match = runtime.locate(_asset(alias))
        if match is not None:
            runtime.click_point(*_box_center(match))
            return
    ocr_match = _find_ocr_button_box(runtime, allowed_texts=allowed_texts)
    if ocr_match is not None:
        runtime.click_point(*_box_center(ocr_match))
        return
    runtime.click_point(*fallback_point)

FORTUNE_OPTION_POINTS = {
    1: _point(0.2, 0.3),
    2: _point(0.8, 0.3),
}
FORTUNE_CONFIRM_POINT = _point(0.77, 0.521)

SPECIAL_EVENT_OPTION_POINT = _point(0.5, 0.25)
SPECIAL_EVENT_CONFIRM_POINT = _point(0.77, 0.521)
BOSS_PREVIEW_CONFIRM_POINT = _point(0.5, 0.7)
SETTLE_NEXT_POINT = _point(0.5, 0.82)
BATTLE_START_POINT = _point(0.5, 0.824)
BATTLE_CONTINUE_POINT = _point(0.5, 0.824)
BATTLE_TEAM_COUNT_CANCEL_POINT = _point(0.43, 0.572)


def _build_option_chooser(runtime, *, option_points: Mapping[int, tuple[int, int]], confirm_point: tuple[int, int]):
    def chooser(option: int) -> None:
        point = option_points.get(option)
        if point is None:
            raise TrailError("CW_OPTION_INVALID", f"invalid currency wars option: {option}")
        runtime.click_point(*point)
        runtime.click_point(*confirm_point)

    return chooser


def _build_season_two_option_chooser(
    runtime, *, stage_name: str, centers: tuple[int, ...], card_y: int,
    confirm: str, confirm_y: tuple[int, int],
) -> OptionChooser:
    def chooser(option: int) -> None:
        if type(option) is not int or not 1 <= option <= len(centers):
            raise TrailError("CW_OPTION_INVALID", f"invalid currency wars option: {option}")
        ocr = OcrRequestConfig(lang="ch", ocr_mode="high")
        if read_cw_stage_text(runtime, ocr=ocr)["value"] != stage_name:
            raise TrailError("CW_EVENT_PAGE_MISMATCH", "当前画面与指定选项的页面不一致")

        runtime.click_point(centers[option - 1], card_y)
        sleep(0.35)
        panel = read_cw_stage_text(runtime, ocr=ocr)
        buttons = []
        for entry in panel["entries"]:
            bounds = entry["bounds"]
            text = entry["text"].strip("()（）✓✔ ")
            if bounds and text == confirm:
                left, top, right, bottom = bounds
                y = round((top + bottom) / 2)
                if confirm_y[0] <= y <= confirm_y[1]:
                    buttons.append((round((left + right) / 2), y))
        selection_read = stage_name != "replenish" or any(
            "已选择1/1" in entry["text"].replace(" ", "") for entry in panel["entries"]
        )
        if panel["value"] != stage_name or len(buttons) != 1 or not selection_read:
            error = TrailError("CW_EVENT_OPTION_UNCONFIRMED", "已尝试选择，请核对当前画面和确认按钮")
            error.known_failure_after_save = True
            raise error
        runtime.click_point(*buttons[0])
        sleep(0.5)
        next_stage = read_cw_stage_text(runtime, ocr=ocr)["value"]
        if next_stage == stage_name or (stage_name == "invest" and next_stage is None):
            error = TrailError("CW_EVENT_CONFIRM_UNCONFIRMED", "已尝试确认，请核对当前画面")
            error.known_failure_after_save = True
            raise error

    return chooser


def _selected_invest_card(image):
    """Identify only a complete bright outline on the known 1080p card layout."""
    if image is None or image.size != (1920, 1080):
        return None
    image = image.convert("RGB")
    pixels = image.load()
    def white(x, y):
        rgb = pixels[x, y]
        return min(rgb) > 192 and max(rgb) - min(rgb) < 55
    def outlined(left, right):
        horizontal = lambda y: sum(white(x, y) for x in range(left + 30, right - 30)) / (right - left - 60)
        vertical = lambda x: sum(white(x, y) for y in range(220, 790)) / 570
        return (max(horizontal(y) for y in range(185, 212)) > .5
                and max(horizontal(y) for y in range(809, 836)) > .5
                and max(vertical(x) for x in range(left - 7, left + 18)) > .5
                and max(vertical(x) for x in range(right - 17, right + 8)) > .5)
    selected = [idx for idx, (left, right) in enumerate(((244, 671), (747, 1174), (1250, 1677)), 1)
                if outlined(left, right)]
    return selected[0] if len(selected) == 1 else None


def _invest_observation(runtime, ocr):
    capture = getattr(runtime, "capture_image", None)
    if not callable(capture) or not callable(getattr(runtime, "ocr_image", None)):
        raise TrailError("CW_EVENT_PAGE_MISMATCH", "无法读取投资页面截图")
    image = capture()
    return read_cw_stage_text(runtime, image=image, ocr=ocr), image


def _invest_title(panel, option):
    left, right = ((244, 671), (747, 1174), (1250, 1677))[option - 1]
    titles = []
    for entry in panel["entries"]:
        bounds = entry["bounds"]
        if bounds:
            x, y = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
            if left < x < right and 470 <= y <= 515 and entry["text"].strip():
                titles.append(entry["text"].strip())
    return titles[0] if len(titles) == 1 else None


def _confirmed_invest_successor(panel):
    if panel["value"] != "preparation":
        return None
    header = project_cw_preparation_header(panel)
    if not header["node"]:
        return None
    def positioned(texts, region):
        for entry in panel["entries"]:
            b = entry["bounds"]
            if b and any(text in entry["text"] for text in texts):
                x, y = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                if region[0] <= x <= region[2] and region[1] <= y <= region[3]:
                    return True
        return False
    if not positioned(("备战阶段",), (390, 15, 540, 70)):
        return None
    if not (positioned(("出战",), (1650, 690, 1920, 830)) or
            positioned(("购买经验",), (180, 810, 370, 970))):
        return None
    return header


def _choose_invest(runtime, option, *, expected_title=None):
    if type(option) is not int or option not in (1, 2, 3):
        raise TrailError("CW_OPTION_INVALID", f"invalid currency wars option: {option}")
    ocr = OcrRequestConfig(lang="ch", ocr_mode="high")
    panel, _ = _invest_observation(runtime, ocr)
    if panel["value"] != "invest":
        raise TrailError("CW_EVENT_PAGE_MISMATCH", "当前画面与投资页面不一致")
    runtime.click_point((460, 960, 1460)[option - 1], 400)
    sleep(0.35)
    try:
        panel, image = _invest_observation(runtime, ocr)
    except TrailError as cause:
        error = TrailError("CW_EVENT_OPTION_UNCONFIRMED", "已尝试选择，无法核对当前画面")
        error.known_failure_after_save = True
        raise error from cause
    buttons = []
    for entry in panel["entries"]:
        bounds = entry["bounds"]
        if bounds and entry["text"].strip() == "确认":
            l, t, r, b = bounds
            x, y = round((l + r) / 2), round((t + b) / 2)
            if 770 <= x <= 1150 and 960 <= y <= 1020:
                buttons.append((x, y))
    title = _invest_title(panel, option)
    if (panel["value"] != "invest" or _selected_invest_card(image) != option or len(buttons) != 1
            or not title or (expected_title is not None and title != expected_title)):
        error = TrailError("CW_EVENT_OPTION_UNCONFIRMED", "已尝试选择，请核对卡片和确认按钮")
        error.known_failure_after_save = True
        raise error
    runtime.click_point(*buttons[0])
    previous = None
    for _ in range(5):
        sleep(0.3)
        try:
            panel, _ = _invest_observation(runtime, ocr)
        except TrailError as cause:
            error = TrailError("CW_EVENT_CONFIRM_UNCONFIRMED", "已尝试确认，无法核对当前画面")
            error.known_failure_after_save = True
            raise error from cause
        current = _confirmed_invest_successor(panel)
        if current is not None and current == previous:
            return {"title": title, "successor": current}
        previous = current
    error = TrailError("CW_EVENT_CONFIRM_UNCONFIRMED", "已尝试确认，请核对当前画面")
    error.known_failure_after_save = True
    raise error


def build_cw_replenish_chooser(runtime) -> OptionChooser:
    return _build_season_two_option_chooser(
        runtime, stage_name="replenish", centers=(252, 606, 960, 1315, 1671), card_y=422,
        confirm="确认", confirm_y=(960, 1020),
    )


def build_cw_invest_chooser(runtime, *, expected_title=None) -> OptionChooser:
    def chooser(option):
        chooser.evidence = _choose_invest(runtime, option, expected_title=expected_title)
        return chooser.evidence
    chooser.evidence = None
    return chooser


def build_cw_encounter_chooser(runtime) -> OptionChooser:
    return _build_season_two_option_chooser(
        runtime, stage_name="encounter", centers=(660, 1254), card_y=483,
        confirm="选择", confirm_y=(880, 930),
    )


def build_cw_fortune_chooser(runtime) -> OptionChooser:
    return _build_option_chooser(runtime, option_points=FORTUNE_OPTION_POINTS, confirm_point=FORTUNE_CONFIRM_POINT)


def build_cw_event_handler(runtime) -> EventHandler:
    def handler() -> tuple[str, str]:
        runtime.click_point(*SPECIAL_EVENT_OPTION_POINT)
        runtime.click_point(*SPECIAL_EVENT_CONFIRM_POINT)
        return "special", "confirm"

    return handler


def _cw_event_type_from_text(text: str) -> CwEventType | None:
    normalized = _normalized_action_text(text)
    normalized_lv = normalized.replace(".", "").lower()
    if "银狼" in normalized and "lv999" in normalized_lv:
        return "lv999_choice"
    if "特殊事件" in normalized or ("特殊" in normalized and "确认" in normalized):
        return "special_confirm"
    if "补给" in normalized:
        return "replenish"
    if "投资" in normalized:
        return "invest"
    if "遭遇" in normalized:
        return "encounter"
    if "命运卜者" in normalized or "命运" in normalized:
        return "fortune"
    return None


def detect_cw_event(runtime) -> CwEventDetection:
    text = _joined_ocr_text(runtime)
    event_type = _cw_event_type_from_text(text)
    if event_type is not None:
        return {"event_type": event_type, "text": text}
    return {"event_type": "unknown", "text": text}


def build_cw_event_router(runtime) -> CwEventRouter:
    legacy_handler = build_cw_event_handler(runtime)

    def router(variable_cost_choice: CwEventOption | None = None) -> CwEventResult:
        event_type = detect_cw_event(runtime)["event_type"]
        if variable_cost_choice is not None:
            if event_type != "lv999_choice":
                raise TrailError("CW_EVENT_CHOICE_MISMATCH", "variable cost choice can only be applied to 银狼LV.999 event")
            choice = variable_cost_choice["choice"]
            target = _find_ocr_button_box(runtime, allowed_texts=LV999_EVENT_CHOICE_TEXTS[choice], capture=None)
            if target is None:
                raise TrailError("CW_EVENT_CHOICE_TARGET_MISSING", "未找到银狼LV.999 事件选项位置")
            runtime.click_point(*_box_center(target))
            return {"event_type": "lv999_choice", "handled": True, "handled_action": "confirm"}
        if event_type == "special_confirm":
            _, handled_action = legacy_handler()
            return {"event_type": "special_confirm", "handled": True, "handled_action": handled_action}
        if event_type in CW_EVENT_NEXT_ACTIONS:
            return {"event_type": event_type, "handled": False, "next_action": CW_EVENT_NEXT_ACTIONS[event_type]}
        return {"event_type": event_type, "handled": False, "next_action": "manual"}

    return router


def open_cw_plot_armor_reselect(runtime, *, option: int | None = None) -> dict:
    if option is not None and (type(option) is not int or not 1 <= option <= 3):
        raise TrailError("CW_AURA_OPTION_INVALID", "光环卡片的位置从左至右为 1 到 3")
    panel = read_cw_stage_text(runtime)
    opened_details = panel["value"] == "preparation"
    if opened_details:
        runtime.click_point(225, 190)
        sleep(0.5)
        panel = read_cw_stage_text(runtime)
    if panel["value"] != "plot_armor_details":
        error = TrailError("CW_AURA_DETAILS_UNCONFIRMED", "未确认进入主角光环与奇遇详情")
        error.known_failure_after_save = opened_details
        raise error
    if option is None:
        return panel

    reading = []
    try:
        image = runtime.capture_image(normalize=True)
        if image is not None and image.size == (1920, 1080):
            region = _AURA_RESELECT_ROW
            crop = image.crop((region["from_x"], region["from_y"], region["to_x"], region["to_y"]))
            reading = runtime.ocr_image(crop, ocr=OcrRequestConfig(lang="ch", ocr_mode="high")) or []
    except (AttributeError, TypeError, ValueError):
        pass
    buttons = []
    for piece in reading:
        box = _normalize_ocr_piece(piece)
        if box is None or _normalized_action_text(box["text"]) != "重选主角":
            continue
        x, y = _box_center(box)
        buttons.append((x + _AURA_RESELECT_ROW["from_x"], y + _AURA_RESELECT_ROW["from_y"]))

    # Positions map to physical cards so a locked slot never shifts later buttons left.
    layout = resolve_cw_plot_armor_card_layout([x - _AURA_RESELECT_TEXT_OFFSET_X for x, _ in buttons])
    targets = []
    if layout is not None and option <= len(layout):
        button_x = layout[option - 1] + _AURA_RESELECT_TEXT_OFFSET_X
        targets = [point for point in buttons if abs(point[0] - button_x) <= PLOT_ARMOR_CARD_X_TOLERANCE]
    if len(targets) != 1:
        error = TrailError("CW_AURA_OPTION_UNAVAILABLE", "该光环卡片未识别到可用的重选按钮")
        error.known_failure_after_save = opened_details
        raise error
    runtime.click_point(*targets[0])
    sleep(0.5)
    confirmation = read_cw_stage_text(runtime)
    if confirmation["value"] != "plot_armor":
        error = TrailError("CW_AURA_RESELECT_UNCONFIRMED", "未确认打开光环重选提示")
        error.known_failure_after_save = True
        raise error
    return confirmation


def build_cw_boss_preview_confirmer(runtime) -> SceneAction:
    return lambda: runtime.click_point(*BOSS_PREVIEW_CONFIRM_POINT)


def build_cw_settle_continuer(runtime) -> SceneAction:
    return lambda: _click_cw_action_button(
        runtime,
        wait_alias=None,
        locate_aliases=("stage.settle", "action.settle_next_page"),
        allowed_texts={"下一步", "下一页"},
        fallback_point=SETTLE_NEXT_POINT,
    )


def build_cw_battle_starter(runtime) -> SceneAction:
    def starter() -> None:
        _click_cw_action_button(
            runtime,
            wait_alias="action.battle_start",
            locate_aliases=(),
            allowed_texts={"开始战斗", "开始挑战", "出战"},
            fallback_point=BATTLE_START_POINT,
        )
        _fail_if_battle_team_count_confirm_dialog(runtime)

    return starter


def _fail_if_battle_team_count_confirm_dialog(runtime) -> None:
    dialog_text = _normalized_action_text(_joined_ocr_text(runtime, capture=CW_BATTLE_TEAM_COUNT_CONFIRM_REGION))
    if "人数未达上限" not in dialog_text or "是否确认出战" not in dialog_text:
        return

    cancel_box = _find_ocr_button_box(
        runtime,
        allowed_texts={"取消"},
        capture=CW_BATTLE_TEAM_COUNT_CONFIRM_REGION,
    )
    if cancel_box is not None:
        runtime.click_point(*_box_center(cancel_box))
    else:
        runtime.click_point(*BATTLE_TEAM_COUNT_CANCEL_POINT)
    error = TrailError("CW_BATTLE_TEAM_COUNT_INSUFFICIENT", "可出战角色人数未达上限，请先检查场上人数")
    setattr(error, "known_failure_after_save", True)
    setattr(error, "data", {"reason": "team_count_insufficient"})
    raise error


def build_cw_battle_continuer(runtime) -> SceneAction:
    return lambda: _click_cw_action_button(
        runtime,
        wait_alias=None,
        locate_aliases=("action.battle_continue",),
        allowed_texts={"继续挑战", "继续"},
        fallback_point=BATTLE_CONTINUE_POINT,
    )


def read_cw_replenish(session: SessionModel) -> dict[str, list[int]]:
    del session
    return {"options": [1, 2, 3, 4, 5]}


def choose_cw_replenish(session: SessionModel, *, option: int, chooser: OptionChooser) -> SessionModel:
    chooser(option)
    mark_cw_stage_stale(session)
    return session


def read_cw_invest(session: SessionModel) -> dict[str, list[int]]:
    del session
    return {"options": [1, 2, 3]}


def choose_cw_invest(session: SessionModel, *, option: int, chooser: OptionChooser) -> SessionModel:
    try:
        evidence = chooser(option)
    except TrailError as error:
        if error.code in {"CW_EVENT_OPTION_UNCONFIRMED", "CW_EVENT_CONFIRM_UNCONFIRMED"}:
            strategy = ensure_cw_state(session).get("strategy")
            if isinstance(strategy, dict):
                strategy["stale"] = True
            mark_cw_stage_stale(session)
        raise
    strategy = ensure_cw_state(session).get("strategy")
    if isinstance(strategy, dict):
        strategy["stale"] = True
    mark_cw_stage_stale(session)
    if isinstance(evidence, dict) and isinstance(evidence.get("successor"), dict):
        _replace_stage_fields(session, value=evidence["successor"]["stage"], stale=False)
    return session


def read_cw_encounter(session: SessionModel) -> dict[str, list[int]]:
    del session
    return {"options": [1, 2]}


def choose_cw_encounter(session: SessionModel, *, option: int, chooser: OptionChooser) -> SessionModel:
    chooser(option)
    mark_cw_stage_stale(session)
    return session


def read_cw_fortune(session: SessionModel) -> dict[str, list[int]]:
    del session
    return {"options": [1, 2]}


def choose_cw_fortune(session: SessionModel, *, option: int, chooser: OptionChooser) -> SessionModel:
    chooser(option)
    mark_cw_stage_stale(session)
    return session


def confirm_cw_boss_preview(session: SessionModel, *, confirmer: SceneAction) -> SessionModel:
    confirmer()
    mark_cw_stage_stale(session)
    return session


def settle_cw_next(session: SessionModel, *, continuer: SceneAction) -> SessionModel:
    continuer()
    mark_cw_stage_stale(session)
    return session


def start_cw_battle(session: SessionModel, *, starter: SceneAction) -> SessionModel:
    starter()
    mark_cw_stage_stale(session)
    return session


def continue_cw_battle(session: SessionModel, *, continuer: SceneAction) -> SessionModel:
    continuer()
    mark_cw_stage_stale(session)
    return session


def _coerce_cw_event_option(variable_cost_choice: Mapping[str, object] | None) -> CwEventOption | None:
    if variable_cost_choice is None:
        return None
    role_name = variable_cost_choice.get("role_name")
    choice = variable_cost_choice.get("choice")
    if not isinstance(role_name, str) or not isinstance(choice, str):
        raise TrailError("CW_EVENT_CHOICE_MISMATCH", "invalid variable cost choice")
    if not is_cw_variable_cost_role(role_name) or choice not in LV999_EVENT_CHOICE_TEXTS:
        raise TrailError("CW_EVENT_CHOICE_MISMATCH", "invalid variable cost choice")
    return {"role_name": role_name, "choice": choice}


def mark_cw_event_unknown_stale(session: SessionModel) -> None:
    cw_state = ensure_cw_state(session)
    mark_cw_stage_stale(session)
    mark_cw_stage_status_stale(session)
    for key in ("slots", "shop", "equipment", "sell_plan"):
        value = cw_state.get(key)
        if isinstance(value, dict):
            value["stale"] = True
        else:
            cw_state[key] = {"stale": True}
    strategy = cw_state.get("strategy")
    if isinstance(strategy, dict):
        strategy["stale"] = True
    else:
        cw_state["strategy"] = {"cards": [], "stale": True}
    cw_state["variable_cost_roles_stale"] = True


def _raise_lv999_state_stale() -> NoReturn:
    raise TrailError("CW_EVENT_LV999_STATE_STALE", "银狼LV.999 状态已过期，请先运行 cw.event.reconcile")


def _raise_lv999_choice_mismatch(message: str = "invalid variable cost choice") -> NoReturn:
    raise TrailError("CW_EVENT_CHOICE_MISMATCH", message)


def _validate_cw_event_option_state(session: SessionModel, event_option: CwEventOption) -> None:
    cw_state = ensure_cw_state(session)
    if is_variable_cost_roles_stale(cw_state):
        _raise_lv999_state_stale()
    roles_value = cw_state.get("variable_cost_roles")
    roles = roles_value if isinstance(roles_value, dict) else {}
    state = roles.get(VARIABLE_COST_ROLE_NAME)
    if not isinstance(state, dict):
        _raise_lv999_choice_mismatch("银狼LV.999 current state is unknown")
    state = cast(dict[str, object], state)
    current_cost = _parse_variable_cost(state.get("cost"))
    if current_cost is None:
        _raise_lv999_choice_mismatch("银狼LV.999 current cost is unknown")
    if _parse_star(state.get("star")) != 2 or state.get("choice_available") is not True:
        _raise_lv999_choice_mismatch("银狼LV.999 choice is not available")
    confirmed_value = state.get("confirmed_choices_by_cost")
    confirmed_by_cost = confirmed_value if isinstance(confirmed_value, dict) else {}
    if _choice_for_cost(confirmed_by_cost, current_cost) is not None:
        _raise_lv999_choice_mismatch("银狼LV.999 choice already confirmed for current cost")
    if event_option["choice"] == "cost_up" and current_cost == max(VARIABLE_COST_ROLE_COSTS):
        _raise_lv999_choice_mismatch("银狼LV.999 already at max cost")


def handle_cw_event(
    session: SessionModel,
    *,
    router: CwEventRouter | None = None,
    handler: EventHandler | None = None,
    variable_cost_choice: Mapping[str, object] | None = None,
) -> dict[str, object]:
    event_option = _coerce_cw_event_option(variable_cost_choice)
    used_router = router is not None
    if event_option is not None and used_router:
        _validate_cw_event_option_state(session, event_option)
    if router is not None:
        result: dict[str, object] = dict(router(event_option))
    else:
        if handler is None:
            raise ValueError("cw event router is required")
        event_type, handled_action = handler()
        result = {"event_type": event_type, "handled": True, "handled_action": handled_action}

    if event_option is not None:
        if used_router and result.get("handled") is not True:
            raise ValueError("unhandled cw event cannot apply variable cost choice")
        cw_state = session.scene_state.get("cw")
        if not isinstance(cw_state, dict):
            raise ValueError("银狼LV.999 current cost is unknown")
        apply_cw_variable_cost_choice(cw_state, role_name=event_option["role_name"], choice=event_option["choice"])
        result["variable_cost_choice"] = dict(event_option)
    if result.get("event_type") == "unknown" and result.get("handled") is not True:
        mark_cw_event_unknown_stale(session)
    if result.get("handled") is True:
        mark_cw_stage_stale(session)
    return result
