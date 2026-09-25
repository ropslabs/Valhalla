"""Token signals on top of the wallet clustering: holder concentration, net new
independent buyers over time, retention, and the combined organic_score.

Everything here is measured per CLUSTER (economic actor), never per raw wallet.
Otherwise one actor splitting activity over many wallets would look like broad,
organic participation.

Positions are derived from trades as net quote value per cluster (buys minus sells).
A position is "meaningful" by the same rule the wash module uses for net new buyers:
above the dust threshold and above ``net_buyer_min_ratio`` of what the cluster bought.

All functions take the trades of ONE token and an ``as_of`` observation time
(default: the latest trade given). Trades after ``as_of`` are ignored.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .clustering import Clustering, cluster_wallets
from .config import DEFAULT_CONFIG, DetectorConfig
from .models import ClusterId, Side, Token, TokenId, Trade, Transfer, Wallet
from .wash import TokenReport, score_token


@dataclass(frozen=True)
class Concentration:
    top_n_share: float
    """Share of the top ``concentration_top_n`` clusters in the total position (1.0 if no holders)."""
    gini: float
    """Gini coefficient over holder clusters' positions (0 = equal)."""
    n_holders: int
    total_position: float
    deployer_share: float
    """Share of the deployer's cluster in the total position; included in the figures above."""


@dataclass(frozen=True)
class NewBuyers:
    series: tuple[tuple[int, int], ...]
    """(window start, distinct clusters that first reached a meaningful position in that window)."""
    total: int
    rate: float
    """``total`` per ``signal_window`` since the token's first trade — comparable across token ages."""


@dataclass(frozen=True)
class Retention:
    rate: float | None
    """Retained / evaluated buyer clusters; None if no buyer is old enough to judge."""
    n_retained: int
    n_evaluated: int

    def effective(self, config: DetectorConfig = DEFAULT_CONFIG) -> float:
        return self.rate if self.rate is not None else config.retention_prior


@dataclass(frozen=True)
class TokenSignals:
    token: TokenId
    wash: TokenReport
    concentration: Concentration
    new_buyers: NewBuyers
    retention: Retention
    organic_score: float


def _prepare(trades: Iterable[Trade], as_of: int | None) -> tuple[list[Trade], int]:
    trades = sorted(trades, key=lambda t: t.timestamp)
    if as_of is None:
        as_of = trades[-1].timestamp if trades else 0
    return [t for t in trades if t.timestamp <= as_of], as_of


def _meaningful(position: float, bought: float, config: DetectorConfig) -> bool:
    return position > max(config.dust_threshold, config.net_buyer_min_ratio * bought)


def _signed(t: Trade) -> float:
    return t.amount if t.side is Side.BUY else -t.amount


def holder_concentration(
    trades: Iterable[Trade],
    clustering: Clustering,
    config: DetectorConfig = DEFAULT_CONFIG,
    deployer: Wallet | None = None,
    as_of: int | None = None,
) -> Concentration:
    trades, _ = _prepare(trades, as_of)
    positions: dict[ClusterId, float] = defaultdict(float)
    for t in trades:
        positions[clustering.cluster(t.wallet)] += _signed(t)
    holders = {c: p for c, p in positions.items() if p > config.dust_threshold}
    total = sum(holders.values())
    if not holders:
        return Concentration(top_n_share=1.0, gini=0.0, n_holders=0, total_position=0.0, deployer_share=0.0)

    ranked = sorted(holders.values())
    n = len(ranked)
    top = sum(ranked[-config.concentration_top_n :])
    gini = 2 * sum(i * x for i, x in enumerate(ranked, start=1)) / (n * total) - (n + 1) / n
    insider = holders.get(clustering.cluster(deployer), 0.0) if deployer is not None else 0.0
    return Concentration(
        top_n_share=top / total,
        gini=max(0.0, gini),
        n_holders=n,
        total_position=total,
        deployer_share=insider / total,
    )


