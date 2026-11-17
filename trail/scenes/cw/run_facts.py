from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from trail.scenes.cw.models import ensure_cw_state


def start_cw_run_facts(session, *, preserve_battle_resume: bool = False) -> dict:
    facts = {
        "run_id": uuid4().hex,
        "opening": None,
        "investment_environment": None,
        "investment_strategies": [],
        "investment_history_incomplete": False,
        "plot_armor": {"status": "unverified", "cards": [], "reselect_remaining": None},
        "plot_armor_history": [],
    }
    if not preserve_battle_resume:
        ensure_cw_state(session).pop("battle_resume", None)
    ensure_cw_state(session)["run_facts"] = facts
    return facts


def cw_run_facts(session) -> dict:
    facts = ensure_cw_state(session).get("run_facts")
    if isinstance(facts, dict):
        return facts
    facts = start_cw_run_facts(session, preserve_battle_resume=True)
    facts["investment_history_incomplete"] = True
    return facts


def record_cw_opening(session, opening: dict) -> None:
    facts = ensure_cw_state(session).get("run_facts")
    # The pre-portal overview belongs to a new run once a portal was selected.
    if not isinstance(facts, dict) or facts["investment_environment"] is not None or facts.get("ended"):
        facts = start_cw_run_facts(session)
    previous = facts.get("opening") or {}
    opening = deepcopy(opening)
    previous_status = previous.get("affixes_status")
    new_status = opening.get("affixes_status")
    if previous_status != "observed" and previous_status != "partial":
        facts["opening"] = opening
        return

    previous_rows = {row["name"]: row for row in previous.get("affixes", [])}
    new_rows = {row["name"]: row for row in opening.get("affixes", [])}
    if new_status == "observed":
        names = new_rows
    elif previous_status == "observed":
        names = previous_rows
        opening["affixes_status"] = "observed"
    else:
        names = {**previous_rows, **new_rows}
        opening["affixes_status"] = "partial"
    opening["affixes"] = [
        deepcopy(new_rows[name] if name in new_rows and new_rows[name].get("description")
                 else previous_rows.get(name, row))
        for name, row in names.items()
    ]
    facts["opening"] = opening


def record_cw_investment(session, selected: dict, *, kind: str, node: str | None = None) -> None:
    if kind == "portal":
        facts = ensure_cw_state(session).get("run_facts")
        if not isinstance(facts, dict) or facts["investment_environment"] is not None or facts.get("ended"):
            facts = start_cw_run_facts(session)
        facts["investment_environment"] = {
            "name": selected.get("portal_title") or None,
            "description": selected.get("portal_description") or None,
        }
        return
    facts = cw_run_facts(session)
    facts["investment_strategies"].append({
        "node": node,
        "name": selected.get("strategy_title") or None,
        "description": selected.get("strategy_description") or None,
    })
    if not selected.get("strategy_title"):
        facts["investment_history_incomplete"] = True


def record_cw_plot_armor(session, reading: dict) -> None:
    facts = cw_run_facts(session)
    history = facts["plot_armor_history"]
    snapshot = deepcopy(reading)
    verified = snapshot.pop("_reselect_verified", False) is True
    if snapshot["status"] == "observed":
        keys = ("cards", "reselect_remaining")
        if not history or any(snapshot[key] != history[-1][key] for key in keys):
            history.append(snapshot)
    current = deepcopy(snapshot)
    if verified and type(snapshot.get("reselect_remaining")) is int:
        current["_reselect_verified"] = True
    facts["plot_armor"] = current


def project_cw_run_facts(facts: dict) -> dict:
    projected = deepcopy(facts)
    armor = projected.get("plot_armor")
    if isinstance(armor, dict):
        if armor.pop("_reselect_verified", False) is not True:
            armor["reselect_remaining"] = None
    return projected


def mark_cw_plot_armor_unverified(session) -> None:
    cw_run_facts(session)["plot_armor"] = {
        "status": "unverified", "cards": [], "reselect_remaining": None,
    }


def summarize_cw_run_facts(session) -> dict | None:
    facts = ensure_cw_state(session).get("run_facts")
    if not isinstance(facts, dict):
        return None
    facts = project_cw_run_facts(facts)
    opening = facts["opening"] or {}
    environment = facts["investment_environment"] or {}
    armor = facts["plot_armor"]
    affixes_status = opening.get("affixes_status", "unknown")
    affixes = [row["name"] for row in opening.get("affixes", [])]
    return {
        "run_id": facts["run_id"],
        "negative_affixes": affixes if affixes_status == "observed" else None,
        "negative_affixes_status": affixes_status,
        **({"negative_affixes_observed": affixes} if affixes_status == "partial" else {}),
        "investment_environment": environment.get("name"),
        "investment_strategies": [
            {"name": row["name"], "node": row["node"]} for row in facts["investment_strategies"]
        ],
        "investment_history_incomplete": facts["investment_history_incomplete"],
        "plot_armor": {
            "status": armor["status"],
            "reselect_remaining": armor["reselect_remaining"],
            "cards": [
                {key: row[key] for key in ("slot", "name", "role", "status")}
                for row in armor["cards"]
            ],
        },
    }
