"""Measure how well search finds the right knowledge, with numbers instead of eyeballing.

    make seed-reset          # the eval set is written against the demo data
    make eval                # run, print, and compare with the baseline
    make eval-baseline       # run and save the result as the new baseline
    make eval-holdout        # the questions that settings are never tuned on

    python -m app.evals.retrieval --rrf-k 20 --candidates 30 --any-word-hybrid
                             # try other search settings without changing code

For every question in evals/retrieval/<workspace>.yaml it runs keyword, vector, and
hybrid search against the real database and embedding model, then reports per mode:

- recall@1/3/5: share of questions with a right chunk in the top 1, 3, or 5
- MRR: how high the first right chunk ranks, on average
- the similarity cutoff that best separates answerable from unanswerable questions

This is a measuring tool, not a pytest test: it needs Ollama, and its numbers belong
to one model and one dataset. tests/test_retrieval_eval.py covers the code itself.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import statistics
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings.base import Embedder
from app.evals.metrics import (
    best_cutoff,
    first_relevant_rank,
    mean_reciprocal_rank,
    recall_at,
)
from app.models.ingestion import KnowledgeChunk
from app.models.knowledge import KnowledgeSource, KnowledgeStatus
from app.models.workspace import Workspace
from app.search.service import (
    DEFAULT_TUNING,
    SearchMode,
    SearchTuning,
    VectorSearchUnavailable,
    search,
)

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "evals" / "retrieval"
DEFAULT_DATASET = EVAL_DIR / "kettle-demo.yaml"
# Each run is saved here too (git-ignored), to compare runs by hand.
LATEST = EVAL_DIR / "results" / "latest.json"

# How deep each search looks. 20 is the API's maximum, and deep enough that a miss
# at depth 20 means search really did not find it, not that it ranked 6th.
DEPTH = 20
RECALL_KS = (1, 3, 5)
MODES = (SearchMode.keyword, SearchMode.vector, SearchMode.hybrid)
CaseType = Literal["exact", "paraphrase", "ticket", "unanswerable"]


def baseline_path(dataset: Path) -> Path:
    """Each question file has its own baseline, next to it. Comparing one file's run
    with another file's baseline would say nothing about search."""
    return dataset.with_name(f"{dataset.stem}.baseline.json")


# ---------------------------------------------------------------- dataset


class EvalCase(BaseModel):
    id: str
    question: str = Field(min_length=1)
    type: CaseType
    # Headings of chunks that answer the question; any one counts. Empty = unanswerable.
    expect: list[str]

    @property
    def answerable(self) -> bool:
        return bool(self.expect)


class EvalSet(BaseModel):
    workspace: str  # the workspace slug the cases are written against
    cases: list[EvalCase]

    @field_validator("cases")
    @classmethod
    def consistent(cls, cases: list[EvalCase]) -> list[EvalCase]:
        ids = [case.id for case in cases]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate case ids: {sorted(duplicates)}")
        for case in cases:
            # The type and the expect list must agree, or the case would be counted in
            # the wrong group (recall vs cutoff) without anyone noticing.
            if (case.type == "unanswerable") == case.answerable:
                raise ValueError(
                    f"{case.id}: type unanswerable must have expect: [] and vice versa"
                )
        return cases


def load_eval_set(path: Path) -> EvalSet:
    return EvalSet.model_validate(yaml.safe_load(path.read_text()))


# ---------------------------------------------------------------- running


class EvalError(Exception):
    """The eval cannot run meaningfully (no workspace, no vectors, ...)."""


@dataclass
class CaseResult:
    id: str
    type: str
    question: str
    expect: list[str]
    # mode -> 1-based rank of the first expected chunk, or None if not in the top DEPTH
    ranks: dict[str, int | None] = field(default_factory=dict)
    # mode -> the top 3 headings returned, to show what came back instead on a miss
    top: dict[str, list[str]] = field(default_factory=dict)
    # Highest cosine similarity from vector search; the input for the cutoff.
    top_similarity: float = 0.0


