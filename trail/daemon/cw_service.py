from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from inspect import Parameter, signature
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from PIL import Image

from trail.artifacts.store import ArtifactStore
from trail.core.errors import TrailError
from trail.core.jsonable import format_exception_detail, format_exception_message, to_jsonable
from trail.daemon.command_timeouts import DEFAULT_CW_BATTLE_RUN_TIMEOUT_SECONDS, resolve_command_execution_timeout
from trail.daemon.command_service import PersistedButResponseUnknown, SideEffectAppliedButStateNotPersisted, _normalize_workspace_path
from trail.daemon.models import RequestCancelled
from trail.output.capture import with_auto_capture, with_selective_capture
from trail.output.envelope import build_image_guidance
from trail.runtime.ocr_config import OcrRequestConfig
from trail.scenes.cw.aura_drag import drag_cw_aura_role
from trail.scenes.cw.battle import classify_cw_battle_page, has_cw_preparation_board, observe_cw_battle_page, run_cw_battle
from trail.scenes.cw.battle_report import battle_hud_from_page
from trail.scenes.cw.entry import enter_cw, is_cw_exact_difficulty_token, start_cw
from trail.scenes.cw.equipment import (
    apply_cw_equipment_read,
    compose_and_equip_cw_equipment,
    open_cw_chimera_projector,
    prepare_cw_equipment,
)
from trail.scenes.cw.equipment_recognition import VectorEquipmentIconRecognizer
from trail.scenes.cw.equipment_resources import build_cw_equipment_catalog, load_cached_equipment_icons
from trail.scenes.cw.events import (
    build_cw_battle_continuer,
    build_cw_battle_starter,
    build_cw_boss_preview_confirmer,
    build_cw_encounter_chooser,
    build_cw_event_router,
    build_cw_fortune_chooser,
    build_cw_invest_chooser,
    build_cw_replenish_chooser,
    build_cw_settle_continuer,
    choose_cw_encounter,
    choose_cw_fortune,
    choose_cw_invest,
    choose_cw_replenish,
    confirm_cw_boss_preview,
    continue_cw_battle,
    handle_cw_event,
    mark_cw_event_unknown_stale,
    open_cw_plot_armor_reselect,
    read_cw_encounter,
    read_cw_fortune,
    read_cw_invest,
    read_cw_replenish,
    settle_cw_next,
    start_cw_battle,
)
from trail.scenes.cw.experience import buy_cw_experience_to_level, quote_cw_experience
from trail.scenes.cw.guide import (
    apply_cw_guide_via_ui,
    complete_cw_guide_or_none,
    fetch_cw_guide_list,
    invalidate_cw_guide_runtime_state,
    require_complete_cw_guide,
)
from trail.scenes.cw.guide import fetch_cw_guide_config, fetch_cw_raw_guide_config
from trail.scenes.cw.models import ensure_cw_state
from trail.scenes.cw.portal import (
    detect_cw_portal,
    detect_portal_collection_matches,
    refresh_cw_portal,
    restart_cw_portal_to_settlement_entry,
    select_cw_portal,
    summarize_portal_cards,
    wait_cw_portal_preparation,
    wait_cw_portal_in_game,
)
from trail.scenes.cw.shop import (
    SHOP_SCAN_OPEN_SETTLE_SECONDS,
    build_cw_shop_buyer,
    build_cw_shop_closer,
    build_cw_shop_exp_buyer,
    build_cw_shop_opener,
    build_cw_shop_page_snapshot_reader,
    build_cw_shop_refresher,
    build_cw_shop_scan_snapshot_reader,
    build_cw_shop_scanner,
    buy_cw_shop_exp,
    buy_cw_shop_slot,
    close_cw_shop,
    open_cw_shop,
    project_cw_shop_snapshot,
    refresh_cw_shop,
    scan_cw_shop,
    shop_cw_status,
)
from trail.scenes.cw.slots import (
    build_cw_crystal_collector,
    build_cw_hand_seller,
    build_cw_slot_icon_reader,
    build_cw_slot_swapper,
    build_cw_slots_reader,
    collect_cw_crystals,
    dismiss_cw_slots_overlay,
    open_cw_weapon_box,
    place_cw_slots,
    plan_cw_hand_sell,
    read_cw_slots,
    sell_cw_hand_slots,
    SlotsSnapshotReader,
    swap_cw_slots,
)
from trail.scenes.cw.stage import (
    build_cw_stage_detector, detect_cw_stage, mark_cw_stage_stale, mark_cw_stage_status_stale,
    project_cw_preparation_header, read_cw_preparation_header, read_cw_stage_text, wait_cw_stage,
)
from trail.scenes.cw.opening import collect_cw_opening
from trail.scenes.cw.plot_armor import read_cw_plot_armor
from trail.scenes.cw.replenish_refresh import refresh_cw_replenish
from trail.scenes.cw.role_recognition import VectorRoleIconRecognizer
from trail.scenes.cw.run_facts import (
    cw_run_facts, mark_cw_plot_armor_unverified, record_cw_investment, record_cw_opening,
    record_cw_plot_armor, summarize_cw_run_facts,
)
from trail.scenes.cw.static_resources import load_default_cw_resource_bundle
from trail.scenes.cw.strategy import detect_cw_strategy, refresh_cw_strategy, select_cw_strategy, summarize_strategy_cards


stage_detector_factory = build_cw_stage_detector
slots_reader_factory = build_cw_slot_icon_reader
slot_swapper_factory = build_cw_slot_swapper
slot_placer_factory = build_cw_slot_swapper
hand_seller_factory = build_cw_hand_seller
crystal_collector_factory = build_cw_crystal_collector
shop_scanner_factory = build_cw_shop_scanner
shop_scan_snapshot_reader_factory = build_cw_shop_scan_snapshot_reader
shop_page_snapshot_reader_factory = build_cw_shop_page_snapshot_reader
shop_buyer_factory = build_cw_shop_buyer
shop_exp_buyer_factory = build_cw_shop_exp_buyer
shop_opener_factory = build_cw_shop_opener
shop_refresher_factory = build_cw_shop_refresher
shop_closer_factory = build_cw_shop_closer
replenish_chooser_factory = build_cw_replenish_chooser
invest_chooser_factory = build_cw_invest_chooser
encounter_chooser_factory = build_cw_encounter_chooser
fortune_chooser_factory = build_cw_fortune_chooser
event_router_factory = build_cw_event_router
boss_preview_confirmer_factory = build_cw_boss_preview_confirmer
battle_starter_factory = build_cw_battle_starter
battle_continuer_factory = build_cw_battle_continuer
settle_continuer_factory = build_cw_settle_continuer
DEFAULT_CW_STAGE_WAIT_TIMEOUT = 120
SIDE_EFFECT_RUNTIME_METHODS = {"click_point", "drag_to", "press_key", "type_text"}
PORTAL_SELECT_EXTRA_CAPTURE_DELAY_SECONDS = 2.0
CW_BATTLE_START_EXTRA_CAPTURE_DELAY_SECONDS = 3.0
DEFAULT_CW_BATTLE_RUN_TIMEOUT = DEFAULT_CW_BATTLE_RUN_TIMEOUT_SECONDS
PORTAL_SELECT_PREP_COLLECT_ORDER: tuple[str, ...] = ("crystals", "slots", "equipment", "shop")
CW_BATTLE_RUN_PREP_COLLECT_ORDER: tuple[str, ...] = ("crystals", "slots", "shop", "equipment")


def _build_shop_snapshot_reader(runtime, *, read_stage_status: bool = False):
    try:
        return shop_scan_snapshot_reader_factory(runtime, read_stage_status=read_stage_status)
    except TypeError as exc:
        if "read_stage_status" not in str(exc):
            raise
        return shop_scan_snapshot_reader_factory(runtime)


def _is_valid_cw_start_difficulty(value: str) -> bool:
    return value in {"lowest", "current", "highest"} or is_cw_exact_difficulty_token(value)


def _validated_start_payload(payload: dict) -> tuple[str, str, str]:
    mode = payload.get("mode")
    difficulty = payload.get("difficulty")
    battle_mode = payload.get("battle_mode")
    if not isinstance(mode, str) or not isinstance(difficulty, str) or not isinstance(battle_mode, str):
        raise TrailError("CW_START_ARGS_REQUIRED", "cw start requires mode/difficulty/battle_mode")
    if mode not in {"new", "continue"}:
        raise TrailError("CW_START_MODE_INVALID", f"unsupported cw start mode: {mode}")
    if not _is_valid_cw_start_difficulty(difficulty):
        raise TrailError("CW_START_DIFFICULTY_INVALID", f"unsupported cw start difficulty: {difficulty}")
    if battle_mode not in {"standard", "overclock"}:
        raise TrailError("CW_START_BATTLE_MODE_INVALID", f"unsupported cw start battle_mode: {battle_mode}")
    return mode, difficulty, battle_mode


def _capture_delay_seconds_for_method(method: str) -> float:
    if method == "cw.portal.select":
        return PORTAL_SELECT_EXTRA_CAPTURE_DELAY_SECONDS
    if method == "cw.battle.start":
        return CW_BATTLE_START_EXTRA_CAPTURE_DELAY_SECONDS
    return 0.0


def _call_with_supported_keywords(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        parameters = signature(fn).parameters.values()
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    accepted: set[str] = set()
    accepts_any = False
    for parameter in parameters:
        if parameter.kind is Parameter.VAR_KEYWORD:
            accepts_any = True
            break
        if parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}:
            accepted.add(parameter.name)
    if accepts_any:
        return fn(*args, **kwargs)
    return fn(*args, **{key: value for key, value in kwargs.items() if key in accepted})


def _callable_accepts_keyword(fn: Callable[..., Any], keyword: str) -> bool:
    try:
        parameters = signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    for parameter in parameters:
        if parameter.kind is Parameter.VAR_KEYWORD:
            return True
        if parameter.name == keyword and parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}:
            return True
    return False


def _runtime_supports_slot_snapshot_read(runtime: object) -> bool:
    return callable(getattr(runtime, "locate", None)) and callable(getattr(runtime, "capture_image", None))


def _apply_cw_equipment_read_with_resources(
    session,
    runtime,
    *,
    workspace_root: str | None,
    cw_resource_service=None,
    request_id: str | None = None,
    read_consumables: bool = False,
) -> dict[str, Any]:
    options: dict[str, Any] = {"workspace_root": workspace_root}
    if request_id is not None:
        options["request_id"] = request_id
    if read_consumables:
        options["read_consumables"] = True
    if cw_resource_service is not None:
        raw_config, recognizer = cw_resource_service.equipment_read_resources(workspace_root=workspace_root)
        options.update(raw_config=raw_config, recognizer=recognizer)
    return apply_cw_equipment_read(session, runtime, **options)


def _equipment_prepare_summary_from_bundle(*, workspace_root: str) -> dict:
    bundle = load_default_cw_resource_bundle(workspace_root=workspace_root)
    items = bundle.equipment_manifest.get("items") if isinstance(bundle.equipment_manifest, dict) else []
    count = len(items) if isinstance(items, list) else 0
    return {"big_version": bundle.big_version, "count": count, "cached": count, "downloaded": 0, "refreshed": False}


def _cached_cw_guide_config(
    *, workspace_root: str | None, cw_resource_service=None, enrich_traits: bool = False
) -> dict[str, Any]:
    if cw_resource_service is None or not hasattr(cw_resource_service, "bundle"):
        return _call_with_supported_keywords(
            fetch_cw_guide_config,
            workspace_root=workspace_root,
            enrich_traits=enrich_traits,
        )
    bundle = cw_resource_service.bundle(workspace_root=workspace_root)
    config = bundle.guide_config_enriched if enrich_traits else bundle.guide_config
    return deepcopy(config) if isinstance(config, dict) else {}


def _cached_cw_raw_config(*, workspace_root: str | None, cw_resource_service=None) -> dict[str, Any] | None:
    if cw_resource_service is None or not hasattr(cw_resource_service, "bundle"):
        return None
    bundle = cw_resource_service.bundle(workspace_root=workspace_root)
    return bundle.raw_config if isinstance(bundle.raw_config, dict) else None


def _role_recognizer_from_bundle(bundle):
    empty_manifest = bundle.role_manifest["empty_templates"]
    templates = {}
    for template_key in ("field", "hand"):
        local_path = empty_manifest[template_key]["local_path"]
        with Image.open(Path(bundle.root) / local_path) as image:
            templates[template_key] = image.convert("RGBA")
    return VectorRoleIconRecognizer.from_precomputed_features(bundle.role_features, empty_templates=templates)


def _default_slots_read_resources(*, workspace_root: str | None) -> tuple[dict[str, Any], dict[str, Any], object]:
    bundle = load_default_cw_resource_bundle(workspace_root=workspace_root)
    raw_config = bundle.raw_config if isinstance(bundle.raw_config, dict) else {}
    guide_config = bundle.guide_config_enriched if isinstance(bundle.guide_config_enriched, dict) else {}
    return raw_config, guide_config, _role_recognizer_from_bundle(bundle)


def _slots_reader_factory_accepts_recognizer(factory) -> bool:
    try:
        parameters = signature(factory).parameters
    except (TypeError, ValueError):
        return True
    return "recognizer" in parameters


def _slots_read_resources(*, workspace_root: str | None, cw_resource_service=None) -> tuple[dict[str, Any], dict[str, Any], object]:
    if cw_resource_service is not None and hasattr(cw_resource_service, "slots_read_resources"):
        return cw_resource_service.slots_read_resources(workspace_root=workspace_root)
    return _default_slots_read_resources(workspace_root=workspace_root)


