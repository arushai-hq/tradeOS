"""
HAWK — Consensus Scoring Engine.

Combines picks from multiple LLM models into a scored consensus.
Pure logic — no I/O, no LLM calls.

Consensus scoring:
  - Each model vote = 1 point for (symbol, direction)
  - Conviction weighting: HIGH=3, MEDIUM=2, LOW=1
  - consensus_score = model_votes × avg_conviction_weight
  - Tags: UNANIMOUS (all), STRONG (≥75%), MAJORITY (≥50%), SINGLE (1)
"""
from __future__ import annotations

import math
from collections import defaultdict

import structlog

log = structlog.get_logger()

CONVICTION_WEIGHT: dict[str, int] = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _avg_conviction_label(avg_weight: float) -> str:
    """Map average conviction weight back to a label."""
    if avg_weight >= 2.5:
        return "HIGH"
    elif avg_weight >= 1.5:
        return "MEDIUM"
    return "LOW"


def _consensus_tag(votes: int, successful: int) -> str:
    """Assign consensus tag based on vote count vs successful model count."""
    if successful < 2:
        return "SINGLE"
    if votes == successful:
        return "UNANIMOUS"
    if votes >= math.ceil(successful * 0.75):
        return "STRONG"
    if votes >= math.ceil(successful * 0.5):
        return "MAJORITY"
    return "SINGLE"


