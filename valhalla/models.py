"""Core data model. Chain-agnostic: a future indexer maps raw chain data onto these types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

Wallet = str
TokenId = str
ClusterId = str


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


@dataclass(frozen=True, slots=True)
class Transfer:
    """Native-token movement. ``from_wallet -> to_wallet`` means from_wallet funded to_wallet."""

    from_wallet: Wallet
    to_wallet: Wallet
    amount: float
    timestamp: int

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError(f"transfer amount must be >= 0, got {self.amount}")


@dataclass(frozen=True, slots=True)
class Trade:
    """A swap of ``token`` against the quote asset.

    ``amount`` is the trade's notional in the quote asset (e.g. native token), so
    volumes are comparable across tokens and buys/sells of one wallet can be netted
    against each other. Price drift between legs is ignored at this layer.
    """

    wallet: Wallet
    token: TokenId
    side: Side
    amount: float
    timestamp: int

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            object.__setattr__(self, "side", Side(self.side))
        if self.amount <= 0:
            raise ValueError(f"trade amount must be > 0, got {self.amount}")


@dataclass(frozen=True, slots=True)
class Token:
    token: TokenId
    deployer: Wallet | None = None
