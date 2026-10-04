"""Host and guest memory of one QEMU VM from PVE's opt-in full listing.

Used only when the entry option requests `qemu.get(full=1)`. With balloon
statistics PVE replaces `mem` by the guest's own view (`total_mem - free_mem`)
and reports host-side usage as `memhost`. A value is published only when the
payload establishes it; otherwise it is unknown, never a copied number.
"""

from collections.abc import Mapping
import math
from typing import Any

from .const import VM_CONTAINER_RUNNING


def _amount(value: Any) -> int | float | None:
    """Return a finite, non-negative PVE number, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _guest_evidence(data: Mapping[str, Any]) -> bool:
    """Return whether `mem` may be a guest-side value in this payload."""
    if "freemem" in data:
        return True
    info = data.get("ballooninfo")
    if info is None:
        return False
    if not isinstance(info, Mapping):
        return True
    return "total_mem" in info or "free_mem" in info


def vm_host_memory(data: Mapping[str, Any]) -> int | float | None:
    """Return host-side memory use in bytes.

    `memhost` is the host value. `mem` stands in for it only when nothing in
    the payload suggests PVE replaced `mem` with a guest-side value.
    """
    if "memhost" in data:
        return _amount(data["memhost"])
    if _guest_evidence(data):
        return None
    return _amount(data.get("mem"))


def vm_guest_memory(data: Mapping[str, Any]) -> int | float | None:
    """Return guest-reported memory use in bytes from consistent balloon data."""
    if data.get("status") != VM_CONTAINER_RUNNING:
        return None
    info = data.get("ballooninfo")
    if not isinstance(info, Mapping):
        return None
    maximum = _amount(info.get("max_mem"))
    total = _amount(info.get("total_mem"))
    free = _amount(info.get("free_mem"))
    used = _amount(data.get("mem"))
    if maximum is None or maximum <= 0 or total is None or free is None:
        return None
    if free > total or used is None or used != total - free:
        return None
    if "freemem" in data and _amount(data["freemem"]) != free:
        return None
    return used


def _percentage(value: float | None, data: Mapping[str, Any]) -> float | None:
    """Return the unclamped share of a positive `maxmem`."""
    maximum = _amount(data.get("maxmem"))
    if value is None or maximum is None or maximum <= 0:
        return None
    return value / maximum * 100


def vm_host_memory_percentage(data: Mapping[str, Any]) -> float | None:
    """Return host-side memory use as a percentage of `maxmem`."""
    return _percentage(vm_host_memory(data), data)


def vm_guest_memory_percentage(data: Mapping[str, Any]) -> float | None:
    """Return guest-reported memory use as a percentage of `maxmem`."""
    return _percentage(vm_guest_memory(data), data)
