"""A controlled pattern is evidence of the synthetic response, not its original cause."""

import copy
import json

import pytest

from argos_studio.comparison import compare_experiment
from argos_studio.core import Store
from argos_studio.investigation import investigate


@pytest.fixture
def plan():
    return {
        "kind": "synthetic_dropout_comparison",
        "version": "synthetic-dropout/1",
        "source": "simulation",
        "sample_rate_hz": 20,
        "phase_duration_s": 6,
        "dropout_at_s": 2,
        "dropout_duration_s": 2,
        "gap_threshold_s": 0.25,
        "timing_tolerance_s": 0.25,
        "max_wall_duration_s": 20,
    }


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "comparison.sqlite3")


def capture(store, *, missing=None, events=(), duration=6, rate=20):
    sid = store.create_session(
        "Synthetic comparison fixture",
        "Observe receipt continuity",
        metadata={"generator": "attitude-sine-v1", "sample_rate_hz": rate},
    )["id"]
    times = [i / rate for i in range(round(duration * rate))]
    store.append_samples(
        sid,
        [
            {
                "elapsed_s": t,
                "source_time_s": t,
                "received_at": 2000 + t,
                "roll_deg": 0,
                "pitch_deg": 0,
                "gyro_x_deg_s": 0,
            }
            for t in times
            if missing is None or not missing(t)
        ],
    )
    for kind, t in events:
        store.add_event(sid, kind, "Synthetic fixture event", t)
    store.finish_session(sid, elapsed_s=duration)
    return store.snapshot(sid)


@pytest.fixture
def captures(store):
    control = capture(store)
    perturbed = capture(
        store,
        missing=lambda t: 2 <= t < 4,
        events=[("dropout_started", 2), ("dropout_ended", 4)],
    )
    reference = investigate(store, perturbed["session"]["id"])
    return control, perturbed, reference


def failed(result):
    return {check["code"] for check in result["checks"] if not check["passed"]}


def test_controlled_signature_links_samples_events_and_reference(captures, plan):
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "supported"
    assert failed(result) == set()
    assert result["control"]["sample_count"] == 120
    assert result["perturbed"]["sample_count"] == 80
    gap = result["intervention"]["gap"]
    assert gap["start_s"] == 1.95
    assert gap["end_s"] == 4.0
    assert gap["before_seq"] == 39
    assert gap["after_seq"] == 40
    assert gap["duration_s"] == pytest.approx(2.05)
    assert result["intervention"]["start_event"]["at_s"] == 2
    assert result["intervention"]["end_event"]["at_s"] == 4
    assert result["reference"]["snapshot_sha256"] == captures[2]["snapshot"]["sha256"]
    assert result["reference"]["report_id"] == captures[2]["id"]
    assert result["difference"]["gap_count"] == 1
    assert result["difference"]["max_interval_s"] == pytest.approx(2)
    assert result["difference"]["reference_gap_s"] == 0
    assert "ne détermine pas la cause" in result["summary"]
    assert any("aucune latence" in text for text in result["limitations"])
    json.dumps(result, allow_nan=False)


def test_comparison_is_deterministic_and_leaves_inputs_unchanged(captures, plan):
    before = copy.deepcopy((captures, plan))
    first = compare_experiment(*captures, plan)
    assert first == compare_experiment(*captures, plan)
    assert (captures, plan) == before
    first["perturbed"]["gaps"][0]["duration_s"] = 0
    first["intervention"]["start_event"]["at_s"] = 999
    first["reference"]["window_s"]["start_s"] = 999
    assert (captures, plan) == before


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    ("field", "value", "check"),
    [
        ("source", "mavlink-udp", "source"),
        ("status", "interrupted", "completed"),
        ("status", "live", "completed"),
        ("elapsed_s", 5, "duration"),
        ("sample_count", 999, "full_snapshot"),
    ],
)
def test_incomparable_or_incomplete_capture_is_inconclusive(
    captures, plan, index, field, value, check
):
    captures[index]["session"][field] = value
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "inconclusive"
    prefix = "control" if index == 0 else "perturbed"
    assert f"{prefix}_{check}" in failed(result)


@pytest.mark.parametrize(
    ("field", "value"), [("generator", "other-generator"), ("sample_rate_hz", 10)]
)
def test_declared_source_configuration_must_match_plan(captures, plan, field, value):
    captures[1]["session"]["metadata"][field] = value
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "inconclusive"
    assert "perturbed_source" in failed(result)


def test_same_capture_cannot_be_its_own_control(captures, plan):
    control, _, report = captures
    result = compare_experiment(control, control, report, plan)
    assert result["outcome"] == "inconclusive"
    assert "distinct_sessions" in failed(result)


def test_control_anomaly_prevents_support_even_when_perturbation_matches(store, captures, plan):
    bad_control = capture(store, missing=lambda t: 0.5 <= t < 0.8)
    result = compare_experiment(bad_control, captures[1], captures[2], plan)
    assert result["outcome"] == "inconclusive"
    assert "control_continuity" in failed(result)


def test_control_intervention_is_not_accepted_as_unperturbed(captures, plan):
    captures[0]["events"].append({"kind": "dropout_started", "at_s": 2})
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "inconclusive"
    assert "control_unperturbed" in failed(result)


@pytest.mark.parametrize("kind", ["dropout_started", "dropout_ended"])
def test_missing_intervention_event_is_inconclusive(captures, plan, kind):
    captures[1]["events"] = [e for e in captures[1]["events"] if e["kind"] != kind]
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "inconclusive"
    assert "intervention_events" in failed(result)
    assert result["intervention"] is None


