"""Seeded, reproducible synthetic market data with ground truth.

Three scenarios (see README):

* ORGANIC — many independent wallets, each funded from its own external source (or a
  CEX hot wallet), diverse buy/hold/sell behaviour.
* WASH    — the threat model: a market-making bot. A few wallets, all funded from ONE
  operator (itself funded by the deployer), trading back and forth; barely any new holders,
  plus spike buyers lured by the fake volume who sell out within hours.
* MIXED   — an organic base with a bot overlay (and some spike buyers) on the same token.

Every dataset carries a ``GroundTruth`` per token so tests can check the detector's
estimates against what was actually generated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .config import DAY, HOUR, MINUTE
from .models import Side, Token, TokenId, Trade, Transfer, Wallet

T0 = 1_700_000_000
CEX_HOT_WALLET = "cex:hot_wallet"


class ScenarioKind(str, Enum):
    ORGANIC = "organic"
    WASH = "wash"
    MIXED = "mixed"


@dataclass(frozen=True)
class GroundTruth:
    token: TokenId
    kind: ScenarioKind
    deployer: Wallet
    organic_wallets: frozenset[Wallet]
    bot_wallets: frozenset[Wallet]
    organic_volume: float
    wash_volume: float

    @property
    def raw_volume(self) -> float:
        return self.organic_volume + self.wash_volume

    @property
    def wash_fraction(self) -> float:
        return self.wash_volume / self.raw_volume if self.raw_volume else 0.0


@dataclass(frozen=True)
class Dataset:
    transfers: tuple[Transfer, ...]
    trades: tuple[Trade, ...]
    tokens: tuple[Token, ...]
    truth: Mapping[TokenId, GroundTruth] = field(default_factory=dict)

    def merge(self, *others: Dataset) -> Dataset:
        parts = (self, *others)
        truth: dict[TokenId, GroundTruth] = {}
        for p in parts:
            overlap = truth.keys() & p.truth.keys()
            if overlap:
                raise ValueError(f"token ids collide when merging: {sorted(overlap)}")
            truth.update(p.truth)
        return Dataset(
            transfers=tuple(sorted((t for p in parts for t in p.transfers), key=_ts)),
            trades=tuple(sorted((t for p in parts for t in p.trades), key=_ts)),
            tokens=tuple(t for p in parts for t in p.tokens),
            truth=truth,
        )


def _ts(x: Transfer | Trade) -> int:
    return x.timestamp


# --- building blocks ---------------------------------------------------------


def _cex_background(rng: random.Random, prefix: str, n: int, start: int, span: int) -> list[Transfer]:
    """Exchange withdrawals to unrelated users — gives the CEX its realistic fanout."""
    return [
        Transfer(CEX_HOT_WALLET, f"{prefix}:cexuser{i}", round(rng.uniform(0.1, 20), 4), start + rng.randrange(span))
        for i in range(n)
    ]


def _organic_actors(
    rng: random.Random,
    token: TokenId,
    n_wallets: int,
    start: int,
    span: int,
    cex_share: float,
    related_share: float,
) -> tuple[list[Transfer], list[Trade], set[Wallet]]:
    """Independent retail wallets. Most are funded from their own external source, some
    via the CEX hot wallet, and a few by another participant (friends/family — a genuine
    small cluster)."""
    transfers: list[Transfer] = []
    trades: list[Trade] = []
    wallets: set[Wallet] = set()
    for i in range(n_wallets):
        w = f"{token}:org{i}"
        wallets.add(w)
        fund_ts = start + rng.randrange(span)
        r = rng.random()
        if i > 0 and r < related_share:
            funder = f"{token}:org{rng.randrange(i)}"
        elif r < related_share + cex_share:
            funder = CEX_HOT_WALLET
        else:
            funder = f"{token}:src{i}"
        transfers.append(Transfer(funder, w, round(rng.uniform(0.5, 5.0), 4), fund_ts))

        t = fund_ts + int(rng.expovariate(1 / (6 * HOUR)))
        position = 0.0
        for k in range(rng.choice([1, 1, 2, 2, 3, 4])):
            if k > 0:
                t += 1 + int(rng.expovariate(1 / (2 * DAY)))
            if k == 0 or position <= 0 or rng.random() < 0.5:
                amount = rng.lognormvariate(0.0, 0.8)
                side = Side.BUY
                position += amount
            else:
                amount = position * rng.uniform(0.3, 1.0)
                side = Side.SELL
                position -= amount
            trades.append(Trade(w, token, side, round(amount, 6), t))
    return transfers, trades, wallets


def _flippers(
    rng: random.Random, token: TokenId, n: int, start: int, span: int
) -> tuple[list[Transfer], list[Trade], set[Wallet]]:
    """Independent spike buyers lured by (fake) volume: they buy once and sell out a few
    hours later — outside the wash window, so organic by volume, but not retained."""
    transfers: list[Transfer] = []
    trades: list[Trade] = []
    wallets: set[Wallet] = set()
    for i in range(n):
        w = f"{token}:flip{i}"
        wallets.add(w)
        fund_ts = start + rng.randrange(span)
        funder = CEX_HOT_WALLET if rng.random() < 0.3 else f"{token}:flipsrc{i}"
        transfers.append(Transfer(funder, w, round(rng.uniform(0.5, 5.0), 4), fund_ts))
        t = fund_ts + int(rng.expovariate(1 / (2 * HOUR)))
        amount = round(rng.lognormvariate(0.0, 0.8), 6)
        trades.append(Trade(w, token, Side.BUY, amount, t))
        trades.append(Trade(w, token, Side.SELL, amount, t + rng.randint(2 * HOUR, 12 * HOUR)))
    return transfers, trades, wallets


def _bot_operation(
    rng: random.Random,
    token: TokenId,
    deployer: Wallet,
    n_bots: int,
    n_rounds: int,
    start: int,
    span: int,
    deployer_funds_operator: bool,
    layering: int,
) -> tuple[list[Transfer], list[Trade], set[Wallet]]:
    """A market-making bot: one operator funds all bot wallets (optionally through
    ``layering`` intermediate hops); bots buy and shortly after sell roughly the same
    notional, from the same or a sibling wallet."""
    operator = f"{token}:operator"
    transfers = [Transfer(CEX_HOT_WALLET, operator, 100.0, start - 3 * HOUR)]
    if deployer_funds_operator:
        transfers.append(Transfer(deployer, operator, 50.0, start - 2 * HOUR))

    bots = [f"{token}:bot{i}" for i in range(n_bots)]
    for i, bot in enumerate(bots):
        parent = operator
        for layer in range(layering):
            mid = f"{token}:mid{i}_{layer}"
            transfers.append(Transfer(parent, mid, 20.0, start - HOUR - (layering - layer) * MINUTE))
            parent = mid
        transfers.append(Transfer(parent, bot, round(rng.uniform(5, 15), 4), start - rng.randint(1, 50) * MINUTE))

    trades: list[Trade] = []
    t = start
    gap = span / n_rounds
    for _ in range(n_rounds):
        t += int(gap * rng.uniform(0.5, 1.5))
        notional = rng.lognormvariate(0.7, 0.4)
        buyer, seller = rng.choice(bots), rng.choice(bots)
        trades.append(Trade(buyer, token, Side.BUY, round(notional, 6), t))
        sell_amount = notional * (1 + rng.gauss(0, 0.02))
        trades.append(Trade(seller, token, Side.SELL, round(sell_amount, 6), t + rng.randint(15, 20 * MINUTE)))
    return transfers, trades, set(bots)


def _finish(
    token: TokenId,
    kind: ScenarioKind,
    deployer: Wallet,
    transfers: list[Transfer],
    organic: tuple[list[Trade], set[Wallet]],
    bots: tuple[list[Trade], set[Wallet]],
) -> Dataset:
    org_trades, org_wallets = organic
    bot_trades, bot_wallets = bots
    truth = GroundTruth(
        token=token,
        kind=kind,
        deployer=deployer,
        organic_wallets=frozenset(org_wallets),
        bot_wallets=frozenset(bot_wallets),
        organic_volume=sum(t.amount for t in org_trades),
        wash_volume=sum(t.amount for t in bot_trades),
    )
    return Dataset(
        transfers=tuple(sorted(transfers, key=_ts)),
        trades=tuple(sorted(org_trades + bot_trades, key=_ts)),
        tokens=(Token(token, deployer),),
        truth={token: truth},
    )


def _deployer(rng: random.Random, token: TokenId, start: int) -> tuple[Wallet, Transfer]:
    deployer = f"{token}:deployer"
    return deployer, Transfer(f"{token}:deployer_src", deployer, round(rng.uniform(50, 200), 4), start - DAY)


# --- scenarios ---------------------------------------------------------------


def organic_scenario(
    seed: int,
    token: TokenId = "ORGANIC",
    n_wallets: int = 200,
    start: int = T0,
    span: int = 7 * DAY,
    cex_share: float = 0.3,
    related_share: float = 0.05,
) -> Dataset:
    rng = random.Random(f"organic:{seed}")
    deployer, deployer_funding = _deployer(rng, token, start)
    transfers, trades, wallets = _organic_actors(rng, token, n_wallets, start, span, cex_share, related_share)
    transfers += [deployer_funding, *_cex_background(rng, token, 80, start, span)]
    return _finish(token, ScenarioKind.ORGANIC, deployer, transfers, (trades, wallets), ([], set()))


def wash_scenario(
    seed: int,
    token: TokenId = "WASH",
    n_bots: int = 6,
    n_rounds: int = 400,
    n_organic: int = 5,
    start: int = T0,
    span: int = 7 * DAY,
    deployer_funds_operator: bool = True,
    layering: int = 0,
    n_flippers: int = 15,
) -> Dataset:
    rng = random.Random(f"wash:{seed}")
    deployer, deployer_funding = _deployer(rng, token, start)
    b_transfers, b_trades, bots = _bot_operation(
        rng, token, deployer, n_bots, n_rounds, start, span, deployer_funds_operator, layering
    )
    o_transfers, o_trades, org = _organic_actors(rng, token, n_organic, start, span, 0.3, 0.0)
    transfers = [deployer_funding, *b_transfers, *o_transfers, *_cex_background(rng, token, 80, start, span)]
    # drawn last so the rest of the scenario is unchanged by the flipper count
    f_transfers, f_trades, flippers = _flippers(rng, token, n_flippers, start, span)
    transfers += f_transfers
    return _finish(token, ScenarioKind.WASH, deployer, transfers, (o_trades + f_trades, org | flippers), (b_trades, bots))


def mixed_scenario(
    seed: int,
    token: TokenId = "MIXED",
    n_wallets: int = 150,
    n_bots: int = 5,
    n_rounds: int = 200,
    start: int = T0,
    span: int = 7 * DAY,
    deployer_funds_operator: bool = False,
    layering: int = 1,
    n_flippers: int = 20,
) -> Dataset:
    rng = random.Random(f"mixed:{seed}")
    deployer, deployer_funding = _deployer(rng, token, start)
    o_transfers, o_trades, org = _organic_actors(rng, token, n_wallets, start, span, 0.3, 0.05)
    b_transfers, b_trades, bots = _bot_operation(
        rng, token, deployer, n_bots, n_rounds, start, span, deployer_funds_operator, layering
    )
    transfers = [deployer_funding, *o_transfers, *b_transfers, *_cex_background(rng, token, 80, start, span)]
    f_transfers, f_trades, flippers = _flippers(rng, token, n_flippers, start, span)
    transfers += f_transfers
    return _finish(token, ScenarioKind.MIXED, deployer, transfers, (o_trades + f_trades, org | flippers), (b_trades, bots))


_BUILDERS = {
    ScenarioKind.ORGANIC: organic_scenario,
    ScenarioKind.WASH: wash_scenario,
    ScenarioKind.MIXED: mixed_scenario,
}


def generate(kind: ScenarioKind | str, seed: int, **kwargs) -> Dataset:
    return _BUILDERS[ScenarioKind(kind)](seed, **kwargs)


def universe(seed: int) -> Dataset:
    """All three scenarios in one market, sharing the CEX hot wallet."""
    return organic_scenario(seed).merge(wash_scenario(seed), mixed_scenario(seed))
