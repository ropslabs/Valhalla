import pytest

from valhalla import DetectorConfig, Side, Token, Trade, Transfer, analyze
from valhalla.config import HOUR, MINUTE

T = 1_000_000
BUY, SELL = Side.BUY, Side.SELL


def tr(wallet, side, amount, ts, token="X"):
    return Trade(wallet, token, side, amount, ts)


def indep(*wallets):
    """Each wallet funded from its own source: all independent."""
    return [Transfer(f"src_{w}", w, 1.0, T - HOUR) for w in wallets]


def run(transfers, trades, tokens=(), **cfg):
    return analyze(transfers, trades, tokens, DetectorConfig(**cfg))["X"]


def test_self_round_trip_within_window_is_wash():
    r = run(indep("a"), [tr("a", BUY, 10, T), tr("a", SELL, 10, T + 5 * MINUTE)])
    assert r.raw_volume == 20
    assert r.round_trip_volume == 20
    assert r.intra_cluster_volume == 0
    assert r.wash_fraction == 1.0
    assert r.organic_volume == 0
    assert r.net_new_buyers == 0


def test_sell_then_buy_back_is_also_a_round_trip():
    r = run(indep("a"), [tr("a", SELL, 4, T), tr("a", BUY, 4, T + MINUTE)])
    assert r.wash_volume == 8


def test_round_trip_outside_window_is_organic():
    r = run(indep("a"), [tr("a", BUY, 10, T), tr("a", SELL, 10, T + 2 * HOUR)], round_trip_window=HOUR)
    assert r.wash_volume == 0
    assert r.organic_volume == 20


def test_partial_round_trip_leaves_net_position_organic():
    r = run(indep("a"), [tr("a", BUY, 10, T), tr("a", SELL, 3, T + MINUTE)])
    assert r.wash_volume == 6
    assert r.organic_volume == 7
    assert r.net_new_buyers == 1


def test_cross_wallet_trade_inside_one_cluster_is_intra_cluster_wash():
    same_source = [Transfer("op", "a", 1, T - HOUR), Transfer("op", "b", 1, T - HOUR)]
    r = run(same_source, [tr("a", BUY, 5, T), tr("b", SELL, 5, T + MINUTE)])
    assert r.intra_cluster_volume == 10
    assert r.round_trip_volume == 0
    assert r.n_wallets == 2
    assert r.n_clusters == 1


def test_opposite_trades_of_independent_wallets_are_organic():
    r = run(indep("a", "b"), [tr("a", BUY, 5, T), tr("b", SELL, 5, T + MINUTE)])
    assert r.wash_volume == 0
    assert r.organic_volume == 10
    assert r.n_clusters == 2


def test_matching_is_per_token():
    trades = [tr("a", BUY, 5, T, token="X"), tr("a", SELL, 5, T + MINUTE, token="Y")]
    reports = analyze(indep("a"), trades)
    assert reports["X"].wash_volume == 0
    assert reports["Y"].wash_volume == 0


def test_deployer_cluster_volume_is_insider_not_organic():
    transfers = [Transfer("dep", "insider", 1, T - HOUR), *indep("retail")]
    trades = [tr("insider", BUY, 8, T), tr("retail", BUY, 2, T)]
    r = run(transfers, trades, tokens=[Token("X", deployer="dep")])
    assert r.insider_volume == 8
    assert r.organic_volume == 2
    assert r.wash_fraction == 0  # buying your own token is not wash, but it is not organic either
    assert r.net_new_buyers == 1  # the deployer's cluster is never a "new" buyer


@pytest.mark.parametrize("seller", ["dep", "insider"], ids=["deployer_self", "clustered_wallet"])
def test_deployer_cluster_round_trip_is_wash_not_insider(seller):
    """A dev must not be able to hide wash trading in the softer insider bucket."""
    transfers = [Transfer("dep", "insider", 1, T - HOUR), *indep("retail")]
    trades = [tr("dep", BUY, 6, T), tr(seller, SELL, 6, T + 5 * MINUTE), tr("retail", BUY, 2, T)]
    r = run(transfers, trades, tokens=[Token("X", deployer="dep")], round_trip_window=HOUR)
    assert r.wash_volume == 12
    assert r.insider_volume == 0
    assert r.organic_volume == 2
    assert r.raw_volume == r.wash_volume + r.insider_volume + r.organic_volume


def test_deployer_cluster_buy_and_hold_is_insider_not_wash():
    transfers = [Transfer("dep", "insider", 1, T - HOUR), *indep("retail")]
    trades = [tr("dep", BUY, 6, T), tr("insider", BUY, 4, T + 5 * MINUTE), tr("retail", BUY, 2, T)]
    r = run(transfers, trades, tokens=[Token("X", deployer="dep")], round_trip_window=HOUR)
    assert r.wash_volume == 0
    assert r.insider_volume == 10
    assert r.organic_volume == 2
    assert r.raw_volume == r.wash_volume + r.insider_volume + r.organic_volume


def test_net_new_buyers_counts_clusters_not_wallets():
    sybils = [Transfer("op", w, 1, T - HOUR) for w in ("s1", "s2", "s3")]
    trades = [tr(w, BUY, 1, T) for w in ("s1", "s2", "s3")] + [tr("real", BUY, 1, T)]
    r = run(sybils + indep("real"), trades)
    assert r.n_wallets == 4
    assert r.n_clusters == 2
    assert r.net_new_buyers == 2
    assert r.cluster_ratio == 0.5


def test_net_new_buyer_needs_meaningful_net_position():
    # bought 10, sold 9.5 two hours later (not wash), net 0.5 < 10% of 10
    r = run(indep("a"), [tr("a", BUY, 10, T), tr("a", SELL, 9.5, T + 2 * HOUR)], net_buyer_min_ratio=0.1)
    assert r.net_new_buyers == 0


def test_volume_identity_holds():
    trades = [tr("a", BUY, 3, T), tr("a", SELL, 2, T + 1), tr("b", BUY, 7, T + 2)]
    r = run(indep("a", "b"), trades)
    assert r.raw_volume == pytest.approx(r.wash_volume + r.insider_volume + r.organic_volume)
    assert r.wash_volume == pytest.approx(r.round_trip_volume + r.intra_cluster_volume)


def test_declared_token_without_trades_gets_empty_report():
    r = analyze([], [], [Token("X")])["X"]
    assert r.raw_volume == 0 and r.wash_fraction == 0 and r.n_wallets == 0 and r.cluster_ratio == 0
