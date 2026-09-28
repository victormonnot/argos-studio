"""Reproducible reception investigations over one immutable evidence snapshot.

These tools inspect stored observations only. Free-text context is retained as
data, never interpreted as instructions. There is no model or command executor.
"""

import hashlib
import json
from collections import Counter
from itertools import pairwise
from typing import Any

from .core import Store, _number, _text

ALGORITHM_VERSION = "reception-quality/1"
GAP_THRESHOLD_S = 0.25
MAX_GAP_FINDINGS = 12
MAX_CLOCK_EXAMPLES = 12


def fingerprint(evidence: dict) -> str:
    """Hash canonical UTF-8 JSON, including raw datagram hashes, not their bytes."""
    encoded = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def receipt_continuity(evidence: dict, start: float, end: float) -> dict:
    """Use exactly the same interval calculation as the session/replay view."""
    analysis = Store._analyze_rows(
        evidence["session"], evidence["samples"], start, end, GAP_THRESHOLD_S
    )
    gaps = analysis.pop("gaps")
    # Keep the largest intervals, then present them in chronological order.
    examples = sorted(
        sorted(gaps, key=lambda gap: (-gap["duration_s"], gap["before_seq"]))[:MAX_GAP_FINDINGS],
        key=lambda gap: gap["before_seq"],
    )
    return {
        **analysis,
        "gap_count": len(gaps),
        "reported_gap_count": len(examples),
        "unreported_gap_count": len(gaps) - len(examples),
        "gaps": examples,
    }


def capture_correlation(evidence: dict, start: float, end: float) -> dict:
    """Inspect datagrams strictly inside a pair of surrounding measurements.

    The accepted disposition means the receiver validated at least one frame
    from its selected identity and peer. A mixed datagram may also contain
    frames from other identities, which must not count as selected heartbeats.
    """
    datagrams = [row for row in evidence["datagrams"] if start < row["elapsed_s"] < end]
    accepted = [row for row in datagrams if row["disposition"] == "accepted"]
    vehicle = evidence["session"]["metadata"].get("vehicle", {})
    identity = vehicle if isinstance(vehicle, dict) else {}
    heartbeats = [
        row
        for row in accepted
        if any(
            frame.get("type") == "HEARTBEAT"
            and frame.get("system_id") == identity.get("system_id")
            and frame.get("component_id") == identity.get("component_id")
            for frame in row["details"].get("frames", [])
        )
    ]
    # Prefer heartbeat evidence, then the first other accepted datagram.
    witnesses = list({row["seq"]: row for row in heartbeats[:1] + accepted[:1]}.values())
    return {
        "available": evidence["session"]["source"] == "mavlink-udp",
        "datagram_count": len(datagrams),
        "accepted_count": len(accepted),
        "selected_heartbeat_datagram_count": len(heartbeats),
        "dispositions": dict(Counter(row["disposition"] for row in datagrams)),
        "witnesses": [_datagram_reference(row) for row in witnesses],
    }


def _datagram_reference(row: dict) -> dict:
    # Raw bytes and complete decoder details remain in the capture export.
    # Keep report size independent of arbitrary decoder metadata verbosity.
    fields = (
        "seq",
        "elapsed_s",
        "received_at",
        "peer_host",
        "peer_port",
        "disposition",
        "payload_sha256",
        "raw_bytes",
        "sample_start_seq",
        "sample_count",
    )
    return {
        **{key: row[key] for key in fields},
        "reason": str(row["details"].get("reason") or "")[:120],
        "frame_count": len(row["details"].get("frames", [])),
    }


def source_clock(evidence: dict, start: float, end: float) -> dict:
    """Find regressions without assuming reboot, clock synchronization or loss."""
    regressions = [
        {"before": left, "after": right}
        for left, right in pairwise(evidence["samples"])
        if right["source_time_s"] < left["source_time_s"] and start <= right["elapsed_s"] <= end
    ]
    return {
        "regression_count": len(regressions),
        "examples": regressions[:MAX_CLOCK_EXAMPLES],
        "unreported_count": max(0, len(regressions) - MAX_CLOCK_EXAMPLES),
    }