def build_consensus(model_results: list[dict], total_models: int) -> dict:
    """
    Build consensus from multiple model results.

    Args:
        model_results: List of dicts, each with:
            - "name": str (display name)
            - "model_id": str (OpenRouter model slug)
            - "picks": list[dict] (watchlist from that model)
            - "metadata": dict (tokens, cost, elapsed, etc.)
        total_models: Total models attempted (including failures).

    Returns:
        Full consensus structure with consensus_picks, per_model, metadata.
    """
    models_used = [m["name"] for m in model_results]
    successful = len(model_results)

    # Empty case: all models failed
    if successful == 0:
        return {
            "mode": "consensus",
            "models_used": [],
            "models_failed": [],
            "total_models": total_models,
            "consensus_picks": [],
            "per_model": {},
            "aggregate_metadata": {
                "total_cost_usd": 0,
                "total_tokens_input": 0,
                "total_tokens_output": 0,
                "total_elapsed_s": 0,
            },
        }

    # --- Collect votes per (symbol, direction) ---
    # votes[(SYM, DIR)] = list of {model_name, conviction, entry_zone, support, resistance, reasoning, risk_flag}
    votes: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for mr in model_results:
        for pick in mr["picks"]:
            sym = pick.get("symbol", "").upper()
            direction = pick.get("direction", "").upper()
            if not sym or direction not in ("LONG", "SHORT"):
                continue
            votes[(sym, direction)].append({
                "model": mr["name"],
                "conviction": pick.get("conviction", "LOW").upper(),
                "entry_zone": pick.get("entry_zone", [0, 0]),
                "support": pick.get("support", 0),
                "resistance": pick.get("resistance", 0),
                "reasoning": pick.get("reasoning", ""),
                "risk_flag": pick.get("risk_flag"),
            })

    # --- Score and rank ---
    scored: list[dict] = []
    for (sym, direction), model_votes_list in votes.items():
        n_votes = len(model_votes_list)
        weights = [CONVICTION_WEIGHT.get(v["conviction"], 1) for v in model_votes_list]
        avg_weight = sum(weights) / len(weights)
        score = n_votes * avg_weight

        # Average entry zones (exclude [0, 0] defaults)
        valid_zones = [v["entry_zone"] for v in model_votes_list
                       if isinstance(v["entry_zone"], list) and len(v["entry_zone"]) == 2
                       and (v["entry_zone"][0] != 0 or v["entry_zone"][1] != 0)]
        if valid_zones:
            avg_ez = [
                sum(z[0] for z in valid_zones) / len(valid_zones),
                sum(z[1] for z in valid_zones) / len(valid_zones),
            ]
        else:
            avg_ez = [0, 0]

        # Average support/resistance
        valid_supports = [v["support"] for v in model_votes_list if v["support"]]
        valid_resistances = [v["resistance"] for v in model_votes_list if v["resistance"]]
        avg_support = sum(valid_supports) / len(valid_supports) if valid_supports else 0
        avg_resistance = sum(valid_resistances) / len(valid_resistances) if valid_resistances else 0

        # Reasoning summary
        reasoning_parts = []
        for v in model_votes_list:
            r = v["reasoning"][:40] if v["reasoning"] else ""
            if r:
                reasoning_parts.append(f"{v['model']}: {r}")
        reasoning_summary = " | ".join(reasoning_parts)

        # Per-model detail for this pick
        per_model_detail = [
            {
                "model": v["model"],
                "conviction": v["conviction"],
                "entry_zone": v["entry_zone"],
                "reasoning": v["reasoning"],
            }
            for v in model_votes_list
        ]

        # Collect risk flags (unique, non-None)
        risk_flags = list({v["risk_flag"] for v in model_votes_list if v["risk_flag"]})

        # Entry zones per model (keyed by name)
        entry_zones_by_model = {v["model"]: v["entry_zone"] for v in model_votes_list}

        scored.append({
            "symbol": sym,
            "direction": direction,
            "consensus_tag": _consensus_tag(n_votes, successful),
            "model_votes": n_votes,
            "consensus_score": round(score, 1),
            "avg_conviction": _avg_conviction_label(avg_weight),
            "entry_zone": [round(avg_ez[0], 2), round(avg_ez[1], 2)],
            "avg_entry_zone": [round(avg_ez[0], 2), round(avg_ez[1], 2)],
            "entry_zones": entry_zones_by_model,
            "support": round(avg_support, 2),
            "resistance": round(avg_resistance, 2),
            "reasoning_summary": reasoning_summary,
            "risk_flag": risk_flags[0] if len(risk_flags) == 1 else (", ".join(risk_flags) if risk_flags else None),
            "per_model_detail": per_model_detail,
        })

    # Sort by score desc, then votes desc, then symbol alphabetically (tiebreaker)
    scored.sort(key=lambda x: (-x["consensus_score"], -x["model_votes"], x["symbol"]))

    # Assign ranks
    for i, pick in enumerate(scored, 1):
        pick["rank"] = i

    # --- Per-model summary ---
    per_model: dict[str, dict] = {}
    for mr in model_results:
        meta = mr.get("metadata", {})
        per_model[mr["name"]] = {
            "name": mr["name"],
            "model_id": mr["model_id"],
            "picks_count": len(mr["picks"]),
            "picks": mr["picks"],
            "tokens": meta.get("tokens_input", 0) + meta.get("tokens_output", 0),
            "cost": meta.get("cost_usd", 0),
            "elapsed_s": meta.get("elapsed_s", 0),
            "metadata": meta,
        }

    # --- Aggregate metadata ---
    total_cost = sum(mr.get("metadata", {}).get("cost_usd", 0) for mr in model_results)
    total_in = sum(mr.get("metadata", {}).get("tokens_input", 0) for mr in model_results)
    total_out = sum(mr.get("metadata", {}).get("tokens_output", 0) for mr in model_results)
    total_elapsed = sum(mr.get("metadata", {}).get("elapsed_s", 0) for mr in model_results)

    return {
        "mode": "consensus",
        "models_used": models_used,
        "models_failed": [],  # Caller sets this from its own tracking
        "total_models": total_models,
        "consensus_picks": scored,
        "per_model": per_model,
        "aggregate_metadata": {
            "total_cost_usd": round(total_cost, 4),
            "total_tokens_input": total_in,
            "total_tokens_output": total_out,
            "total_elapsed_s": round(total_elapsed, 2),
        },
    }
