"""Valhalla core: funding-graph clustering and wash detection over abstract data.

Detects manipulated vs. organic trading. It does NOT predict whether a project is
legitimate, and without identity it cannot fully resist Sybils — see README.
"""

from .clustering import Clustering, cluster_wallets
from .config import DEFAULT_CONFIG, DetectorConfig
from .graph import build_funding_graph, find_hubs
from .models import ClusterId, Side, Token, TokenId, Trade, Transfer, Wallet
from .wash import TokenReport, analyze, score_token

__all__ = [
    "DEFAULT_CONFIG",
    "ClusterId",
    "Clustering",
    "DetectorConfig",
    "Side",
    "Token",
    "TokenId",
    "TokenReport",
    "Trade",
    "Transfer",
    "Wallet",
    "analyze",
    "build_funding_graph",
    "cluster_wallets",
    "find_hubs",
    "score_token",
]
