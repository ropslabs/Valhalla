"""Unit tests for the cluster-level token signals (concentration, new buyers, retention, score)."""

import pytest

from valhalla import DetectorConfig, Side, Token, Trade, Transfer, analyze_signals, cluster_wallets
from valhalla.config import DAY, HOUR
from valhalla.signals import holder_concentration, new_buyers, organic_score, retention

T = 1_000_000
BUY, SELL = Side.BUY, Side.SELL


def tr(wallet, side, amount, ts=T, token="X"):
    return Trade(wallet, token, side, amount, ts)


def indep(*wallets):
    return [Transfer(f"src_{w}", w, 1.0, T - HOUR) for w in wallets]


def clustering(transfers, trades, extra=()):
    return cluster_wallets(transfers, trades, DetectorConfig(), extra_wallets=extra)


# --- holder concentration ----------------------------------------------------


def test_concentration_top_n_share_and_gini():
    trades = [tr("a", BUY, 6), tr("b", BUY, 3), tr("c", BUY, 1)]
    c = holder_concentration(trades, clustering(indep("a", "b", "c"), trades), DetectorConfig(concentration_top_n=1))
    assert c.n_holders == 3
    assert c.total_position == 10
    assert c.top_n_share == pytest.approx(0.6)
    # Gini of (1, 3, 6): 2*(1*1 + 2*3 + 3*6)/(3*10) - 4/3
    assert c.gini == pytest.approx(2 * 25 / 30 - 4 / 3)


def test_equal_holders_have_zero_gini():
    trades = [tr(w, BUY, 2) for w in "abcd"]
    c = holder_concentration(trades, clustering(indep(*"abcd"), trades), DetectorConfig(concentration_top_n=2))
    assert c.gini == pytest.approx(0.0)
    assert c.top_n_share == pytest.approx(0.5)


def test_positions_are_net_and_sold_out_clusters_are_not_holders():
    trades = [tr("a", BUY, 5), tr("a", SELL, 5, T + DAY), tr("b", BUY, 4), tr("b", SELL, 1, T + DAY), tr("c", SELL, 3)]
    c = holder_concentration(trades, clustering(indep(*"abc"), trades))
    assert c.n_holders == 1
    assert c.total_position == 3


def test_deployer_cluster_counts_but_is_reported_separately():
    transfers = [Transfer("dep", "insider", 1, T - HOUR), *indep("r1", "r2")]
    trades = [tr("insider", BUY, 8), tr("r1", BUY, 1), tr("r2", BUY, 1)]
    c = holder_concentration(trades, clustering(transfers, trades, ["dep"]), DetectorConfig(concentration_top_n=1), deployer="dep")
    assert c.n_holders == 3
    assert c.deployer_share == pytest.approx(0.8)
    assert c.top_n_share == pytest.approx(0.8)


def test_no_holders_counts_as_fully_concentrated():
    c = holder_concentration([], clustering([], []))
    assert c.n_holders == 0 and c.top_n_share == 1.0


# --- the decisive Sybil check: concentration must be measured per cluster ------------------


def _split_position(funded_by_one_source: bool):
    """One actor splits a 100-unit position over 20 wallets; 20 independent retail
    holders hold 1 unit each."""
    sybils = [f"s{i}" for i in range(20)]
    retail = [f"r{i}" for i in range(20)]
    funding = [Transfer("whale", w, 1.0, T - HOUR) for w in sybils] if funded_by_one_source else indep(*sybils)
    transfers = funding + indep(*retail)
    trades = [tr(w, BUY, 5) for w in sybils] + [tr(w, BUY, 1) for w in retail]
    return transfers, trades


def test_sybil_split_position_is_detected_as_concentrated():
    cfg = DetectorConfig(concentration_top_n=5)
    transfers, trades = _split_position(funded_by_one_source=True)
    c = holder_concentration(trades, cluster_wallets(transfers, trades, cfg), cfg)
    assert c.n_holders == 21  # one actor + 20 retail, not 40 wallets
    assert c.top_n_share >= 0.85
    assert c.gini >= 0.75


