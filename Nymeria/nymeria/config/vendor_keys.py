"""Whose key a shared provider slot holds: the vendor's, a gateway's, or a proxy's.

``OPENAI_API_KEY`` and ``GEMINI_API_KEY`` serve two masters: the LLM route
(which may send them to a gateway such as LiteLLM or CLIProxy) and the media
tools (which always call the vendor). Judging which one a stored value belongs
to decides where it may go (#431, #433). The wizard judges ``.env.docker``
values; since #435 the container also judges the app's saved copy in
``/data/settings.env``, so these predicates live in the config layer and the
in-container report never imports the wizard package (``nymeria.setup`` pulls
the runner and finalize). ``setup/tool_keys.py`` re-exports them.

Values are judged in memory only: callers get a boolean or a shape name, never
a fragment of the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional

SlotShape = Literal["vendor", "gateway", "gatekeeper"]


@dataclass(frozen=True)
class VendorKeySlot:
    """A shared LLM key slot whose real vendor key has a direct-call twin."""

    # Where a real vendor key lives while a gateway owns the shared slot.
    direct: str
    # The LLM provider whose route key lives in the shared slot.
    vendor: str
    # Every key the vendor issues starts with this; a value without it is not
    # treated as the vendor's key (a hand-kept proxy key, say).
    key_prefix: str


# Keyed by the shared slot. Anthropic has the same split
# (``secret_keys.DIRECT_KEY_SLOTS``) but no vendor-shape rule here: its
# gatekeeper and direct key never share a slot the media tools read.
VENDOR_KEY_SLOTS: dict[str, VendorKeySlot] = {
    "GEMINI_API_KEY": VendorKeySlot("GEMINI_DIRECT_API_KEY", "google", "AIza"),
    "OPENAI_API_KEY": VendorKeySlot("OPENAI_DIRECT_API_KEY", "openai", "sk-"),
}


def _vendor_host(vendor: str) -> Callable[[str], bool]:
    from .settings import is_google_api_host, is_openai_api_host

    return is_google_api_host if vendor == "google" else is_openai_api_host


def route_feeds_gateway(provider: str, base_url: str, *, vendor: str) -> bool:
    """True when an LLM route makes ``vendor``'s key slot a gateway's key.

    ``provider`` is the route's provider (any spelling) and ``vendor`` the
    provider whose key slot is in question (``openai``, ``google``): only a
    route OF that provider reads the slot, and only a base URL off the
    vendor's own hosts makes the key the gateway's rather than the vendor's.
    """
    from .llm_providers import normalize_llm_provider

    if not provider or normalize_llm_provider(provider) != vendor:
        return False
    base_url = (base_url or "").strip()
    return bool(base_url) and not _vendor_host(vendor)(base_url)


def _looks_like_gatekeeper(value: str) -> bool:
    from ..vendor.react_agent.cliproxy import looks_like_cliproxy_gatekeeper_key

    return looks_like_cliproxy_gatekeeper_key(value)


def slot_holds_vendor_key(slot: str, value: str, *, provider: str, base_url: str) -> bool:
    """True when ``value`` in the shared ``slot`` is the VENDOR's own key.

    ``provider`` and ``base_url`` are the LLM route that was configured beside
    it. Vendor-shaped (``sk-`` for OpenAI, ``AIza`` for Google), not a
    ``cpx-`` gatekeeper, and not behind a route that feeds the slot to a
    gateway (whose key it would then be). Such a value is not a usable CLIProxy
    gatekeeper and must never become a gateway's bearer; a reconfigure that
    hands the slot to a gateway moves it to the direct slot instead (#431).
    Only slots with a direct twin qualify.
    """
    direct = VENDOR_KEY_SLOTS.get(slot)
    value = (value or "").strip()
    # The vendor prefix also rules out a ``cpx-`` gatekeeper.
    if direct is None or not value.startswith(direct.key_prefix):
        return False
    return not route_feeds_gateway(provider, base_url, vendor=direct.vendor)


def slot_holds_gateway_key(slot: str, value: str, *, provider: str, base_url: str) -> bool:
    """True when ``value`` in the shared ``slot`` is a GATEWAY's key.

    The complement of ``slot_holds_vendor_key`` for a set slot: the route
    configured beside it (``provider``, ``base_url``) feeds the slot to a
    gateway (LiteLLM, a local server, a proxy), so the value only works
    there. ``cpx-`` gatekeepers are tracked apart. Such a value must not
    outlive its route: once the slot is the vendor's again the media tools
    would send it to the vendor (#433). Only slots with a direct twin qualify.
    """
    direct = VENDOR_KEY_SLOTS.get(slot)
    value = (value or "").strip()
    if direct is None or not value or _looks_like_gatekeeper(value):
        return False
    return route_feeds_gateway(provider, base_url, vendor=direct.vendor)


def shared_slot_shape(
    slot: str, value: str, *, provider: str, base_url: str
) -> Optional[SlotShape]:
    """``gatekeeper``, ``vendor``, ``gateway`` or None for a shared slot's value.

    None for an empty value, a slot with no direct twin, or a value that is
    neither (a non-vendor-shaped key beside a route that keeps the slot the
    vendor's). A shape name, never the value.
    """
    if slot not in VENDOR_KEY_SLOTS or not (value or "").strip():
        return None
    if _looks_like_gatekeeper(value):
        return "gatekeeper"
    if slot_holds_vendor_key(slot, value, provider=provider, base_url=base_url):
        return "vendor"
    if slot_holds_gateway_key(slot, value, provider=provider, base_url=base_url):
        return "gateway"
    return None


__all__ = [
    "SlotShape",
    "VENDOR_KEY_SLOTS",
    "VendorKeySlot",
    "route_feeds_gateway",
    "shared_slot_shape",
    "slot_holds_gateway_key",
    "slot_holds_vendor_key",
]
