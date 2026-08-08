"""Extraction quality against labelled meetings.

Two metric families, deliberately kept apart:

**Sentence-level positive F1** is the metric the published literature uses for
this task, so it is the only number comparable to an outside baseline. On the
AMC-A corpus (AliMeeting4MUG), the ICASSP 2023 paper reports 70.82 F1 for
StructBERT with local+global context and Context-Drop. Our extractor does not
classify sentences -- it emits structured items -- so each extracted item is
mapped back to the transcript sentence it cites, which yields a sentence-level
prediction that can be scored the same way.

**Item-level metrics** are what the product actually needs: did we get the
title, the owner, the deadline and a quote that really appears in the
transcript. No public baseline exists, so the comparison is against a
single-prompt extractor run on the same input.

A ceiling that must be reported with any number here: inter-annotator Kappa on
AMC-A is 0.47 (and 0.36 on ICSI). Action items are genuinely subjective, so a
score in the 70s is near the human agreement band, and a claim of "95% F1"
would indicate a measurement error rather than a good model.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import read_text_file

SENTENCE_END = re.compile(r"(?<=[。！？!?])")
ANNOTATOR_KAPPA_AMC_A = 0.47
PUBLISHED_SENTENCE_F1_BASELINE = {
    "corpus": "AMC-A (AliMeeting4MUG)",
    "system": "StructBERT + local/global context + Context-Drop (dynamic)",
    "positive_f1": 70.82,
    "source": "ICASSP 2023, arXiv:2303.16763",
    "inter_annotator_kappa": ANNOTATOR_KAPPA_AMC_A,
}


def normalize(text: str) -> str:
    """Fold width, case and punctuation so quote matching is not brittle."""

    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"[\s，。！？?；;：:、‘’“”\"'（）()【】\[\],.!-]+", "", text)


def split_sentences(transcript: str) -> list[str]:
    """Split on terminal punctuation, matching the corpus' sentence unit.

    AMC-A treats semantic units ending in a period, question mark or
    exclamation as sentences for annotation, so the same rule is used here.
    """

    lines: list[str] = []
    for raw_line in str(transcript or "").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # Drop a leading "speaker (00:01:00): " prefix if present.
        line = re.sub(r"^[^:：]{0,40}[（(]\d{1,2}:\d{2}:\d{2}[）)]\s*[:：]\s*", "", line)
        for piece in SENTENCE_END.split(line):
            piece = piece.strip()
            if piece:
                lines.append(piece)
    return lines


@dataclass
class LabelledMeeting:
    """One meeting with sentence-level action-item labels."""

    meeting_id: str
    transcript: str
    sentences: list[str]
    positive_sentence_indices: set[int]
    expected_items: list[dict[str, Any]] = field(default_factory=list)
    meeting_date: str = ""
    participants: list[str] = field(default_factory=list)

    @property
    def positive_rate(self) -> float:
        return (
            len(self.positive_sentence_indices) / len(self.sentences)
            if self.sentences
            else 0.0
        )


def load_amc_a(path: str | Path) -> list[LabelledMeeting]:
    """Read AMC-A style JSONL: one meeting per line, sentence list with labels.

    Expected shape per line, which is the layout the AliMeeting4MUG action-item
    release uses after `data_script` processing::

        {"meeting_key": "...", "sentences": [{"text": "...", "label": 0|1}, ...]}

    Field aliases are accepted because the released archives are not uniform.
    """

    meetings: list[LabelledMeeting] = []
    for line in read_text_file(path).splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        meeting_id = str(
            record.get("meeting_key")
            or record.get("meeting_id")
            or record.get("id")
            or f"meeting_{len(meetings) + 1}"
        )
        raw_sentences = (
            record.get("sentences")
            or record.get("utterances")
            or record.get("data")
            or []
        )
        sentences: list[str] = []
        positives: set[int] = set()
        for index, item in enumerate(raw_sentences):
            if isinstance(item, str):
                sentences.append(item)
                continue
            text = str(
                item.get("text") or item.get("sentence") or item.get("content") or ""
            )
            sentences.append(text)
            label = item.get("label")
            if label is None:
                label = item.get("action_item") or item.get("is_action_item")
            if str(label) in {"1", "True", "true"}:
                positives.add(index)
        meetings.append(
            LabelledMeeting(
                meeting_id=meeting_id,
                transcript="\n".join(sentences),
                sentences=sentences,
                positive_sentence_indices=positives,
            )
        )
    return meetings


def _timestamp(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "00:00:00"
    hours, remainder = divmod(max(0, total), 3600)
    minutes, second = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{second:02d}"


def load_alimeeting4mug(
    root: str | Path,
    *,
    split: str = "dev",
    limit: int | None = None,
    max_sentences: int | None = None,
) -> list[LabelledMeeting]:
    """Read the released AliMeeting4MUG archives (AMC-A action-item labels).

    Each line of the TSV holds one meeting as JSON with `sentences`
    (``{id, speaker, start_time, end_time, s}``) and `action_ids`, the sentence
    ids annotated as action items.

    ``limit`` and ``max_sentences`` exist because these meetings are long: the
    dev split alone is ~46k sentences, so running a model-backed extractor over
    all of it is a deliberate spend, not a default.
    """

    import zipfile

    archive = Path(root) / "data" / f"{split}.zip"
    if not archive.exists():
        raise FileNotFoundError(
            f"{archive} not found; expected the released AliMeeting4MUG layout"
        )
    with zipfile.ZipFile(archive) as bundle:
        inner = bundle.namelist()[0]
        raw = bundle.read(inner).decode("utf-8", errors="replace")

    meetings: list[LabelledMeeting] = []
    for line in raw.splitlines()[1:]:
        if "\t" not in line:
            continue
        _, content = line.split("\t", 1)
        record = json.loads(content)
        raw_sentences = record.get("sentences") or []
        if max_sentences:
            raw_sentences = raw_sentences[:max_sentences]
        sentences: list[str] = []
        # Sentence ids are 1-based in the release and are not guaranteed to be
        # contiguous, so labels are matched on the id rather than the position.
        index_by_id: dict[str, int] = {}
        lines: list[str] = []
        for position, item in enumerate(raw_sentences):
            text = str(item.get("s") or "")
            sentences.append(text)
            index_by_id[str(item.get("id"))] = position
            # The corpus stores offsets in seconds, but this project's extractor
            # requires HH:MM:SS so it can align every claim to a timestamped
            # line. Rendering it here keeps the corpus usable without relaxing
            # the source-alignment contract, which is the thing being measured.
            lines.append(
                f'{item.get("speaker", "")} ({_timestamp(item.get("start_time"))}): {text}'
            )
        positives = {
            index_by_id[str(entry.get("id"))]
            for entry in record.get("action_ids") or []
            if str(entry.get("id")) in index_by_id
        }
        meetings.append(
            LabelledMeeting(
                meeting_id=str(record.get("meeting_key") or f"m{len(meetings)}"),
                transcript="\n".join(lines),
                sentences=sentences,
                positive_sentence_indices=positives,
            )
        )
        if limit and len(meetings) >= limit:
            break
    return meetings


def _best_matching_sentences(
    normalized_sentences: list[str], items: Iterable[dict[str, Any]]
) -> set[int]:
    """Locate the one sentence each quote cites.

    One item cites one sentence, so only its best match is credited. Crediting
    every substring hit instead inflated a single item into ten-plus sentences,
    because a transcript is full of short backchannels ("对", "是这个") that any
    longer quote contains -- which destroyed precision across the whole corpus.
    """

    matched: set[int] = set()
    for item in items:
        quote = normalize(item.get("source_quote") or "")
        if not quote:
            continue
        best_index: int | None = None
        best_length = 0
        for index, sentence in enumerate(normalized_sentences):
            if not sentence:
                continue
            if sentence == quote:
                best_index, best_length = index, len(sentence)
                break
            if sentence in quote or quote in sentence:
                overlap = min(len(sentence), len(quote))
                if overlap > best_length:
                    best_index, best_length = index, overlap
        # A two-character coincidence is not a citation.
        if best_index is not None and best_length >= 4:
            matched.add(best_index)
    return matched


def load_project_cases(path: str | Path) -> list[LabelledMeeting]:
    """Read this project's own annotated meetings.

    Human expectations are recorded per action item (what a coordinator would
    have to confirm), not per sentence, so sentence labels are derived by
    locating each expected quote in the transcript.
    """

    payload = json.loads(read_text_file(path))
    meetings: list[LabelledMeeting] = []
    for case in payload.get("cases", []):
        transcript = str(case.get("transcript") or "")
        sentences = split_sentences(transcript)
        normalized = [normalize(sentence) for sentence in sentences]
        expected = case.get("expected") or []
        # Gold labels have to be located the same way predictions are, or the
        # two sides are not comparable. Sharing the matcher also means the
        # one-item-one-sentence rule applies here: a transcript is full of short
        # backchannels that any longer quote contains, and crediting every
        # substring hit would inflate one annotation into several gold
        # sentences.
        positives = _best_matching_sentences(normalized, expected)
        meetings.append(
            LabelledMeeting(
                meeting_id=str(case.get("case_id") or f"case_{len(meetings) + 1}"),
                transcript=transcript,
                sentences=sentences,
                positive_sentence_indices=positives,
                expected_items=expected,
                meeting_date=str(case.get("meeting_date") or ""),
                participants=list(case.get("participants") or []),
            )
        )
    return meetings


def _prf(true_positive: int, false_positive: int, false_negative: int) -> dict[str, float]:
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
    }


def predicted_sentence_indices(
    meeting: LabelledMeeting, items: Iterable[dict[str, Any]]
) -> set[int]:
    """Map extracted items back to the sentences they cite.

    An item whose quote matches no sentence still counts as a prediction, but
    it cannot be credited to any sentence -- which is exactly the behaviour we
    want, since an unlocatable quote is a fabricated citation.
    """

    return _best_matching_sentences(
        [normalize(sentence) for sentence in meeting.sentences], items
    )


def score_sentences(
    meeting: LabelledMeeting, items: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    predicted = predicted_sentence_indices(meeting, items)
    gold = meeting.positive_sentence_indices
    return _prf(
        len(predicted & gold),
        len(predicted - gold),
        len(gold - predicted),
    )


def _matches(expected: dict[str, Any], predicted: dict[str, Any]) -> bool:
    """One expected item is matched by the prediction citing the same evidence.

    Titles are paraphrased freely by any extractor, so identity is anchored on
    the cited quote rather than on wording.
    """

    expected_quote = normalize(expected.get("source_quote") or "")
    predicted_quote = normalize(predicted.get("source_quote") or "")
    if expected_quote and predicted_quote:
        if expected_quote in predicted_quote or predicted_quote in expected_quote:
            return True
    expected_title = normalize(expected.get("title") or "")
    predicted_title = normalize(predicted.get("title") or "")
    return bool(
        expected_title
        and predicted_title
        and (expected_title in predicted_title or predicted_title in expected_title)
    )


def score_items(
    meeting: LabelledMeeting, items: list[dict[str, Any]]
) -> dict[str, Any]:
    expected = meeting.expected_items
    unmatched = list(items)
    matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for want in expected:
        for candidate in unmatched:
            if _matches(want, candidate):
                matched_pairs.append((want, candidate))
                unmatched.remove(candidate)
                break
    detection = _prf(
        len(matched_pairs),
        len(unmatched),
        len(expected) - len(matched_pairs),
    )
    normalized_transcript = normalize(meeting.transcript)
    field_hits = {"owner_name": 0, "deadline_iso": 0, "deliverable": 0}
    field_total = {"owner_name": 0, "deadline_iso": 0, "deliverable": 0}
    # Collaborators are scored as a set: a task named two people is not "wrong
    # owner", it is a task with an owner and a collaborator, and the extractor
    # has always been able to say so. Omitting this from the gold schema forced
    # annotators to write null for every jointly-assigned task, which would
    # have scored a correct extraction as an error.
    collaborator_hits = 0
    collaborator_total = 0
    for want, got in matched_pairs:
        wanted_collaborators = {
            normalize(name) for name in want.get("collaborator_names") or [] if name
        }
        if wanted_collaborators:
            collaborator_total += 1
            got_collaborators = {
                normalize(name) for name in got.get("collaborator_names") or [] if name
            }
            if wanted_collaborators == got_collaborators:
                collaborator_hits += 1
        for name in field_hits:
            if want.get(name) in (None, ""):
                continue
            field_total[name] += 1
            if normalize(want.get(name) or "") == normalize(got.get(name) or ""):
                field_hits[name] += 1
    groundable = 0
    for item in items:
        quote = normalize(item.get("source_quote") or "")
        if quote and quote in normalized_transcript:
            groundable += 1
    return {
        "detection": detection,
        "field_accuracy": {
            **{
                name: round(field_hits[name] / field_total[name], 4)
                if field_total[name]
                else None
                for name in field_hits
            },
            "collaborator_names": (
                round(collaborator_hits / collaborator_total, 4)
                if collaborator_total
                else None
            ),
        },
        "predicted_items": len(items),
        # A quote that cannot be found in the transcript is a fabricated
        # citation, and the extractor's own repair step exists to prevent it.
        "quote_grounding_rate": round(groundable / len(items), 4) if items else None,
        "ungrounded_quotes": len(items) - groundable,
    }


TIME_EXPRESSION = re.compile(
    r"(今天|明天|后天|大后天|昨天|本周|下周|上周|周[一二三四五六日天]|"
    r"星期[一二三四五六日天]|月底|月初|下个?月|本月|年底|年初|"
    r"\d{1,2}\s*[月日号]|\d{1,2}\s*[:：]\s*\d{2}|"
    r"[上下]午|晚上|早上|之前|以前|截止|deadline|近期|尽快)"
)


def benchmark_aligned(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only items whose cited sentence carries a time frame.

    AMC-A labels a sentence positive when it contains task description **and a
    time frame and an owner**. This project's extractor deliberately emits a
    task even when neither is stated, marking it `needs_confirmation` for the
    coordinator to fill in -- so the raw predictions are a superset of the
    corpus definition by design, not by accident.

    Filtering here makes the two definitions comparable. It is an
    evaluation-time projection only: nothing in the product changes, and both
    the filtered and unfiltered scores are reported so the gap is visible
    rather than argued away.
    """

    aligned: list[dict[str, Any]] = []
    for item in items:
        quote = str(item.get("source_quote") or "")
        stated_deadline = item.get("deadline_iso") or item.get("deadline_text")
        if stated_deadline or TIME_EXPRESSION.search(quote):
            aligned.append(item)
    return aligned


