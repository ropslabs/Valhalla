"""Per-token wash scoring on top of a wallet clustering.

A DEX trade has the pool as counterparty, so "volume inside one cluster" means: the
same economic actor both bought and sold the token within ``round_trip_window``, ending
with no net position from those legs. Opposite-side trades of one cluster are matched
FIFO within the window; every matched unit is wash on both legs:

* ``round_trip_volume``    — both legs from the same wallet,
* ``intra_cluster_volume`` — legs from different wallets of the same cluster.

Volume from the deployer's cluster that is not already wash is ``insider_volume``:
not manipulation per se, but not organic demand either. Everything else is organic:

    raw_volume = wash_volume + insider_volume + organic_volume
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

from .clustering import Clustering, cluster_wallets
from .config import DEFAULT_CONFIG, DetectorConfig
from .models import Side, Token, TokenId, Trade, Transfer, Wallet

_EPS = 1e-12


@dataclass(frozen=True)
class TokenReport:
    token: TokenId
    raw_volume: float
    wash_volume: float
    round_trip_volume: float
    intra_cluster_volume: float
    insider_volume: float
    organic_volume: float
    """Estimate: raw minus everything the detector attributes to wash or insiders."""
    wash_fraction: float
    n_wallets: int
    n_clusters: int
    """Independent economic actors among the token's traders."""
    net_new_buyers: int
    """Independent clusters (deployer's excluded) ending with a meaningful net long position."""

    @property
    def cluster_ratio(self) -> float:
        """Independent actors per wallet; near 1 is organic, near 0 is one actor with many wallets."""
        return self.n_clusters / self.n_wallets if self.n_wallets else 0.0


@dataclass
class _Lot:
    timestamp: int
    remaining: float
    wallet: Wallet
    index: int


def score_token(
    token: TokenId,
    trades: Iterable[Trade],
    clustering: Clustering,
    config: DetectorConfig = DEFAULT_CONFIG,
    deployer: Wallet | None = None,
) -> TokenReport:
    trades = sorted((t for t in trades if t.token == token), key=lambda t: t.timestamp)
    clusters = [clustering.cluster(t.wallet) for t in trades]
    washed = [0.0] * len(trades)
    self_rt = cross = 0.0

    open_lots: dict[tuple[str, Side], deque[_Lot]] = defaultdict(deque)
    for i, (t, c) in enumerate(zip(trades, clusters)):
        opposite = open_lots[(c, t.side.opposite)]
        while opposite and opposite[0].timestamp < t.timestamp - config.round_trip_window:
            opposite.popleft()
        remaining = t.amount
        while remaining > _EPS and opposite:
            lot = opposite[0]
            m = min(remaining, lot.remaining)
            lot.remaining -= m
            remaining -= m
            washed[i] += m
            washed[lot.index] += m
            if lot.wallet == t.wallet:
                self_rt += 2 * m
            else:
                cross += 2 * m
            if lot.remaining <= _EPS:
                opposite.popleft()
        if remaining > _EPS:
            open_lots[(c, t.side)].append(_Lot(t.timestamp, remaining, t.wallet, i))

    insider_cluster = clustering.cluster(deployer) if deployer is not None else None
    raw = sum(t.amount for t in trades)
    wash = self_rt + cross
    insider = sum(t.amount - washed[i] for i, (t, c) in enumerate(zip(trades, clusters)) if c == insider_cluster)

    bought: dict[str, float] = defaultdict(float)
    net: dict[str, float] = defaultdict(float)
    for t, c in zip(trades, clusters):
        if t.side is Side.BUY:
            bought[c] += t.amount
            net[c] += t.amount
        else:
            net[c] -= t.amount
    net_new_buyers = sum(
        1
        for c, n in net.items()
        if c != insider_cluster and n > max(config.dust_threshold, config.net_buyer_min_ratio * bought[c])
    )

    return TokenReport(
        token=token,
        raw_volume=raw,
        wash_volume=wash,
        round_trip_volume=self_rt,
        intra_cluster_volume=cross,
        insider_volume=insider,
        organic_volume=max(0.0, raw - wash - insider),
        wash_fraction=wash / raw if raw else 0.0,
        n_wallets=len({t.wallet for t in trades}),
        n_clusters=len(set(clusters)),
        net_new_buyers=net_new_buyers,
    )


def analyze(
    transfers: Iterable[Transfer],
    trades: Iterable[Trade],
    tokens: Iterable[Token] = (),
    config: DetectorConfig = DEFAULT_CONFIG,
) -> dict[TokenId, TokenReport]:
    """Pure entry point: data in, one report per token out.

    Clustering is global (an actor is an actor across tokens); scoring is per token.
    Tokens appear if they are traded or declared in ``tokens``.
    """
    trades = list(trades)
    deployers = {t.token: t.deployer for t in tokens}
    clustering = cluster_wallets(transfers, trades, config, extra_wallets=[d for d in deployers.values() if d])

    by_token: dict[TokenId, list[Trade]] = {tok: [] for tok in deployers}
    for t in trades:
        by_token.setdefault(t.token, []).append(t)
    return {
        tok: score_token(tok, tok_trades, clustering, config, deployers.get(tok))
        for tok, tok_trades in by_token.items()
    }
