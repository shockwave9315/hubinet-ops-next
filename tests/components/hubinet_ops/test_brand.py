"""Tests for the integration's brand images."""

from pathlib import Path
import struct

import custom_components.hubinet_ops
import pytest

BRAND = Path(custom_components.hubinet_ops.__file__).parent / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_brand_icons_are_square_pngs(name: str, size: int) -> None:
    """Home Assistant serves these from the integration's brand folder."""
    data = (BRAND / name).read_bytes()
    assert data[:8] == PNG_SIGNATURE
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (size, size)


def test_brand_source_is_kept_next_to_the_images() -> None:
    """The editable SVG source ships with the rendered images."""
    assert (BRAND / "icon.svg").read_text().lstrip().startswith("<svg")