def test_same_trades_without_common_funding_look_dispersed():
    """Counter-check: identical trades, but the 20 big wallets are independently
    funded. Only the funding structure differs, so the gap to the test above is
    exactly what cluster-level measurement buys."""
    cfg = DetectorConfig(concentration_top_n=5)
    transfers, trades = _split_position(funded_by_one_source=False)
    c = holder_concentration(trades, cluster_wallets(transfers, trades, cfg), cfg)
    assert c.n_holders == 40
    assert c.top_n_share <= 0.25
    assert c.gini <= 0.45


# --- net new independent buyers over time -------------------------------------


def test_new_buyer_series_counts_distinct_clusters_per_window():
    cfg = DetectorConfig(signal_window=DAY)
    transfers = [Transfer("op", "s1", 1, T - HOUR), Transfer("op", "s2", 1, T - HOUR), *indep("a", "b", "c")]
    trades = [
        tr("s1", BUY, 1, T), tr("s2", BUY, 1, T + HOUR),  # one actor, two wallets -> 1
        tr("a", BUY, 1, T + 2 * HOUR),                      # day 0 -> 2 total
        tr("b", BUY, 1, T + DAY + HOUR),                    # day 1 -> 1
        tr("a", BUY, 1, T + DAY + 2 * HOUR),                # a again: not new
        tr("c", BUY, 1, T + 2 * DAY + HOUR),                # day 2 -> 1
    ]
    nb = new_buyers(trades, clustering(transfers, trades), cfg)
    assert [count for _, count in nb.series] == [2, 1, 1]
    assert [start for start, _ in nb.series] == [T, T + DAY, T + 2 * DAY]
    assert nb.total == 4


def test_buy_and_sell_out_within_a_window_is_not_a_new_buyer():
    trades = [tr("a", BUY, 5, T), tr("a", SELL, 5, T + 3 * HOUR), tr("b", BUY, 1, T)]
    nb = new_buyers(trades, clustering(indep("a", "b"), trades), DetectorConfig(signal_window=DAY))
    assert nb.total == 1


def test_returning_holder_is_not_new_again():
    trades = [tr("a", BUY, 5, T), tr("a", SELL, 5, T + DAY + HOUR), tr("a", BUY, 5, T + 2 * DAY + HOUR)]
    nb = new_buyers(trades, clustering(indep("a"), trades), DetectorConfig(signal_window=DAY))
    assert nb.total == 1


def test_deployer_cluster_is_never_a_new_buyer():
    transfers = [Transfer("dep", "insider", 1, T - HOUR)]
    trades = [tr("insider", BUY, 5, T)]
    nb = new_buyers(trades, clustering(transfers, trades, ["dep"]), DetectorConfig(), deployer="dep")
    assert nb.total == 0


