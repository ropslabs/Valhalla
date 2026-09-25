"""Central tuning knobs. Every threshold the detector uses lives here."""

from __future__ import annotations

from dataclasses import dataclass, field

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


@dataclass(frozen=True)
class DetectorConfig:
    # --- funding graph -------------------------------------------------------
    dust_threshold: float = 0.01
    """Transfers below this amount are ignored (defeats dust-spam linking every wallet)."""

    hub_min_fanout: int = 50
    """A wallet that funded at least this many distinct wallets is treated as public
    infrastructure (CEX hot wallet, bridge, faucet) and neither links nor is traversed.
    Trade-off: an operator funding more wallets than this from one address escapes the
    common-source rule — but then pays for that many wallets and looks like a hub."""

    known_hubs: frozenset[str] = field(default_factory=frozenset)
    """Externally labelled infrastructure wallets, treated as hubs regardless of fanout."""

    # --- clustering ----------------------------------------------------------
    max_hops: int = 2
    """Two wallets are linked if they share a funding ancestor within this many hops
    (hop 0 = the wallet itself, so direct/mutual funding is always covered)."""

    funding_window: int = 3 * DAY
    """Funding within this distance of the recipient's first activity gets full weight."""

    funding_half_life: int = 7 * DAY
    """Beyond the window, a funding edge's weight halves every ``funding_half_life``."""

    min_link_strength: float = 0.25
    """Two wallets sharing an ancestor are linked if the product of their path
    strengths (product of edge weights along each path) reaches this value."""

    # --- wash detection ------------------------------------------------------
    round_trip_window: int = 1 * HOUR
    """Opposite-side trades of the same cluster within this window net out as wash."""

    net_buyer_min_ratio: float = 0.1
    """A cluster counts as a net new buyer if its net position exceeds this fraction
    of what it bought (and the dust threshold)."""

    def __post_init__(self) -> None:
        if not isinstance(self.known_hubs, frozenset):
            object.__setattr__(self, "known_hubs", frozenset(self.known_hubs))
        if self.dust_threshold < 0:
            raise ValueError("dust_threshold must be >= 0")
        if self.hub_min_fanout < 2:
            raise ValueError("hub_min_fanout must be >= 2")
        if self.max_hops < 0:
            raise ValueError("max_hops must be >= 0")
        if self.funding_window < 0 or self.funding_half_life <= 0:
            raise ValueError("funding_window must be >= 0 and funding_half_life > 0")
        if not 0 < self.min_link_strength <= 1:
            raise ValueError("min_link_strength must be in (0, 1]")
        if self.round_trip_window < 0:
            raise ValueError("round_trip_window must be >= 0")
        if not 0 <= self.net_buyer_min_ratio < 1:
            raise ValueError("net_buyer_min_ratio must be in [0, 1)")


DEFAULT_CONFIG = DetectorConfig()
