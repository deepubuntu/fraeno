import pytest

from fraeno.validation.rate_window import MessageRateWindow


def test_messages_before_measurement_window_are_excluded() -> None:
    window = MessageRateWindow()

    for _ in range(60):
        window.record()

    window.begin()
    for _ in range(100):
        window.record()

    assert window.count == 100
    assert window.rate_hz(5.0) == 20.0


def test_begin_resets_a_previous_measurement_window() -> None:
    window = MessageRateWindow()
    window.begin()
    window.record()

    window.begin()

    assert window.count == 0


def test_rate_requires_a_positive_measurement_duration() -> None:
    window = MessageRateWindow()
    window.begin()

    with pytest.raises(ValueError, match="greater than zero"):
        window.rate_hz(0)