def test_new_buyer_rate_is_age_normalized():
    """A 2-day-old token and a 10-day-old token with the same daily inflow get the same rate."""
    cfg = DetectorConfig(signal_window=DAY)
    as_of = T + 10 * DAY
    old = [tr(f"old{i}", BUY, 1, T + (i // 5) * DAY + HOUR) for i in range(50)]
    young = [tr(f"young{i}", BUY, 1, T + 8 * DAY + (i // 5) * DAY + HOUR) for i in range(10)]
    c = clustering(indep(*(t.wallet for t in old + young)), old + young)
    rate_old = new_buyers(old, c, cfg, as_of=as_of).rate
    rate_young = new_buyers(young, c, cfg, as_of=as_of).rate
    assert rate_old == pytest.approx(5.0)
    assert rate_young == pytest.approx(5.0)


# --- retention -------------------------------------------------------------


def test_retention_holder_vs_spike_seller():
    cfg = DetectorConfig(signal_window=DAY, retention_windows=3)
    trades = [
        tr("holder", BUY, 5, T),
        tr("partial", BUY, 10, T), tr("partial", SELL, 6, T + DAY),  # still holds 40 %
        tr("flipper", BUY, 5, T), tr("flipper", SELL, 5, T + 4 * HOUR),
        tr("late", BUY, 1, T + 9 * DAY),  # too recent to judge
    ]
    r = retention(trades, clustering(indep("holder", "partial", "flipper", "late"), trades), cfg, as_of=T + 10 * DAY)
    assert r.n_evaluated == 3
    assert r.n_retained == 2
    assert r.rate == pytest.approx(2 / 3)


def test_selling_after_the_horizon_still_counts_as_retained():
    cfg = DetectorConfig(signal_window=DAY, retention_windows=3)
    trades = [tr("a", BUY, 5, T), tr("a", SELL, 5, T + 5 * DAY)]
    r = retention(trades, clustering(indep("a"), trades), cfg, as_of=T + 10 * DAY)
    assert r.rate == 1.0


def test_retention_falls_back_to_prior_when_nothing_is_old_enough():
    cfg = DetectorConfig(signal_window=DAY, retention_windows=3, retention_prior=0.5)
    trades = [tr("a", BUY, 5, T)]
    r = retention(trades, clustering(indep("a"), trades), cfg, as_of=T + DAY)
    assert r.n_evaluated == 0
    assert r.rate is None
    assert r.effective(cfg) == 0.5


def test_retention_is_per_cluster_not_per_wallet():
    """One actor rotating through 5 wallets that each buy and dump is ONE flipper."""
    cfg = DetectorConfig(signal_window=DAY, retention_windows=1)
    transfers = [Transfer("op", f"s{i}", 1, T - HOUR) for i in range(5)] + indep("holder")
    trades = [tr("holder", BUY, 1, T)]
    for i in range(5):
        trades += [tr(f"s{i}", BUY, 2, T + i * HOUR), tr(f"s{i}", SELL, 2, T + i * HOUR + 30 * 60)]
    r = retention(trades, clustering(transfers, trades), cfg, as_of=T + 5 * DAY)
    assert r.n_evaluated == 2
    assert r.rate == pytest.approx(0.5)


# --- organic score ---------------------------------------------------------------


def test_organic_score_is_the_configured_weighted_sum():
    cfg = DetectorConfig(
        new_buyers_scale=10.0,
        weight_new_buyers=0.4,
        weight_retention=0.3,
        weight_concentration=0.2,
        weight_wash=0.5,
        retention_prior=0.5,
    )
    trades = [tr("a", BUY, 3, T), tr("b", BUY, 1, T)]
    c = clustering(indep("a", "b"), trades)
    conc = holder_concentration(trades, c, cfg)
    nb = new_buyers(trades, c, cfg, as_of=T)
    ret = retention(trades, c, cfg, as_of=T)
    expected = 0.4 * (nb.rate / (nb.rate + 10.0)) + 0.3 * 0.5 + 0.2 * (1 - conc.top_n_share) - 0.5 * 0.25
    assert organic_score(0.25, conc, nb, ret, cfg) == pytest.approx(expected)


def test_organic_score_drops_with_wash_fraction():
    trades = [tr("a", BUY, 3, T), tr("b", BUY, 1, T)]
    c = clustering(indep("a", "b"), trades)
    parts = (holder_concentration(trades, c), new_buyers(trades, c), retention(trades, c))
    assert organic_score(0.0, *parts) > organic_score(0.5, *parts) > organic_score(1.0, *parts)


def test_analyze_signals_bundles_everything_per_token():
    transfers = indep("a", "b")
    trades = [tr("a", BUY, 3, T), tr("a", SELL, 3, T + 60), tr("b", BUY, 2, T, token="Y")]
    out = analyze_signals(transfers, trades, [Token("X"), Token("Z")])
    assert set(out) == {"X", "Y", "Z"}
    assert out["X"].wash.wash_fraction == 1.0
    assert out["X"].concentration.n_holders == 0
    assert out["Y"].concentration.n_holders == 1
    assert out["Z"].new_buyers.total == 0
    assert out["Y"].organic_score > out["X"].organic_score


@pytest.mark.parametrize(
    "kwargs",
    [
        {"signal_window": 0},
        {"concentration_top_n": 0},
        {"retention_windows": 0},
        {"retention_prior": 1.5},
        {"new_buyers_scale": 0},
        {"weight_wash": -0.1},
    ],
)
def test_config_rejects_nonsense_signal_knobs(kwargs):
    with pytest.raises(ValueError):
        DetectorConfig(**kwargs)
