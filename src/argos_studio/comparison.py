"""Compare a bounded synthetic intervention with its unperturbed control.

Only recorded receipt intervals and generator events support the result. A
matching synthetic pattern never establishes the cause of the original finding.
"""

import copy
import math
from typing import Any

ALGORITHM_VERSION = "synthetic-comparison/1"
GENERATOR = "attitude-sine-v1"


def _plan_number(plan: dict, field: str, *, positive: bool = True) -> float:
    value = plan[field]
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise ValueError(f"Invalid experiment plan: {field}")
    return float(value)


def _summary(snapshot: dict) -> dict:
    analysis = snapshot["analysis"]
    return {
        "session_id": snapshot["session"]["id"],
        "sample_count": len(snapshot["samples"]),
        "duration_s": snapshot["session"]["elapsed_s"],
        "median_interval_s": analysis["median_interval_s"],
        "max_interval_s": analysis["max_interval_s"],
        "gap_count": len(analysis["gaps"]),
        "gaps": copy.deepcopy(analysis["gaps"]),
    }


def _reference(report: dict) -> dict:
    continuity = next(
        (tool["result"] for tool in report["tools"] if tool["name"] == "receipt_continuity"),
        {},
    )
    return {
        "report_id": report["id"],
        "snapshot_sha256": report["snapshot"]["sha256"],
        "source": report["snapshot"]["source"],
        "window_s": copy.deepcopy(report["window_s"]),
        "sample_count": continuity.get("sample_count"),
        "median_interval_s": continuity.get("median_interval_s"),
        "max_interval_s": continuity.get("max_interval_s"),
        "gap_count": continuity.get("gap_count"),
        "max_gap_s": continuity.get("max_gap_s"),
    }


