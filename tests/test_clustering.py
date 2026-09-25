import pytest

from valhalla import DetectorConfig, Side, Trade, Transfer, build_funding_graph, cluster_wallets, find_hubs
from valhalla.config import DAY, HOUR

T = 1_000_000


def buy(wallet, ts=T, amount=1.0, token="X"):
    return Trade(wallet, token, Side.BUY, amount, ts)


# --- graph ---------------------------------------------------------------------


def test_graph_ignores_dust_and_aggregates_parallel_transfers():
    cfg = DetectorConfig(dust_threshold=0.1)
    g = build_funding_graph(
        [
            Transfer("a", "b", 1.0, T),
            Transfer("a", "b", 2.0, T + 10),
            Transfer("a", "c", 0.05, T),  # dust
            Transfer("a", "a", 5.0, T),  # self-transfer
        ],
        cfg,
    )
    assert set(g.edges) == {("a", "b")}
    assert g["a"]["b"]["amount"] == 3.0
    assert g["a"]["b"]["timestamps"] == [T, T + 10]


def test_hubs_by_fanout_and_by_label():
    cfg = DetectorConfig(hub_min_fanout=3, known_hubs=frozenset({"bridge"}))
    transfers = [Transfer("cex", f"u{i}", 1.0, T) for i in range(3)]
    transfers += [Transfer("small", "u0", 1.0, T), Transfer("small", "u1", 1.0, T), Transfer("bridge", "u2", 1.0, T)]
    assert find_hubs(build_funding_graph(transfers, cfg), cfg) == {"cex", "bridge"}


# --- linking rules ---------------------------------------------------------------


def test_common_source_links_wallets():
    c = cluster_wallets([Transfer("src", "a", 1, T), Transfer("src", "b", 1, T)], [buy("a"), buy("b")])
    assert c.same_cluster("a", "b")


def test_unrelated_wallets_stay_apart():
    c = cluster_wallets([Transfer("s1", "a", 1, T), Transfer("s2", "b", 1, T)], [buy("a"), buy("b")])
    assert not c.same_cluster("a", "b")
    assert c.cluster("a") != c.cluster("b")


def test_direct_and_mutual_funding_link():
    direct = cluster_wallets([Transfer("a", "b", 1, T)], [buy("a"), buy("b")])
    assert direct.same_cluster("a", "b")
    mutual = cluster_wallets([Transfer("a", "b", 1, T), Transfer("b", "a", 1, T)], [buy("a"), buy("b")])
    assert mutual.same_cluster("a", "b")


def test_hop_depth_is_respected():
    # src -> m1 -> a   and   src -> m2 -> b : common ancestor at 2 hops
    transfers = [
        Transfer("src", "m1", 1, T - 20),
        Transfer("src", "m2", 1, T - 20),
        Transfer("m1", "a", 1, T - 10),
        Transfer("m2", "b", 1, T - 10),
    ]
    trades = [buy("a"), buy("b")]
    assert cluster_wallets(transfers, trades, DetectorConfig(max_hops=2)).same_cluster("a", "b")
    assert not cluster_wallets(transfers, trades, DetectorConfig(max_hops=1)).same_cluster("a", "b")


def test_hub_neither_links_nor_is_traversed():
    cfg = DetectorConfig(hub_min_fanout=3)
    transfers = [Transfer("cex", w, 1, T) for w in ("a", "b", "x")]
    # "op" funds the hub: without the traversal stop, a and b would link through op.
    transfers.append(Transfer("op", "cex", 1, T - 1))
    c = cluster_wallets(transfers, [buy("a"), buy("b")], cfg)
    assert "cex" in c.hubs
    assert not c.same_cluster("a", "b")


def test_dust_does_not_link():
    cfg = DetectorConfig(dust_threshold=0.01)
    c = cluster_wallets([Transfer("spam", "a", 0.001, T), Transfer("spam", "b", 0.001, T)], [buy("a"), buy("b")], cfg)
    assert not c.same_cluster("a", "b")


def test_funding_near_first_activity_counts_more():
    cfg = DetectorConfig(funding_window=1 * DAY, funding_half_life=2 * DAY, min_link_strength=0.25)
    fresh = [Transfer("src", "a", 1, T - HOUR), Transfer("src", "b", 1, T - HOUR)]
    assert cluster_wallets(fresh, [buy("a"), buy("b")], cfg).same_cluster("a", "b")

    # b was funded 30 days before its first trade: weight 0.5**(29/2) ~ 0, link drops.
    stale = [Transfer("src", "a", 1, T - HOUR), Transfer("src", "b", 1, T - 30 * DAY)]
    assert not cluster_wallets(stale, [buy("a"), buy("b")], cfg).same_cluster("a", "b")

    # 3 days outside the window: 0.5**(2/2) = 0.5 >= 0.25, still linked.
    aging = [Transfer("src", "a", 1, T - HOUR), Transfer("src", "b", 1, T - 3 * DAY)]
    assert cluster_wallets(aging, [buy("a"), buy("b")], cfg).same_cluster("a", "b")


def test_link_is_transitive_through_shared_members():
    transfers = [Transfer("s1", "a", 1, T), Transfer("s1", "b", 1, T), Transfer("s2", "b", 1, T), Transfer("s2", "c", 1, T)]
    c = cluster_wallets(transfers, [buy("a"), buy("b"), buy("c")])
    assert c.same_cluster("a", "c")
    assert c.members(c.cluster("a")) == {"a", "b", "c"}


def test_extra_wallets_participate_and_unknown_wallets_are_singletons():
    c = cluster_wallets([Transfer("dep", "a", 1, T)], [buy("a")], extra_wallets=["dep"])
    assert c.same_cluster("dep", "a")
    assert c.cluster("nobody") == "nobody"
    assert c.members("nobody") == {"nobody"}


def test_cluster_ids_are_deterministic():
    transfers = [Transfer("src", w, 1, T) for w in ("c", "a", "b")]
    trades = [buy("c"), buy("a"), buy("b")]
    ids = {cluster_wallets(transfers, list(p), DetectorConfig()).cluster("a") for p in (trades, trades[::-1])}
    assert ids == {"a"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dust_threshold": -1},
        {"hub_min_fanout": 1},
        {"max_hops": -1},
        {"funding_half_life": 0},
        {"min_link_strength": 0},
        {"min_link_strength": 1.5},
        {"round_trip_window": -1},
        {"net_buyer_min_ratio": 1.0},
    ],
)
def test_config_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        DetectorConfig(**kwargs)