def evaluate_extractor(
    meetings: list[LabelledMeeting],
    extractor: Callable[[LabelledMeeting], list[dict[str, Any]]],
    *,
    name: str,
) -> dict[str, Any]:
    """Score one extraction strategy over every labelled meeting."""

    sentence_totals = {"true_positive": 0, "false_positive": 0, "false_negative": 0}
    item_totals = {"true_positive": 0, "false_positive": 0, "false_negative": 0}
    per_meeting: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    grounded = 0
    predicted_total = 0
    # Meetings that genuinely contain no action item are the sharpest test of
    # over-extraction: any prediction there is unambiguously a false alarm, and
    # F1 alone hides them because they contribute no true positives.
    empty_meetings = 0
    empty_meetings_clean = 0
    # Storing the raw predictions makes any later re-scoring free: a run can
    # be replayed against a different label definition without paying for the
    # model again, which is what keeps a blind set usable more than once.
    predictions: dict[str, list[dict[str, Any]]] = {}
    for meeting in meetings:
        try:
            items = list(extractor(meeting))
        except Exception as error:  # an extractor failure is a result, not a crash
            errors.append({"meeting_id": meeting.meeting_id, "error": str(error)[:300]})
            continue
        predictions[meeting.meeting_id] = items
        sentence_score = score_sentences(meeting, items)
        item_score = score_items(meeting, items)
        for key in sentence_totals:
            sentence_totals[key] += sentence_score[key]
            item_totals[key] += item_score["detection"][key]
        predicted_total += item_score["predicted_items"]
        grounded += item_score["predicted_items"] - item_score["ungrounded_quotes"]
        if not meeting.positive_sentence_indices:
            empty_meetings += 1
            if not items:
                empty_meetings_clean += 1
        per_meeting.append(
            {
                "meeting_id": meeting.meeting_id,
                "sentences": len(meeting.sentences),
                "gold_positive_sentences": len(meeting.positive_sentence_indices),
                "sentence_level": sentence_score,
                "item_level": item_score,
            }
        )
    return {
        "extractor": name,
        "meetings_scored": len(per_meeting),
        "meetings_failed": len(errors),
        "errors": errors,
        "sentence_level_positive_f1": _prf(**sentence_totals),
        "item_level_detection": _prf(**item_totals),
        "quote_grounding_rate": (
            round(grounded / predicted_total, 4) if predicted_total else None
        ),
        "meetings_without_action_items": empty_meetings,
        "empty_meetings_kept_clean": empty_meetings_clean,
        "false_alarm_rate_on_empty_meetings": (
            round(1 - empty_meetings_clean / empty_meetings, 4)
            if empty_meetings
            else None
        ),
        "per_meeting": per_meeting,
        "predictions": predictions,
    }