def build_report(
    evidence: dict,
    *,
    start_s: float | None = None,
    end_s: float | None = None,
    context: str = "",
) -> dict[str, Any]:
    """Build a bounded, deterministic report; identical inputs produce identical output."""
    session = evidence["session"]
    duration = session["elapsed_s"]
    start = 0.0 if start_s is None else _number(start_s, "start_s", minimum=0)
    end = duration if end_s is None else _number(end_s, "end_s", minimum=0)
    if start > end or end > duration:
        raise ValueError("La fenêtre doit rester dans la durée enregistrée de la session.")
    context = _text(context, "context", 2000, empty=True)
    window = {"start_s": start, "end_s": end}
    samples = evidence["samples"]
    events = evidence["events"]
    datagrams = evidence["datagrams"]
    continuity = receipt_continuity(evidence, start, end)
    clock = source_clock(evidence, start, end)
    tools = [
        {
            "name": "receipt_continuity",
            "parameters": {**window, "gap_threshold_s": GAP_THRESHOLD_S},
            "result": continuity,
        },
        {"name": "source_clock", "parameters": window, "result": clock},
    ]
    proofs = []
    findings = []

    def proof(kind: str, title: str, bounds: dict, data: Any) -> str:
        identifier = f"e{len(proofs) + 1}"
        proofs.append(
            {"id": identifier, "kind": kind, "title": title, "window_s": bounds, "data": data}
        )
        return identifier

    def finding(code, title, observation, bounds, refs, hypotheses, uncertainties, next_check):
        findings.append(
            {
                "id": f"f{len(findings) + 1}",
                "code": code,
                "title": title,
                "observation": observation,
                "window_s": bounds,
                "evidence": refs,
                "hypotheses": hypotheses,
                "uncertainties": uncertainties,
                "next_check": next_check,
            }
        )

    check_capture = {
        "title": "Comparer l’émission et la réception sur un nouvel essai local",
        "steps": [
            "Conserver la même configuration de flux et noter tout changement.",
            "Enregistrer côté émetteur les ATTITUDE produites, et côté Studio les datagrammes.",
            "Comparer les séquences dans chaque horloge, sans soustraire des horloges distinctes.",
        ],
        "expected_evidence": (
            "Deux traces permettant de distinguer absence d’émission et absence de réception ; "
            "la seule capture Studio ne suffit pas."
        ),
    }
    check_synthetic = {
        "title": "Comparer un témoin et une interruption synthétique",
        "steps": [
            "Enregistrer une session synthétique sans intervention.",
            "Répéter avec une seule interruption de 2 s et conserver ses événements.",
            "Comparer les intervalles mesurés et les instants de suspension/reprise.",
        ],
        "expected_evidence": (
            "Un intervalle mesuré autour de l’intervention, distinct de sa durée demandée ; "
            "ce contrôle ne valide aucun matériel."
        ),
    }
    check = check_synthetic if session["source"] == "simulation" else check_capture
    by_seq = {sample["seq"]: sample for sample in samples}
    for gap in continuity["gaps"]:
        bounds = {key: gap[key] for key in ("start_s", "end_s")}
        refs = [
            proof(
                "sample_pair",
                f"Échantillons #{gap['before_seq']} → #{gap['after_seq']}",
                bounds,
                {
                    "before": by_seq[gap["before_seq"]],
                    "after": by_seq[gap["after_seq"]],
                    "interval_s": gap["duration_s"],
                    "window_overlap_s": gap["window_overlap_s"],
                },
            )
        ]
        correlation = capture_correlation(evidence, gap["start_s"], gap["end_s"])
        tools.append({"name": "capture_correlation", "parameters": bounds, "result": correlation})
        observation = (
            f"{gap['duration_s']:.3f} s entre les échantillons "
            f"#{gap['before_seq']} et #{gap['after_seq']}, au-delà du seuil de 0,25 s."
        )
        hypotheses = ["La cause de cet intervalle reste indéterminée."]
        uncertainties = [
            "L’intervalle porte sur la réception ; il ne mesure ni pertes de paquets "
            "ni latence du capteur à l’affichage."
        ]
        code, title = "sample_gap", "Intervalle sans mesures"
        if correlation["available"]:
            refs.append(
                proof("capture_interval", "Réceptions dans cet intervalle", bounds, correlation)
            )
            if correlation["accepted_count"]:
                code, title = "attitude_gap_with_traffic", "ATTITUDE absentes, trames reçues"
                observation += (
                    f" {correlation['accepted_count']} datagramme(s) validé(s) de la source "
                    "sélectionnée ont été conservés strictement entre ces deux mesures."
                )
                if correlation["selected_heartbeat_datagram_count"]:
                    observation += (
                        f" {correlation['selected_heartbeat_datagram_count']} contiennent "
                        "un HEARTBEAT de cette identité."
                    )
                hypotheses = [
                    "Une absence totale de réception de cette source sur tout l’intervalle "
                    "est contredite par ces trames.",
                    "Une interruption du flux ATTITUDE ou une perte sélective reste possible.",
                ]
                uncertainties.append(
                    "Quelques trames reçues ne prouvent pas une liaison continue, "
                    "un véhicule sain ou une cadence ATTITUDE conforme."
                )
            else:
                code, title = "attitude_gap_without_traffic", "Aucune trame sélectionnée conservée"
                observation += (
                    " Aucun datagramme accepté de cette source n’est conservé entre ces mesures."
                )
                hypotheses = [
                    "Arrêt de l’émetteur, transport interrompu ou acquisition incomplète "
                    "restent compatibles avec ces seules observations."
                ]
                uncertainties.append(
                    "Les datagrammes exclus ne prouvent pas la présence de la source sélectionnée."
                )
        else:
            uncertainties.append(
                "Cet outil ne dispose pas des autres trames pour cette source ; "
                "il ne peut pas distinguer silence global et absence d’ATTITUDE."
            )
        related = [
            event
            for event in events
            if gap["start_s"] <= event["at_s"] <= gap["end_s"]
            and event["kind"] in {"dropout_started", "dropout_ended", "source_error"}
        ]
        if related:
            refs.append(
                proof(
                    "events",
                    "Événements enregistrés dans l’intervalle",
                    bounds,
                    {"count": len(related), "examples": related[:4]},
                )
            )
        if session["source"] == "simulation" and any(
            event["kind"] == "dropout_started" for event in related
        ):
            observation += " Une suspension du générateur est enregistrée dans cet intervalle."
            hypotheses = ["L’intervalle est compatible avec la suspension synthétique enregistrée."]
            uncertainties.append(
                "L’événement indique la suspension demandée ; sa durée et l’intervalle "
                "mesuré diffèrent selon la cadence et l’ordonnancement."
            )
        finding(code, title, observation, bounds, refs, hypotheses, uncertainties, check)

    if not continuity["gap_count"]:
        sufficient = continuity["sample_count"] >= 2
        finding(
            "no_gap_observed" if sufficient else "insufficient_data",
            "Aucun grand intervalle observé" if sufficient else "Mesures insuffisantes",
            (
                "Aucun intervalle entre mesures de cette fenêtre ne dépasse 0,25 s."
                if sufficient
                else "Moins de deux mesures dans cette fenêtre ; "
                "la continuité ne peut pas être évaluée."
            ),
            window,
            [proof("continuity", "Mesure de continuité", window, continuity)],
            [],
            [
                "L’absence d’intervalle détecté ne prouve pas la qualité du système "
                "ni la cadence attendue."
            ],
            check,
        )

    # Silence at a capture boundary is a coverage observation, not a gap
    # bracketed by two samples. Never fabricate a sample at either edge.
    edges = []
    if samples:
        if min(end, samples[0]["elapsed_s"]) - start > GAP_THRESHOLD_S:
            edges.append(("leading", start, min(end, samples[0]["elapsed_s"]), samples[0]))
        if end - max(start, samples[-1]["elapsed_s"]) > GAP_THRESHOLD_S:
            edges.append(("trailing", max(start, samples[-1]["elapsed_s"]), end, samples[-1]))
    for side, left, right, witness in edges:
        bounds = {"start_s": left, "end_s": right}
        finding(
            f"{side}_coverage",
            "Bord de fenêtre sans mesures",
            f"{right - left:.3f} s sans mesure conservée au bord de cette fenêtre.",
            bounds,
            [
                proof(
                    "coverage",
                    "Borne enregistrée et mesure la plus proche",
                    bounds,
                    {
                        "side": side,
                        "nearest_sample": witness,
                        "recorded_duration_s": duration,
                    },
                )
            ],
            [],
            ["Ce bord n’est pas encadré par deux mesures et n’est pas compté comme interruption."],
            check,
        )
    if clock["regression_count"]:
        finding(
            "source_clock_regression",
            "Horloge de source revenue en arrière",
            f"{clock['regression_count']} recul(s) du temps de source dans la fenêtre.",
            window,
            [proof("source_clock", "Paires de mesures et horloges", window, clock)],
            ["Réinitialisation, rebouclage du compteur ou réception hors ordre sont possibles."],
            [
                "Le recul ne démontre pas un redémarrage. "
                "La continuité utilise l’horloge de réception."
            ],
            {
                "title": "Confronter le recul aux traces de l’émetteur",
                "steps": [
                    "Conserver les horodatages d’origine et rechercher un événement de démarrage."
                ],
                "expected_evidence": "Un événement indépendant pour départager les hypothèses.",
            },
        )
    scoped_datagrams = [row for row in datagrams if start <= row["elapsed_s"] <= end]
    excluded = [row for row in scoped_datagrams if row["disposition"] != "accepted"]
    if excluded:
        counts = dict(Counter(row["disposition"] for row in excluded))
        finding(
            "excluded_datagrams",
            "Datagrammes écartés du flux mesuré",
            f"{len(excluded)} datagramme(s) conservé(s) mais exclus des mesures "
            "dans cette fenêtre.",
            window,
            [
                proof(
                    "excluded_datagrams",
                    "Motifs et références de capture",
                    window,
                    {
                        "dispositions": counts,
                        "examples": [_datagram_reference(row) for row in excluded[:4]],
                        "unreported_count": max(0, len(excluded) - 4),
                    },
                )
            ],
            [],
            [
                "Une exclusion peut venir du format, de la signature, de l’identité ou du pair. "
                "Elle ne prouve pas une corruption du réseau ni une perte de mesures attendues."
            ],
            {
                "title": "Vérifier le profil de l’émetteur local",
                "steps": [
                    "Comparer format, signature et identité aux paramètres de capture.",
                    "Inspecter les octets des datagrammes référencés dans l’export brut.",
                ],
                "expected_evidence": "Une correspondance entre octets et motif d’exclusion.",
            },
        )
    scoped_events = [event for event in events if start <= event["at_s"] <= end]
    incomplete = [
        event for event in scoped_events if event["kind"] in {"capture_limit", "source_error"}
    ]
    if incomplete or (session["status"] == "interrupted" and end == duration):
        finding(
            "capture_incomplete",
            "Acquisition interrompue ou bornée",
            "L’acquisition a rencontré une limite ou une interruption ; "
            "la fin enregistrée ne démontre pas l’arrêt de la source.",
            window,
            [
                proof(
                    "capture_boundary",
                    "État et événements de fin d’acquisition",
                    window,
                    {
                        "status": session["status"],
                        "recorded_duration_s": duration,
                        "ended_at": session["ended_at"],
                        "events": incomplete[:4],
                        "event_count": len(incomplete),
                    },
                )
            ],
            [],
            ["Des observations peuvent manquer après la dernière écriture conservée."],
            {
                "title": "Compléter la trace dans une nouvelle session",
                "steps": [
                    "Examiner le motif d’arrêt enregistré.",
                    "Conserver une nouvelle capture locale avec une durée adaptée aux limites.",
                ],
                "expected_evidence": "Une capture avec fin observée et motif d’arrêt explicite.",
            },
        )
    tools.append(
        {
            "name": "event_context",
            "parameters": window,
            "result": {
                "count": len(scoped_events),
                "kinds": dict(Counter(event["kind"] for event in scoped_events)),
                "interpretation": "Annotations conservées comme observations non vérifiées.",
            },
        }
    )
    outcome = (
        "observations"
        if any(f["code"] not in {"no_gap_observed", "insufficient_data"} for f in findings)
        else "insufficient_data"
        if continuity["sample_count"] < 2
        else "no_gap_observed"
    )
    summary = (
        f"{continuity['gap_count']} intervalle(s) au-delà de 0,25 s ; "
        f"{len(findings)} constat(s) relié(s) aux preuves."
        if outcome == "observations"
        else "Données insuffisantes pour conclure sur la continuité."
        if outcome == "insufficient_data"
        else "Aucun intervalle au-delà de 0,25 s observé entre les mesures de cette fenêtre."
    )
    limitations = [
        "Investigation déterministe de réception ; aucun modèle de langage n’est utilisé.",
        "Le contexte saisi est conservé, sans interprétation automatique ni valeur de preuve.",
        "Les horloges de source et de réception ne sont pas supposées synchronisées. "
        "Aucune latence de bout en bout ni cause physique n’est déduite.",
        "Le seuil de 0,25 s est un seuil d’inspection, pas une exigence de cadence du véhicule.",
        "Les preuves d’un intervalle conservent ses deux bornes, même hors de la fenêtre choisie.",
        "Le rapport est figé à la durée enregistrée au moment de la lecture. "
        "Les nouvelles mesures et annotations nécessitent une nouvelle investigation.",
        "Les vérifications proposées ne sont pas exécutées par cette investigation.",
    ]
    if session["source"] == "simulation":
        limitations.append("Mesures synthétiques ; aucun matériel ni modèle physique n’est évalué.")
    elif session["source"] == "argos-recording":
        limitations.append(
            "Enregistrement importé : seuls les ATTITUDE importés sont analysés ici. "
            "Les autres trames restent dans le fichier original, sans corrélation par cet outil."
        )
    else:
        limitations.append(
            "L’environnement déclaré et l’identité MAVLink ne sont pas authentifiés."
        )
    if continuity["unreported_gap_count"]:
        limitations.append(
            f"Seuls les {MAX_GAP_FINDINGS} plus grands intervalles sont détaillés ; "
            f"{continuity['unreported_gap_count']} autre(s) sont comptés sans constat individuel."
        )
    return {
        "schema_version": 1,
        "kind": "reception_quality",
        "algorithm_version": ALGORITHM_VERSION,
        "context": context,
        "window_s": window,
        "outcome": outcome,
        "summary": summary,
        "snapshot": {
            "sha256": fingerprint(evidence),
            "status": session["status"],
            "elapsed_s": duration,
            "sample_count": len(samples),
            "datagram_count": len(datagrams),
            "event_count": len(events),
            "source": session["source"],
            "objective": session["objective"],
            "session": session,
            "last_sample_seq": samples[-1]["seq"] if samples else None,
            "last_datagram_seq": datagrams[-1]["seq"] if datagrams else None,
            "last_event_id": max((event["id"] for event in events), default=None),
        },
        "findings": findings,
        "evidence": proofs,
        "tools": tools,
        "limitations": limitations,
    }


def investigate(store: Store, session_id: str, **parameters) -> dict:
    evidence = store.investigation_input(session_id)
    return store.save_investigation(session_id, build_report(evidence, **parameters))
