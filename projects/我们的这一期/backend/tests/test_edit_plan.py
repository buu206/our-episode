"""Offline contract tests; no model calls or claims about editorial quality."""
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.repository import DomainError
from app.services.director import DirectorError, parse_result
from app.services.pipeline import build_edit_plan
from test_director import ASSETS, envelope, make_director, payload, propose


def parsed(data):
    return parse_result(json.dumps(data), ASSETS, require_edit_plan=True)


@pytest.mark.parametrize("change", [
    {"start": -1}, {"start": True}, {"end": "8"}, {"end": float("inf")},
    {"end": 2.01}, {"end": 9.0}, {"asset_id": "asset_b"},
    {"caption": "长" * 101}, {"command": "unsafe"},
])
def test_segment_rejects_untrusted_structure_or_ungrounded_range(change):
    data = payload()
    data["directions"][0]["edit_plan"]["segments"][0].update(change)
    with pytest.raises(DirectorError):
        parsed(data)


@pytest.mark.parametrize("plan", [None, {}, {"segments": [], "narration_text": ""},
    {"segments": [{"asset_id": "asset_a", "start": 2, "end": 8, "caption": ""}],
     "narration_text": "长" * 121}])
def test_new_output_requires_complete_plan(plan):
    data = payload()
    data["directions"][0]["edit_plan"] = plan
    with pytest.raises(DirectorError):
        parsed(data)


@pytest.mark.parametrize(("count", "narration", "accepted"), [
    (9, "有旁白", True), (10, "有旁白", False), (15, "", True), (16, "", False),
])
def test_duration_limit_counts_repeated_clips(count, narration, accepted):
    data = payload()
    plan = data["directions"][0]["edit_plan"]
    plan["segments"] *= count
    plan["narration_text"] = narration
    if accepted:
        parsed(data)
    else:
        with pytest.raises(DirectorError):
            parsed(data)


def test_missing_new_plan_does_not_silently_fall_back_on_live_adapter():
    data = payload()
    del data["directions"][0]["edit_plan"]
    # Historical storage remains readable; new billed results must meet new contract.
    assert parse_result(json.dumps(data), ASSETS).directions[0].edit_plan is None
    with pytest.raises(DirectorError) as error:
        propose(make_director(lambda _: httpx.Response(200, json=envelope(json.dumps(data)))))
    assert error.value.code == "INVALID_MODEL_OUTPUT"
    assert error.value.usage["cost_status"] == "known"


def test_two_levels_of_evidence_cannot_launder_unreviewed_ranges():
    data = payload()
    data["directions"][0]["evidence"][0]["end"] = 10
    data["directions"][0]["edit_plan"]["segments"][0]["end"] = 10
    with pytest.raises(DirectorError) as error:
        propose(make_director(lambda _: httpx.Response(200, json=envelope(json.dumps(data)))))
    assert error.value.code == "UNGROUNDED_EVIDENCE"


@pytest.mark.parametrize("packaging", ["standard", "light", "original", "variety", "variety_v2"])
def test_compile_preserves_model_order_not_evidence_order_and_respects_packaging(packaging):
    data = payload()["directions"][0]
    data["edit_plan"]["segments"] = [
        {"asset_id": "asset_a", "start": 6.0, "end": 8.0, "caption": "后半段"},
        {"asset_id": "asset_a", "start": 2.0, "end": 4.0, "caption": "前半段"},
    ]
    original = deepcopy(data)
    segments, narration = build_edit_plan(data, {"asset_a": SimpleNamespace(id="asset_a", duration=20)}, packaging)
    assert [segment["start"] for segment in segments] == [6, 2]
    assert narration == ("一位参与者举杯。" if packaging in ("standard", "variety", "variety_v2") else "")
    assert segments[0]["caption"] == ("后半段" if packaging in ("standard", "variety", "variety_v2") else "")
    assert data == original


def test_bad_stored_plan_is_rejected_not_truncated_or_replaced():
    data = payload()["directions"][0]
    data["edit_plan"]["segments"] *= 10
    with pytest.raises(DomainError) as error:
        build_edit_plan(data, {"asset_a": SimpleNamespace(id="asset_a", duration=20)})
    assert error.value.code == "INVALID_PLAN"
