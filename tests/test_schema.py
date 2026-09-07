"""Tests for amv.schema — the airbag between the LLM and TouchDesigner."""

from __future__ import annotations

import copy

import jsonschema
import pytest

from amv.schema import (
    PALETTES,
    PARTICLE_MODES,
    SCENES,
    TRANSITION_MODES,
    DecisionError,
    load_schema,
    validate_and_clamp,
)


def base_decision() -> dict:
    return {
        "scene": "tunnel",
        "palette": "violet_cyan",
        "feedback": 0.4,
        "symmetry": 6,
        "camera_speed": 0.3,
        "particle_mode": "spiral",
        "projectm_mix": 0.0,
        "transition": {"mode": "glide", "beats": 4},
        "on_drop": {
            "scene": "particle_field",
            "palette": "infrared",
            "particle_mode": "burst",
        },
        "intent": "build tension into the drop",
    }


# -- schema shape -----------------------------------------------------------


def test_constants_match_spec():
    assert SCENES == (
        "fractal_temple",
        "tunnel",
        "particle_field",
        "kaleido_mesh",
        "projectm_blend",
    )
    assert PALETTES == ("violet_cyan", "acid_lime", "amber_dusk", "mono_white", "infrared")
    assert PARTICLE_MODES == ("spiral", "burst", "rain", "orbit", "none")
    assert TRANSITION_MODES == ("cut", "glide", "on_next_kick")


def test_schema_is_strict_everywhere():
    """OpenAI strict mode needs additionalProperties:false and full required lists."""

    def walk(node, path="<root>"):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path} allows extra properties"
            props = set(node["properties"])
            assert set(node.get("required", [])) == props, f"{path} does not require every field"
            for key, child in node["properties"].items():
                walk(child, f"{path}.{key}")

    walk(load_schema())


def test_valid_decision_passes_jsonschema():
    out = validate_and_clamp(base_decision())
    jsonschema.validate(out, load_schema())
    assert out == base_decision()


# -- clamping ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "given", "expected"),
    [
        ("feedback", 1.5, 0.98),
        ("feedback", -0.4, 0.0),
        ("camera_speed", 9.0, 1.0),
        ("camera_speed", -2, 0.0),
        ("projectm_mix", 3.3, 1.0),
        ("symmetry", 99, 16),
        ("symmetry", 0, 1),
        ("symmetry", -5, 1),
    ],
)
def test_numbers_are_clamped(field, given, expected):
    d = base_decision()
    d[field] = given
    assert validate_and_clamp(d)[field] == expected


def test_transition_beats_are_clamped():
    d = base_decision()
    d["transition"]["beats"] = 64
    assert validate_and_clamp(d)["transition"]["beats"] == 16


# -- coercion ---------------------------------------------------------------


def test_numeric_strings_are_coerced():
    d = base_decision()
    d["feedback"] = "0.55"
    d["symmetry"] = " 8 "
    d["camera_speed"] = "1"
    out = validate_and_clamp(d)
    assert out["feedback"] == pytest.approx(0.55)
    assert out["symmetry"] == 8 and isinstance(out["symmetry"], int)
    assert out["camera_speed"] == pytest.approx(1.0)


@pytest.mark.parametrize(("given", "expected"), [(6.7, 7), (6.2, 6), ("3.6", 4), (15.9, 16)])
def test_ints_are_rounded(given, expected):
    d = base_decision()
    d["symmetry"] = given
    out = validate_and_clamp(d)
    assert out["symmetry"] == expected
    assert isinstance(out["symmetry"], int)


def test_rounding_then_clamping_stays_in_range():
    d = base_decision()
    d["symmetry"] = 16.4  # rounds to 16, must not become 17
    d["transition"]["beats"] = 16.6  # rounds to 17, must clamp back to 16
    out = validate_and_clamp(d)
    assert out["symmetry"] == 16
    assert out["transition"]["beats"] == 16


def test_non_numeric_string_raises():
    d = base_decision()
    d["feedback"] = "quite a lot"
    with pytest.raises(DecisionError):
        validate_and_clamp(d)


def test_boolean_is_not_a_number():
    d = base_decision()
    d["feedback"] = True
    with pytest.raises(DecisionError):
        validate_and_clamp(d)


# -- intent -----------------------------------------------------------------


def test_intent_is_truncated_to_120_chars():
    d = base_decision()
    d["intent"] = "x" * 400
    out = validate_and_clamp(d)
    assert len(out["intent"]) == 120
    jsonschema.validate(out, load_schema())


def test_short_intent_is_untouched():
    d = base_decision()
    d["intent"] = "keep it"
    assert validate_and_clamp(d)["intent"] == "keep it"


# -- rejections -------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"scene": "hyper_cube"},
        {"palette": "beige"},
        {"particle_mode": "swarm"},
    ],
)
def test_invalid_enum_raises(patch):
    d = base_decision()
    d.update(patch)
    with pytest.raises(DecisionError):
        validate_and_clamp(d)


def test_invalid_nested_enum_raises():
    d = base_decision()
    d["transition"]["mode"] = "fade"
    with pytest.raises(DecisionError):
        validate_and_clamp(d)
    d = base_decision()
    d["on_drop"]["scene"] = "nope"
    with pytest.raises(DecisionError):
        validate_and_clamp(d)


@pytest.mark.parametrize("key", sorted(base_decision()))
def test_missing_key_raises(key):
    d = base_decision()
    del d[key]
    with pytest.raises(DecisionError):
        validate_and_clamp(d)


def test_missing_nested_key_raises():
    d = base_decision()
    del d["on_drop"]["particle_mode"]
    with pytest.raises(DecisionError):
        validate_and_clamp(d)
    d = base_decision()
    del d["transition"]["beats"]
    with pytest.raises(DecisionError):
        validate_and_clamp(d)


def test_non_dict_raises():
    with pytest.raises(DecisionError):
        validate_and_clamp(["tunnel"])  # type: ignore[arg-type]


def test_unknown_extra_keys_are_dropped():
    d = base_decision()
    d["strobe"] = 1.0
    out = validate_and_clamp(d)
    assert "strobe" not in out
    jsonschema.validate(out, load_schema())


def test_input_is_not_mutated():
    d = base_decision()
    before = copy.deepcopy(d)
    d["feedback"] = 5.0
    before["feedback"] = 5.0
    validate_and_clamp(d)
    assert d == before
