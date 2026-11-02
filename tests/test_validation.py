"""check_value(): one form value against its field's rules."""

from typing import Any

import pytest
from fastapi import HTTPException

from app import tools
from app.server import check_value


def field(tool: str, name: str) -> dict[str, Any]:
    return next(f for f in tools.BY_ID[tool]["fields"] if f["name"] == name)


PROMPT = field("image.moss", "prompt")
STEPS = field("image.moss", "steps")
SIZE = field("image.moss", "size")
SEED = field("image.moss", "seed")
NEGATIVE = field("image.moss", "negative")
AUDIO = field("video.t2v", "audio")
END_AT = field("video.extend", "end_at")


def refused(f: dict[str, Any], value: str | None) -> str:
    with pytest.raises(HTTPException) as exc:
        check_value(f, value)
    assert exc.value.status_code == 400
    return str(exc.value.detail)


@pytest.mark.parametrize("value", [None, "", "   ", "\n\t"])
def test_required_field_needs_text(value: str | None) -> None:
    assert refused(PROMPT, value) == "Prompt is required"


@pytest.mark.parametrize("value", [None, ""])
def test_empty_optional_field_takes_the_default(value: str | None) -> None:
    assert check_value(SEED, value) is None
    assert check_value(NEGATIVE, value) is None


def test_text_is_stored_as_given() -> None:
    assert check_value(PROMPT, "  a lighthouse\n") == "  a lighthouse\n"


@pytest.mark.parametrize("value", ["10", "100", "55.5", " 20 "])
def test_number_in_range(value: str) -> None:
    assert check_value(STEPS, value) == value


@pytest.mark.parametrize("value", ["9", "9.99", "101", "-1"])
def test_number_out_of_range(value: str) -> None:
    assert refused(STEPS, value) == "Steps must be between 10 and 100"


@pytest.mark.parametrize("value", ["abc", "1,5", "0x10"])
def test_number_must_parse(value: str) -> None:
    assert refused(STEPS, value) == "Steps must be a number"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e400"])
def test_number_must_be_finite(value: str) -> None:
    assert refused(STEPS, value) == "Steps must be a finite number"


@pytest.mark.parametrize("value", ["0", "42", "4294967295"])
def test_seed_in_range(value: str) -> None:
    assert check_value(SEED, value) == value


@pytest.mark.parametrize("value", ["-1", "4294967296", "1e10"])
def test_seed_out_of_range(value: str) -> None:
    assert refused(SEED, value) == "Seed must be between 0 and 4294967295"


def test_select_value_must_be_an_option() -> None:
    assert check_value(SIZE, "1024x1024") == "1024x1024"
    assert refused(SIZE, "800x600") == "Size: unknown option"
    assert refused(SIZE, "704x1280") == "Size: unknown option"  # a video size


@pytest.mark.parametrize("value", ["true", "false"])
def test_checkbox_accepts_true_and_false(value: str) -> None:
    assert check_value(AUDIO, value) == value


@pytest.mark.parametrize("value", ["on", "1", "True", "yes"])
def test_checkbox_refuses_other_values(value: str) -> None:
    assert refused(AUDIO, value) == "Generate sound must be true or false"


def test_number_with_only_a_minimum_accepts_large_values() -> None:
    assert check_value(END_AT, "1.1") == "1.1"
    assert check_value(END_AT, "600") == "600"


@pytest.mark.xfail(
    reason="check_value prints the missing bound as None: 'must be between 1.1 and None'",
    strict=True,
)
def test_number_with_only_a_minimum_names_its_bound() -> None:
    assert "None" not in refused(END_AT, "0.5")
