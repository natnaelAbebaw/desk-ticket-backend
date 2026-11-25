"""Retrieval metrics. Pure functions over ranks and scores, so they are easy to test.

A "rank" is the 1-based position of the first relevant result for one question, or
None if no relevant result came back at all.
"""

from collections.abc import Sequence
from dataclasses import dataclass


def first_relevant_rank(headings: Sequence[str], expected: Sequence[str]) -> int | None:
    """Position (1 = top) of the first result whose heading is one of the expected."""
    wanted = set(expected)
    for index, heading in enumerate(headings):
        if heading in wanted:
            return index + 1
    return None


def recall_at(ranks: Sequence[int | None], k: int) -> float:
    """Share of questions with a relevant result in the top k.

    Strictly this is "success@k" (or hit rate): a question counts once if any of its
    expected chunks is found. The usual recall@k counts what fraction of all relevant
    chunks were found, but here the expected list holds alternatives, any one of which
    answers the question, so finding one is what matters. Most RAG write-ups use
    "recall@k" in this sense.
    """
    if not ranks:
        return 0.0
    return sum(1 for rank in ranks if rank is not None and rank <= k) / len(ranks)


def mean_reciprocal_rank(ranks: Sequence[int | None]) -> float:
    """Mean of 1/rank: 1st place scores 1, 2nd 0.5, 3rd 0.33; not found scores 0.

    Unlike recall@k it rewards ranking the answer higher, not just including it.
    Results are only fetched down to a fixed depth, so this is MRR at that depth.
    """
    if not ranks:
        return 0.0
    return sum(1 / rank for rank in ranks if rank is not None) / len(ranks)


@dataclass(frozen=True)
class Cutoff:
    """The best "answer only if the top similarity is at least threshold" rule."""

    threshold: float
    # Share of answerable questions whose top similarity reaches the threshold.
    answered: float
    # Share of unanswerable questions whose top similarity stays below it.
    refused: float

    @property
    def balanced_accuracy(self) -> float:
        return (self.answered + self.refused) / 2


def best_cutoff(answerable: Sequence[float], unanswerable: Sequence[float]) -> Cutoff | None:
    """Find the similarity threshold that best separates the two groups.

    Each input is the top vector similarity per question. Every observed score is
    tried as a threshold, and the one with the best balanced accuracy wins: the mean
    of "answerable questions answered" and "unanswerable questions refused". Balanced,
    so that having more answerable than unanswerable cases does not tilt the result.
    Ties go to the higher threshold, which refuses rather than guesses.

    The returned threshold sits halfway between the chosen score and the next lower
    score, so a new question scoring a hair under the chosen one is not refused
    because of where the observed samples happened to land.
    """
    if not answerable or not unanswerable:
        return None
    scores = sorted(set(answerable) | set(unanswerable))
    best: Cutoff | None = None
    for index, threshold in enumerate(scores):
        answered = sum(score >= threshold for score in answerable) / len(answerable)
        refused = sum(score < threshold for score in unanswerable) / len(unanswerable)
        # >= so a later (higher) threshold wins a tie.
        if best is None or (answered + refused) / 2 >= best.balanced_accuracy:
            below = scores[index - 1] if index > 0 else threshold
            best = Cutoff(threshold=(threshold + below) / 2, answered=answered, refused=refused)
    return best
