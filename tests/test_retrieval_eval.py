"""The retrieval eval: metric maths, the question file, and one end-to-end run.

The real numbers come from `make eval` with Ollama. Here the FakeEmbedder stands in,
so these tests check that the eval works, not how good search is.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.embeddings import EmbeddingError, FakeEmbedder
from app.evals.metrics import best_cutoff, first_relevant_rank, mean_reciprocal_rank, recall_at
from app.evals.retrieval import (
    DEFAULT_DATASET,
    EVAL_DIR,
    EvalError,
    EvalSet,
    baseline_path,
    build_report,
    knowledge_fingerprint,
    load_eval_set,
    run_cases,
)
from app.ingestion import worker
from app.models.ingestion import KnowledgeChunk
from app.models.knowledge import KnowledgeSource, KnowledgeStatus
from app.search.service import DEFAULT_TUNING
from app.seed.__main__ import seed_workspace
from app.seed.data import WORKSPACES

# ---------------------------------------------------------------- metrics


def test_first_relevant_rank() -> None:
    assert first_relevant_rank(["a", "b", "c"], ["c", "b"]) == 2
    assert first_relevant_rank(["a", "b"], ["z"]) is None
    assert first_relevant_rank([], ["a"]) is None


def test_recall_and_mrr() -> None:
    ranks = [1, 3, None, 6]
    assert recall_at(ranks, 1) == 0.25
    assert recall_at(ranks, 5) == 0.5
    assert mean_reciprocal_rank(ranks) == pytest.approx((1 + 1 / 3 + 0 + 1 / 6) / 4)
    assert recall_at([], 5) == 0.0
    assert mean_reciprocal_rank([]) == 0.0


def test_cutoff_separates_clean_groups_halfway() -> None:
    cutoff = best_cutoff([0.8, 0.75, 0.7], [0.5, 0.55])
    assert cutoff is not None
    assert (cutoff.answered, cutoff.refused) == (1.0, 1.0)
    # Halfway between the lowest answerable (0.7) and the highest unanswerable (0.55).
    assert cutoff.threshold == pytest.approx(0.625)


def test_cutoff_with_overlap_balances_both_groups() -> None:
    # One unanswerable question (0.72) scores like an answerable one, so no threshold
    # is perfect. Refusing it would mean refusing 0.7 and 0.6 too (two of four
    # answerable), so the best rule lets it through and answers every answerable one.
    cutoff = best_cutoff([0.9, 0.8, 0.7, 0.6], [0.72, 0.5, 0.4])
    assert cutoff is not None
    assert cutoff.answered == 1.0
    assert cutoff.refused == pytest.approx(2 / 3)
    assert cutoff.threshold == pytest.approx(0.55)  # between 0.6 and 0.5
    assert best_cutoff([], [0.5]) is None


# ---------------------------------------------------------------- the question file


HOLDOUT = EVAL_DIR / "kettle-demo-holdout.yaml"


@pytest.mark.parametrize("dataset", [DEFAULT_DATASET, HOLDOUT], ids=lambda path: path.stem)
def test_question_file_is_valid(dataset: Path) -> None:
    eval_set = load_eval_set(dataset)
    assert eval_set.workspace == "kettle-demo"
    types = {case.type for case in eval_set.cases}
    assert types == {"exact", "paraphrase", "ticket", "unanswerable"}


def test_duplicate_ids_and_mismatched_types_are_rejected() -> None:
    case = {"id": "a", "question": "q", "type": "exact", "expect": ["x"]}
    with pytest.raises(ValidationError, match="duplicate"):
        EvalSet.model_validate({"workspace": "w", "cases": [case, case]})
    with pytest.raises(ValidationError, match="unanswerable"):
        EvalSet.model_validate({"workspace": "w", "cases": [{**case, "type": "unanswerable"}]})


def test_holdout_shares_no_question_with_the_tuning_set() -> None:
    # A question in both files would be tuned on and then used as proof.
    tuned = {case.question.lower() for case in load_eval_set(DEFAULT_DATASET).cases}
    held_out = {case.question.lower() for case in load_eval_set(HOLDOUT).cases}
    assert not tuned & held_out


def test_each_dataset_has_its_own_baseline() -> None:
    assert baseline_path(DEFAULT_DATASET) != baseline_path(HOLDOUT)


# ---------------------------------------------------------------- end to end


class DyingEmbedder(FakeEmbedder):
    """Answers a number of queries, then fails like an Ollama that was stopped."""

    def __init__(self, answers: int) -> None:
        super().__init__()
        self.answers = answers

    async def embed_query(self, text: str) -> list[float]:
        if self.answers == 0:
            raise EmbeddingError("Cannot reach Ollama")
        self.answers -= 1
        return await super().embed_query(text)


# 0: fails in vector search, which raises. 1: fails in the hybrid search that follows,
# which degrades silently. Both must stop the eval rather than record wrong numbers.
@pytest.mark.parametrize("answers", [0, 1])
async def test_eval_stops_when_the_model_fails(
    db_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
    answers: int,
) -> None:
    async with factory() as session:
        await seed_workspace(session, WORKSPACES[0])
    while await worker.run_once(factory, settings, embedder):
        pass
    async with factory() as session:
        with pytest.raises(EvalError, match="embedding model failed"):
            await run_cases(session, DyingEmbedder(answers), load_eval_set(DEFAULT_DATASET))


async def test_eval_runs_on_seeded_data(
    db_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    async with factory() as session:
        for spec in WORKSPACES:
            await seed_workspace(session, spec)
    while await worker.run_once(factory, settings, embedder):
        pass

    eval_set = load_eval_set(DEFAULT_DATASET)
    async with factory() as session:
        workspace_id, results = await run_cases(session, embedder, eval_set)

        # Every expected label must be a real, retrievable heading. Otherwise a typo or
        # a renamed section makes a case a permanent miss that looks like bad search.
        headings = set(
            await session.scalars(
                select(KnowledgeChunk.heading)
                .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
                .where(
                    KnowledgeChunk.workspace_id == workspace_id,
                    KnowledgeSource.status == KnowledgeStatus.approved.value,
                )
            )
        )
        all_cases = [*eval_set.cases, *load_eval_set(HOLDOUT).cases]
        missing = {label for case in all_cases for label in case.expect} - headings
        assert not missing, f"expected labels not in the seeded data: {missing}"

        fingerprint = await knowledge_fingerprint(session, workspace_id)

    by_id = {result.id: result for result in results}
    assert len(results) == len(eval_set.cases)
    # Keyword search finds exact error codes no matter which embedder is used.
    assert by_id["code-k310"].ranks["keyword"] == 1
    # Unanswerable questions have no ranks to find, but do get a similarity.
    assert by_id["banana-bread"].ranks == {"keyword": None, "vector": None, "hybrid": None}

    report = build_report(
        results,
        model=embedder.model,
        dataset=DEFAULT_DATASET,
        fingerprint=fingerprint,
        tuning=DEFAULT_TUNING,
    )
    summary = report["summary"]
    assert summary["answerable"] + summary["unanswerable"] == len(eval_set.cases)
    assert set(summary["modes"]) == {"keyword", "vector", "hybrid"}
    assert 0 <= summary["modes"]["hybrid"]["recall@5"] <= 1
    json.dumps(report)  # the report is saved as JSON, so it must serialize
