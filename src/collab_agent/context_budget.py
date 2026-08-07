from __future__ import annotations

import math
import os
import re
from copy import deepcopy
from typing import Any

from .models import canonical_json


DEFAULT_TASK_RESULT_TOKEN_BUDGET = 12_000
DEFAULT_COLLABORATION_HINT_TOKEN_BUDGET = 1_500

QWEN_ESTIMATOR = "qwen-bpe.v1"
FALLBACK_ESTIMATOR = "cjk-aware-char-class.v1"

# Measured against provider usage: the previous bytes/3 estimator was not
# uniformly conservative.  It over-counted prose (CJK 1.6x, ASCII 2.2x) but
# under-counted serialized JSON with long hex identifiers at 0.72x -- which is
# exactly the shape of a task-result context.  A budget guard that under-counts
# its own worst case is not a guard, so the real tokenizer is used when present.
_CJK = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]"
)
# Conservative per-character token weights for the offline fallback, each above
# the measured Qwen rate so the fallback never admits an over-budget context.
_CJK_TOKEN_WEIGHT = 1.0
_OTHER_TOKEN_WEIGHT = 0.6

_tokenizer: Any = None
_tokenizer_loaded = False


class ContextBudgetError(RuntimeError):
    """Raised when the mandatory context cannot fit without losing its meaning."""


def _qwen_tokenizer() -> Any:
    """Load the official local Qwen tokenizer once; None when unavailable.

    Offline runs (tests, the local processor) must not require the SDK, so a
    miss degrades to the conservative heuristic rather than failing.
    """

    global _tokenizer, _tokenizer_loaded
    if _tokenizer_loaded:
        return _tokenizer
    _tokenizer_loaded = True
    if os.environ.get("COLWORK_DISABLE_QWEN_TOKENIZER"):
        return None
    try:
        from dashscope import get_tokenizer

        _tokenizer = get_tokenizer(
            os.environ.get("BAILIAN_MODEL", "qwen-plus")
        )
    except Exception:
        # Any SDK, model-name or import failure falls back deterministically.
        _tokenizer = None
    return _tokenizer


def token_estimator_name() -> str:
    return QWEN_ESTIMATOR if _qwen_tokenizer() else FALLBACK_ESTIMATOR


def count_text_tokens(text: str) -> int:
    """Token count for one string, using the real tokenizer when available."""

    if not text:
        return 0
    tokenizer = _qwen_tokenizer()
    if tokenizer is not None:
        try:
            return len(tokenizer.encode(text))
        except Exception:
            pass
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return max(
        1,
        math.ceil(cjk * _CJK_TOKEN_WEIGHT + other * _OTHER_TOKEN_WEIGHT),
    )


def estimate_tokens(value: Any) -> int:
    """Return a deterministic token estimate for a context object.

    The provider's actual usage is still recorded after the call; this estimate
    is the pre-call admission guard.  Callers that also send a system prompt
    must reserve its cost separately -- see ``reserved_tokens``.
    """

    if isinstance(value, str):
        return max(1, count_text_tokens(value))
    return max(1, count_text_tokens(canonical_json(value)))


def task_result_token_budget() -> int:
    raw = os.environ.get("COLWORK_TASK_RESULT_TOKEN_BUDGET")
    if raw is None:
        return DEFAULT_TASK_RESULT_TOKEN_BUDGET
    try:
        budget = int(raw)
    except ValueError as error:
        raise ContextBudgetError(
            "COLWORK_TASK_RESULT_TOKEN_BUDGET must be an integer"
        ) from error
    if budget <= 0:
        raise ContextBudgetError(
            "COLWORK_TASK_RESULT_TOKEN_BUDGET must be positive"
        )
    return budget


def collaboration_hint_token_budget() -> int:
    raw = os.environ.get("COLWORK_COLLABORATION_HINT_TOKEN_BUDGET")
    if raw is None:
        return DEFAULT_COLLABORATION_HINT_TOKEN_BUDGET
    try:
        budget = int(raw)
    except ValueError as error:
        raise ContextBudgetError(
            "COLWORK_COLLABORATION_HINT_TOKEN_BUDGET must be an integer"
        ) from error
    if budget <= 0:
        raise ContextBudgetError(
            "COLWORK_COLLABORATION_HINT_TOKEN_BUDGET must be positive"
        )
    return budget


