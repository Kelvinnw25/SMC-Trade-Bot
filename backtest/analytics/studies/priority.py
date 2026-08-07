"""Study 6 — the research ranking. M16.

This is the only output that says what to look at next, and it is deliberately
built from the other studies' numbers rather than written by hand: a priority
list assembled by judgement is an opinion with citations bolted on, and the
citations are the part that matters.

## The tiering rule, declared before the data

    Priority 1  significant after correction, both interval methods agree,
                effect at or above the practical floor, and the finding is
                stable across partitions where stability was measured
    Priority 2  a real signal that is missing one of those — significant but
                immaterial, material but unstable, or descriptive only
    Priority 3  measured and found to be nothing, or too sparse to test.
                Listed explicitly, because knowing where *not* to spend effort
                is the cheaper half of research

Every entry carries sample size, effect size, interval and expected impact.
`expected_impact_r` is the arithmetic consequence of the measured effect on the
run's own population — `effect x n` — and nothing more. It is not a forecast: it
says what the difference was worth over this window, which is the only thing the
evidence supports.

## What is deliberately absent

No parameter values, no thresholds, no weights. Phase 3 was asked to find *why*
and to rank *what to investigate*. Choosing a change is a decision for a person
holding this evidence, and a file that proposed one would be doing that person's
job badly.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["build"]

#: The practical floor in R, shared with the factor classifier.
MATERIAL_R = 0.02


def build(studies: dict[str, Any], research: dict[str, Any],
          scored_n: int) -> tuple[dict[str, Any], str]:
    """Assemble the ranking from the other studies' payloads."""
    findings: list[dict[str, Any]] = []
    findings += _from_hypotheses(research)
    findings += _from_factors(studies.get("score_component_analysis") or {})
    findings += _from_direction(studies.get("direction_analysis") or {})
    findings += _from_risk(studies.get("risk_analysis") or {})
    findings += _from_drawdown(studies.get("drawdown_analysis") or {})

    tiers = {1: [], 2: [], 3: []}
    for finding in findings:
        tiers[finding["priority"]].append(finding)
    for entries in tiers.values():
        entries.sort(key=lambda f: -abs(f.get("expected_impact_r") or 0.0))

    payload = {
        "source": "derived",
        "study": "strategy_research_priority",
        "rule": {
            "priority_1": "significant after correction, both interval methods "
                          "agree, effect >= the practical floor, and stable "
                          "where stability was measured",
            "priority_2": "a real signal missing one of those conditions",
            "priority_3": "measured and found to be nothing, or too sparse",
            "material_floor_r": MATERIAL_R,
            "expected_impact_r": "effect x n over this run's own population. "
                                 "Not a forecast — what the difference was "
                                 "worth over this window.",
        },
        "population_n": scored_n,
        "priority_1": tiers[1],
        "priority_2": tiers[2],
        "priority_3": tiers[3],
        "no_parameter_recommendations": (
            "Phase 3 was asked to find why and to rank what to investigate. "
            "Choosing a weight or a threshold is a decision for a person "
            "holding this evidence."),
    }
    return payload, _markdown(payload)


def _tier(*, significant: Optional[bool], material: bool,
          stable: Optional[bool], inferential: bool) -> int:
    if not inferential:
        return 3
    if significant and material and stable is not False:
        return 1
    if significant or material:
        return 2
    return 3


