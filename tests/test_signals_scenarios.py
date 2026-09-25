"""Acceptance contract for the token signals on the synthetic scenarios.

Expected: ORGANIC best, WASH worst, MIXED in between on new buyers, retention and wash
fraction, and the organic_score ranking ORGANIC > MIXED > WASH stable across seeds.

Concentration is the exception: a wash bot ends flat, holds nothing and therefore
does not raise concentration. MIXED vs ORGANIC concentration differs only by chance
(holder count), so it is only required to be clearly below WASH.
"""

import pytest

from valhalla import analyze_signals
from valhalla.synthetic import universe

SEEDS = range(8)


@pytest.fixture(scope="module", params=SEEDS)
def signals(request):
    ds = universe(request.param)
    return analyze_signals(ds.transfers, ds.trades, ds.tokens)


def test_new_independent_buyers_are_ordered(signals):
    rate = {t: s.new_buyers.rate for t, s in signals.items()}
    assert rate["ORGANIC"] > rate["MIXED"] > rate["WASH"]
    assert signals["ORGANIC"].new_buyers.total >= 150
    # spike buyers holding across a window boundary count as new; retention catches them
    assert signals["WASH"].new_buyers.total <= 0.1 * signals["ORGANIC"].new_buyers.total


def test_retention_is_ordered(signals):
    ret = {t: s.retention.rate for t, s in signals.items()}
    assert ret["ORGANIC"] > ret["MIXED"] > ret["WASH"]
    assert ret["ORGANIC"] >= 0.6
    assert ret["WASH"] <= 0.4


def test_wash_token_is_the_concentrated_one(signals):
    conc = {t: s.concentration.top_n_share for t, s in signals.items()}
    assert conc["WASH"] >= 0.9
    assert conc["ORGANIC"] <= 0.35 and conc["MIXED"] <= 0.4


def test_wash_fraction_is_ordered(signals):
    wash = {t: s.wash.wash_fraction for t, s in signals.items()}
    assert wash["ORGANIC"] < wash["MIXED"] < wash["WASH"]


def test_organic_score_ranks_organic_over_mixed_over_wash(signals):
    ranking = sorted(signals, key=lambda t: signals[t].organic_score, reverse=True)
    assert ranking == ["ORGANIC", "MIXED", "WASH"]


def test_organic_score_ranking_is_stable_across_seeds():
    """Not just per-seed: the worst ORGANIC score beats the best MIXED, and the worst
    MIXED beats the best WASH."""
    scores = {"ORGANIC": [], "MIXED": [], "WASH": []}
    for seed in SEEDS:
        ds = universe(seed)
        for token, s in analyze_signals(ds.transfers, ds.trades, ds.tokens).items():
            scores[token].append(s.organic_score)
    assert min(scores["ORGANIC"]) > max(scores["MIXED"])
    assert min(scores["MIXED"]) > max(scores["WASH"])
