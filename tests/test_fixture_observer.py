from fraeno.fixture_observer import observation
from fraeno.validation.observation import SystemObservation


def test_fixture_safe_and_breaking_profiles_are_valid_observations() -> None:
    safe = SystemObservation.from_dict(observation("1.0.1"))
    broken = SystemObservation.from_dict(observation("2.0.0"))

    assert safe.healthy
    assert not broken.healthy
    assert (
        broken.topics["/sensor/reading"].publishers[0].qos.reliability
        == "best_effort"
    )