def test_request_without_resumption_does_not_prove_execution(captures, plan):
    captures[1]["events"] = [{"kind": "dropout_requested", "at_s": 2}]
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "inconclusive"
    assert result["intervention"] is None


def test_repeated_intervention_is_not_the_fixed_profile(captures, plan):
    captures[1]["events"].append(copy.deepcopy(captures[1]["events"][0]))
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "inconclusive"
    assert "intervention_events" in failed(result)


@pytest.mark.parametrize(("event_index", "time"), [(0, 2.5), (1, 3.5), (1, 4.4)])
def test_event_timing_must_match_profile(captures, plan, event_index, time):
    captures[1]["events"][event_index]["at_s"] = time
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "inconclusive"
    assert "intervention_timing" in failed(result)


def test_good_captures_without_expected_gap_are_not_reproduced(store, captures, plan):
    uninterrupted = capture(store, events=[("dropout_started", 2), ("dropout_ended", 4)])
    result = compare_experiment(captures[0], uninterrupted, captures[2], plan)
    assert result["outcome"] == "not_reproduced"
    assert failed(result) == {"observed_response"}
    assert result["intervention"]["gap"] is None


def test_extra_gap_is_not_the_expected_signature(store, captures, plan):
    perturbed = capture(
        store,
        missing=lambda t: 2 <= t < 4 or 5 <= t < 5.3,
        events=[("dropout_started", 2), ("dropout_ended", 4)],
    )
    result = compare_experiment(captures[0], perturbed, captures[2], plan)
    assert result["outcome"] == "not_reproduced"
    assert result["perturbed"]["gap_count"] == 2


def test_gap_must_surround_intervention_events(store, captures, plan):
    unrelated = capture(
        store,
        missing=lambda t: 1 <= t < 3,
        events=[("dropout_started", 2), ("dropout_ended", 4)],
    )
    result = compare_experiment(captures[0], unrelated, captures[2], plan)
    assert result["outcome"] == "not_reproduced"
    assert result["intervention"]["gap"] is None


def test_gap_length_must_match_requested_suspension(store, captures, plan):
    late = capture(
        store,
        missing=lambda t: 2 <= t < 4.6,
        events=[("dropout_started", 2), ("dropout_ended", 4)],
    )
    result = compare_experiment(captures[0], late, captures[2], plan)
    assert result["outcome"] == "not_reproduced"
    assert failed(result) == {"observed_response"}


@pytest.mark.parametrize("missing", [lambda t: t < 0.5, lambda t: t >= 5.5, lambda t: True])
def test_leading_trailing_or_empty_capture_is_inconclusive(store, captures, plan, missing):
    bad = capture(store, missing=missing)
    result = compare_experiment(bad, captures[1], captures[2], plan)
    assert result["outcome"] == "inconclusive"
    assert "control_coverage" in failed(result)


def test_low_volume_and_wrong_observed_cadence_are_inconclusive(store, captures, plan):
    bad = capture(store, missing=lambda t: round(t * 20) % 2 == 1)
    result = compare_experiment(bad, captures[1], captures[2], plan)
    assert result["outcome"] == "inconclusive"
    assert {"control_sample_count", "control_cadence"} <= failed(result)


def test_filtered_snapshot_cannot_pass_as_complete(store, captures, plan):
    partial = store.snapshot(captures[1]["session"]["id"], start_s=0.5, end_s=5.5)
    result = compare_experiment(captures[0], partial, captures[2], plan)
    assert result["outcome"] == "inconclusive"
    assert "perturbed_full_snapshot" in failed(result)


def test_source_error_prevents_valid_comparison_even_with_completed_status(captures, plan):
    captures[1]["events"].append({"kind": "source_error", "at_s": 5})
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "inconclusive"
    assert "perturbed_completed" in failed(result)


def test_reference_remains_descriptive_even_for_different_origin_and_gap(captures, plan):
    reference = captures[2]
    reference["snapshot"]["source"] = "argos-recording"
    continuity = next(t["result"] for t in reference["tools"] if t["name"] == "receipt_continuity")
    continuity["max_gap_s"] = 4
    continuity["gap_count"] = 3
    result = compare_experiment(*captures, plan)
    assert result["outcome"] == "supported"
    assert result["reference"]["source"] == "argos-recording"
    assert result["reference"]["gap_count"] == 3
    assert result["difference"]["reference_gap_s"] == pytest.approx(-1.95)


def test_missing_reference_continuity_does_not_invent_gap_numbers(captures, plan):
    captures[2]["tools"] = []
    result = compare_experiment(*captures, plan)
    assert result["reference"]["gap_count"] is None
    assert result["reference"]["max_gap_s"] is None
    assert result["difference"]["reference_gap_s"] is None


def test_short_test_profile_uses_the_same_comparison_rules(store, captures, plan):
    plan.update(phase_duration_s=1.2, dropout_at_s=0.35, dropout_duration_s=0.4)
    control = capture(store, duration=1.2)
    perturbed = capture(
        store,
        duration=1.2,
        missing=lambda t: 0.35 <= t < 0.75,
        events=[("dropout_started", 0.35), ("dropout_ended", 0.75)],
    )
    result = compare_experiment(control, perturbed, captures[2], plan)
    assert result["outcome"] == "supported"
    assert result["perturbed"]["sample_count"] == 16


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "mavlink-udp"),
        ("version", "unknown"),
        ("phase_duration_s", float("nan")),
        ("sample_rate_hz", 0),
        ("sample_rate_hz", True),
        ("dropout_duration_s", 7),
        ("gap_threshold_s", 3),
    ],
)
def test_invalid_or_unsupported_plan_is_rejected(captures, plan, field, value):
    plan[field] = value
    with pytest.raises(ValueError):
        compare_experiment(*captures, plan)