def bound_collaboration_hint_context(
    context: dict[str, Any], *, token_budget: int | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bound private confirmed preferences without summarizing or inventing them."""

    budget = (
        collaboration_hint_token_budget()
        if token_budget is None
        else int(token_budget)
    )
    if budget <= 0:
        raise ContextBudgetError("token_budget must be positive")
    bounded = deepcopy(context)
    estimated_before = estimate_tokens(bounded)
    memories = bounded.setdefault("confirmed_memories", [])
    omitted_refs: list[str] = []
    while estimate_tokens(bounded) > budget and memories:
        removed = memories.pop(0)
        omitted_refs.append(
            f'memory:{removed.get("memory_id") or "unknown"}:v{removed.get("version") or 0}'
        )
    estimated_after = estimate_tokens(bounded)
    if estimated_after > budget:
        raise ContextBudgetError(
            "mandatory collaboration-hint context exceeds the configured token budget"
        )
    included_refs = [
        f'memory:{item.get("memory_id")}:v{item.get("version")}'
        for item in memories
    ]
    return bounded, {
        "context_manifest_version": "context-manifest.v1",
        "purpose": "COLLABORATION_HINT",
        "token_budget": budget,
        "estimated_input_tokens_before": estimated_before,
        "estimated_input_tokens": estimated_after,
        "included_refs": included_refs,
        "omitted_refs": omitted_refs,
        "truncation_strategy": (
            ["DROP_OLDEST_CONFIRMED_MEMORY"] if omitted_refs else []
        ),
        "truncated": bool(omitted_refs),
        "estimator": token_estimator_name(),
    }


def _attachment_ref(attachment: dict[str, Any], index: int) -> str:
    return str(attachment.get("source_ref") or f"attachment:{index}")


def _included_refs(context: dict[str, Any]) -> list[str]:
    refs = ["task:contract", "submission:claim"]
    evidence = context.get("evidence") or {}
    refs.extend(
        str(link.get("source_ref") or f"link:{index}")
        for index, link in enumerate(evidence.get("links") or [])
    )
    refs.extend(
        _attachment_ref(attachment, index)
        for index, attachment in enumerate(evidence.get("attachments") or [])
    )
    refs.extend(
        f'version:{version.get("version_id") or index}'
        for index, version in enumerate(context.get("previous_version_context") or [])
    )
    collaboration = context.get("collaboration_inputs") or {}
    refs.extend(
        str(item.get("source_ref"))
        for item in collaboration.get("upstream_results") or []
        if item.get("source_ref")
    )
    decision = collaboration.get("collective_decision")
    if isinstance(decision, dict) and decision.get("source_ref"):
        refs.append(str(decision["source_ref"]))
    return refs


def _largest_prefix_that_fits(
    context: dict[str, Any],
    *,
    container: dict[str, Any],
    field: str,
    budget: int,
) -> int:
    original = str(container.get(field) or "")
    low = 0
    high = len(original)
    while low < high:
        middle = (low + high + 1) // 2
        container[field] = original[:middle]
        if estimate_tokens(context) <= budget:
            low = middle
        else:
            high = middle - 1
    container[field] = original[:low]
    return low


def bound_task_result_context(
    context: dict[str, Any],
    *,
    token_budget: int | None = None,
    reserved_tokens: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fit one task-result context to a budget using an explicit loss order.

    Current task intent and the current submission remain authoritative.  Old
    revision context is removed first, attachment tails second, and the current
    submission body only as the final fallback.  The returned manifest makes
    every omission inspectable without creating another workflow entity.

    ``reserved_tokens`` is the cost of everything the caller sends alongside the
    context -- the system prompt and the user framing.  It was previously
    uncounted, which let a context be admitted while the real request ran several
    times over budget.
    """

    total_budget = (
        task_result_token_budget() if token_budget is None else int(token_budget)
    )
    if total_budget <= 0:
        raise ContextBudgetError("token_budget must be positive")
    reserved = max(0, int(reserved_tokens))
    budget = total_budget - reserved
    if budget <= 0:
        raise ContextBudgetError(
            "the prompt overhead alone exceeds the configured token budget"
        )

    bounded = deepcopy(context)
    estimated_before = estimate_tokens(bounded)
    omitted_refs: list[str] = []
    strategies: list[str] = []

    previous = bounded.setdefault("previous_version_context", [])
    while estimate_tokens(bounded) > budget and previous:
        removed = previous.pop(0)
        omitted_refs.append(f'version:{removed.get("version_id") or "unknown"}')
        if "DROP_OLDEST_PREVIOUS_VERSION" not in strategies:
            strategies.append("DROP_OLDEST_PREVIOUS_VERSION")

    attachments = (bounded.get("evidence") or {}).get("attachments") or []
    while estimate_tokens(bounded) > budget:
        candidates = [
            (len(str(item.get("extracted_text") or "")), index, item)
            for index, item in enumerate(attachments)
            if str(item.get("extracted_text") or "")
        ]
        if not candidates:
            break
        _, index, attachment = max(candidates, key=lambda value: (value[0], -value[1]))
        original = str(attachment.get("extracted_text") or "")
        attachment["truncated"] = True
        kept = _largest_prefix_that_fits(
            bounded,
            container=attachment,
            field="extracted_text",
            budget=budget,
        )
        omitted_refs.append(f"{_attachment_ref(attachment, index)}:text_tail")
        if "TRUNCATE_LONGEST_ATTACHMENT_TEXT" not in strategies:
            strategies.append("TRUNCATE_LONGEST_ATTACHMENT_TEXT")
        if kept > 0 or estimate_tokens(bounded) <= budget:
            break

    if estimate_tokens(bounded) > budget:
        submission = bounded.get("submission_claim") or {}
        original = str(submission.get("content") or "")
        if original:
            submission["content_truncated"] = True
            kept = _largest_prefix_that_fits(
                bounded,
                container=submission,
                field="content",
                budget=budget,
            )
            if kept < len(original):
                omitted_refs.append("submission:claim:content_tail")
                strategies.append("TRUNCATE_CURRENT_SUBMISSION_CONTENT_LAST")

    estimated_after = estimate_tokens(bounded)
    if estimated_after > budget:
        raise ContextBudgetError(
            "mandatory task-result context exceeds the configured token budget"
        )

    manifest = {
        "context_manifest_version": "context-manifest.v1",
        "purpose": str(
            bounded.get("processing_purpose") or "TASK_RESULT_REVIEW"
        ),
        "token_budget": total_budget,
        "context_token_budget": budget,
        "reserved_prompt_tokens": reserved,
        "estimated_input_tokens_before": estimated_before + reserved,
        "estimated_input_tokens": estimated_after + reserved,
        "estimated_context_tokens": estimated_after,
        "included_refs": _included_refs(bounded),
        "omitted_refs": omitted_refs,
        "truncation_strategy": strategies,
        "truncated": bool(omitted_refs),
        "estimator": token_estimator_name(),
    }
    return bounded, manifest
