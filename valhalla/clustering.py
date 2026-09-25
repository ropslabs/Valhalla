"""Collapse wallets into "economic actors" via shared funding ancestry.

Rule: two participating wallets belong to the same actor if they share a funding
ancestor within ``max_hops`` (a wallet is its own ancestor at hop 0, so direct and
mutual funding are covered), and the product of their path strengths reaches
``min_link_strength``. A path's strength is the product of its edge weights; an edge
weighs 1.0 if the funding happened within ``funding_window`` of the recipient's first
activity and decays with ``funding_half_life`` beyond that. Hubs are skipped entirely.
Links are transitive (connected components).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

import networkx as nx

from .config import DEFAULT_CONFIG, DetectorConfig
from .graph import build_funding_graph, find_hubs
from .models import ClusterId, Trade, Transfer, Wallet


@dataclass(frozen=True)
class Clustering:
    cluster_of: Mapping[Wallet, ClusterId]
    clusters: Mapping[ClusterId, frozenset[Wallet]]
    hubs: frozenset[Wallet]

    def cluster(self, wallet: Wallet) -> ClusterId:
        """Cluster id of ``wallet``; wallets never seen are their own singleton actor."""
        return self.cluster_of.get(wallet, wallet)

    def members(self, cluster_id: ClusterId) -> frozenset[Wallet]:
        return self.clusters.get(cluster_id, frozenset({cluster_id}))

    def same_cluster(self, a: Wallet, b: Wallet) -> bool:
        return self.cluster(a) == self.cluster(b)


def first_activity(graph: nx.DiGraph, trades: Iterable[Trade]) -> dict[Wallet, int]:
    """Earliest trade or outgoing (non-dust) transfer per wallet."""
    first: dict[Wallet, int] = {}
    for t in trades:
        if t.timestamp < first.get(t.wallet, t.timestamp + 1):
            first[t.wallet] = t.timestamp
    for u, _, data in graph.edges(data=True):
        ts = min(data["timestamps"])
        if ts < first.get(u, ts + 1):
            first[u] = ts
    return first


def funding_weight(funded_at: int, first_active: int | None, config: DetectorConfig) -> float:
    if first_active is None:
        return 1.0
    excess = abs(first_active - funded_at) - config.funding_window
    return 1.0 if excess <= 0 else 0.5 ** (excess / config.funding_half_life)


def funding_ancestors(
    graph: nx.DiGraph,
    wallet: Wallet,
    first_active: Mapping[Wallet, int],
    hubs: frozenset[Wallet],
    config: DetectorConfig,
) -> dict[Wallet, float]:
    """Ancestors within ``max_hops`` mapped to their strongest path strength.

    Paths weaker than ``min_link_strength`` are pruned: the partner's strength is at
    most 1.0, so such a path can never produce a link.
    """
    best = {wallet: 1.0}
    frontier = {wallet: 1.0}
    for _ in range(config.max_hops):
        nxt: dict[Wallet, float] = {}
        for node, strength in frontier.items():
            if node not in graph:
                continue
            for funder in graph.predecessors(node):
                if funder in hubs:
                    continue
                w = max(funding_weight(ts, first_active.get(node), config) for ts in graph[funder][node]["timestamps"])
                s = strength * w
                if s >= config.min_link_strength and s > best.get(funder, 0.0):
                    best[funder] = s
                    nxt[funder] = s
        frontier = nxt
    return best


def cluster_wallets(
    transfers: Iterable[Transfer],
    trades: Iterable[Trade],
    config: DetectorConfig = DEFAULT_CONFIG,
    extra_wallets: Iterable[Wallet] = (),
) -> Clustering:
    """Cluster every trading wallet plus ``extra_wallets`` (e.g. token deployers).

    Cluster ids are the lexicographically smallest member, so results are independent
    of input order.
    """
    trades = list(trades)
    graph = build_funding_graph(transfers, config)
    hubs = find_hubs(graph, config)
    first_active = first_activity(graph, trades)

    participants = sorted({t.wallet for t in trades} | set(extra_wallets))
    # ancestor -> [(participant, strength)]
    reached: dict[Wallet, list[tuple[Wallet, float]]] = {}
    for p in participants:
        for anc, s in funding_ancestors(graph, p, first_active, hubs, config).items():
            reached.setdefault(anc, []).append((p, s))

    links = nx.Graph()
    links.add_nodes_from(participants)
    for members in reached.values():
        if len(members) < 2:
            continue
        # If any pair under this ancestor qualifies, the strongest member qualifies with
        # each of them, so linking everyone to the strongest one is sufficient.
        anchor, s_max = max(members, key=lambda m: (m[1], m[0]))
        for p, s in members:
            if p != anchor and s * s_max >= config.min_link_strength:
                links.add_edge(anchor, p)

    cluster_of: dict[Wallet, ClusterId] = {}
    clusters: dict[ClusterId, frozenset[Wallet]] = {}
    for component in nx.connected_components(links):
        cid = min(component)
        clusters[cid] = frozenset(component)
        for w in component:
            cluster_of[w] = cid
    return Clustering(cluster_of=cluster_of, clusters=clusters, hubs=hubs)