def compare_experiment(
    control_snapshot: dict,
    perturbed_snapshot: dict,
    reference_report: dict,
    plan: dict,
) -> dict[str, Any]:
    """Compare complete Store snapshots, without updating them or using wall time.

    Failed comparability checks make the result inconclusive. With usable
    captures, an absent or different response is explicitly not reproduced.
    """
    if (
        plan.get("kind") != "synthetic_dropout_comparison"
        or plan.get("version") != "synthetic-dropout/1"
        or plan.get("source") != "simulation"
    ):
        raise ValueError("Unsupported synthetic experiment plan")
    rate = _plan_number(plan, "sample_rate_hz")
    duration = _plan_number(plan, "phase_duration_s")
    dropout_at = _plan_number(plan, "dropout_at_s", positive=False)
    dropout_duration = _plan_number(plan, "dropout_duration_s")
    threshold = _plan_number(plan, "gap_threshold_s")
    tolerance = _plan_number(plan, "timing_tolerance_s", positive=False)
    period = 1 / rate
    if dropout_at + dropout_duration >= duration or dropout_duration <= threshold:
        raise ValueError("The plan must permit observation of a gap and resumed samples")

    checks = []

    def check(code: str, passed: bool, explanation: str) -> None:
        checks.append({"code": code, "passed": bool(passed), "explanation": explanation})

    check(
        "distinct_sessions",
        control_snapshot["session"]["id"] != perturbed_snapshot["session"]["id"],
        "Le témoin et la phase perturbée doivent provenir de deux captures distinctes.",
    )
    for label, snapshot, expected_duration in (
        ("control", control_snapshot, duration),
        ("perturbed", perturbed_snapshot, duration - dropout_duration),
    ):
        display_label = "Témoin" if label == "control" else "Avec interruption"
        session, samples, analysis = (
            snapshot["session"],
            snapshot["samples"],
            snapshot["analysis"],
        )
        metadata = session["metadata"]
        check(
            f"{label}_source",
            session["source"] == "simulation"
            and metadata.get("generator") == GENERATOR
            and metadata.get("sample_rate_hz") == rate,
            f"{display_label} : source synthétique {GENERATOR}, cadence déclarée {rate:g} Hz.",
        )
        check(
            f"{label}_completed",
            session["status"] == "completed"
            and not any(event["kind"] == "source_error" for event in snapshot["events"]),
            f"{display_label} : acquisition terminée sans erreur de source enregistrée.",
        )
        check(
            f"{label}_duration",
            abs(session["elapsed_s"] - duration) <= tolerance + 1e-9,
            f"{display_label} : durée enregistrée {session['elapsed_s']:.3f} s ; "
            f"prévue {duration:g} s, tolérance {tolerance:g} s.",
        )
        check(
            f"{label}_full_snapshot",
            analysis["window_s"] == {"start_s": 0.0, "end_s": session["elapsed_s"]}
            and analysis["sample_count"] == len(samples) == session["sample_count"]
            and analysis["gap_threshold_s"] == threshold,
            f"{display_label} : toutes les mesures sont analysées "
            f"avec le seuil prévu {threshold:g} s.",
        )
        edge_tolerance = max(2 * period, tolerance)
        check(
            f"{label}_coverage",
            len(samples) >= 2
            and samples[0]["elapsed_s"] <= edge_tolerance
            and 0 <= session["elapsed_s"] - samples[-1]["elapsed_s"] <= edge_tolerance,
            f"{display_label} : mesures présentes près du début et de la fin "
            f"(écart maximal {edge_tolerance:g} s).",
        )
        expected_count = expected_duration * rate
        check(
            f"{label}_sample_count",
            len(samples) >= 0.8 * expected_count,
            f"{display_label} : {len(samples)} mesures conservées, au moins 80 % "
            f"des {expected_count:g} mesures nominales attendues hors suspension.",
        )
        median = analysis["median_interval_s"]
        check(
            f"{label}_cadence",
            median is not None and 0.8 * period <= median <= 1.2 * period,
            f"{display_label} : intervalle médian compatible avec {period:g} s (±20 %).",
        )

    control, perturbed = _summary(control_snapshot), _summary(perturbed_snapshot)
    check(
        "control_unperturbed",
        not any(
            event["kind"] in {"dropout_started", "dropout_ended"}
            for event in control_snapshot["events"]
        ),
        "Aucun événement de suspension n’est enregistré dans le témoin.",
    )
    check(
        "control_continuity",
        control["gap_count"] == 0,
        "Le témoin ne présente aucun intervalle supérieur au seuil prévu.",
    )
    starts = [e for e in perturbed_snapshot["events"] if e["kind"] == "dropout_started"]
    ends = [e for e in perturbed_snapshot["events"] if e["kind"] == "dropout_ended"]
    events_complete = len(starts) == len(ends) == 1
    check(
        "intervention_events",
        events_complete,
        "Une seule suspension et une seule reprise doivent être enregistrées ; "
        "une demande seule ne démontre pas la reprise.",
    )
    event_timing = False
    matching_gap = None
    if events_complete:
        start, end = starts[0]["at_s"], ends[0]["at_s"]
        event_timing = (
            abs(start - dropout_at) <= tolerance + 1e-9
            and dropout_duration - 1e-9 <= end - start <= dropout_duration + period + tolerance
            and 0 <= start < end <= perturbed["duration_s"]
        )
        matching_gap = next(
            (
                gap
                for gap in perturbed["gaps"]
                if gap["start_s"] <= start <= end <= gap["end_s"] + 1e-9
            ),
            None,
        )
    check(
        "intervention_timing",
        event_timing,
        f"La suspension doit commencer vers {dropout_at:g} s (±{tolerance:g} s) "
        f"et la reprise suivre les {dropout_duration:g} s demandées.",
    )
    comparable = all(item["passed"] for item in checks)
    response_matches = (
        perturbed["gap_count"] == 1
        and matching_gap is not None
        and dropout_duration - 1e-9
        <= matching_gap["duration_s"]
        <= dropout_duration + 2 * period + tolerance
    )
    check(
        "observed_response",
        response_matches,
        "Un seul intervalle doit encadrer les événements de suspension et de reprise, "
        f"avec une durée comprise entre {dropout_duration:g} et "
        f"{dropout_duration + 2 * period + tolerance:g} s.",
    )
    outcome = (
        "inconclusive" if not comparable else "supported" if response_matches else "not_reproduced"
    )
    summaries = {
        "supported": "Le témoin reste continu et la suspension synthétique produit "
        "l’intervalle attendu. Ce résultat ne détermine pas la cause du constat initial.",
        "not_reproduced": "Les captures sont comparables, mais la réponse attendue à la "
        "suspension synthétique n’est pas reproduite dans les mesures.",
        "inconclusive": "Les conditions de comparaison ne sont pas toutes vérifiées ; "
        "cet essai ne permet pas de conclure sur la réponse attendue.",
    }
    reference = _reference(reference_report)
    largest_gap = max((gap["duration_s"] for gap in perturbed["gaps"]), default=0.0)
    return {
        "schema_version": 1,
        "algorithm_version": ALGORITHM_VERSION,
        "outcome": outcome,
        "summary": summaries[outcome],
        "checks": checks,
        "control": control,
        "perturbed": perturbed,
        "intervention": {
            "start_event": copy.deepcopy(starts[0]),
            "end_event": copy.deepcopy(ends[0]),
            "gap": copy.deepcopy(matching_gap),
        }
        if events_complete
        else None,
        "reference": reference,
        "difference": {
            "max_interval_s": perturbed["max_interval_s"] - control["max_interval_s"]
            if perturbed["max_interval_s"] is not None and control["max_interval_s"] is not None
            else None,
            "gap_count": perturbed["gap_count"] - control["gap_count"],
            "reference_gap_s": largest_gap - reference["max_gap_s"]
            if reference["max_gap_s"] is not None
            else None,
        },
        "limitations": [
            "Ces deux captures proviennent d’un générateur trigonométrique synthétique ; "
            "elles ne mesurent ni matériel ni dynamique de vol.",
            "Les écarts utilisent uniquement l’horloge locale de réception ; "
            "aucune latence entre émission et réception n’est estimée.",
            "La comparaison au rapport initial décrit seulement des intervalles. "
            "Une durée similaire ne démontre ni cause commune, ni perte de paquets, "
            "ni état de santé du véhicule.",
            "Un seul témoin et un seul essai perturbé sont conservés ; "
            "la répétabilité et les autres mécanismes d’interruption restent à vérifier.",
        ],
    }
