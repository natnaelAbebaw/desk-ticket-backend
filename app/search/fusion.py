"""Reciprocal Rank Fusion (RRF): merge several ranked lists into one.

Keyword and vector search score results on scales that cannot be compared: ts_rank_cd
might give 0.1 or 4.0, cosine similarity is between -1 and 1, and neither number means
the same thing from one query to the next. RRF ignores the scores and only uses each
result's position in each list:

    score(item) = sum over lists of  1 / (k + rank in that list)

An item ranked 1st in one list scores 1/61; ranked 1st in both, 2/61. An item found by
both searches beats one found by a single search, unless the single search ranked it
much higher. k (60 in the original paper, and the usual default) dampens the gap
between the top ranks, so one list's first place cannot outweigh everything else.
"""

from collections.abc import Hashable, Sequence

RRF_K = 60


# [Key: Hashable] is Python 3.12 generic syntax: the keys can be any hashable type
# (chunk ids here, plain strings in tests) and the result uses the same type.
def reciprocal_rank_fusion[Key: Hashable](
    rankings: Sequence[Sequence[Key]], *, k: int = RRF_K
) -> list[tuple[Key, float]]:
    """Return (key, score) pairs, best first. Each ranking lists keys best first."""
    scores: dict[Key, float] = {}
    for ranking in rankings:
        for index, key in enumerate(ranking):
            # enumerate starts at 0; ranks start at 1.
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + index + 1)
    # sorted() is stable, so equal scores keep first-seen order: an item from the
    # first ranking (keyword) wins ties. That keeps results deterministic.
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