def frozen_state() -> dict[str, Any]:
    """Record what produced these numbers, so «blind» is auditable.

    A held-out set only means something if the extractor was frozen before the
    set existed. Without this, that ordering is a claim the reader has to take
    on trust; with it, the prompt version and commit in the report can be
    checked against when the annotation file was created.
    """

    import subprocess
    from datetime import UTC, datetime

    from .extraction import (
        ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
        ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION,
    )

    def git(*args: str) -> str | None:
        try:
            return subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        except Exception:
            return None

    dirty = git("status", "--porcelain")
    return {
        "prompt_version": ACTION_ITEM_EXTRACTION_PROMPT_VERSION,
        "tools_prompt_version": ACTION_ITEM_EXTRACTION_TOOLS_PROMPT_VERSION,
        "commit": git("rev-parse", "HEAD"),
        # A dirty tree means the committed prompt is not what actually ran, so
        # the reader cannot reconstruct this number.
        "working_tree_clean": (dirty == "") if dirty is not None else None,
        "run_at": datetime.now(UTC).isoformat(),
    }


def compare_extractors(
    meetings: list[LabelledMeeting],
    extractors: dict[str, Callable[[LabelledMeeting], list[dict[str, Any]]]],
) -> dict[str, Any]:
    """Run every strategy over the same meetings so the deltas are meaningful."""

    results = {
        name: evaluate_extractor(meetings, extractor, name=name)
        for name, extractor in extractors.items()
    }
    corpus = {
        "meetings": len(meetings),
        "sentences": sum(len(meeting.sentences) for meeting in meetings),
        "gold_positive_sentences": sum(
            len(meeting.positive_sentence_indices) for meeting in meetings
        ),
    }
    corpus["positive_sentence_rate"] = (
        round(corpus["gold_positive_sentences"] / corpus["sentences"], 4)
        if corpus["sentences"]
        else None
    )
    return {
        "schema_version": "extraction-evaluation.v1",
        "frozen_state": frozen_state(),
        "corpus": corpus,
        "published_reference": PUBLISHED_SENTENCE_F1_BASELINE,
        "interpretation_ceiling": (
            "AMC-A 标注者间 Kappa 仅 0.47（ICSI 为 0.36），行动项判定本身高度主观。"
            "句级 F1 在 70 上下即已接近人类一致性带；出现 90+ 通常说明测量口径有问题，"
            "而不是模型很好。"
        ),
        "results": results,
    }