def _build_slots_reader_for_service(
    runtime,
    *,
    targets: list[str] | None,
    workspace_root: str | None,
    cw_resource_service=None,
    request_id: str | None = None,
    dismiss_initial_overlay: bool = True,
    collapse_expanded_hand: bool = True,
) -> tuple[SlotsSnapshotReader, dict[str, Any]]:
    if _slots_reader_factory_accepts_recognizer(slots_reader_factory):
        _raw_config, guide_config, role_recognizer = _slots_read_resources(
            workspace_root=workspace_root,
            cw_resource_service=cw_resource_service,
        )
        reader = _call_with_supported_keywords(
            slots_reader_factory,
            runtime,
            recognizer=role_recognizer,
            targets=targets,
            request_id=request_id,
            dismiss_initial_overlay=dismiss_initial_overlay,
            **({"collapse_expanded_hand": False} if not collapse_expanded_hand else {}),
        )
        return reader, deepcopy(guide_config) if isinstance(guide_config, dict) else {}

    reader = _call_with_supported_keywords(
        slots_reader_factory,
        runtime,
        targets=targets,
        request_id=request_id,
        dismiss_initial_overlay=dismiss_initial_overlay,
        **({"collapse_expanded_hand": False} if not collapse_expanded_hand else {}),
    )
    guide_config = _cached_cw_guide_config(
        workspace_root=workspace_root,
        cw_resource_service=cw_resource_service,
        enrich_traits=True,
    )
    return reader, guide_config


def _begin_runtime_scope(runtime) -> None:
    begin_capture_scope = getattr(runtime, "begin_capture_scope", None)
    if callable(begin_capture_scope):
        begin_capture_scope()


def _end_runtime_scope(runtime) -> None:
    end_capture_scope = getattr(runtime, "end_capture_scope", None)
    if callable(end_capture_scope):
        end_capture_scope()


class _RuntimeSideEffectTracker:
    def __init__(self):
        self.side_effect_applied = False

    def mark_applied(self) -> None:
        self.side_effect_applied = True


class _SideEffectTrackingRuntime:
    def __init__(self, runtime, tracker: _RuntimeSideEffectTracker):
        self._runtime = runtime
        self._tracker = tracker
        self._suppress_side_effects = False

    @contextmanager
    def suppress_side_effect_tracking(self):
        previous = self._suppress_side_effects
        self._suppress_side_effects = True
        try:
            yield
        finally:
            self._suppress_side_effects = previous

    def _mark_applied(self) -> None:
        if not self._suppress_side_effects:
            self._tracker.mark_applied()

    def __getattr__(self, name: str):
        attribute = getattr(self._runtime, name)
        if not callable(attribute) or name not in SIDE_EFFECT_RUNTIME_METHODS:
            return attribute

        def wrapped(*args, **kwargs):
            try:
                result = attribute(*args, **kwargs)
            except TrailError as error:
                if _safe_error_attr(error, "completed_after_side_effect"):
                    self._mark_applied()
                raise
            except Exception:
                # Input backends can raise after the UI has already reacted.
                self._mark_applied()
                raise
            self._mark_applied()
            return result

        return wrapped


class _RequestScopedCaptureRuntime:
    def __init__(self, runtime, request_id: str, *, extra_delay_seconds: float = 0.0,
                 verified_frame: Image.Image | None = None):
        self._runtime = runtime
        self._request_id = request_id
        self._extra_delay_seconds = extra_delay_seconds
        self._verified_frame = verified_frame

    def capture_after_action(self, optional: bool = False):
        if self._verified_frame is not None:
            return self._runtime.save_capture_image_to_workspace(self._verified_frame, request_id=self._request_id)
        if self._extra_delay_seconds > 0:
            sleep(self._extra_delay_seconds)
        return self._runtime.capture_after_action(optional=optional, request_id=self._request_id)

    def __getattr__(self, name: str):
        return getattr(self._runtime, name)


