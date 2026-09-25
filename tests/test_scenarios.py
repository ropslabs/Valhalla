"""Acceptance contract: the detector must separate the three synthetic scenarios.

Written before the implementation. Each property is checked over several seeds so a
pass is not an accident of one random draw.
"""

import pytest

from valhalla import DEFAULT_CONFIG, analyze, cluster_wallets
from valhalla.synthetic import (
    CEX_HOT_WALLET,
    ScenarioKind,
    generate,
    mixed_scenario,
    organic_scenario,
    universe,
    wash_scenario,
)

SEEDS = range(8)


def report_of(ds, config=DEFAULT_CONFIG):
    (token,) = ds.truth
    return analyze(ds.transfers, ds.trades, ds.tokens, config)[token], ds.truth[token]


def clustering_of(ds, config=DEFAULT_CONFIG):
    deployers = [t.deployer for t in ds.tokens if t.deployer]
    return cluster_wallets(ds.transfers, ds.trades, config, extra_wallets=deployers)


# --- generator ---------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ScenarioKind))
def test_generator_is_reproducible(kind):
    assert generate(kind, 42) == generate(kind, 42)
    assert generate(kind, 42) != generate(kind, 43)


# --- ORGANIC -----------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_organic_has_low_wash_and_keeps_its_volume(seed):
    report, truth = report_of(organic_scenario(seed))
    assert report.raw_volume == pytest.approx(truth.raw_volume)
    assert report.wash_fraction < 0.10
    assert report.organic_volume >= 0.90 * truth.organic_volume


@pytest.mark.parametrize("seed", SEEDS)
def test_organic_wallets_are_mostly_independent(seed):
    report, _ = report_of(organic_scenario(seed))
    assert report.n_wallets == 200
    # a few friends-and-family pairs are genuinely linked, everything else is independent
    assert report.n_clusters >= 0.85 * report.n_wallets
    assert report.net_new_buyers >= 0.5 * report.n_clusters


@pytest.mark.parametrize("seed", SEEDS)
def test_cex_hot_wallet_does_not_merge_its_customers(seed):
    ds = organic_scenario(seed)
    clustering = clustering_of(ds)
    assert CEX_HOT_WALLET in clustering.hubs
    assert max(len(m) for m in clustering.clusters.values()) <= 5


# --- WASH (threat model) ------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_wash_bot_is_flagged(seed):
    report, truth = report_of(wash_scenario(seed))
    assert report.wash_fraction > 0.85
    assert report.organic_volume <= 0.15 * report.raw_volume
    # the estimate must not credit bot residue as organic demand
    assert report.organic_volume <= 1.1 * truth.organic_volume + 0.05 * report.raw_volume


@pytest.mark.parametrize("seed", SEEDS)
def test_wash_bot_wallets_collapse_into_one_actor_with_the_deployer(seed):
    ds = wash_scenario(seed)
    truth = ds.truth["WASH"]
    clustering = clustering_of(ds)
    bot_clusters = {clustering.cluster(w) for w in truth.bot_wallets}
    assert len(bot_clusters) == 1
    assert clustering.cluster(truth.deployer) in bot_clusters


@pytest.mark.parametrize("seed", SEEDS)
def test_wash_has_few_independent_actors_and_new_holders(seed):
    report, truth = report_of(wash_scenario(seed))
    assert report.n_wallets == len(truth.bot_wallets) + len(truth.organic_wallets)
    assert report.n_clusters == len(truth.organic_wallets) + 1
    assert report.net_new_buyers <= len(truth.organic_wallets)


# --- MIXED -------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_mixed_separates_both_components(seed):
    report, truth = report_of(mixed_scenario(seed))
    assert report.wash_fraction == pytest.approx(truth.wash_fraction, abs=0.10)
    assert report.organic_volume == pytest.approx(truth.organic_volume, rel=0.15)


@pytest.mark.parametrize("seed", SEEDS)
def test_mixed_bot_cluster_does_not_swallow_organic_wallets(seed):
    ds = mixed_scenario(seed)
    truth = ds.truth["MIXED"]
    clustering = clustering_of(ds)
    (bot_cluster,) = {clustering.cluster(w) for w in truth.bot_wallets}
    assert not any(clustering.cluster(w) == bot_cluster for w in truth.organic_wallets)


# --- separation across scenarios --------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_scenarios_are_ordered_by_wash_fraction(seed):
    organic, _ = report_of(organic_scenario(seed))
    mixed, _ = report_of(mixed_scenario(seed))
    wash, _ = report_of(wash_scenario(seed))
    assert organic.wash_fraction < mixed.wash_fraction < wash.wash_fraction


@pytest.mark.parametrize("seed", SEEDS)
def test_raw_volume_lies_organic_volume_does_not(seed):
    """The whole point: by raw volume the bot token looks biggest; by organic volume
    it ranks clearly below the organic token."""
    reports = analyze(*_parts(universe(seed)))
    by_raw = sorted(reports, key=lambda t: reports[t].raw_volume, reverse=True)
    by_organic = sorted(reports, key=lambda t: reports[t].organic_volume, reverse=True)
    assert by_raw[0] == "WASH"
    assert by_organic == ["ORGANIC", "MIXED", "WASH"]
    assert reports["ORGANIC"].organic_volume > 5 * reports["WASH"].organic_volume


@pytest.mark.parametrize("seed", SEEDS)
def test_scenarios_do_not_contaminate_each_other_in_a_shared_market(seed):
    ds = universe(seed)
    together = analyze(*_parts(ds))
    for token, single in [("ORGANIC", organic_scenario(seed)), ("WASH", wash_scenario(seed)), ("MIXED", mixed_scenario(seed))]:
        alone, _ = report_of(single)
        assert together[token] == alone


def _parts(ds):
    return ds.transfers, ds.trades, ds.tokens


# --- honest limitation -------------------------------------------------------


def test_known_limitation_hub_funded_sybils_evade_clustering():
    """Documented limitation, pinned as a test: bots each funded through a CEX hot
    wallet share no usable funding ancestor, so the cluster rule cannot link them.
    Only the per-wallet round-trip rule still bites. If this test starts failing,
    update the README's limitations section."""
    ds = wash_scenario(0)
    rerouted = tuple(
        t.__class__(CEX_HOT_WALLET, t.to_wallet, t.amount, t.timestamp) if t.from_wallet == "WASH:operator" else t
        for t in ds.transfers
    )
    truth = ds.truth["WASH"]
    clustering = cluster_wallets(rerouted, ds.trades, DEFAULT_CONFIG)
    assert len({clustering.cluster(w) for w in truth.bot_wallets}) == len(truth.bot_wallets)

    report = analyze(rerouted, ds.trades, ds.tokens)["WASH"]
    baseline = analyze(ds.transfers, ds.trades, ds.tokens)["WASH"]
    assert report.wash_fraction < baseline.wash_fraction