async def knowledge_fingerprint(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    """A hash of every retrievable chunk. If it differs from the baseline's, the
    knowledge itself changed (a reseed with other docs, an approval), so a change in
    the numbers is not only down to search."""
    hashes = await session.scalars(
        select(KnowledgeChunk.content_hash)
        .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
        .where(
            KnowledgeChunk.workspace_id == workspace_id,
            KnowledgeSource.status == KnowledgeStatus.approved.value,
        )
        .order_by(KnowledgeChunk.content_hash)
    )
    return hashlib.sha256("".join(hashes).encode()).hexdigest()[:16]


async def run_cases(
    session: AsyncSession,
    embedder: Embedder,
    eval_set: EvalSet,
    *,
    tuning: SearchTuning = DEFAULT_TUNING,
) -> tuple[uuid.UUID, list[CaseResult]]:
    workspace_id = await session.scalar(
        select(Workspace.id).where(Workspace.slug == eval_set.workspace)
    )
    if workspace_id is None:
        raise EvalError(f"no workspace {eval_set.workspace!r}; run `make seed-reset`")
    embedded = await session.scalar(
        select(func.count())
        .select_from(KnowledgeChunk)
        .where(
            KnowledgeChunk.workspace_id == workspace_id,
            KnowledgeChunk.embedding_model == embedder.model,
        )
    )
    if not embedded:
        # Vector and hybrid results would be meaningless, so stop instead of reporting
        # misleading numbers.
        raise EvalError(
            f"no chunks embedded with {embedder.model}; is the worker running? "
            "(make worker, then make seed-reset or make reembed)"
        )

    results = []
    for case in eval_set.cases:
        result = CaseResult(case.id, case.type, case.question, case.expect)
        for mode in MODES:
            try:
                found = await search(
                    session,
                    embedder,
                    workspace_id=workspace_id,
                    query=case.question,
                    mode=mode,
                    limit=DEPTH,
                    tuning=tuning,
                )
            except VectorSearchUnavailable as exc:
                raise EvalError(f"the embedding model failed on {case.id!r}: {exc}") from exc
            if found.degraded:
                # Hybrid does not raise when the model is down: it quietly returns
                # keyword results. Right for users, but here it would record keyword
                # numbers as hybrid's, so stop.
                raise EvalError(f"the embedding model failed on {case.id!r} (hybrid degraded)")
            headings = [hit.heading for hit in found.hits]
            result.ranks[mode.value] = first_relevant_rank(headings, case.expect)
            result.top[mode.value] = headings[:3]
            if mode is SearchMode.vector and found.hits:
                result.top_similarity = found.hits[0].score
        # Each search ran in this session's transaction; end it so SET LOCAL settings
        # and snapshots do not pile up over a long run.
        await session.rollback()
        results.append(result)
    return workspace_id, results


# ---------------------------------------------------------------- summarizing


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    answerable = [r for r in results if r.expect]
    unanswerable = [r for r in results if not r.expect]
    modes: dict[str, Any] = {}
    for mode in MODES:
        ranks = [r.ranks[mode.value] for r in answerable]
        by_type = {}
        for case_type in sorted({r.type for r in answerable}):
            typed = [r.ranks[mode.value] for r in answerable if r.type == case_type]
            by_type[case_type] = {"cases": len(typed), "recall@5": round(recall_at(typed, 5), 3)}
        modes[mode.value] = {
            **{f"recall@{k}": round(recall_at(ranks, k), 3) for k in RECALL_KS},
            "mrr": round(mean_reciprocal_rank(ranks), 3),
            "by_type": by_type,
        }
    good = [r.top_similarity for r in answerable]
    bad = [r.top_similarity for r in unanswerable]
    cutoff = best_cutoff(good, bad)
    return {
        "cases": len(results),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "modes": modes,
        "similarity": {
            "answerable_median": round(statistics.median(good), 3) if good else None,
            "answerable_min": round(min(good), 3) if good else None,
            "unanswerable_median": round(statistics.median(bad), 3) if bad else None,
            "unanswerable_max": round(max(bad), 3) if bad else None,
            "cutoff": (
                {
                    "threshold": round(cutoff.threshold, 3),
                    "answered": round(cutoff.answered, 3),
                    "refused": round(cutoff.refused, 3),
                }
                if cutoff
                else None
            ),
        },
    }


def build_report(
    results: list[CaseResult],
    *,
    model: str,
    dataset: Path,
    fingerprint: str,
    tuning: SearchTuning,
) -> dict[str, Any]:
    return {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "dataset": dataset.name,
        # Hash of the questions file: a different hash means the questions changed.
        "dataset_hash": hashlib.sha256(dataset.read_bytes()).hexdigest()[:16],
        "knowledge_fingerprint": fingerprint,
        "tuning": asdict(tuning),
        "summary": summarize(results),
        "cases": [asdict(r) for r in results],
    }


# ---------------------------------------------------------------- printing


def _delta(now: float, before: float | None) -> str:
    if before is None:
        return ""
    diff = now - before
    if abs(diff) < 0.0005:
        return "  ="
    return f" {'▲' if diff > 0 else '▼'}{abs(diff):.2f}"


def print_report(report: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    summary = report["summary"]
    print(
        f"\nRetrieval eval: {summary['cases']} questions "
        f"({summary['answerable']} answerable, {summary['unanswerable']} unanswerable), "
        f"model {report['model']}, tuning {report['tuning']}"
    )
    if baseline:
        for key, label in (
            ("model", "embedding model"),
            ("dataset_hash", "questions file"),
            ("knowledge_fingerprint", "knowledge"),
        ):
            if baseline.get(key) != report[key]:
                print(f"  note: the {label} differs from the baseline, so deltas mix causes")
    before_modes = baseline["summary"]["modes"] if baseline else {}

    print(f"\n  {'mode':<9}" + "".join(f"{f'R@{k}':>12}" for k in RECALL_KS) + f"{'MRR':>12}")
    for mode, numbers in summary["modes"].items():
        before = before_modes.get(mode, {})
        keys = [f"recall@{k}" for k in RECALL_KS] + ["mrr"]
        cells = [f"{numbers[key]:.2f}{_delta(numbers[key], before.get(key))}" for key in keys]
        print(f"  {mode:<9}" + "".join(f"{cell:>12}" for cell in cells))

    print("\n  recall@5 by question type")
    types = list(next(iter(summary["modes"].values()))["by_type"])
    print(f"  {'mode':<9}" + "".join(f"{t:>14}" for t in types))
    for mode, numbers in summary["modes"].items():
        cells = [
            f"{numbers['by_type'][t]['recall@5']:.2f} ({numbers['by_type'][t]['cases']})"
            for t in types
        ]
        print(f"  {mode:<9}" + "".join(f"{cell:>14}" for cell in cells))

    similarity = summary["similarity"]
    print("\n  top vector similarity (cosine)")
    print(
        f"    answerable:   median {similarity['answerable_median']}, "
        f"lowest {similarity['answerable_min']}"
    )
    print(
        f"    unanswerable: median {similarity['unanswerable_median']}, "
        f"highest {similarity['unanswerable_max']}"
    )
    cutoff = similarity["cutoff"]
    if cutoff:
        print(
            f"    best cutoff {cutoff['threshold']}: answers {cutoff['answered']:.0%} of "
            f"answerable questions, refuses {cutoff['refused']:.0%} of unanswerable ones"
        )

    misses = [
        case
        for case in report["cases"]
        if case["expect"] and (case["ranks"]["hybrid"] is None or case["ranks"]["hybrid"] > 5)
    ]
    print(f"\n  hybrid misses at 5: {len(misses)}")
    for case in misses:
        print(f"    [{case['id']}] {case['question']}")
        print(f"      expected: {' | '.join(case['expect'])}")
        print(
            f"      rank: {case['ranks']['hybrid'] or 'not found'}; top 3: {case['top']['hybrid']}"
        )

    unanswerable = sorted(
        (case for case in report["cases"] if not case["expect"]),
        key=lambda case: -case["top_similarity"],
    )
    if unanswerable and cutoff:
        leaks = [c for c in unanswerable if c["top_similarity"] >= cutoff["threshold"]]
        print(f"\n  unanswerable questions at or above the cutoff: {len(leaks)}")
        for case in leaks:
            print(
                f"    [{case['id']}] {case['top_similarity']:.3f}  {case['question']}"
                f"  -> {case['top']['vector'][:1]}"
            )

    if baseline:
        before = {case["id"]: case["ranks"]["hybrid"] for case in baseline["cases"]}
        changed = [
            (case["id"], before[case["id"]], case["ranks"]["hybrid"])
            for case in report["cases"]
            if case["expect"]
            and case["id"] in before
            and before[case["id"]] != case["ranks"]["hybrid"]
        ]
        if changed:
            print("\n  hybrid rank changes vs baseline (None = not found)")
            for case_id, old, new in changed:
                print(f"    {case_id}: {old} -> {new}")


# ---------------------------------------------------------------- command line


async def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieval eval (see module docstring).")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--save-baseline", action="store_true", help="save as the new baseline")
    parser.add_argument("--rrf-k", type=int, default=DEFAULT_TUNING.rrf_k)
    parser.add_argument("--candidates", type=int, default=DEFAULT_TUNING.candidates)
    parser.add_argument(
        "--any-word-hybrid",
        action="store_true",
        help="hybrid keyword side matches any word instead of every word",
    )
    args = parser.parse_args()

    # Imported here so importing this module (in tests) never builds the app's engine.
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.db.session import SessionLocal, engine
    from app.embeddings import build_embedder

    settings = get_settings()
    configure_logging(settings)
    # httpx logs every request at INFO: one line per embedding, which buries the report.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    tuning = SearchTuning(
        candidates=args.candidates, rrf_k=args.rrf_k, hybrid_any_word=args.any_word_hybrid
    )
    eval_set = load_eval_set(args.dataset)
    embedder = build_embedder(settings)
    try:
        async with SessionLocal() as session:
            workspace_id, results = await run_cases(session, embedder, eval_set, tuning=tuning)
            fingerprint = await knowledge_fingerprint(session, workspace_id)
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await embedder.aclose()
        await engine.dispose()

    report = build_report(
        results, model=embedder.model, dataset=args.dataset, fingerprint=fingerprint, tuning=tuning
    )
    baseline_file = baseline_path(args.dataset.resolve())
    baseline = json.loads(baseline_file.read_text()) if baseline_file.exists() else None
    print_report(report, None if args.save_baseline else baseline)

    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(report, indent=2) + "\n")
    if args.save_baseline:
        baseline_file.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nSaved the baseline to {baseline_file}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