class CwService:
    def __init__(self, *, runtime_service, cw_resource_service=None):
        self.runtime_service = runtime_service
        self.cw_resource_service = cw_resource_service

    def guide_config(self, *, workspace_root: str | None, enrich_traits: bool = False) -> dict:
        return _cached_cw_guide_config(
            workspace_root=workspace_root,
            cw_resource_service=self.cw_resource_service,
            enrich_traits=enrich_traits,
        )

    def handle(self, *, method: str, payload: dict, workspace_root: str, session_service, cancellation_token=None) -> dict | None:
        session, _, _, handlers, _, _, _ = self._context(
            method=method,
            payload=payload,
            workspace_root=workspace_root,
            session_service=session_service,
            cancellation_token=cancellation_token,
        )

        result = handlers[method]()
        session_service.save_session(session)
        return result

    def handle_with_capture(
        self,
        *,
        method: str,
        payload: dict,
        workspace_root: str,
        session_service,
        request_id: str,
        verbose: bool = False,
        cancellation_token=None,
    ) -> dict | None:
        session, _, runtime, handlers, _, _, _ = self._context(
            method=method,
            payload=payload,
            workspace_root=workspace_root,
            session_service=session_service,
            request_id=request_id,
            cancellation_token=cancellation_token,
        )

        capture_runtime = _RequestScopedCaptureRuntime(runtime(), request_id)
        scene_warnings: list[dict] = []
        handler = handlers[method]
        if method != "cw.shop.plan_exp":
            handler = lambda: _handle_and_save_session(handlers[method], session_service, session, scene_warnings)
        response = with_selective_capture(
            capture_runtime,
            handler,
            verbose=verbose,
        )
        return _merge_envelope_warnings(response, scene_warnings)

    def handle_mutation(
        self,
        *,
        method: str,
        payload: dict,
        workspace_root: str,
        session_service,
        request_id: str,
        verbose: bool = False,
        cancellation_token=None,
    ) -> dict | None:
        session, _, runtime, handlers, tracker, end_runtime_scope_if_started, runtime_if_started = self._context(
            method=method,
            payload=payload,
            workspace_root=workspace_root,
            session_service=session_service,
            request_id=request_id,
            track_side_effects=True,
            shared_capture_scope=True,
            cancellation_token=cancellation_token,
        )

        def collect_runtime_debug(debug_runtime=None) -> dict | None:
            if not verbose:
                return None
            if debug_runtime is None:
                debug_runtime = runtime_if_started()
                if debug_runtime is None:
                    return None
            return _safe_collect_debug(debug_runtime, verbose=verbose)

        def unknown_result_envelope(
            error: Exception,
            *,
            last_known_stage: str | None = None,
            screenshot: str | None = None,
            warnings: list[dict] | None = None,
            references: list[dict] | None = None,
            debug_runtime=None,
        ) -> dict:
            return _unknown_result_envelope(
                error,
                last_known_stage=last_known_stage,
                screenshot=screenshot,
                warnings=warnings,
                references=references,
                debug=collect_runtime_debug(debug_runtime),
            )

        def attach_runtime_debug(error: Exception) -> None:
            debug = collect_runtime_debug()
            if not debug:
                return
            raw_debug = _safe_error_attr(error, "debug")
            if isinstance(raw_debug, dict):
                debug = to_jsonable({**debug, **raw_debug})
            try:
                setattr(error, "debug", debug)
            except Exception:
                return

        try:
            if method == "cw.start":
                _validated_start_payload(payload)
            try:
                result = handlers[method]()
            except RequestCancelled:
                if cancellation_token is not None and tracker.side_effect_applied:
                    setattr(cancellation_token, "side_effect_stage", "side_effect_applied")
                raise
            except CwSideEffectAppliedError as error:
                raise SideEffectAppliedButStateNotPersisted(
                    unknown_result_envelope(error, last_known_stage="side_effect_applied")
                ) from error
            except TrailError as error:
                if _safe_error_attr(error, "known_failure_after_save"):
                    try:
                        session_service.save_session(session)
                    except Exception as save_error:
                        raise SideEffectAppliedButStateNotPersisted(
                            unknown_result_envelope(save_error, last_known_stage="side_effect_applied")
                        ) from save_error

                    extra_delay_seconds = PORTAL_SELECT_EXTRA_CAPTURE_DELAY_SECONDS if method == "cw.portal.select" else 0.0
                    capture_runtime = None
                    try:
                        capture_runtime = _RequestScopedCaptureRuntime(
                            runtime(),
                            request_id,
                            extra_delay_seconds=extra_delay_seconds,
                        )
                        response = with_auto_capture(capture_runtime, lambda: (_ for _ in ()).throw(error), verbose=verbose)
                        if isinstance(response, dict):
                            response_data = _error_data(error)
                            if response_data:
                                response["data"] = response_data
                        return response
                    except Exception as capture_error:
                        screenshot = _safe_capture_after_action(capture_runtime)
                        raise PersistedButResponseUnknown(
                            unknown_result_envelope(
                                capture_error,
                                last_known_stage="state_persisted",
                                screenshot=screenshot,
                                warnings=_safe_collect_warnings(capture_runtime),
                                references=_safe_match_references(capture_runtime, screenshot=screenshot),
                                debug_runtime=capture_runtime,
                            )
                        ) from capture_error
                if tracker.side_effect_applied or _safe_error_attr(error, "completed_after_side_effect"):
                    raise SideEffectAppliedButStateNotPersisted(
                        unknown_result_envelope(error, last_known_stage="side_effect_applied")
                    ) from error
                attach_runtime_debug(error)
                raise
            except Exception as error:
                if isinstance(error, RequestCancelled):
                    raise
                if tracker.side_effect_applied or _safe_error_attr(error, "completed_after_side_effect"):
                    raise SideEffectAppliedButStateNotPersisted(
                        unknown_result_envelope(error, last_known_stage="side_effect_applied")
                    ) from error
                attach_runtime_debug(error)
                raise

            verified_frame = result.pop("_reconcile_frame", None) if method in {
                "cw.event.reconcile", "cw.battle.run", "cw.event.drag_aura_role", "cw.replenish.refresh",
                "cw.shop.buy_exp_to_level",
            } else None
            result, scene_warnings = _pop_scene_warnings(result)
            no_purchase_input = method == "cw.shop.buy_exp_to_level" and not tracker.side_effect_applied
            if (not no_purchase_input
                    and method not in {"cw.stage.opening", "cw.event.drag_aura_role", "cw.replenish.refresh"}
                    and not _has_fresh_auto_collect_equipment(method, result)):
                _mark_cw_equipment_stale(session)

            try:
                session_service.save_session(session)
            except Exception as error:
                raise SideEffectAppliedButStateNotPersisted(
                    unknown_result_envelope(error, last_known_stage="side_effect_applied")
                ) from error

            extra_delay_seconds = _capture_delay_seconds_for_method(method)
            capture_runtime = None
            try:
                capture_runtime = _RequestScopedCaptureRuntime(
                    runtime(),
                    request_id,
                    extra_delay_seconds=extra_delay_seconds,
                    verified_frame=verified_frame,
                )
                response = with_auto_capture(capture_runtime, lambda: result, verbose=verbose)
                if (method in {"cw.equipment.compose", "cw.event.drag_aura_role", "cw.replenish.refresh"}
                        and isinstance(response, dict) and response.get("ok") is True
                        and response.get("screenshot") is None):
                    code = ("CW_EQUIPMENT_COMPOSE_SCREENSHOT_REQUIRED" if method == "cw.equipment.compose"
                            else "CW_PAGE_ACTION_SCREENSHOT_REQUIRED")
                    error = TrailError(
                        code,
                        f"{method} success requires screenshot",
                    )
                    response = _merge_envelope_warnings(response, scene_warnings)
                    raise PersistedButResponseUnknown(
                        _unknown_result_envelope_from_response(
                            error,
                            response=response,
                            last_known_stage="state_persisted",
                        )
                    )
                return _merge_envelope_warnings(response, scene_warnings)
            except Exception as error:
                if isinstance(error, PersistedButResponseUnknown):
                    raise
                screenshot = _safe_capture_after_action(capture_runtime)
                raise PersistedButResponseUnknown(
                    unknown_result_envelope(
                        error,
                        last_known_stage="state_persisted",
                        screenshot=screenshot,
                        warnings=_safe_collect_warnings(capture_runtime),
                        references=_safe_match_references(capture_runtime, screenshot=screenshot),
                        debug_runtime=capture_runtime,
                    )
                ) from error
        finally:
            end_runtime_scope_if_started()

    def _context(
        self,
        *,
        method: str,
        payload: dict,
        workspace_root: str,
        session_service,
        request_id: str | None = None,
        track_side_effects: bool = False,
        shared_capture_scope: bool = False,
        cancellation_token=None,
    ):
        if cancellation_token is not None:
            cancellation_token.throw_if_cancelled()
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise TrailError("SESSION_REQUIRED", f"cw method requires session: {method}")

        session = session_service.load_session(session_id)
        artifact_store = ArtifactStore(Path(workspace_root) / ".trail" / "artifacts")
        runtime_holder: dict[str, object] = {}
        scope_state = {"started": False}
        tracker = _RuntimeSideEffectTracker()

        def runtime():
            if "runtime" not in runtime_holder:
                resolved_runtime = self.runtime_service.get_runtime(
                    workspace_root=workspace_root,
                    window_binding=session.window_binding,
                )
                if track_side_effects:
                    setattr(resolved_runtime, "raise_post_input_foreground_error", True)
                    resolved_runtime = _SideEffectTrackingRuntime(resolved_runtime, tracker)
                runtime_holder["runtime"] = resolved_runtime
            if shared_capture_scope and not scope_state["started"]:
                _begin_runtime_scope(runtime_holder["runtime"])
                scope_state["started"] = True
            return runtime_holder["runtime"]

        def end_runtime_scope_if_started() -> None:
            if not scope_state["started"]:
                return
            _end_runtime_scope(runtime_holder["runtime"])
            scope_state["started"] = False

        def runtime_if_started():
            return runtime_holder.get("runtime")

        def guide_config(*, enrich_traits: bool = False) -> dict:
            return _cached_cw_guide_config(
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
                enrich_traits=enrich_traits,
            )

        def validated_enter_payload() -> dict:
            if any(key in payload for key in ("mode", "difficulty", "battle_mode")):
                raise TrailError(
                    "CW_ENTER_ARGS_NOT_SUPPORTED",
                    "cw enter no longer accepts mode/difficulty/battle_mode; use cw start",
                )
            return payload

        def run_start() -> dict:
            mode, difficulty, battle_mode = _validated_start_payload(payload)
            return _call_with_supported_keywords(
                _start_cw,
                session,
                runtime=runtime(),
                mode=mode,
                difficulty=difficulty,
                battle_mode=battle_mode,
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
                check_cancelled=cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None,
            )

        def run_portal_select() -> dict:
            if payload.get("apply_guide") is False:
                runtime_instance = runtime()
                selected = select_cw_portal(session, card_idx=payload["card_idx"], runtime=runtime_instance)
                try:
                    current = wait_cw_portal_in_game(session, runtime=runtime_instance, after_selection=True)
                except Exception as error:
                    raise CwSideEffectAppliedError("cw.portal.select input was sent before waiting for the next page") from error
                record_cw_investment(session, selected, kind="portal")
                response = {**selected, "status": "observed", "stage": current.get("stage"),
                            **{key: current[key] for key in ("voyage_text", "observed_stage", "reason") if current.get(key)}}
                if current.get("preparation_verified") is True and current.get("stage") == "preparation":
                    return _collect_cw_preparation_facts(
                        session, runtime=runtime_instance, workspace_root=workspace_root,
                        cw_resource_service=self.cw_resource_service, request_id=request_id,
                        response_data=response, order=PORTAL_SELECT_PREP_COLLECT_ORDER,
                        previous_slots_snapshot=_fresh_previous_slots_snapshot(session))
                return response
            guide = _require_selected_guide(session)
            return _select_portal_and_apply_selected_guide(
                session,
                runtime=runtime(),
                card_idx=payload["card_idx"],
                guide=guide,
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
                request_id=request_id,
            )

        def run_stage_opening() -> dict:
            try:
                result = collect_cw_opening(
                    runtime(),
                    check_cancelled=cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None,
                )
            finally:
                if cancellation_token is not None and tracker.side_effect_applied:
                    setattr(cancellation_token, "side_effect_stage", "side_effect_applied")
            record_cw_opening(session, result)
            return result

        def run_strategy_select() -> dict:
            current_runtime = runtime()
            header = read_cw_preparation_header(current_runtime)
            try:
                selected = select_cw_strategy(session, card_idx=payload["card_idx"], runtime=current_runtime)
            except TrailError as error:
                if error.code == "CW_EVENT_CONFIRM_UNCONFIRMED":
                    cw_run_facts(session)["investment_history_incomplete"] = True
                raise
            evidence = selected.pop("_selection_evidence", None)
            successor = evidence.get("successor", {}) if isinstance(evidence, dict) else {}
            node = successor.get("node") or (header.get("node") if header.get("stage") == "invest" else None)
            record_cw_investment(session, selected, kind="strategy", node=node)
            return selected

        def run_invest_choose() -> dict:
            current_runtime = runtime()
            cards = []
            node = None
            if callable(getattr(current_runtime, "capture_image", None)):
                header = read_cw_preparation_header(current_runtime)
                node = header.get("node") if header.get("stage") == "invest" else None
                cards = summarize_strategy_cards(current_runtime.ocr(), guide_config().get("strategy_list", []))
            try:
                chooser = invest_chooser_factory(current_runtime)
                result = choose_cw_invest(session, option=payload["option"], chooser=chooser)
            except TrailError as error:
                if error.code == "CW_EVENT_CONFIRM_UNCONFIRMED":
                    cw_run_facts(session)["investment_history_incomplete"] = True
                raise
            evidence = getattr(chooser, "evidence", None)
            title = evidence.get("title") if isinstance(evidence, dict) else None
            successor = evidence.get("successor", {}) if isinstance(evidence, dict) else {}
            node = successor.get("node") or node
            selected = next((card for card in cards if card.get("card_idx") == payload["option"] and card.get("strategy_title") == title), {})
            if not selected and title:
                selected = {"strategy_title": title}
            record_cw_investment(session, selected, kind="strategy", node=node)
            return result.scene_state["cw"]["stage"]

        def read_plot_armor_panel(current_runtime) -> None:
            panel = read_cw_stage_text(current_runtime, ocr=OcrRequestConfig(lang="ch", ocr_mode="high"))
            if panel["value"] != "plot_armor_details":
                raise TrailError("CW_AURA_DETAILS_UNCONFIRMED", "未确认进入主角光环与奇遇详情")
            _, config, recognizer = _slots_read_resources(
                workspace_root=workspace_root, cw_resource_service=self.cw_resource_service,
            )
            armor = read_cw_plot_armor(current_runtime, panel, spotlight_list=config.get("spot_light_list", []),
                                       recognizer=recognizer)
            record_cw_plot_armor(session, armor)

        def refresh_plot_armor(current_runtime, stage, *, deadline=None) -> str | None:
            """Only leave a panel that is confirmed to be the details we opened."""
            if not callable(getattr(current_runtime, "capture_image", None)):
                return None
            if stage not in {"preparation", "plot_armor_details"}:
                return None
            mark_cw_plot_armor_unverified(session)
            entered = stage == "preparation"
            class AuraRuntime:
                def __getattr__(self, name):
                    value = getattr(current_runtime, name)
                    if name not in SIDE_EFFECT_RUNTIME_METHODS or not callable(value):
                        return value
                    def checked(*args, **kwargs):
                        if cancellation_token is not None:
                            cancellation_token.throw_if_cancelled()
                        if deadline is not None and monotonic() >= deadline:
                            raise TrailError("CW_BATTLE_BUDGET_EXHAUSTED", "战斗时间预算已到")
                        return value(*args, **kwargs)
                    return checked
            guarded_runtime = AuraRuntime() if deadline is not None else current_runtime
            try:
                try:
                    if cancellation_token is not None:
                        cancellation_token.throw_if_cancelled()
                    open_cw_plot_armor_reselect(guarded_runtime)
                    read_plot_armor_panel(current_runtime)
                except TrailError:
                    # Opening or reading can fail after the click has already applied.
                    pass
                try:
                    observed = read_cw_preparation_header(current_runtime)["stage"]
                    if entered and observed == "plot_armor_details":
                        if cancellation_token is not None:
                            cancellation_token.throw_if_cancelled()
                        if deadline is not None and monotonic() >= deadline:
                            return observed
                        guarded_runtime.press_key("esc")
                        sleep(0.35)
                        observed = read_cw_preparation_header(current_runtime)["stage"]
                    if deadline is not None and monotonic() >= deadline and observed == "preparation":
                        return "unknown"
                    return observed or "unknown"
                except TrailError:
                    return "unknown"
            except Exception:
                # Cancellation or an unexpected backend failure must not leave a
                # persisted preparation snapshot after the UI may have moved.
                mark_cw_event_unknown_stale(session)
                session_service.save_session(session)
                raise

        def stop_after_unconfirmed_armor(result: dict, stage: str | None) -> dict:
            stage = stage if stage and stage != "unknown" else "unknown"
            mark_cw_event_unknown_stale(session)
            if stage != "unknown":
                detect_cw_stage(session, detector=lambda: stage)
            for key in ("_screenshot", "decision", "slots", "shop", "equipment", "crystals", "skill_info", "hp", "coins", "exp"):
                result.pop(key, None)
            result.update(stage=stage, stale=True, in_battle=False, next_action="cw.event.reconcile",
                          current_node={"stage": stage, "node": None, "hp": None})
            result["run_facts"] = summarize_cw_run_facts(session)
            return result

        def run_aura_reselect() -> dict:
            option = payload.get("option")
            current_runtime = runtime()
            try:
                result = open_cw_plot_armor_reselect(current_runtime, option=option)
            except TrailError as error:
                if option is not None and error.code == "CW_AURA_RESELECT_UNCONFIRMED" and _safe_error_attr(error, "known_failure_after_save"):
                    mark_cw_plot_armor_unverified(session)
                raise
            if option is not None:
                mark_cw_plot_armor_unverified(session)
            elif callable(getattr(current_runtime, "capture_image", None)):
                read_plot_armor_panel(current_runtime)
            result["run_facts"] = summarize_cw_run_facts(session)
            return result

        def run_stage_read() -> dict:
            current_runtime = runtime()
            result = read_cw_stage_text(current_runtime)
            if result["value"] == "plot_armor":
                mark_cw_plot_armor_unverified(session)
            elif result["value"] == "plot_armor_details" and callable(getattr(current_runtime, "capture_image", None)):
                read_plot_armor_panel(current_runtime)
                result["run_facts"] = summarize_cw_run_facts(session)
            return result

        def run_verified_page_action(action, *, aura: bool = False, **kwargs) -> dict:
            check_cancelled = cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None
            try:
                result, frame = action(runtime(), check_cancelled=check_cancelled, **kwargs)
            except TrailError as error:
                if _safe_error_attr(error, "known_failure_after_save"):
                    mark_cw_stage_stale(session)
                    if aura:
                        mark_cw_plot_armor_unverified(session)
                raise
            detect_cw_stage(session, detector=lambda: result["value"])
            if aura:
                mark_cw_plot_armor_unverified(session)
            if track_side_effects:
                result["_reconcile_frame"] = frame
            return result

        def run_guide_apply() -> dict:
            _validate_cw_guide_apply_payload(payload)
            guide = _require_selected_guide(session)
            return _apply_selected_guide_via_ui(session, runtime=runtime(), guide=guide)

        def run_shop_scan() -> dict:
            base_config = guide_config()
            applied = scan_cw_shop(
                session,
                scanner=_build_shop_snapshot_reader(runtime()),
                guide_config=base_config,
            )
            trait_config = guide_config(enrich_traits=True) if _shop_has_fresh_slots(session) else None
            return project_cw_shop_snapshot(
                applied.session,
                guide_config=trait_config,
                include_field_trait_summary=trait_config is not None,
                shop_snapshot=applied.response_snapshot,
            )

        def run_shop_buy_slot() -> dict:
            runtime_instance = runtime()
            slots_reader: SlotsSnapshotReader | None = None
            should_verify_slots = _callable_accepts_keyword(buy_cw_shop_slot, "slots_reader")
            if should_verify_slots and not _runtime_supports_slot_snapshot_read(runtime_instance):
                raise TrailError("SLOTS_READER_UNAVAILABLE", "cw.shop.buy_slot requires slots verification support")
            if should_verify_slots:
                slots_reader, base_config = _build_slots_reader_for_service(
                    runtime_instance,
                    targets=None,
                    workspace_root=workspace_root,
                    cw_resource_service=self.cw_resource_service,
                    request_id=request_id,
                    dismiss_initial_overlay=False,
                )
            else:
                base_config = guide_config()
            return _call_with_supported_keywords(
                buy_cw_shop_slot,
                session,
                slot=payload["slot"],
                expect=payload["expect"],
                buyer=shop_buyer_factory(runtime_instance),
                scanner=shop_scanner_factory(runtime_instance),
                slots_reader=slots_reader,
                preflight_read_scope=getattr(runtime_instance, "suppress_side_effect_tracking", None),
                guide_config=base_config,
            ).response_snapshot

        def run_shop_buy_exp() -> dict:
            base_config = guide_config()
            return buy_cw_shop_exp(
                session,
                buyer=shop_exp_buyer_factory(runtime()),
                scanner=_build_shop_snapshot_reader(runtime(), read_stage_status=True),
                guide_config=base_config,
            ).response_snapshot

        def experience_context() -> tuple[int, dict | None]:
            target_level = payload.get("target_level")
            if type(target_level) is not int or target_level < 1:
                raise TrailError("CW_OPTION_INVALID", "target_level must be a positive integer")
            facts = session.scene_state.get("cw", {}).get("run_facts")
            opening = facts.get("opening") if isinstance(facts, dict) and not facts.get("ended") else None
            return target_level, opening if isinstance(opening, dict) else None

        def run_shop_plan_exp() -> dict:
            target_level, opening = experience_context()
            current_runtime = runtime()
            result, frame = quote_cw_experience(current_runtime, opening=opening, target_level=target_level)
            if frame is not None:
                result["_screenshot"] = current_runtime.save_capture_image_to_workspace(frame, request_id=request_id)
            return result

        def run_shop_buy_exp_to_level() -> dict:
            target_level, opening = experience_context()
            max_gold = payload.get("max_gold")
            if max_gold is not None and (type(max_gold) is not int or max_gold < 0):
                raise TrailError("CW_OPTION_INVALID", "max_gold must be a nonnegative integer")
            result, frame = buy_cw_experience_to_level(
                runtime(),
                opening=opening,
                target_level=target_level,
                max_gold=max_gold,
                check_cancelled=cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None,
            )
            if tracker.side_effect_applied:
                cw_state = ensure_cw_state(session)
                for key in ("shop", "slots"):
                    cw_state[key] = {**cw_state.get(key, {}), "stale": True}
                cw_state["sell_plan"] = {}
                mark_cw_stage_status_stale(session)
            return {**result, "_reconcile_frame": frame}

        def equipment_read_resources() -> tuple[dict, object | None]:
            if self.cw_resource_service is not None and hasattr(self.cw_resource_service, "equipment_read_resources"):
                return self.cw_resource_service.equipment_read_resources(workspace_root=workspace_root)
            raw_config = _cached_cw_raw_config(
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
            )
            if raw_config is None:
                raw_config = _call_with_supported_keywords(fetch_cw_raw_guide_config, workspace_root=workspace_root)
            catalog = build_cw_equipment_catalog(raw_config)
            recognizer = VectorEquipmentIconRecognizer(load_cached_equipment_icons(catalog, workspace_root=workspace_root))
            return raw_config, recognizer

        def run_equipment_read() -> dict:
            return _apply_cw_equipment_read_with_resources(
                session,
                runtime(),
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
                request_id=request_id,
                read_consumables=payload.get("read_consumables") is True,
            )

        def run_equipment_prepare() -> dict:
            if bool(payload.get("refresh")) and self.cw_resource_service is not None:
                return self.cw_resource_service.refresh_equipment_workspace_override(workspace_root=workspace_root)
            if self.cw_resource_service is not None:
                return self.cw_resource_service.equipment_prepare_summary(workspace_root=workspace_root)
            return _equipment_prepare_summary_from_bundle(workspace_root=workspace_root)

        def run_slots_read() -> dict:
            slots_reader, trait_config = _build_slots_reader_for_service(
                runtime(),
                targets=payload.get("slot"),
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
                request_id=request_id,
            )
            return read_cw_slots(
                session,
                reader=slots_reader,
                targets=payload.get("slot"),
                guide_config=trait_config,
            ).response_snapshot

        def run_equipment_compose() -> dict:
            raw_config, recognizer = equipment_read_resources()
            slots_reader, _trait_config = _build_slots_reader_for_service(
                runtime(),
                targets=None,
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
                request_id=request_id,
            )
            return compose_and_equip_cw_equipment(
                session,
                runtime(),
                name=payload["name"],
                slot=payload["slot"],
                role=payload["role"],
                workspace_root=workspace_root,
                request_id=request_id,
                raw_config=raw_config,
                recognizer=recognizer,
                slots_reader=slots_reader,
                guide_config=guide_config(enrich_traits=True),
                preflight_read_scope=runtime().suppress_side_effect_tracking,
            )

        def run_battle_run() -> dict[str, Any]:
            timeout = resolve_command_execution_timeout("cw.battle.run", payload)
            current_runtime = runtime()
            can_read_armor = callable(getattr(current_runtime, "capture_image", None))
            facts = cw_run_facts(session) if can_read_armor else ensure_cw_state(session).get("run_facts")
            checked_armor = False
            resume = ensure_cw_state(session).get("battle_resume") or {}
            resuming_battle = any(resume.get(key) for key in ("start_pending", "in_battle_hint", "report", "terminal"))
            if can_read_armor and not resuming_battle and facts["plot_armor"]["status"] in {"unverified", "partial"}:
                if callable(getattr(current_runtime, "screenshot", None)) and callable(getattr(current_runtime, "ocr_image", None)):
                    observation = observe_cw_battle_page(current_runtime, detected_stage=None)
                    if observation.frame is not None:
                        # Keep the preflight decision on this frame; no second OCR or input.
                        observation._headline_text = ""
                        state = classify_cw_battle_page(current_runtime, session=session, detected_stage=None, observation=observation)
                        header = {"stage": "preparation" if state == "battle_start" and has_cw_preparation_board(observation) else None}
                    else:
                        header = read_cw_preparation_header(current_runtime)
                else:
                    header = read_cw_preparation_header(current_runtime)
                if header["stage"] == "preparation":
                    checked_armor = True
                    restored_stage = refresh_plot_armor(current_runtime, "preparation")
                else:
                    restored_stage = None
                if header["stage"] == "preparation" and restored_stage != "preparation":
                    return stop_after_unconfirmed_armor({"status": "blocked"}, restored_stage)
            battle_started_at = monotonic()
            last_progress = [None, 0.0]
            evidence_counter = [0]

            def save_evidence(frame, kind):
                if frame is None or not callable(getattr(current_runtime, "save_capture_image_to_workspace", None)):
                    return None
                evidence_counter[0] += 1
                path = current_runtime.save_capture_image_to_workspace(
                    frame, request_id=f"{kind}-{evidence_counter[0]}-{request_id}")
                return _normalize_workspace_path(path, workspace_root=Path(workspace_root))

            def terminal_checkpoint(terminal, frame, phase):
                # The battle loop invokes this synchronously before its irreversible click.
                kind = phase
                evidence = save_evidence(frame, f"terminal-{kind}")
                if evidence and isinstance(terminal.get(kind), dict):
                    terminal[kind]["evidence"] = evidence
                ensure_cw_state(session).setdefault("battle_resume", {})["terminal"] = deepcopy(terminal)
                session_service.save_session(session)

            def publish_progress(progress, frame):
                publisher = getattr(cancellation_token, "publish_progress", None)
                if not callable(publisher):
                    return
                try:
                    now = monotonic()
                    # A percentage change alone does not trigger another disk write.
                    signature = (progress.get("phase"), progress.get("source"), progress.get("round"),
                                 getattr(cancellation_token, "side_effect_stage", "none"))
                    if signature == last_progress[0] and now - last_progress[1] < 10.0:
                        return
                    try:
                        evidence = save_evidence(frame, "progress")
                    except Exception:
                        evidence = None
                    snapshot = deepcopy(progress)
                    snapshot["observed_at"] = datetime.now(timezone.utc).isoformat()
                    snapshot["elapsed_seconds"] = round(now - battle_started_at, 1)
                    if evidence:
                        snapshot["evidence"] = evidence
                    publisher(snapshot)
                    last_progress[:] = [signature, now]
                except Exception:
                    # Progress is advisory; it must never interrupt game execution.
                    return

            result = _call_with_supported_keywords(
                run_cw_battle,
                session,
                runtime=current_runtime,
                timeout=timeout if timeout is not None else DEFAULT_CW_BATTLE_RUN_TIMEOUT,
                cancellation_token=cancellation_token,
                stop_at_plane=payload.get("stop_at_plane") is True,
                terminal_checkpoint=terminal_checkpoint,
                battle_checkpoint=lambda: session_service.save_session(session),
                publish_progress=publish_progress,
            )
            if result.get("reason") == "reserve_overflow":
                result["next_action"] = "cw.slots.read"
            if result.get("stage") == "game_over" and facts:
                facts["ended"] = True
            if _cw_battle_run_reaches_preparation(result) and callable(getattr(current_runtime, "ocr", None)):
                reading = _read_post_battle_transition(
                    current_runtime,
                    check_cancelled=(cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None),
                    budget=max(0.0, (timeout if timeout is not None else DEFAULT_CW_BATTLE_RUN_TIMEOUT) - (monotonic() - battle_started_at)),
                    previous_round=result.get("round"),
                )
                if reading is not None and reading.pop("_stable_preparation", False):
                    observed_node = reading.get("_transition_node")
                    if observed_node and observed_node != result.get("round"):
                        result["current_node"] = {"stage": "preparation", "node": observed_node, "hp": None}
                elif reading is not None:
                    stage = reading.get("value") or "unknown"
                    observed_transition_node = reading.pop("_transition_node", None)
                    verified_frame = reading.pop("_frame", None)
                    if verified_frame is not None:
                        result["_reconcile_frame"] = verified_frame
                    mark_cw_event_unknown_stale(session)
                    detect_cw_stage(session, detector=lambda: stage)
                    if stage == "unknown":
                        mark_cw_stage_stale(session)
                    result.pop("_screenshot", None)
                    current = project_cw_preparation_header(reading)
                    if stage in CW_DECISION_TEXT_STAGES and current["node"] is None and observed_transition_node != result.get("round"):
                        current["node"] = observed_transition_node
                    result.update(stage=stage, stale=True, next_action="cw.stage.read" if stage != "unknown" else "cw.event.reconcile",
                                  current_node=current)
                    if stage in CW_DECISION_TEXT_STAGES:
                        result["decision"] = reading
                    return complete_battle_run_facts(result)
            if result.get("status") == "completed" and result.get("stage") in CW_DECISION_TEXT_STAGES:
                _attach_cw_decision_text(result, runtime())
                if "decision" in result:
                    result["current_node"] = project_cw_preparation_header(result["decision"])
                result["next_action"] = "cw.stage.read"
            if (can_read_armor and not checked_armor and _cw_battle_run_reaches_preparation(result)
                    and facts["plot_armor"]["status"] in {"unverified", "partial"}):
                reading, frame = _observe_battle_collection_page(current_runtime)
                if cancellation_token is not None:
                    cancellation_token.throw_if_cancelled()
                if reading.get("value") == "preparation" and (_has_level_up_overlay(reading) or monotonic() >= battle_started_at + (timeout if timeout is not None else DEFAULT_CW_BATTLE_RUN_TIMEOUT)):
                    reading["value"] = "unknown"
                if reading.get("value") != "preparation":
                    stage = reading.get("value") or "unknown"
                    mark_cw_event_unknown_stale(session)
                    if stage != "unknown":
                        detect_cw_stage(session, detector=lambda: stage)
                    current = project_cw_preparation_header(reading)
                    observed_node = (result.get("current_node") or {}).get("node")
                    if stage in CW_DECISION_TEXT_STAGES:
                        if current["node"] is None and observed_node != result.get("round"):
                            current["node"] = observed_node
                        result["decision"] = reading
                    result.update(stage=stage, stale=True, current_node=current,
                                  next_action="cw.event.reconcile" if stage == "unknown" else "cw.stage.read")
                    if frame is not None:
                        result["_reconcile_frame"] = frame
                    return complete_battle_run_facts(result)
                restored_stage = refresh_plot_armor(current_runtime, "preparation", deadline=battle_started_at + (timeout if timeout is not None else DEFAULT_CW_BATTLE_RUN_TIMEOUT))
                if restored_stage != "preparation":
                    return stop_after_unconfirmed_armor(result, restored_stage)
                checked_armor = True
            if result.get("status") == "completed" and result.get("stage") not in {"preparation", "game_over", "terminal"} and can_read_armor:
                reading, frame = _observe_battle_collection_page(current_runtime)
                stage = reading.get("value") or "unknown"
                result.pop("decision", None)
                if stage == "preparation":
                    stage = "unknown"
                    reading["value"] = stage
                if stage == "unknown" and frame is None and result.get("stage") == "layer_transition":
                    stage = "layer_transition"
                    reading["value"] = stage
                    frame = None
                mark_cw_event_unknown_stale(session)
                detect_cw_stage(session, detector=lambda: stage)
                if stage == "unknown":
                    mark_cw_stage_stale(session)
                result.update(stage=stage, stale=True, current_node=project_cw_preparation_header(reading),
                              next_action="cw.event.reconcile" if stage == "unknown" else "cw.stage.read")
                if stage in CW_DECISION_TEXT_STAGES:
                    result["decision"] = reading
                if frame is not None:
                    result["_reconcile_frame"] = frame
            if payload.get("collect_crystals") is False or not _cw_battle_run_reaches_preparation(result):
                if (payload.get("collect_crystals") is False and _cw_battle_run_reaches_preparation(result)
                        and callable(getattr(current_runtime, "ocr_image", None))):
                    if cancellation_token is not None:
                        cancellation_token.throw_if_cancelled()
                    reading, frame = _observe_battle_collection_page(current_runtime)
                    stage = reading.get("value") or "unknown"
                    current = project_cw_preparation_header(reading)
                    observed = (result.get("current_node") or {}).get("node")
                    if stage in CW_DECISION_TEXT_STAGES and current["node"] is None and observed != result.get("round"):
                        current["node"] = observed
                    result.update(stage=stage, current_node=current)
                    if stage != "preparation":
                        mark_cw_event_unknown_stale(session)
                        detect_cw_stage(session, detector=lambda: stage)
                        if stage == "unknown":
                            mark_cw_stage_stale(session)
                        result.update(stale=True, next_action="cw.event.reconcile" if stage == "unknown" else "cw.stage.read")
                        if stage in CW_DECISION_TEXT_STAGES:
                            result["decision"] = reading
                    if frame is not None:
                        result.pop("_screenshot", None)
                        result["_reconcile_frame"] = frame
                if result.get("status") == "completed" and "current_node" not in result and facts:
                    if result.get("stage") == "preparation":
                        try:
                            current = read_cw_preparation_header(current_runtime)
                            result["current_node"] = current
                            if current["stage"] in CW_DECISION_TEXT_STAGES:
                                mark_cw_event_unknown_stale(session)
                                detect_cw_stage(session, detector=lambda: current["stage"])
                                result.update(stage=current["stage"], stale=True, next_action="cw.stage.read")
                                _attach_cw_decision_text(result, current_runtime)
                        except TrailError as error:
                            result["current_node"] = {"stage": "preparation", "node": None, "hp": None}
                            result["next_action"] = "cw.stage.read"
                            result.setdefault("warnings", []).append({
                                "code": "CW_CURRENT_NODE_READ_FAILED", "detail_code": error.code,
                                "message": str(error),
                            })
                    else:
                        result["current_node"] = {"stage": result.get("stage"), "node": None, "hp": None}
                return complete_battle_run_facts(result)

            result_data = dict(result)
            guide = complete_cw_guide_or_none(ensure_cw_state(session))
            skill_info = _operation_guide_skill_info(guide)
            if skill_info:
                result_data["skill_info"] = skill_info
            result = _collect_cw_preparation_facts(
                session,
                runtime=runtime(),
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
                request_id=request_id,
                response_data=result_data,
                order=CW_BATTLE_RUN_PREP_COLLECT_ORDER,
                guard_battle_inputs=True,
                battle_deadline=battle_started_at + (timeout if timeout is not None else DEFAULT_CW_BATTLE_RUN_TIMEOUT),
                check_cancelled=(cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None),
            )
            return complete_battle_run_facts(result)

        def complete_battle_run_facts(result: dict) -> dict:
            if result.get("stage") == "plot_armor":
                mark_cw_plot_armor_unverified(session)
            if (summary := summarize_cw_run_facts(session)) is not None:
                result["run_facts"] = summary
            return result

        def run_event_handle() -> dict[str, object]:
            result = _call_with_supported_keywords(
                handle_cw_event,
                session,
                router=event_router_factory(runtime()),
                variable_cost_choice=payload.get("variable_cost_choice"),
            )
            if result.get("event_type") == "unknown" and result.get("handled") is not True:
                stale_facts = _stale_fact_names(ensure_cw_state(session))
                result["stale"] = bool(stale_facts)
                result["stale_facts"] = _join_fact_names(stale_facts)
                result["crystals_stale"] = False
                result["reconcile_action"] = "cw.event.reconcile"
            return result

        def run_event_reconcile() -> dict[str, Any]:
            current_runtime = runtime()
            previous_cw_state = deepcopy(ensure_cw_state(session))
            previous_strategy = previous_cw_state.get("strategy")
            previous_sell_plan = previous_cw_state.get("sell_plan")
            previous_variable_cost_stale = previous_cw_state.get("variable_cost_roles_stale") is True
            mark_cw_event_unknown_stale(session)
            detected_stage = stage_detector_factory(current_runtime)()
            if not detected_stage and callable(getattr(current_runtime, "ocr_image", None)):
                observation = observe_cw_battle_page(current_runtime, detected_stage=None)
                if battle_hud_from_page(observation.page_ocr_pieces):
                    detected_stage = "battle"
            if isinstance(ensure_cw_state(session).get("run_facts"), dict) or callable(
                getattr(current_runtime, "capture_image", None)
            ):
                cw_run_facts(session)["investment_history_incomplete"] = True
                mark_cw_plot_armor_unverified(session)
                restored_stage = refresh_plot_armor(current_runtime, detected_stage)
                if restored_stage is not None:
                    detected_stage = restored_stage
            response_data: dict[str, Any] = {}
            attempted: list[str] = []

            if not isinstance(detected_stage, str) or not detected_stage or detected_stage == "unknown":
                mark_cw_stage_stale(session)
                mark_cw_stage_status_stale(session)
                return _finalize_reconcile_payload(
                    session,
                    stage="unknown",
                    response_data=response_data,
                    attempted=attempted,
                    previous_variable_cost_stale=previous_variable_cost_stale,
                    next_action="manual",
                )

            detect_cw_stage(session, detector=lambda: detected_stage)
            if detected_stage == "preparation":
                attempted = list(RECONCILE_PREPARATION_FACT_ORDER)
                try:
                    response_data = _collect_cw_preparation_facts(
                        session,
                        runtime=current_runtime,
                        workspace_root=workspace_root,
                        cw_resource_service=self.cw_resource_service,
                        request_id=request_id,
                        order=RECONCILE_PREPARATION_FACT_ORDER,
                        include_crystals=False,
                        reconcile_partial=True,
                        check_cancelled=(cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None),
                    )
                    attempted = response_data.pop("_attempted_steps", attempted)
                    if "shop" in attempted:
                        response_data.pop("_screenshot", None)
                    collection_failed = response_data.pop("_collection_failed", False)
                    collection_stage = response_data.pop("_collection_stage", None)
                    collection_frame = response_data.pop("_collection_frame", None)
                    if collection_failed:
                        response_data.pop("_screenshot", None)
                        if collection_stage is not None:
                            detected_stage = collection_stage
                        else:
                            try:
                                detected_stage = stage_detector_factory(current_runtime)() or "unknown"
                            except TrailError:
                                detected_stage = "unknown"
                        if detected_stage == "unknown":
                            mark_cw_stage_stale(session)
                        else:
                            detect_cw_stage(session, detector=lambda: detected_stage)
                    if collection_failed and detected_stage in CW_DECISION_TEXT_STAGES:
                        _attach_cw_decision_text(response_data, current_runtime)
                    shop_snapshot = response_data.get("shop")
                    if isinstance(shop_snapshot, dict) and shop_snapshot.get("stale") is False:
                        closed_shop = {**shop_snapshot, "opened": False}
                        ensure_cw_state(session)["shop"] = deepcopy(closed_shop)
                        response_data["shop"] = closed_shop
                except TrailError:
                    attempted = ["slots"]
                    response_data = {}
                    collection_frame = None
                if track_side_effects and collection_frame is not None:
                    response_data["_reconcile_frame"] = collection_frame
                _restore_reconcile_reference_facts(
                    session,
                    previous_strategy=previous_strategy,
                    previous_sell_plan=previous_sell_plan,
                )
                return _finalize_reconcile_payload(
                    session,
                    stage=detected_stage,
                    response_data=response_data,
                    attempted=attempted,
                    previous_variable_cost_stale=previous_variable_cost_stale,
                    next_action=("manual" if detected_stage == "unknown" else _next_action_for_reconcile_stage(detected_stage)),
                )

            if detected_stage == "shop":
                attempted = ["shop"]
                close_frame = None
                try:
                    base_config = _cached_cw_guide_config(
                        workspace_root=workspace_root,
                        cw_resource_service=self.cw_resource_service,
                        enrich_traits=True,
                    )
                    previous_shop = deepcopy(ensure_cw_state(session).get("shop"))
                    shop_result = scan_cw_shop(
                        session,
                        scanner=_call_with_supported_keywords(
                            shop_page_snapshot_reader_factory, current_runtime, recover_missing=True,
                            check_cancelled=(cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None),
                        ),
                        guide_config=base_config,
                    )
                    shop_response = _response_snapshot_or_fallback(shop_result, {})
                    shop_response = _guard_reconcile_shop_scan(session, previous_shop, shop_response)
                    shop_warnings = _pop_response_snapshot_warnings(shop_response)
                    close_failed = False
                    if cancellation_token is not None:
                        cancellation_token.throw_if_cancelled()
                    try:
                        close_cw_shop(session, closer=shop_closer_factory(current_runtime))
                    except TrailError as error:
                        close_failed = True
                        shop_warnings.append({"code": "CW_SHOP_CLOSE_UNCONFIRMED", "detail_code": error.code,
                                              "message": str(error)})
                    detected_stage, close_frame = _wait_for_reconcile_shop_close(
                        current_runtime,
                        check_cancelled=(cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None),
                    )
                    if detected_stage == "unknown":
                        mark_cw_stage_stale(session)
                    else:
                        detect_cw_stage(session, detector=lambda: detected_stage)
                    confirmed = detected_stage == "preparation" and not close_failed and shop_response.get("stale") is False
                    opened = True if detected_stage == "shop" else False if detected_stage != "unknown" else None
                    shop_result = {**project_cw_shop_snapshot(session), **shop_response,
                                   "opened": opened, "stale": not confirmed}
                    ensure_cw_state(session)["shop"] = deepcopy(shop_result)
                    response_data["shop"] = shop_result
                    if shop_warnings:
                        response_data["warnings"] = shop_warnings
                    if detected_stage in CW_DECISION_TEXT_STAGES:
                        _attach_cw_decision_text(response_data, current_runtime)
                except TrailError:
                    shop_state = ensure_cw_state(session).get("shop")
                    if isinstance(shop_state, dict):
                        shop_state["stale"] = True
                    else:
                        ensure_cw_state(session)["shop"] = {"stale": True}
                if track_side_effects and close_frame is not None:
                    response_data["_reconcile_frame"] = close_frame
                return _finalize_reconcile_payload(
                    session,
                    stage=detected_stage,
                    response_data=response_data,
                    attempted=attempted,
                    previous_variable_cost_stale=previous_variable_cost_stale,
                    next_action=("manual" if detected_stage == "unknown" else _next_action_for_reconcile_stage(detected_stage)),
                )

            next_action = _next_action_for_reconcile_stage(detected_stage)
            if detected_stage in CW_DECISION_TEXT_STAGES:
                _attach_cw_decision_text(response_data, current_runtime)
            return _finalize_reconcile_payload(
                session,
                stage=detected_stage,
                response_data=response_data,
                attempted=attempted,
                previous_variable_cost_stale=previous_variable_cost_stale,
                next_action=next_action,
            )

        handlers = {
            "cw.enter": lambda: validated_enter_payload() and enter_cw(session, runtime=runtime()).scene_state["cw"]["entry"],
            "cw.start": run_start,
            "cw.portal.select": run_portal_select,
            "cw.portal.detect": lambda: _attach_guides_to_portal_snapshot(
                session,
                detect_cw_portal(
                    session,
                    runtime=runtime(),
                    portal_list=guide_config().get("portal_list", []),
                ),
                workspace_root=workspace_root,
            ),
            "cw.portal.refresh": lambda: _attach_guides_to_portal_snapshot(
                session,
                refresh_cw_portal(
                    session,
                    runtime=runtime(),
                    portal_list=guide_config().get("portal_list", []),
                ),
                workspace_root=workspace_root,
            ),
            "cw.portal.restart": lambda: _call_with_supported_keywords(
                _restart_cw,
                session,
                runtime=runtime(),
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
                check_cancelled=cancellation_token.throw_if_cancelled if cancellation_token is not None else lambda: None,
            ),
            "cw.strategy.detect": lambda: detect_cw_strategy(
                session,
                runtime=runtime(),
                strategy_list=guide_config().get("strategy_list", []),
            ),
            "cw.strategy.select": run_strategy_select,
            "cw.strategy.refresh": lambda: refresh_cw_strategy(
                session,
                card_idx=payload["card_idx"],
                runtime=runtime(),
                strategy_list=guide_config().get("strategy_list", []),
            ),
            "cw.stage.detect": lambda: detect_cw_stage(
                session,
                detector=stage_detector_factory(runtime()),
            ).scene_state["cw"]["stage"],
            "cw.stage.read": run_stage_read,
            "cw.stage.opening": run_stage_opening,
            "cw.stage.wait": lambda: wait_cw_stage(
                session,
                detector=stage_detector_factory(runtime()),
                timeout=payload.get("timeout", DEFAULT_CW_STAGE_WAIT_TIMEOUT),
            ).scene_state["cw"]["stage"],
            "cw.guide.apply": run_guide_apply,
            "cw.guide.current": lambda: _current_guide(session, artifact_store=artifact_store),
            "cw.equipment.prepare": run_equipment_prepare,
            "cw.equipment.read": run_equipment_read,
            "cw.equipment.compose": run_equipment_compose,
            "cw.equipment.open_chimera_projector": lambda: open_cw_chimera_projector(session, runtime()),
            "cw.slots.read": run_slots_read,
            "cw.slots.swap": lambda: swap_cw_slots(
                session,
                source=payload["source"],
                target=payload["target"],
                swapper=slot_swapper_factory(runtime()),
            ).scene_state["cw"]["slots"],
            "cw.slots.place": lambda: {
                **place_cw_slots(
                    session,
                    actions=payload["actions"],
                    placer=slot_placer_factory(runtime()),
                ).scene_state["cw"]["slots"],
                "applied_actions": len(payload["actions"]),
                "verification": "pending",
                "stale": True,
            },
            "cw.shop.open": lambda: open_cw_shop(
                session,
                opener=shop_opener_factory(runtime()),
            ).scene_state["cw"]["shop"],
            "cw.shop.scan": run_shop_scan,
            "cw.shop.buy_slot": run_shop_buy_slot,
            "cw.shop.buy_exp": run_shop_buy_exp,
            "cw.shop.plan_exp": run_shop_plan_exp,
            "cw.shop.buy_exp_to_level": run_shop_buy_exp_to_level,
            "cw.shop.refresh": lambda: refresh_cw_shop(
                session,
                refresher=shop_refresher_factory(runtime()),
            ).scene_state["cw"]["shop"],
            "cw.shop.close": lambda: close_cw_shop(
                session,
                closer=shop_closer_factory(runtime()),
            ).scene_state["cw"]["shop"],
            "cw.shop.status": lambda: _shop_status(
                session,
                artifact_store=artifact_store,
                workspace_root=workspace_root,
                cw_resource_service=self.cw_resource_service,
            ),
            "cw.crystals.collect": lambda: collect_cw_crystals(
                session,
                collector=crystal_collector_factory(runtime()),
            ).scene_state["cw"]["metrics"],
            "cw.hand.sell": lambda: sell_cw_hand_slots(
                session,
                slots=payload["slots"],
                seller=hand_seller_factory(runtime()),
            ).scene_state["cw"]["slots"],
            "cw.hand.open_box": lambda: open_cw_weapon_box(session, runtime(), slot=payload.get("slot")),
            "cw.hand.sell_plan": lambda: plan_cw_hand_sell(session),
            "cw.replenish.read": lambda: read_cw_replenish(session),
            "cw.replenish.refresh": lambda: run_verified_page_action(refresh_cw_replenish),
            "cw.replenish.choose": lambda: choose_cw_replenish(
                session,
                option=payload["option"],
                chooser=replenish_chooser_factory(runtime()),
            ).scene_state["cw"]["stage"],
            "cw.invest.read": lambda: read_cw_invest(session),
            "cw.invest.choose": run_invest_choose,
            "cw.encounter.read": lambda: read_cw_encounter(session),
            "cw.encounter.choose": lambda: choose_cw_encounter(
                session,
                option=payload["option"],
                chooser=encounter_chooser_factory(runtime()),
            ).scene_state["cw"]["stage"],
            "cw.fortune.read": lambda: read_cw_fortune(session),
            "cw.fortune.choose": lambda: choose_cw_fortune(
                session,
                option=payload["option"],
                chooser=fortune_chooser_factory(runtime()),
            ).scene_state["cw"]["stage"],
            "cw.boss_preview.confirm": lambda: confirm_cw_boss_preview(
                session,
                confirmer=boss_preview_confirmer_factory(runtime()),
            ).scene_state["cw"]["stage"],
            "cw.battle.run": run_battle_run,
            "cw.battle.clear_in_progress": lambda: clear_cw_battle_resume_hint(session),
            "cw.battle.start": lambda: start_cw_battle(
                session,
                starter=battle_starter_factory(runtime()),
            ).scene_state["cw"]["stage"],
            "cw.battle.continue": lambda: continue_cw_battle(
                session,
                continuer=battle_continuer_factory(runtime()),
            ).scene_state["cw"]["stage"],
            "cw.settle.next": lambda: _call_with_supported_keywords(settle_cw_next,
                session,
                continuer=settle_continuer_factory(runtime()),
                runtime=runtime(), checkpoint=lambda: session_service.save_session(session),
            ).scene_state["cw"]["stage"],
            "cw.event.handle": run_event_handle,
            "cw.event.reconcile": run_event_reconcile,
            "cw.event.open_aura_reselect": run_aura_reselect,
            "cw.event.drag_aura_role": lambda: run_verified_page_action(
                drag_cw_aura_role, aura=True, source=payload.get("source"),
            ),
        }
        if method not in handlers:
            raise TrailError("DAEMON_METHOD_NOT_SUPPORTED", f"unsupported method: {method}")

        return session, artifact_store, runtime, handlers, tracker, end_runtime_scope_if_started, runtime_if_started


