"""Test the tip overlap selection logic in the API core."""
import pytest

from opentrons.protocol_api.core.engine.overlap_versions import overlap_for_api_version
from opentrons.protocols.api_support.types import APIVersion

from ... import versions_below, versions_at_or_above


@pytest.mark.parametrize(
    "api_version", versions_below(APIVersion(2, 19), flex_only=False)
)
def test_all_below_219_use_v0(api_version: APIVersion) -> None:
    """Versions below 2.19 should use v0."""
    assert overlap_for_api_version(api_version) == "v0"


@pytest.mark.parametrize("api_version", versions_at_or_above(APIVersion(2, 19)))
def test_all_above_219_use_v1(api_version: APIVersion) -> None:
    """Versions above 2.19 should use v1."""
    assert overlap_for_api_version(api_version) == "v1"


def test_future_api_version_uses_v1() -> None:
    """Future versions should use v1."""
    assert overlap_for_api_version(APIVersion(2, 99)) == "v1"
