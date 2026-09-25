"""Funding graph: nodes are wallets, a directed edge A -> B means A funded B."""

from __future__ import annotations

from typing import Iterable

import networkx as nx

from .config import DEFAULT_CONFIG, DetectorConfig
from .models import Transfer, Wallet


def build_funding_graph(transfers: Iterable[Transfer], config: DetectorConfig = DEFAULT_CONFIG) -> nx.DiGraph:
    """Aggregate non-dust transfers into one edge per (funder, recipient) pair.

    Edge attributes: ``amount`` (sum) and ``timestamps`` (every contributing transfer,
    in input order) — the latter feeds the time weighting in clustering.
    """
    g = nx.DiGraph()
    for t in transfers:
        if t.amount < config.dust_threshold or t.from_wallet == t.to_wallet:
            continue
        if g.has_edge(t.from_wallet, t.to_wallet):
            edge = g[t.from_wallet][t.to_wallet]
            edge["amount"] += t.amount
            edge["timestamps"].append(t.timestamp)
        else:
            g.add_edge(t.from_wallet, t.to_wallet, amount=t.amount, timestamps=[t.timestamp])
    return g


def find_hubs(graph: nx.DiGraph, config: DetectorConfig = DEFAULT_CONFIG) -> frozenset[Wallet]:
    """Public infrastructure: high-fanout funders plus externally labelled wallets."""
    by_fanout = {n for n in graph if graph.out_degree(n) >= config.hub_min_fanout}
    return frozenset(by_fanout | config.known_hubs)