def new_buyers(
    trades: Iterable[Trade],
    clustering: Clustering,
    config: DetectorConfig = DEFAULT_CONFIG,
    deployer: Wallet | None = None,
    as_of: int | None = None,
) -> NewBuyers:
    """Per ``signal_window``: distinct clusters that, for the first time, end the window
    holding a meaningful position. A cluster is counted at most once, ever; the
    deployer's cluster never."""
    trades, as_of = _prepare(trades, as_of)
    if not trades:
        return NewBuyers(series=(), total=0, rate=0.0)
    w = config.signal_window
    first = trades[0].timestamp
    n_windows = (as_of - first) // w + 1
    insider = clustering.cluster(deployer) if deployer is not None else None

    by_window: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        by_window[(t.timestamp - first) // w].append(t)

    position: dict[ClusterId, float] = defaultdict(float)
    bought: dict[ClusterId, float] = defaultdict(float)
    counted: set[ClusterId] = set()
    counts = [0] * n_windows
    for i in range(n_windows):
        touched = set()
        for t in by_window.get(i, ()):
            c = clustering.cluster(t.wallet)
            position[c] += _signed(t)
            if t.side is Side.BUY:
                bought[c] += t.amount
            touched.add(c)
        for c in touched - counted:
            if c != insider and _meaningful(position[c], bought[c], config):
                counted.add(c)
                counts[i] += 1

    return NewBuyers(
        series=tuple((first + i * w, n) for i, n in enumerate(counts)),
        total=len(counted),
        rate=len(counted) / n_windows,
    )


def retention(
    trades: Iterable[Trade],
    clustering: Clustering,
    config: DetectorConfig = DEFAULT_CONFIG,
    as_of: int | None = None,
) -> Retention:
    """Share of buyer clusters still holding a meaningful position
    ``retention_windows * signal_window`` after their first buy. Clusters whose horizon
    lies after ``as_of`` are not judged yet."""
    trades, as_of = _prepare(trades, as_of)
    horizon = config.retention_windows * config.signal_window
    by_cluster: dict[ClusterId, list[Trade]] = defaultdict(list)
    for t in trades:
        by_cluster[clustering.cluster(t.wallet)].append(t)

    evaluated = retained = 0
    for cluster_trades in by_cluster.values():
        first_buy = next((t.timestamp for t in cluster_trades if t.side is Side.BUY), None)
        if first_buy is None or first_buy + horizon > as_of:
            continue
        until = [t for t in cluster_trades if t.timestamp <= first_buy + horizon]
        position = sum(_signed(t) for t in until)
        bought = sum(t.amount for t in until if t.side is Side.BUY)
        evaluated += 1
        retained += _meaningful(position, bought, config)

    return Retention(rate=retained / evaluated if evaluated else None, n_retained=retained, n_evaluated=evaluated)


def organic_score(
    wash_fraction: float,
    concentration: Concentration,
    new_buyers: NewBuyers,
    retention: Retention,
    config: DetectorConfig = DEFAULT_CONFIG,
) -> float:
    """Weighted combination; range [-weight_wash, weight_new_buyers + weight_retention +
    weight_concentration]. Age-normalized through the new-buyer rate; retention falls
    back to ``retention_prior`` while no buyer is old enough to judge."""
    new_component = new_buyers.rate / (new_buyers.rate + config.new_buyers_scale)
    return (
        config.weight_new_buyers * new_component
        + config.weight_retention * retention.effective(config)
        + config.weight_concentration * (1.0 - concentration.top_n_share)
        - config.weight_wash * wash_fraction
    )


def analyze_signals(
    transfers: Iterable[Transfer],
    trades: Iterable[Trade],
    tokens: Iterable[Token] = (),
    config: DetectorConfig = DEFAULT_CONFIG,
    as_of: int | None = None,
) -> dict[TokenId, TokenSignals]:
    """All signals per token, over one global clustering. ``as_of`` defaults to the latest
    trade across ALL tokens, so token ages are measured against the same clock."""
    trades, as_of = _prepare(trades, as_of)
    deployers = {t.token: t.deployer for t in tokens}
    clustering = cluster_wallets(transfers, trades, config, extra_wallets=[d for d in deployers.values() if d])

    by_token: dict[TokenId, list[Trade]] = {tok: [] for tok in deployers}
    for t in trades:
        by_token.setdefault(t.token, []).append(t)

    out: dict[TokenId, TokenSignals] = {}
    for tok, tok_trades in by_token.items():
        deployer = deployers.get(tok)
        wash = score_token(tok, tok_trades, clustering, config, deployer)
        conc = holder_concentration(tok_trades, clustering, config, deployer, as_of)
        nb = new_buyers(tok_trades, clustering, config, deployer, as_of)
        ret = retention(tok_trades, clustering, config, as_of)
        out[tok] = TokenSignals(
            token=tok,
            wash=wash,
            concentration=conc,
            new_buyers=nb,
            retention=ret,
            organic_score=organic_score(wash.wash_fraction, conc, nb, ret, config),
        )
    return out
