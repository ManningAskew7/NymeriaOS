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

A shape depends on the LLM route the value sits beside. Where the judge's own
environment is not that route (the wizard's post-start check runs in a
container already recreated on the NEW route, while the app saved its copy
under the old one), the route travels as ``RouteFacts``: two booleans per
slot, rendered as ``route:`` argv tokens, never the route's values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Mapping, Optional

# ``unknown``: a vendor-shaped value whose route could not be judged, so it may
# be the vendor's key or a gateway's (a LiteLLM key also starts ``sk-``).
SlotShape = Literal["vendor", "gateway", "gatekeeper", "unknown"]


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


def _provider_is(provider: str, vendor: str) -> bool:
    from .llm_providers import normalize_llm_provider

    return bool(provider) and normalize_llm_provider(provider) == vendor


def _base_url_off_vendor(base_url: str, vendor: str) -> bool:
    base_url = (base_url or "").strip()
    return bool(base_url) and not _vendor_host(vendor)(base_url)


def route_feeds_gateway(provider: str, base_url: str, *, vendor: str) -> bool:
    """True when an LLM route makes ``vendor``'s key slot a gateway's key.

    ``provider`` is the route's provider (any spelling) and ``vendor`` the
    provider whose key slot is in question (``openai``, ``google``): only a
    route OF that provider reads the slot, and only a base URL off the
    vendor's own hosts makes the key the gateway's rather than the vendor's.
    """
    return _provider_is(provider, vendor) and _base_url_off_vendor(base_url, vendor)


@dataclass(frozen=True)
class RouteFacts:
    """What an LLM route says about one shared slot, as two booleans (#435).

    ``provider``: the route's provider is the slot's vendor; ``base_url``: the
    route's base URL is set and off the vendor's own hosts. Both true is
    ``route_feeds_gateway``. None is unknown (the caller could not read that
    part of the route). Never a value, so it may cross the wizard's exec
    boundary as an argv token.
    """

    provider: Optional[bool]
    base_url: Optional[bool]

    @property
    def feeds_gateway(self) -> Optional[bool]:
        """``route_feeds_gateway`` in three values: one known False decides."""
        if self.provider is False or self.base_url is False:
            return False
        if self.provider and self.base_url:
            return True
        return None


UNKNOWN_ROUTE = RouteFacts(None, None)


def route_facts(slot: str, *, provider: Optional[str], base_url: Optional[str]) -> RouteFacts:
    """``RouteFacts`` for the shared ``slot`` under a route; a None part is unknown."""
    vendor = VENDOR_KEY_SLOTS[slot].vendor
    return RouteFacts(
        None if provider is None else _provider_is(provider, vendor),
        None if base_url is None else _base_url_off_vendor(base_url, vendor),
    )


# argv tokens: `route:OPENAI_API_KEY:provider=1,base_url=?` (1, 0 or ? each).
# No double quote and no backslash (the Windows argv round trip).
ROUTE_FACTS_PREFIX = "route:"
_FACT_CODES = {True: "1", False: "0", None: "?"}
_ROUTE_TOKEN = re.compile(
    r"route:([A-Z][A-Z0-9_]*):provider=([01?]),base_url=([01?])"
)


def route_facts_tokens(facts: Mapping[str, RouteFacts]) -> list[str]:
    """One ``route:`` argv token per shared slot in ``facts``."""
    return [
        f"{ROUTE_FACTS_PREFIX}{slot}:provider={_FACT_CODES[fact.provider]},"
        f"base_url={_FACT_CODES[fact.base_url]}"
        for slot, fact in sorted(facts.items())
        if slot in VENDOR_KEY_SLOTS
    ]


def parse_route_facts(argv: Iterable[str]) -> Optional[dict[str, RouteFacts]]:
    """The ``route:`` tokens in ``argv``, per shared slot.

    None when there is no ``route:`` token at all: the caller then judges
    under its own environment's route. Otherwise every shared slot gets an
    entry, UNKNOWN for one with no token or a malformed one, so a garbled
    hint can only make a judgement more careful, never a guess.
    """
    tokens = [arg for arg in argv if arg.startswith(ROUTE_FACTS_PREFIX)]
    if not tokens:
        return None
    decode = {"1": True, "0": False, "?": None}
    facts = dict.fromkeys(VENDOR_KEY_SLOTS, UNKNOWN_ROUTE)
    for token in tokens:
        match = _ROUTE_TOKEN.fullmatch(token)
        if match and match.group(1) in VENDOR_KEY_SLOTS:
            facts[match.group(1)] = RouteFacts(
                decode[match.group(2)], decode[match.group(3)]
            )
    return facts


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


def shape_under_route(
    slot: str, value: str, *, feeds_gateway: Optional[bool]
) -> Optional[SlotShape]:
    """A shared slot's value judged under a route that does (or does not, or
    may) feed the slot to a gateway: the one rule behind ``shared_slot_shape``.

    ``feeds_gateway`` None (the route is unknown): a vendor-shaped value is
    ``unknown``, since it may be the vendor's key or a gateway's; anything
    else cannot be the vendor's key and reads None.
    """
    entry = VENDOR_KEY_SLOTS.get(slot)
    value = (value or "").strip()
    if entry is None or not value:
        return None
    if _looks_like_gatekeeper(value):
        return "gatekeeper"
    if feeds_gateway:
        return "gateway"
    vendor_shaped = value.startswith(entry.key_prefix)
    if feeds_gateway is None:
        return "unknown" if vendor_shaped else None
    return "vendor" if vendor_shaped else None


def shared_slot_shape(
    slot: str, value: str, *, provider: str, base_url: str
) -> Optional[SlotShape]:
    """``gatekeeper``, ``vendor``, ``gateway`` or None for a shared slot's value.

    None for an empty value, a slot with no direct twin, or a value that is
    neither (a non-vendor-shaped key beside a route that keeps the slot the
    vendor's). A shape name, never the value. The same rule as
    ``slot_holds_vendor_key`` and ``slot_holds_gateway_key``.
    """
    entry = VENDOR_KEY_SLOTS.get(slot)
    if entry is None:
        return None
    feeds = route_feeds_gateway(provider, base_url, vendor=entry.vendor)
    return shape_under_route(slot, value, feeds_gateway=feeds)


__all__ = [
    "ROUTE_FACTS_PREFIX",
    "RouteFacts",
    "SlotShape",
    "UNKNOWN_ROUTE",
    "VENDOR_KEY_SLOTS",
    "VendorKeySlot",
    "parse_route_facts",
    "route_facts",
    "route_facts_tokens",
    "route_feeds_gateway",
    "shape_under_route",
    "shared_slot_shape",
    "slot_holds_gateway_key",
    "slot_holds_vendor_key",
]