def _from_hypotheses(research: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for result in research.get("results") or []:
        block = result["expectancy"]
        effect = block["effect"] or 0.0
        n = block["left"]["n"] + block["right"]["n"]
        material = abs(effect) >= MATERIAL_R
        out.append({
            "source_study": "hypothesis_registry",
            "finding": result["question"],
            "name": result["name"],
            "n": n,
            "effect_r": effect,
            "cohens_d": block["cohens_d"],
            "welch_ci": block["welch_ci"],
            "bootstrap_ci": block["bootstrap_ci"],
            "p_adjusted": block["p_adjusted"],
            "significant": block["significant"],
            "material": material,
            "expected_impact_r": effect * min(block["left"]["n"],
                                              block["right"]["n"]),
            "interpretation": block["interpretation"],
            "priority": _tier(significant=block["significant"],
                              material=material, stable=None,
                              inferential=block["inferential"]),
        })
    return out


def _from_factors(study: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for factor in study.get("factors") or []:
        if factor["verdict"] in ("INVARIANT",):
            out.append({
                "source_study": "score_component_analysis",
                "finding": f"`{factor['factor']}` never varies across the "
                           f"population, so it contributes no information to "
                           f"the score",
                "name": factor["factor"], "n": factor["present"]["n"]
                + factor["absent"]["n"],
                "effect_r": None, "welch_ci": None, "bootstrap_ci": None,
                "p_adjusted": None, "significant": None, "material": False,
                "expected_impact_r": 0.0,
                "interpretation": factor["interpretation"],
                "priority": 2,
            })
            continue
        if factor["verdict"] not in ("USEFUL", "HARMFUL"):
            out.append({
                "source_study": "score_component_analysis",
                "finding": f"`{factor['factor']}` shows no measurable effect on "
                           f"expectancy",
                "name": factor["factor"],
                "n": factor["present"]["n"] + factor["absent"]["n"],
                "effect_r": factor["effect_r"], "welch_ci": factor["welch_ci"],
                "bootstrap_ci": factor["bootstrap_ci"],
                "p_adjusted": factor["p_adjusted"], "significant": False,
                "material": factor["material"], "expected_impact_r": 0.0,
                "interpretation": factor["interpretation"],
                "priority": 3,
            })
            continue
        effect = factor["effect_r"] or 0.0
        smaller = min(factor["present"]["n"], factor["absent"]["n"])
        out.append({
            "source_study": "score_component_analysis",
            "finding": f"`{factor['factor']}` is {factor['verdict'].lower()}: "
                       f"present is {effect:+.4f} R versus absent",
            "name": factor["factor"],
            "n": factor["present"]["n"] + factor["absent"]["n"],
            "effect_r": effect, "cohens_d": factor["cohens_d"],
            "welch_ci": factor["welch_ci"],
            "bootstrap_ci": factor["bootstrap_ci"],
            "p_adjusted": factor["p_adjusted"], "significant": True,
            "material": factor["material"],
            "expected_impact_r": effect * smaller,
            "interpretation": factor["interpretation"],
            "priority": _tier(significant=True, material=factor["material"],
                              stable=None, inferential=True),
        })
    return out


def _from_direction(study: dict[str, Any]) -> list[dict[str, Any]]:
    headline = (study.get("headline") or {}).get("comparison")
    if not headline:
        return []
    consistency = study.get("overall_sign_consistency")
    stable = None if consistency is None else consistency >= 0.75
    effect = headline["effect"] or 0.0
    smaller = min(headline["left"]["n"], headline["right"]["n"])
    return [{
        "source_study": "direction_analysis",
        "finding": (f"SHORT outperforms LONG by {effect:+.4f} R, and the sign "
                    f"holds in {consistency:.0%} of inferential partition cells"
                    if consistency is not None else
                    f"SHORT outperforms LONG by {effect:+.4f} R"),
        "name": "direction_asymmetry",
        "n": headline["left"]["n"] + headline["right"]["n"],
        "effect_r": effect, "cohens_d": headline["cohens_d"],
        "welch_ci": headline["welch_ci"],
        "bootstrap_ci": headline["bootstrap_ci"],
        "p_adjusted": headline["p_adjusted"],
        "significant": headline["significant"],
        "material": abs(effect) >= MATERIAL_R,
        "stability": consistency,
        "expected_impact_r": effect * smaller,
        "interpretation": (
            f"sign consistency {consistency:.0%} — "
            + ("stable across partitions, which is what a structural "
               "asymmetry looks like" if stable else
               "not stable across partitions, which is what a period effect "
               "looks like")) if consistency is not None else
        headline["interpretation"],
        "priority": _tier(significant=headline["significant"],
                          material=abs(effect) >= MATERIAL_R, stable=stable,
                          inferential=headline["inferential"]),
    }]


def _from_risk(study: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    attainment = study.get("target_attainment") or {}
    reachable = attainment.get("reachable_share")
    if reachable is not None:
        out.append({
            "source_study": "risk_analysis",
            "finding": f"only {reachable:.1%} of trades ever saw their own "
                       f"structural target, and "
                       f"{attainment.get('unrealised_reachable')} that did were "
                       f"not taken",
            "name": "target_attainment",
            "n": attainment.get("n"), "effect_r": None,
            "welch_ci": None, "bootstrap_ci": None, "p_adjusted": None,
            "significant": None, "material": True,
            "expected_impact_r": None,
            "interpretation": attainment.get("reading"),
            "priority": 2,
        })
    for row in study.get("hypotheses") or []:
        if not row["supported"]:
            continue
        out.append({
            "source_study": "risk_analysis",
            "finding": f"the '{row['hypothesis']}' explanation is consistent "
                       f"with the data",
            "name": row["hypothesis"].replace(" ", "_"),
            "n": None, "effect_r": None, "welch_ci": None,
            "bootstrap_ci": None, "p_adjusted": None, "significant": None,
            "material": True, "expected_impact_r": None,
            "interpretation": f"{row['prediction']} — measured "
                              f"{row['measured']}",
            "priority": 2,
        })
    return out


#: `symbol` is excluded from the drawdown ranking. A symbol that was only listed
#: during the drawdown window has no trades outside it, so its lift is enormous
#: and meaningless — the first pass surfaced a symbol at 145x that contributed
#: *positive* R. The dimension stays in the study payload, where it is presented
#: with its counts; it is only unfit for an automatic ranking.
_DRAWDOWN_EXCLUDED = ("symbol",)

#: A candidate has to be over-represented, common enough to matter, and actually
#: losing money inside the window. Lift alone finds artifacts.
_DRAWDOWN_MIN_LIFT = 1.2
_DRAWDOWN_MIN_N = 100
_DRAWDOWN_MAX_NET_R = -50.0


def _from_drawdown(study: dict[str, Any]) -> list[dict[str, Any]]:
    composition = study.get("composition") or {}
    out = []
    for dimension, rows in composition.items():
        if dimension in _DRAWDOWN_EXCLUDED:
            continue
        for row in rows[:3]:
            if ((row.get("lift") or 0) < _DRAWDOWN_MIN_LIFT
                    or row["inside_n"] < _DRAWDOWN_MIN_N
                    or (row.get("inside_net_r") or 0.0) > _DRAWDOWN_MAX_NET_R):
                continue
            out.append({
                "source_study": "drawdown_analysis",
                "finding": f"`{dimension}={row['key']}` is {row['lift']:.2f}x "
                           f"over-represented inside the worst drawdown",
                "name": f"drawdown_{dimension}_{row['key']}",
                "n": row["inside_n"] + row["outside_n"],
                "effect_r": None, "welch_ci": None, "bootstrap_ci": None,
                "p_adjusted": None, "significant": None, "material": True,
                "expected_impact_r": row["inside_net_r"],
                "interpretation": "selection, not cause — trades inside a "
                                  "drawdown are chosen because they lost. A "
                                  "candidate to test, never a conclusion.",
                "priority": 2,
            })
    return out


def _markdown(payload: dict[str, Any]) -> str:
    out = ["# Strategy research priority\n\n",
           "> Assembled from the other studies' numbers, not written by hand. "
           "Every entry carries sample size, effect size, interval and "
           "expected impact.\n\n",
           f"> **No parameter is proposed.** {payload['no_parameter_recommendations']}\n\n",
           "## Tiering rule\n\n| Tier | Rule |\n|---|---|\n"]
    for tier in (1, 2, 3):
        out.append(f"| **Priority {tier}** | {payload['rule'][f'priority_{tier}']} |\n")
    out.append(f"\n`expected_impact_r` — {payload['rule']['expected_impact_r']}\n")
    out.append(f"\nPopulation: {payload['population_n']} scored trades.\n")

    titles = {1: "Priority 1 — highest confidence",
              2: "Priority 2 — interesting, needs more evidence",
              3: "Priority 3 — do not touch, evidence is weak or absent"}
    for tier in (1, 2, 3):
        entries = payload[f"priority_{tier}"]
        out.append(f"\n## {titles[tier]}\n\n")
        if not entries:
            out.append("_nothing in this tier_\n")
            continue
        out.append("| Finding | study | n | effect R | 95% CI | p adj | "
                   "impact R |\n")
        out.append("|---|---|---:|---:|---|---:|---:|\n")
        for entry in entries:
            out.append(
                f"| {entry['finding']} | `{entry['source_study']}` | "
                f"{_num(entry.get('n'), 0)} | {_num(entry.get('effect_r'))} | "
                f"{_ci(entry.get('welch_ci'))} | "
                f"{_num(entry.get('p_adjusted'))} | "
                f"{_num(entry.get('expected_impact_r'), 1)} |\n")
        out.append("\n")
        for entry in entries:
            out.append(f"- **{entry['name']}** — {entry['interpretation']}\n")
        out.append("\n")
    return "".join(out)


def _num(value: Any, places: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.{places}f}"


def _ci(bounds: Optional[list]) -> str:
    if not bounds or bounds[0] is None:
        return "—"
    mark = " ✓" if (bounds[0] > 0 or bounds[1] < 0) else ""
    return f"[{bounds[0]:+.4f}, {bounds[1]:+.4f}]{mark}"