def _handle_and_save_session(handler, session_service, session, scene_warnings: list[dict] | None = None):
    try:
        result = handler()
    except TrailError as error:
        if _safe_error_attr(error, "known_failure_after_save"):
            session_service.save_session(session)
        raise
    result, warnings = _pop_scene_warnings(result)
    if scene_warnings is not None:
        scene_warnings.extend(warnings)
    session_service.save_session(session)
    return result


def _mark_cw_equipment_stale(session) -> None:
    equipment = ensure_cw_state(session).get("equipment")
    if isinstance(equipment, dict) and equipment.get("stale") is not True:
        equipment["stale"] = True


def _cw_equipment_auto_collect_warning(error: Exception) -> dict:
    warning = {
        "code": "CW_EQUIPMENT_AUTO_COLLECT_FAILED",
        "message": format_exception_message(error),
    }
    detail_code = getattr(error, "code", None)
    if isinstance(detail_code, str) and detail_code:
        warning["detail_code"] = detail_code
    return warning


def _cw_slots_auto_collect_uncertain_warning(error: TrailError) -> dict:
    warning = {
        "code": "CW_SLOTS_AUTO_COLLECT_UNCERTAIN",
        "message": format_exception_message(error),
    }
    detail_code = getattr(error, "code", None)
    if isinstance(detail_code, str) and detail_code:
        warning["detail_code"] = detail_code
    position = getattr(error, "position", None)
    if position is not None:
        warning["position"] = to_jsonable(position)
    nested_warnings = getattr(error, "warnings", None)
    if isinstance(nested_warnings, list) and nested_warnings:
        warning["warnings"] = to_jsonable(nested_warnings)
    return warning


def _fresh_previous_slots_snapshot(session) -> dict | None:
    slots = ensure_cw_state(session).get("slots")
    if not isinstance(slots, dict) or slots.get("stale") is not False:
        return None
    areas = [slots.get(area) for area in ("front", "back", "hand")]
    if not any(isinstance(area, list) for area in areas):
        return None
    if not any(item is not None for area in areas if isinstance(area, list) for item in area):
        return None
    return deepcopy(slots)



RECONCILE_FACT_ORDER: tuple[str, ...] = (
    "stage",
    "stage/status",
    "slots",
    "shop",
    "equipment",
    "strategy",
    "sell_plan",
    "variable_cost_roles",
)
RECONCILE_PREPARATION_FACT_ORDER: tuple[str, ...] = ("slots", "shop", "equipment")


def _ordered_fact_names(names: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for preferred in RECONCILE_FACT_ORDER:
        if preferred in names and preferred not in seen:
            ordered.append(preferred)
            seen.add(preferred)
    for name in names:
        if name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def _join_fact_names(names: list[str] | tuple[str, ...] | set[str]) -> str:
    ordered = _ordered_fact_names(names)
    return "|".join(ordered) if ordered else "none"


def _is_fresh_mapping(value: object) -> bool:
    return isinstance(value, dict) and value.get("stale") is False


def _stale_fact_names(cw_state: dict[str, Any]) -> list[str]:
    stale: list[str] = []
    stage = cw_state.get("stage")
    status = stage.get("status") if isinstance(stage, dict) else None
    if not isinstance(stage, dict) or stage.get("stale") is not False or not isinstance(status, dict) or status.get("stale") is not False:
        stale.append("stage/status")
    for key in ("slots", "shop", "equipment"):
        if not _is_fresh_mapping(cw_state.get(key)):
            stale.append(key)
    strategy = cw_state.get("strategy")
    if not isinstance(strategy, dict) or strategy.get("stale") is not False:
        stale.append("strategy")
    sell_plan = cw_state.get("sell_plan")
    if not isinstance(sell_plan, dict) or sell_plan.get("stale") is not False:
        stale.append("sell_plan")
    if cw_state.get("variable_cost_roles_stale") is True:
        stale.append("variable_cost_roles")
    return _ordered_fact_names(stale)


def _fresh_reconciled_fact_names(cw_state: dict[str, Any], *, response_data: dict[str, Any], attempted: list[str], previous_variable_cost_stale: bool) -> list[str]:
    fresh: list[str] = []
    stage = cw_state.get("stage")
    if isinstance(stage, dict) and stage.get("value") and stage.get("stale") is False:
        fresh.append("stage")
    for key in ("slots", "shop", "equipment"):
        if key in response_data and _is_fresh_mapping(cw_state.get(key)):
            fresh.append(key)
    if previous_variable_cost_stale and any(name in attempted for name in ("slots", "shop")) and cw_state.get("variable_cost_roles_stale") is not True:
        fresh.append("variable_cost_roles")
    return _ordered_fact_names(fresh)


def _restore_reconcile_reference_facts(session, *, previous_sell_plan: object, previous_strategy: object) -> None:
    cw_state = ensure_cw_state(session)
    if isinstance(previous_strategy, dict):
        strategy = deepcopy(previous_strategy)
        strategy["stale"] = True
        cw_state["strategy"] = strategy
    else:
        cw_state["strategy"] = {"cards": [], "stale": True}
    if isinstance(previous_sell_plan, dict) and previous_sell_plan:
        sell_plan = deepcopy(previous_sell_plan)
        sell_plan["stale"] = True
        cw_state["sell_plan"] = sell_plan
    else:
        current = cw_state.get("sell_plan")
        if isinstance(current, dict):
            current["stale"] = True
        else:
            cw_state["sell_plan"] = {"stale": True}


def _next_action_for_reconcile_stage(stage: str) -> str | None:
    if stage in {"settle", "layer_transition", "battle", "game_over"}:
        return "cw.battle.run"
    if stage in {"replenish", "invest", "encounter", "fortune", "event"}:
        return "cw.event.handle"
    if stage in CW_DECISION_TEXT_STAGES:
        return "cw.stage.read"
    return None


def _finalize_reconcile_payload(
    session,
    *,
    stage: str,
    response_data: dict[str, Any],
    attempted: list[str],
    previous_variable_cost_stale: bool,
    next_action: str | None = None,
) -> dict[str, Any]:
    cw_state = ensure_cw_state(session)
    response_data.pop("crystals", None)
    response_data["stage"] = stage
    if (summary := summarize_cw_run_facts(session)) is not None:
        response_data["run_facts"] = summary
    stale_facts = _stale_fact_names(cw_state)
    response_data["stale"] = bool(stale_facts)
    response_data["reconciled"] = _join_fact_names(
        _fresh_reconciled_fact_names(
            cw_state,
            response_data=response_data,
            attempted=attempted,
            previous_variable_cost_stale=previous_variable_cost_stale,
        )
    )
    response_data["stale_facts"] = _join_fact_names(stale_facts)
    if attempted:
        response_data["attempted"] = _join_fact_names(attempted)
    if stale_facts:
        response_data["todo"] = _join_fact_names(stale_facts)
        response_data["why"] = "stale_after_reconcile"
    if next_action:
        response_data["next_action"] = next_action
    return response_data


def _has_fresh_auto_collect_equipment(method: str, result: object) -> bool:
    if method not in {"cw.portal.select", "cw.battle.run", "cw.event.reconcile"} or not isinstance(result, dict):
        return False
    equipment = result.get("equipment")
    if not isinstance(equipment, dict):
        return False
    stale = equipment.get("stale")
    return stale is False or (type(stale) in (int, float) and stale == 0)


def _pop_scene_warnings(data: object) -> tuple[object, list[dict]]:
    if not isinstance(data, dict):
        return data, []
    raw = data.pop("warnings", None)
    return deepcopy(data), deepcopy(raw) if isinstance(raw, list) else []


def _merge_envelope_warnings(envelope: dict, warnings: list[dict]) -> dict:
    if not warnings:
        return envelope
    merged = deepcopy(envelope)
    merged["warnings"] = [*deepcopy(merged.get("warnings") or []), *deepcopy(warnings)]
    return merged


def _current_guide(session, *, artifact_store: ArtifactStore):
    del artifact_store
    return _require_selected_guide(session)


def clear_cw_battle_resume_hint(session) -> dict:
    resume = ensure_cw_state(session).get("battle_resume")
    ordinary_keys = ("in_battle_hint", "start_pending", "start_node", "report", "settlement_actions")
    had_hint = isinstance(resume, dict) and any(resume.get(key) for key in ordinary_keys)
    if isinstance(resume, dict):
        for key in ordinary_keys:
            resume.pop(key, None)
    return {"cleared": had_hint}


def _require_selected_guide(session) -> dict:
    cw_state = session.scene_state.get("cw")
    return require_complete_cw_guide(cw_state if isinstance(cw_state, dict) else {})


def _operation_guide_skill_info(guide: dict | None) -> list[dict[str, str]]:
    if not isinstance(guide, dict):
        return []
    operation_guide = str(guide.get("operation_guide") or "").strip()
    if not operation_guide:
        return []
    return [{"name": "运营思路", "text": operation_guide}]


def _shop_has_fresh_slots(session) -> bool:
    cw_state = session.scene_state.get("cw")
    slots = cw_state.get("slots") if isinstance(cw_state, dict) else None
    if not isinstance(slots, dict) or slots.get("stale", True) is not False:
        return False
    return isinstance(slots.get("front"), list) or isinstance(slots.get("back"), list)


def _shop_status(session, *, artifact_store: ArtifactStore, workspace_root: str, cw_resource_service=None) -> dict:
    del artifact_store
    trait_config = (
        _cached_cw_guide_config(
            workspace_root=workspace_root,
            cw_resource_service=cw_resource_service,
            enrich_traits=True,
        )
        if _shop_has_fresh_slots(session)
        else None
    )
    payload = shop_cw_status(
        session,
        guide_config=trait_config,
        include_field_trait_summary=trait_config is not None,
    )
    cw_state = session.scene_state.get("cw")
    guide_state = cw_state.get("guide") if isinstance(cw_state, dict) else None
    if not isinstance(guide_state, dict):
        payload.pop("guide_summary", None)
    return payload


def _validate_cw_guide_apply_payload(payload: dict) -> None:
    if any(key in payload for key in ("lineup_id", "guide", "artifact", "artifact_id")):
        raise TrailError(
            "CW_GUIDE_APPLY_ARGS_NOT_SUPPORTED",
            "cw guide.apply no longer accepts lineup_id/guide; use guide.fetch.cw --select",
        )


def _apply_selected_guide_via_ui(session, *, runtime, guide: dict | None = None) -> dict:
    selected_guide = guide if guide is not None else _require_selected_guide(session)
    apply_cw_guide_via_ui(runtime, share_code=selected_guide["share_code"])
    invalidate_cw_guide_runtime_state(session)
    return selected_guide


def _response_snapshot_or_fallback(result: object, fallback: object) -> dict[str, Any]:
    snapshot = getattr(result, "response_snapshot", None)
    if isinstance(snapshot, dict):
        return deepcopy(snapshot)
    return deepcopy(fallback) if isinstance(fallback, dict) else {}


def _pop_response_snapshot_warnings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    raw = snapshot.pop("warnings", None)
    return deepcopy(raw) if isinstance(raw, list) else []


def _cw_battle_run_reaches_preparation(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return (
        result.get("status") == "completed"
        and result.get("stage") == "preparation"
        and result.get("in_battle") is not True
    )


CW_DECISION_TEXT_STAGES = frozenset({
    "voyage_companion", "plot_armor", "plot_armor_details", "weapon_box", "chimera_pick", "ranger_pick",
    "replenish", "encounter", "invest", "confirm_leave",
})


# A level-up board can precede a decision page by several seconds.
CW_POST_BATTLE_TRANSITION_BUDGET_SECONDS = 15.0
CW_POST_BATTLE_TRANSITION_MIN_SECONDS = 10.0
CW_POST_BATTLE_TRANSITION_POLL_SECONDS = 0.5


def _has_level_up_overlay(reading):
    for entry in reading.get("entries", []):
        box = entry.get("bounds")
        if ("等级提升" in str(entry.get("text", "")) and box is not None
                and 800 <= (box[0] + box[2]) / 2 <= 1120 and 160 <= (box[1] + box[3]) / 2 <= 260):
            return True
    return False


def _read_post_battle_transition(runtime, *, check_cancelled: Callable[[], None], budget: float, previous_round=None) -> dict[str, Any] | None:
    """Return a decision/unknown reading; None means preparation can proceed."""
    check_cancelled()
    transition_node = None
    node_conflict = False
    start = monotonic()
    clear_count = 0
    last_frame = None
    limit = min(CW_POST_BATTLE_TRANSITION_BUDGET_SECONDS, budget)
    while monotonic() - start < limit:
        check_cancelled()
        try:
            if callable(getattr(runtime, "ocr_image", None)) and callable(getattr(runtime, "capture_image", None)):
                reading, frame = _observe_battle_collection_page(runtime)
                if frame is not None:
                    last_frame = frame
                    reading["_frame"] = frame
            else:
                reading = read_cw_stage_text(runtime)
        except TrailError:
            reading = {"value": None, "entries": []}
        check_cancelled()
        stage = reading.get("value")
        if stage not in {None, "unknown", "preparation"}:
            return {**reading, "_transition_node": transition_node}
        if stage == "preparation":
            current_node = project_cw_preparation_header(reading).get("node")
            if current_node is not None and current_node != previous_round and not node_conflict:
                if transition_node is not None and current_node != transition_node:
                    transition_node, node_conflict = None, True
                else:
                    transition_node = current_node
        clear = stage == "preparation" and not _has_level_up_overlay(reading)
        clear_count = clear_count + 1 if clear else 0
        if (clear_count >= 2 and CW_POST_BATTLE_TRANSITION_MIN_SECONDS <= monotonic() - start < limit):
            return {**reading, "_stable_preparation": True, "_transition_node": transition_node}
        sleep(min(CW_POST_BATTLE_TRANSITION_POLL_SECONDS, max(0.0, limit - (monotonic() - start))))
    return {"value": "unknown", "entries": [], "_frame": last_frame}



def _observe_battle_collection_page(runtime, *, image=None):
    """Read stage and text from one fresh frame before a post-battle input."""
    capture = getattr(runtime, "capture_image", None)
    if not callable(capture) or not callable(getattr(runtime, "ocr_image", None)):
        return {"value": "unknown", "entries": []}, None
    frame = image
    try:
        frame = image if image is not None else capture(normalize=True)
        reading = read_cw_stage_text(runtime, image=frame)
        detected = stage_detector_factory(runtime)(image=frame)
        text_stage = reading.get("value")
        stage = text_stage if text_stage in CW_DECISION_TEXT_STAGES or text_stage == "layer_transition" else detected
        if stage == "shop":
            from trail.scenes.cw.equipment_grid import season_two_shop_overlay
            text = "".join(str(entry.get("text", "")) for entry in reading.get("entries", []))
            if not isinstance(frame, Image.Image) or not season_two_shop_overlay(frame.convert("RGB")) or not any(x in text for x in ("刷新", "收起")):
                stage = "unknown"
        reading["value"] = stage or "unknown"
        return reading, frame
    except (TrailError, TypeError, AttributeError):
        return {"value": "unknown", "entries": []}, frame


def _attach_cw_decision_text(data: dict[str, Any], runtime) -> None:
    if not callable(getattr(runtime, "ocr", None)):
        return
    try:
        reading = read_cw_stage_text(runtime)
    except TrailError as error:
        data.setdefault("warnings", []).append({"code": error.code, "message": str(error)})
    else:
        data["decision"] = reading


def _wait_for_reconcile_shop_close(
    runtime, *, check_cancelled: Callable[[], None],
) -> tuple[str, Image.Image | None]:
    """Observe the close without sending input; keep the frame behind the final stage."""
    detect = stage_detector_factory(runtime)
    capture = getattr(runtime, "capture_image", None)
    keep_frame = (callable(capture)
                  and callable(getattr(runtime, "save_capture_image_to_workspace", None))
                  and _callable_accepts_keyword(detect, "image"))
    deadline = monotonic() + 2.0
    while True:
        check_cancelled()
        frame = None
        try:
            if keep_frame:
                frame = capture(normalize=True)
            stage = (detect(image=frame) if frame is not None else detect()) or "unknown"
        except TrailError:
            stage = "unknown"
        check_cancelled()
        remaining = deadline - monotonic()
        if stage not in {"shop", "unknown"} or remaining <= 0:
            return stage, frame
        sleep(min(0.25, remaining))


def _guard_reconcile_shop_scan(session, previous: object, response: dict[str, Any]) -> dict[str, Any]:
    """An OCR miss is not evidence that a shop card is empty."""
    items = response.get("items")
    rows = items if isinstance(items, list) else []
    known = [row for row in rows if isinstance(row, dict) and row.get("name")
             and (row.get("price") is not None or row.get("cost") is not None)]
    slots = {row.get("slot") for row in known if row.get("slot") in {1, 2, 3, 4, 5}}
    if len(slots) == 5:
        return response
    current = ensure_cw_state(session).get("shop")
    current = dict(current) if isinstance(current, dict) else {}
    response = dict(response)
    if not known and isinstance(previous, dict):
        current["items"] = deepcopy(previous.get("items", []))
        response["items"] = deepcopy(previous.get("items", []))
    current["stale"] = response["stale"] = True
    ensure_cw_state(session)["shop"] = current
    response.setdefault("warnings", []).append({"code": "CW_SHOP_SCAN_UNCERTAIN", "message": "商店部分卡槽未确认"})
    return response


def _collect_cw_preparation_facts(
    session,
    *,
    runtime,
    workspace_root: str | None = None,
    cw_resource_service=None,
    request_id: str | None = None,
    response_data: dict[str, Any] | None = None,
    order: tuple[str, ...] = PORTAL_SELECT_PREP_COLLECT_ORDER,
    previous_slots_snapshot: dict[str, Any] | None = None,
    include_crystals: bool = True,
    reconcile_partial: bool = False,
    check_cancelled: Callable[[], None] = lambda: None,
    guard_battle_inputs: bool = False,
    battle_deadline: float | None = None,
) -> dict[str, Any]:
    data = dict(response_data) if isinstance(response_data, dict) else {}
    slots_reader: SlotsSnapshotReader | None = None
    guide_config: dict[str, Any] | None = None
    slots_snapshot: dict[str, Any] | None = None
    slot_warnings: list[dict[str, Any]] = []
    equipment_snapshot: dict[str, Any] | None = None
    equipment_warnings: list[dict[str, Any]] = []
    shop_snapshot: dict[str, Any] | None = None
    shop_warnings: list[dict[str, Any]] = []
    prep_warnings: list[dict[str, Any]] = []

    def ensure_slots_reader_and_config() -> tuple[SlotsSnapshotReader, dict[str, Any]]:
        nonlocal slots_reader, guide_config
        if slots_reader is None or guide_config is None:
            slots_reader, guide_config = _build_slots_reader_for_service(
                runtime,
                targets=None,
                workspace_root=workspace_root,
                cw_resource_service=cw_resource_service,
                request_id=request_id,
                dismiss_initial_overlay=False,
                **({"collapse_expanded_hand": False} if guard_battle_inputs else {}),
            )
        return slots_reader, guide_config

    def guard(expected: str, *, control: str | None = None):
        if not guard_battle_inputs:
            return None
        check_cancelled()
        reading, frame = _observe_battle_collection_page(runtime)
        stage = reading.get("value") or "unknown"
        control_entry = next((entry for entry in reading.get("entries", [])
                            if control and control in str(entry.get("text", "")) and entry.get("bounds") is not None
                            and 1500 <= (entry["bounds"][0] + entry["bounds"][2]) / 2 <= 1720
                            and 920 <= (entry["bounds"][1] + entry["bounds"][3]) / 2 <= 1040), None)
        check_cancelled()
        within_budget = battle_deadline is None or monotonic() < battle_deadline
        if (stage == expected and within_budget and not _has_level_up_overlay(reading)
                and (control is None or control_entry is not None)):
            return control_entry
        if stage == expected:
            stage = "unknown"
            reading["value"] = stage
        mark_cw_event_unknown_stale(session)
        detect_cw_stage(session, detector=lambda: stage)
        if stage == "unknown":
            mark_cw_stage_stale(session)
        previous_node = (data.get("current_node") or {}).get("node")
        data.update(stage=stage, stale=True,
                    next_action="cw.event.reconcile" if stage == "unknown" else "cw.stage.read",
                    current_node=project_cw_preparation_header(reading))
        if stage in CW_DECISION_TEXT_STAGES:
            if data["current_node"]["node"] is None and previous_node != data.get("round"):
                data["current_node"]["node"] = previous_node
            data["decision"] = reading
        data.pop("_screenshot", None)
        if frame is not None:
            data["_reconcile_frame"] = frame
        raise TrailError("CW_PREP_STAGE_CHANGED", "战后页面未确认处于预期阶段")

    crystal_observation = [None, None]

    def verify_crystal_frame(image):
        check_cancelled()
        reading, frame = _observe_battle_collection_page(runtime, image=image)
        check_cancelled()
        if (_has_level_up_overlay(reading) or (battle_deadline is not None and monotonic() >= battle_deadline)) and reading.get("value") == "preparation":
            reading["value"] = "unknown"
        crystal_observation[:] = [reading, frame]
        return reading.get("value") or "unknown"

    def collect_crystals() -> None:
        guard("preparation")
        collector = (_call_with_supported_keywords(crystal_collector_factory, runtime, verify_frame=verify_crystal_frame)
                     if guard_battle_inputs else crystal_collector_factory(runtime))
        collect_cw_crystals(session, collector=collector)

    def collect_slots() -> None:
        nonlocal slots_snapshot, slot_warnings
        if not guard_battle_inputs:
            dismiss_cw_slots_overlay(runtime)
        reader, config = ensure_slots_reader_and_config()
        try:
            slots_result = read_cw_slots(
                session,
                reader=reader,
                guide_config=config,
            )
            slots_snapshot = _response_snapshot_or_fallback(slots_result, ensure_cw_state(session).get("slots") or {})
            slots_screenshot = slots_snapshot.pop("_screenshot", None)
            if slots_screenshot is not None and data.get("_screenshot") is None:
                data["_screenshot"] = slots_screenshot
            slot_warnings = _pop_response_snapshot_warnings(slots_snapshot)
        except TrailError as error:
            if error.code != "SLOTS_RECOGNITION_UNCERTAIN":
                raise
            error_screenshot = getattr(error, "screenshot", None)
            if error_screenshot is not None and data.get("_screenshot") is None:
                data["_screenshot"] = error_screenshot
            warning = _cw_slots_auto_collect_uncertain_warning(error)
            previous_slots = _fresh_previous_slots_snapshot(session) or previous_slots_snapshot
            if previous_slots is not None:
                slots_snapshot = previous_slots
                warning["preserved_previous"] = 1
            slot_warnings = [warning]

    def collect_equipment() -> None:
        nonlocal equipment_snapshot, equipment_warnings
        try:
            equipment_snapshot = deepcopy(
                _apply_cw_equipment_read_with_resources(
                    session,
                    runtime,
                    workspace_root=workspace_root,
                    cw_resource_service=cw_resource_service,
                )
            )
        except Exception as error:
            equipment_warnings.append(_cw_equipment_auto_collect_warning(error))

    def collect_shop() -> None:
        nonlocal shop_snapshot, shop_warnings
        _reader, config = ensure_slots_reader_and_config()
        check_cancelled()
        open_entry = guard("preparation", control="商店")
        if guard_battle_inputs:
            left, top, right, bottom = open_entry["bounds"]
            open_cw_shop(session, opener=lambda: runtime.click_point(int((left + right) / 2), int((top + bottom) / 2)))
        else:
            open_cw_shop(session, opener=shop_opener_factory(runtime))
        sleep(SHOP_SCAN_OPEN_SETTLE_SECONDS)
        previous_shop = deepcopy(ensure_cw_state(session).get("shop")) if reconcile_partial else None
        shop_result = scan_cw_shop(
            session,
            scanner=(_call_with_supported_keywords(shop_page_snapshot_reader_factory, runtime, recover_missing=True,
                                                   check_cancelled=check_cancelled) if reconcile_partial
                     else shop_page_snapshot_reader_factory(runtime)),
            guide_config=config,
        )
        shop_projection = project_cw_shop_snapshot(session)
        shop_response = _response_snapshot_or_fallback(shop_result, {})
        if reconcile_partial:
            shop_response = _guard_reconcile_shop_scan(session, previous_shop, shop_response)
            shop_projection = project_cw_shop_snapshot(session)
        shop_warnings = _pop_response_snapshot_warnings(shop_response)
        candidate = {**shop_projection, **shop_response}
        check_cancelled()
        close_entry = guard("shop", control="收起")
        if reconcile_partial:
            close_stage = None
            try:
                close_cw_shop(session, closer=shop_closer_factory(runtime))
                close_stage, close_frame = _wait_for_reconcile_shop_close(runtime, check_cancelled=check_cancelled)
                if close_stage != "preparation":
                    data["_collection_stage"] = close_stage
                    data["_collection_frame"] = close_frame
                    raise TrailError("CW_SHOP_CLOSE_UNCONFIRMED", "未确认商店已关闭并返回备战")
            except TrailError:
                shop = ensure_cw_state(session).get("shop")
                if isinstance(shop, dict):
                    opened = None if close_stage in {None, "unknown"} else close_stage == "shop"
                    shop.update(opened=opened, stale=True)
                raise
            candidate.update(opened=False)
            ensure_cw_state(session)["shop"] = deepcopy(candidate)
        else:
            if guard_battle_inputs:
                left, top, right, bottom = close_entry["bounds"]
                close_cw_shop(session, closer=lambda: runtime.click_point(int((left + right) / 2), int((top + bottom) / 2)))
            else:
                close_cw_shop(session, closer=shop_closer_factory(runtime))
        shop_snapshot = candidate

    actions: dict[str, Callable[[], None]] = {
        "slots": collect_slots,
        "equipment": collect_equipment,
        "shop": collect_shop,
    }
    if include_crystals:
        actions["crystals"] = collect_crystals
    attempted_steps = []
    for step in order:
        action = actions.get(step)
        if action is not None:
            check_cancelled()
            attempted_steps.append(step)
            try:
                action()
            except TrailError as error:
                if not reconcile_partial and (response_data is None or response_data.get("status") != "completed"):
                    raise
                if reconcile_partial:
                    data["_collection_failed"] = True
                    if step == "shop":
                        shop = ensure_cw_state(session).get("shop")
                        if isinstance(shop, dict):
                            shop["stale"] = True
                prep_warnings.append({"code": "CW_PREP_COLLECT_FAILED", "detail_code": error.code,
                                      "step": step, "message": str(error)})
                break
        if step == "crystals" and include_crystals:
            metrics = ensure_cw_state(session).get("metrics") or {}
            if metrics.get("last_crystal_collection") == "blocked":
                data["stage"] = metrics["stage"]
                data["stale"] = metrics["stage"] == "unknown"
                data["next_action"] = "cw.event.reconcile" if data["stage"] == "unknown" else "cw.stage.read"
                if guard_battle_inputs:
                    mark_cw_event_unknown_stale(session)
                    detect_cw_stage(session, detector=lambda: data["stage"])
                    if data["stage"] == "unknown":
                        mark_cw_stage_stale(session)
                    data["stale"] = True
                reading, frame = crystal_observation
                if guard_battle_inputs and isinstance(reading, dict):
                    data.pop("_screenshot", None)
                    if frame is not None:
                        data["_reconcile_frame"] = frame
                    if data["stage"] in CW_DECISION_TEXT_STAGES:
                        data["decision"] = reading
                else:
                    _attach_cw_decision_text(data, runtime)
                    reading = data.get("decision")
                previous_node = (data.get("current_node") or {}).get("node")
                if isinstance(reading, dict) and reading.get("value") == data["stage"]:
                    data["current_node"] = project_cw_preparation_header(reading)
                    if (data["stage"] in CW_DECISION_TEXT_STAGES and data["current_node"]["node"] is None
                            and previous_node != data.get("round")):
                        data["current_node"]["node"] = previous_node
                else:
                    data["current_node"] = {"stage": data["stage"], "node": None, "hp": None}
                break

    if (data.get("stage") == "preparation" and callable(getattr(runtime, "ocr", None))
            and response_data is not None and response_data.get("status") == "completed"):
        try:
            if guard_battle_inputs:
                reading, frame = _observe_battle_collection_page(runtime)
                current = project_cw_preparation_header(reading)
                if frame is not None and current["stage"] == "preparation":
                    data.pop("_screenshot", None)
                    data["_reconcile_frame"] = frame
            else:
                current = read_cw_preparation_header(runtime)
                reading, frame = None, None
            previous_node = (data.get("current_node") or {}).get("node")
            if (guard_battle_inputs and current["stage"] in CW_DECISION_TEXT_STAGES and current["node"] is None
                    and previous_node != data.get("round")):
                current["node"] = previous_node
            data["current_node"] = current
            if guard_battle_inputs and current["stage"] != "preparation":
                stage = current["stage"]
                mark_cw_event_unknown_stale(session)
                detect_cw_stage(session, detector=lambda: stage)
                if stage == "unknown":
                    mark_cw_stage_stale(session)
                data.update(stage=stage, stale=True, next_action="cw.event.reconcile" if stage == "unknown" else "cw.stage.read")
                data.pop("_screenshot", None)
                if frame is not None:
                    data["_reconcile_frame"] = frame
                if stage in CW_DECISION_TEXT_STAGES:
                    data["decision"] = reading
                slots_snapshot = equipment_snapshot = shop_snapshot = None
            elif current["stage"] in CW_DECISION_TEXT_STAGES:
                data["stage"] = current["stage"]
                data["next_action"] = "cw.stage.read"
                data.pop("_screenshot", None)
                slots_snapshot = equipment_snapshot = shop_snapshot = None
                mark_cw_event_unknown_stale(session)
                detect_cw_stage(session, detector=lambda: current["stage"])
                _attach_cw_decision_text(data, runtime)
        except TrailError as error:
            prep_warnings.append({"code": "CW_PREP_COLLECT_FAILED", "detail_code": error.code,
                                  "step": "current_node", "message": str(error)})
    if guard_battle_inputs and data.get("stage") != "preparation":
        slots_snapshot = equipment_snapshot = shop_snapshot = None
        data.pop("skill_info", None)
        data.pop("_screenshot", None)
    if reconcile_partial:
        data["_attempted_steps"] = attempted_steps
    if include_crystals:
        data["crystals"] = deepcopy(ensure_cw_state(session).get("metrics") or {})
    if slots_snapshot is not None:
        data["slots"] = slots_snapshot
    if equipment_snapshot is not None:
        data["equipment"] = deepcopy(equipment_snapshot)
    if shop_snapshot is not None:
        data["shop"] = shop_snapshot
    response_warnings = [*slot_warnings, *equipment_warnings, *shop_warnings, *prep_warnings]
    if response_warnings:
        existing_warnings = data.get("warnings")
        data["warnings"] = [
            *(deepcopy(existing_warnings) if isinstance(existing_warnings, list) else []),
            *response_warnings,
        ]
    return data


def _select_portal_and_apply_selected_guide(
    session,
    *,
    runtime,
    card_idx: int,
    guide: dict[str, Any] | None = None,
    workspace_root: str | None = None,
    cw_resource_service=None,
    request_id: str | None = None,
) -> dict[str, Any]:
    selected_guide = guide if guide is not None else _require_selected_guide(session)
    selected = select_cw_portal(session, card_idx=card_idx, runtime=runtime)
    wait_cw_portal_preparation(session, runtime=runtime)
    record_cw_investment(session, selected, kind="portal")
    previous_slots_snapshot = _fresh_previous_slots_snapshot(session)
    try:
        _apply_selected_guide_via_ui(session, runtime=runtime, guide=selected_guide)
    except Exception as error:
        raise CwSideEffectAppliedError("cw.portal.select guide apply side effect already ran") from error
    selected_data = dict(selected)
    skill_info = _operation_guide_skill_info(selected_guide)
    if skill_info:
        selected_data["skill_info"] = skill_info
    return _collect_cw_preparation_facts(
        session,
        runtime=runtime,
        workspace_root=workspace_root,
        cw_resource_service=cw_resource_service,
        request_id=request_id,
        response_data=selected_data,
        order=PORTAL_SELECT_PREP_COLLECT_ORDER,
        previous_slots_snapshot=previous_slots_snapshot,
    )


def _start_cw(
    session,
    *,
    runtime,
    mode: str,
    difficulty: str,
    battle_mode: str,
    workspace_root: str | None = None,
    cw_resource_service=None,
    check_cancelled: Callable[[], None] = lambda: None,
) -> dict:
    refreshed = _call_with_supported_keywords(
        start_cw,
        session,
        mode=mode,
        difficulty=difficulty,
        battle_mode=battle_mode,
        runtime=runtime,
        check_cancelled=check_cancelled,
    )
    entry_state = ensure_cw_state(refreshed).get("entry")
    cards = summarize_portal_cards(
        runtime.ocr(),
        _cached_cw_guide_config(workspace_root=workspace_root, cw_resource_service=cw_resource_service).get(
            "portal_list", []
        ),
        collection_matches=detect_portal_collection_matches(runtime),
    )
    portal_snapshot = {
        "cards": _attach_guides_to_cards(cards, workspace_root=workspace_root),
        "mode": entry_state.get("mode") if isinstance(entry_state, dict) else None,
        "difficulty": entry_state.get("difficulty") if isinstance(entry_state, dict) else None,
        "battle_mode": entry_state.get("battle_mode") if isinstance(entry_state, dict) else None,
        "stale": False,
    }
    ensure_cw_state(refreshed)["portal"] = portal_snapshot
    return portal_snapshot


def _restart_cw(
    session, *, runtime, workspace_root: str | None = None, cw_resource_service=None,
    check_cancelled: Callable[[], None] = lambda: None,
) -> dict:
    entry_state = ensure_cw_state(session).get("entry") if isinstance(ensure_cw_state(session).get("entry"), dict) else {}
    portal_state = ensure_cw_state(session).get("portal") if isinstance(ensure_cw_state(session).get("portal"), dict) else {}
    mode = entry_state.get("mode") if entry_state.get("mode") is not None else portal_state.get("mode")
    difficulty = entry_state.get("difficulty") if entry_state.get("difficulty") is not None else portal_state.get("difficulty")
    battle_mode = entry_state.get("battle_mode") if entry_state.get("battle_mode") is not None else portal_state.get("battle_mode")
    if not isinstance(mode, str) or not isinstance(difficulty, str) or not isinstance(battle_mode, str):
        raise TrailError("CW_PORTAL_ENTRY_TRUTH_REQUIRED", "cw portal.restart requires recorded mode/difficulty/battle_mode")

    select_cw_portal(session, card_idx=1, runtime=runtime)
    wait_cw_portal_in_game(session, runtime=runtime)
    restart_cw_portal_to_settlement_entry(session, runtime=runtime)
    return _start_cw(
        session,
        runtime=runtime,
        mode="continue",
        difficulty=difficulty,
        battle_mode=battle_mode,
        workspace_root=workspace_root,
        cw_resource_service=cw_resource_service,
        check_cancelled=check_cancelled,
    )


def _attach_guides_to_cards(
    cards: list[dict[str, object]],
    *,
    timeout: int = 10,
    workspace_root: str | None = None,
    allow_network: bool = False,
) -> list[dict[str, object]]:
    if not allow_network:
        return cards

    portal_titles: list[str] = []
    seen_titles: set[str] = set()
    for card in cards:
        title = card.get("portal_title")
        if not isinstance(title, str) or not title or title in seen_titles:
            continue
        seen_titles.add(title)
        portal_titles.append(title)

    if not portal_titles:
        return cards

    try:
        guide_payload = fetch_cw_guide_list(
            page=1,
            limit=3,
            trait_id=None,
            order=None,
            next_page_token=None,
            match_change_job=None,
            match_hard=None,
            portal=portal_titles if len(portal_titles) > 1 else portal_titles[0],
            timeout=timeout,
            workspace_root=workspace_root,
        )
    except TrailError:
        return cards

    guides_by_portal: dict[str, list[dict[str, object]]] = {}
    portal_groups = guide_payload.get("portals")
    if isinstance(portal_groups, list):
        for group in portal_groups:
            if not isinstance(group, dict):
                continue
            title = group.get("portal_title")
            guides = group.get("list")
            if isinstance(title, str) and isinstance(guides, list):
                guides_by_portal[title] = guides[:3]
    else:
        title = portal_titles[0]
        guides = guide_payload.get("list")
        if isinstance(guides, list):
            guides_by_portal[title] = guides[:3]

    enriched: list[dict[str, object]] = []
    for card in cards:
        enriched_card = dict(card)
        title = card.get("portal_title")
        guides = guides_by_portal.get(title) if isinstance(title, str) else None
        if guides:
            enriched_card["guides"] = guides
        enriched.append(enriched_card)
    return enriched


def _attach_guides_to_portal_snapshot(
    session,
    snapshot: dict[str, object],
    *,
    timeout: int = 10,
    workspace_root: str | None = None,
    allow_network: bool = False,
) -> dict[str, object]:
    enriched = {
        **snapshot,
        "cards": _attach_guides_to_cards(
            snapshot.get("cards", []),
            timeout=timeout,
            workspace_root=workspace_root,
            allow_network=allow_network,
        ),
    }
    ensure_cw_state(session)["portal"] = enriched
    return enriched


def _safe_capture_after_action(runtime) -> str | None:
    capture_after_action = getattr(runtime, "capture_after_action", None)
    if not callable(capture_after_action):
        return None
    try:
        return capture_after_action(optional=False)
    except Exception:
        return None


def _safe_match_references(runtime, *, screenshot) -> list[dict]:
    if screenshot is None:
        return []
    match_references = getattr(runtime, "match_references", None)
    if not callable(match_references):
        return []
    try:
        references = match_references(screenshot) or []
    except Exception:
        return []
    return to_jsonable(references) if isinstance(references, list) else []


def _safe_collect_warnings(runtime) -> list[dict]:
    collect_warnings = getattr(runtime, "collect_warnings", None)
    if not callable(collect_warnings):
        return []
    try:
        warnings = collect_warnings() or []
    except Exception:
        return []
    return to_jsonable(warnings) if isinstance(warnings, list) else []


def _safe_collect_debug(runtime, *, verbose: bool) -> dict | None:
    if runtime is None or not verbose:
        return None

    debug: dict = {}
    consume_debug_trace = getattr(runtime, "consume_debug_trace", None)
    if callable(consume_debug_trace):
        try:
            trace = consume_debug_trace() or []
        except Exception:
            trace = []
        if isinstance(trace, list) and trace:
            debug["trace"] = to_jsonable(trace)

    consume_debug_context = getattr(runtime, "consume_debug_context", None)
    if callable(consume_debug_context):
        try:
            context = consume_debug_context() or {}
        except Exception:
            context = {}
        if isinstance(context, dict):
            filtered_context = {
                    key: value
                    for key, value in context.items()
                    if key not in {"trace", "request_id", "detail"}
            }
            debug.update(to_jsonable(filtered_context))

    return to_jsonable(debug) or None


def _unknown_result_envelope(
    error: Exception,
    *,
    last_known_stage: str | None = None,
    screenshot: str | None = None,
    warnings: list[dict] | None = None,
    references: list[dict] | None = None,
    debug: dict | None = None,
) -> dict:
    debug_payload = to_jsonable(debug or {})
    debug_payload["detail"] = _format_exception_detail(error)
    error_data = _error_data(error)
    if error_data:
        debug_payload["error_data"] = error_data
    if isinstance(last_known_stage, str) and last_known_stage:
        debug_payload["last_known_stage"] = last_known_stage
    payload = {
        "ok": False,
        "data": {},
        "screenshot": screenshot,
        "timing": {},
        "warnings": to_jsonable(warnings or []),
        "references": to_jsonable(references or []),
        "debug": debug_payload,
        "error": {
            "code": "DAEMON_UNAVAILABLE",
            "message": "mutation result unknown",
        },
    }
    guidance = build_image_guidance(screenshot)
    if guidance is not None:
        payload["image_guidance"] = guidance
    return payload


def _unknown_result_envelope_from_response(
    error: Exception,
    *,
    response: dict | None,
    last_known_stage: str,
) -> dict:
    previous = to_jsonable(response or {})
    screenshot = previous.get("screenshot")
    debug_payload = to_jsonable(previous.get("debug") or {})
    debug_payload["detail"] = _format_exception_detail(error)
    error_data = _error_data(error)
    if error_data:
        debug_payload["error_data"] = error_data
    debug_payload["last_known_stage"] = last_known_stage
    payload = {
        "ok": False,
        "data": {},
        "screenshot": screenshot,
        "timing": to_jsonable(previous.get("timing") or {}),
        "warnings": to_jsonable(previous.get("warnings") or []),
        "references": to_jsonable(previous.get("references") or []),
        "debug": debug_payload,
        "error": {
            "code": "DAEMON_UNAVAILABLE",
            "message": "mutation result unknown",
        },
    }
    guidance = build_image_guidance(screenshot)
    if guidance is not None:
        payload["image_guidance"] = guidance
    return payload


def _format_exception_detail(error: Exception) -> str:
    return format_exception_detail(error)


def _safe_error_attr(error: Exception, name: str):
    try:
        return getattr(error, name, None)
    except Exception:
        return None


def _error_data(error: Exception) -> dict:
    raw_data = _safe_error_attr(error, "data")
    data = to_jsonable(raw_data) if isinstance(raw_data, dict) else {}
    tainted = _safe_error_attr(error, "tainted")
    if tainted is not None and "tainted" not in data:
        data["tainted"] = bool(tainted)
    return data


class CwSideEffectAppliedError(Exception):
    pass
