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

from .models import read_text_file, stable_hash

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


MAX_PERSON_NAME_LENGTH = 12


def resolve_participant(name: Any, participants: Iterable[str]) -> str | None:
    """Map a name as spoken in a transcript onto one roster entry.

    A transcript calls people whatever the speakers call them -- «苏绒» for the
    person the roster lists as «绒», «静雅» for «张静雅». The extractor has no
    roster, so it faithfully writes the spoken form, and an exact string
    comparison then reports a correctly identified person as an error.

    Returns None when nothing matches and, deliberately, also when more than
    one entry does: an ambiguous name must not be silently resolved to
    whichever roster entry happened to come first. Nicknames that are not a
    substring either way («子恒» for «黄Z恒») stay unresolved here; those need
    the human-confirmed alias map, not a guess.
    """

    text = str(name or "").strip()
    if not text:
        return None
    roster = [str(person).strip() for person in participants if str(person).strip()]
    folded = text.casefold()
    for person in roster:
        if person.casefold() == folded:
            return person
    # Only a plausible person name may be resolved by containment; a sentence
    # fragment would otherwise swallow whichever roster name it happens to hold.
    if len(text) > MAX_PERSON_NAME_LENGTH:
        return None
    candidates = [
        person
        for person in roster
        if person.casefold() in folded or folded in person.casefold()
    ]
    return candidates[0] if len(candidates) == 1 else None


def _same_person(
    expected: Any, predicted: Any, participants: Iterable[str]
) -> bool:
    roster = list(participants)
    if not roster:
        return normalize(expected or "") == normalize(predicted or "")
    left = resolve_participant(expected, roster)
    right = resolve_participant(predicted, roster)
    if left is not None and right is not None:
        return left == right
    return normalize(expected or "") == normalize(predicted or "")


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
    roster = meeting.participants
    unmatched = list(items)
    matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for want in expected:
        for candidate in unmatched:
            if _matches(want, candidate):
                matched_pairs.append((want, candidate))
                unmatched.remove(candidate)
                break
    # AMC-A supplies sentence labels but no structured item gold. Returning a
    # synthetic all-zero item score here made "not annotated" look like "the
    # extractor found nothing".  Keep grounding diagnostics, but mark item
    # detection explicitly unavailable.
    detection = (
        _prf(
            len(matched_pairs),
            len(unmatched),
            len(expected) - len(matched_pairs),
        )
        if expected
        else None
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
        def as_people(names: Any) -> set[str]:
            resolved = set()
            for name in names or []:
                if not name:
                    continue
                person = resolve_participant(name, roster) if roster else None
                resolved.add(person or normalize(name))
            return resolved

        wanted_collaborators = as_people(want.get("collaborator_names"))
        if wanted_collaborators:
            collaborator_total += 1
            if wanted_collaborators == as_people(got.get("collaborator_names")):
                collaborator_hits += 1
        for name in field_hits:
            if want.get(name) in (None, ""):
                continue
            field_total[name] += 1
            if name == "owner_name":
                if _same_person(want.get(name), got.get(name), roster):
                    field_hits[name] += 1
                continue
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


def _recall_gold_coverage(
    artifact: dict[str, Any] | None,
    gold_sentence_indices: set[int],
) -> dict[str, Any] | None:
    """Measure where recall evidence survived without treating support as FP.

    Sentence F1 intentionally credits only a cited anchor. For a recall-first
    pipeline we also need to know whether a gold sentence was retained as
    explicit candidate support, or survived into the evidence shown in either
    a draft or a review hint. Those are recall-only coverage measures: support
    text is not itself a predicted action sentence, so precision is undefined.
    """

    if not isinstance(artifact, dict) or not isinstance(
        artifact.get("raw_candidates"), list
    ):
        return None
    unit_by_id = {
        str(unit.get("unit_id")): unit
        for unit in artifact.get("units") or []
        if isinstance(unit, dict) and unit.get("unit_id")
    }

    def line_indices(unit_ids: Iterable[Any]) -> set[int]:
        result: set[int] = set()
        for unit_id in unit_ids:
            unit = unit_by_id.get(str(unit_id))
            if unit is None:
                continue
            try:
                result.add(int(unit.get("line_index")))
            except (TypeError, ValueError):
                continue
        return result

    anchors: set[int] = set()
    explicit_evidence: set[int] = set()
    for candidate in artifact.get("raw_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        candidate_anchors = line_indices(candidate.get("anchor_unit_ids") or [])
        anchors.update(candidate_anchors)
        explicit_evidence.update(candidate_anchors)
        explicit_evidence.update(
            line_indices(candidate.get("support_unit_ids") or [])
        )

    routed_evidence: set[int] = set()
    for item in [
        *(artifact.get("draft_items") or []),
        *(artifact.get("review_hints") or []),
    ]:
        if isinstance(item, dict):
            routed_evidence.update(line_indices(item.get("evidence_unit_ids") or []))

    gold = set(gold_sentence_indices)

    def surface(indices: set[int]) -> dict[str, Any]:
        hits = len(gold.intersection(indices))
        return {
            "hits": hits,
            "recall": round(hits / len(gold), 4) if gold else None,
        }

    return {
        "gold_positive_sentences": len(gold),
        "raw_anchor": surface(anchors),
        "explicit_candidate_evidence": surface(explicit_evidence),
        "routed_candidate_evidence": surface(routed_evidence),
    }
def evaluate_extractor(
    meetings: list[LabelledMeeting],
    extractor: Callable[[LabelledMeeting], list[dict[str, Any]]],
    *,
    name: str,
    checkpoint_dir: str | Path | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Score one extraction strategy over every labelled meeting."""

    sentence_totals = {"true_positive": 0, "false_positive": 0, "false_negative": 0}
    item_totals = {"true_positive": 0, "false_positive": 0, "false_negative": 0}
    per_meeting: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    grounded = 0
    predicted_total = 0
    # A zero-label AMC-A meeting is not necessarily action-free under this
    # product's broader definition (owner/deadline may be missing). Keep this
    # useful workload slice, but do not call every prediction a false alarm.
    zero_label_meetings = 0
    zero_label_meetings_clean = 0
    # Storing the raw predictions makes any later re-scoring free: a run can
    # be replayed against a different label definition without paying for the
    # model again, which is what keeps a blind set usable more than once.
    predictions: dict[str, list[dict[str, Any]]] = {}
    stage_totals = {
        "units": 0,
        "windows": 0,
        "model_windows_succeeded": 0,
        "rule_candidates": 0,
        "model_candidates": 0,
        "raw_candidates": 0,
        "sufficient_candidates": 0,
        "draft_ready_candidates": 0,
        "weak_signal_candidates": 0,
        "anchor_unit_references": 0,
        "support_unit_references": 0,
        "evidence_unit_references": 0,
        "evidence_bridge_unit_references": 0,
        "model_support_trimmed_candidates": 0,
        "model_unknown_reference_candidates": 0,
        "model_unit_id_canonicalized_candidates": 0,
        "visible_context_anchor_recovered_candidates": 0,
        "visible_context_duplicate_merged_candidates": 0,
        "draft_items": 0,
        "review_hints": 0,
        "candidate_failures": 0,
    }
    routing_reason_totals: dict[str, int] = {}
    usage_totals: dict[str, int | None] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls_with_usage": 0,
    }
    usage_seen = {"prompt_tokens": False, "completion_tokens": False, "total_tokens": False}
    recall_coverage_hits = {
        "raw_anchor": 0,
        "explicit_candidate_evidence": 0,
        "routed_candidate_evidence": 0,
    }
    recall_artifact_meetings = 0
    item_gold_meetings = 0
    checkpoint_root = Path(checkpoint_dir) if checkpoint_dir else None
    extractor_signature = str(
        getattr(extractor, "run_signature", "")
        or f"{getattr(extractor, '__module__', '')}.{getattr(extractor, '__qualname__', type(extractor).__qualname__)}"
    )
    if checkpoint_root:
        checkpoint_root.mkdir(parents=True, exist_ok=True)
    for meeting in meetings:
        checkpoint_path = (
            checkpoint_root
            / f"{stable_hash([name, extractor_signature, meeting.meeting_id, meeting.transcript])[:24]}.json"
            if checkpoint_root
            else None
        )
        extraction_error: str | None = None
        artifact: dict[str, Any] | None = None
        reused = False
        try:
            if resume and checkpoint_path and checkpoint_path.exists():
                recovered = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                # Successful meetings are the expensive part worth reusing.
                # A failed checkpoint is evidence, not a terminal result: a
                # continuation retries it so transient provider failures can
                # actually finish the corpus.
                if recovered.get("error") is None:
                    items = list(recovered.get("items") or [])
                    artifact = recovered.get("artifact")
                    reused = True
            if not reused:
                items = list(extractor(meeting))
                artifact = getattr(extractor, "last_artifact", None)
                if checkpoint_path:
                    checkpoint_path.write_text(
                        json.dumps(
                            {"items": items, "error": None, "artifact": artifact},
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
        except Exception as error:  # an extractor failure is a result, not a crash
            extraction_error = str(error)[:300]
            items = []
            if checkpoint_path:
                checkpoint_path.write_text(
                    json.dumps(
                        {"items": [], "error": extraction_error, "artifact": None},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        if extraction_error:
            errors.append(
                {"meeting_id": meeting.meeting_id, "error": extraction_error}
            )
        predictions[meeting.meeting_id] = items
        sentence_score = score_sentences(meeting, items)
        item_score = score_items(meeting, items)
        for key in sentence_totals:
            sentence_totals[key] += sentence_score[key]
            if item_score["detection"] is not None:
                item_totals[key] += item_score["detection"][key]
        if item_score["detection"] is not None:
            item_gold_meetings += 1
        predicted_total += item_score["predicted_items"]
        grounded += item_score["predicted_items"] - item_score["ungrounded_quotes"]
        if not meeting.positive_sentence_indices:
            zero_label_meetings += 1
            if not items:
                zero_label_meetings_clean += 1
        stage = None
        recall_coverage = _recall_gold_coverage(
            artifact, meeting.positive_sentence_indices
        )
        if recall_coverage is not None:
            recall_artifact_meetings += 1
            for surface in recall_coverage_hits:
                recall_coverage_hits[surface] += int(
                    recall_coverage[surface]["hits"]
                )
        if isinstance(artifact, dict):
            raw_coverage = artifact.get("coverage") or {}
            stage = {
                "pipeline_status": artifact.get("pipeline_status"),
                "units": int(raw_coverage.get("units_total") or 0),
                "windows": int(raw_coverage.get("windows_total") or 0),
                "model_windows_succeeded": int(
                    raw_coverage.get("model_windows_succeeded") or 0
                ),
                "emit_coverage_rate": raw_coverage.get("coverage_rate"),
                "model_window_success_rate": raw_coverage.get(
                    "model_window_success_rate"
                ),
                "raw_candidates": int(raw_coverage.get("raw_candidates") or 0),
                "rule_candidates": int(raw_coverage.get("rule_candidates") or 0),
                "model_candidates": int(raw_coverage.get("model_candidates") or 0),
                "sufficient_candidates": int(
                    raw_coverage.get("sufficient_candidates") or 0
                ),
                "draft_ready_candidates": int(
                    raw_coverage.get("draft_ready_candidates")
                    or raw_coverage.get("sufficient_candidates")
                    or 0
                ),
                "weak_signal_candidates": int(
                    raw_coverage.get("weak_signal_candidates") or 0
                ),
                "anchor_unit_references": int(
                    raw_coverage.get("anchor_unit_references") or 0
                ),
                "support_unit_references": int(
                    raw_coverage.get("support_unit_references") or 0
                ),
                "evidence_unit_references": int(
                    raw_coverage.get("evidence_unit_references") or 0
                ),
                "evidence_bridge_unit_references": int(
                    raw_coverage.get("evidence_bridge_unit_references") or 0
                ),
                "model_support_trimmed_candidates": int(
                    raw_coverage.get("model_support_trimmed_candidates") or 0
                ),
                "model_unknown_reference_candidates": int(
                    raw_coverage.get("model_unknown_reference_candidates") or 0
                ),
                "model_unit_id_canonicalized_candidates": int(
                    raw_coverage.get("model_unit_id_canonicalized_candidates")
                    or 0
                ),
                "visible_context_anchor_recovered_candidates": int(
                    raw_coverage.get(
                        "visible_context_anchor_recovered_candidates"
                    )
                    or 0
                ),
                "visible_context_duplicate_merged_candidates": int(
                    raw_coverage.get(
                        "visible_context_duplicate_merged_candidates"
                    )
                    or 0
                ),
                "draft_items": int(raw_coverage.get("draft_items") or 0),
                "review_hints": int(raw_coverage.get("review_hints") or 0),
                "candidate_failures": len(artifact.get("failures") or []),
                "routing_reasons": dict(raw_coverage.get("routing_reasons") or {}),
                "usage": dict(artifact.get("usage") or {}),
            }
            for key in stage_totals:
                stage_totals[key] += int(stage.get(key) or 0)
            for reason, count in stage["routing_reasons"].items():
                routing_reason_totals[str(reason)] = (
                    routing_reason_totals.get(str(reason), 0) + int(count or 0)
                )
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = stage["usage"].get(key)
                if isinstance(value, int):
                    usage_totals[key] = int(usage_totals[key] or 0) + value
                    usage_seen[key] = True
            usage_totals["calls_with_usage"] = int(
                usage_totals["calls_with_usage"] or 0
            ) + sum(
                isinstance(call.get("total_tokens"), int)
                for call in artifact.get("token_calls") or []
                if isinstance(call, dict)
            )
        per_meeting.append(
            {
                "meeting_id": meeting.meeting_id,
                "extraction_status": "FAILED" if extraction_error else "SUCCEEDED",
                "extraction_error": extraction_error,
                "sentences": len(meeting.sentences),
                "gold_positive_sentences": len(meeting.positive_sentence_indices),
                "has_gold_positive_sentence": bool(
                    meeting.positive_sentence_indices
                ),
                "sentence_level": sentence_score,
                "item_level": item_score,
                "stages": stage,
                "recall_gold_coverage": recall_coverage,
            }
        )
    gold_positive_sentences = sum(
        len(meeting.positive_sentence_indices) for meeting in meetings
    )
    return {
        "extractor": name,
        "meetings_attempted": len(meetings),
        "meetings_scored": len(meetings) - len(errors),
        "meetings_failed": len(errors),
        "errors": errors,
        "sentence_level_positive_f1": _prf(**sentence_totals),
        "item_level_detection": (
            _prf(**item_totals) if item_gold_meetings else None
        ),
        "item_level_gold_meetings": item_gold_meetings,
        "quote_grounding_rate": (
            round(grounded / predicted_total, 4) if predicted_total else None
        ),
        "amc_zero_label_meetings": zero_label_meetings,
        "amc_zero_label_meetings_kept_clean": zero_label_meetings_clean,
        "prediction_rate_on_amc_zero_label_meetings": (
            round(1 - zero_label_meetings_clean / zero_label_meetings, 4)
            if zero_label_meetings
            else None
        ),
        # Backward-compatible aliases. They are definitionally weaker than the
        # old names suggested; new reports should use the AMC-specific keys.
        "meetings_without_action_items": zero_label_meetings,
        "empty_meetings_kept_clean": zero_label_meetings_clean,
        "false_alarm_rate_on_empty_meetings": (
            round(1 - zero_label_meetings_clean / zero_label_meetings, 4)
            if zero_label_meetings
            else None
        ),
        "per_meeting": per_meeting,
        "predictions": predictions,
        "stage_totals": stage_totals,
        "routing_reason_totals": dict(sorted(routing_reason_totals.items())),
        "recall_gold_coverage": (
            {
                "gold_positive_sentences": gold_positive_sentences,
                "meetings_with_recall_artifact": recall_artifact_meetings,
                **{
                    surface: {
                        "hits": hits,
                        "recall": (
                            round(hits / gold_positive_sentences, 4)
                            if gold_positive_sentences
                            else None
                        ),
                    }
                    for surface, hits in recall_coverage_hits.items()
                },
            }
            if recall_artifact_meetings
            else None
        ),
        "usage_totals": {
            **{
                key: usage_totals[key] if usage_seen[key] else None
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            },
            "calls_with_usage": usage_totals["calls_with_usage"],
        },
        "checkpoint_dir": str(checkpoint_root.resolve()) if checkpoint_root else None,
        "extractor_signature": extractor_signature,
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
    from .recall import (
        EVIDENCE_POLICY_VERSION,
        MODEL_CANDIDATE_POLICY_VERSION,
        RECALL_PROMPT_VERSION,
        RULE_POLICY_VERSION,
        WINDOW_POLICY_VERSION,
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
        "recall_prompt_version": RECALL_PROMPT_VERSION,
        "window_policy_version": WINDOW_POLICY_VERSION,
        "rule_policy_version": RULE_POLICY_VERSION,
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "model_candidate_policy_version": MODEL_CANDIDATE_POLICY_VERSION,
        "commit": git("rev-parse", "HEAD"),
        # A dirty tree means the committed prompt is not what actually ran, so
        # the reader cannot reconstruct this number.
        "working_tree_clean": (dirty == "") if dirty is not None else None,
        "run_at": datetime.now(UTC).isoformat(),
    }


def compare_extractors(
    meetings: list[LabelledMeeting],
    extractors: dict[str, Callable[[LabelledMeeting], list[dict[str, Any]]]],
    *,
    checkpoint_dir: str | Path | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Run every strategy over the same meetings so the deltas are meaningful."""

    results = {
        name: evaluate_extractor(
            meetings,
            extractor,
            name=name,
            checkpoint_dir=(Path(checkpoint_dir) / name if checkpoint_dir else None),
            resume=resume,
        )
        for name, extractor in extractors.items()
    }
    corpus = {
        "meetings": len(meetings),
        "meeting_ids": [meeting.meeting_id for meeting in meetings],
        "dataset_fingerprint": stable_hash(
            [
                {
                    "meeting_id": meeting.meeting_id,
                    "sentences": meeting.sentences,
                    "positive_sentence_indices": sorted(
                        meeting.positive_sentence_indices
                    ),
                    "expected_items": meeting.expected_items,
                }
                for meeting in meetings
            ]
        ),
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
        "schema_version": "extraction-evaluation.v4",
        "frozen_state": frozen_state(),
        "corpus": corpus,
        "published_reference": PUBLISHED_SENTENCE_F1_BASELINE,
        "interpretation_ceiling": (
            "AMC-A 标注者间 Kappa 仅 0.47（ICSI 为 0.36），行动项判定本身高度主观。"
            "句级 F1 在 70 上下即已接近人类一致性带；出现 90+ 通常说明测量口径有问题，"
            "而不是模型很好。"
        ),
        "metric_definitions": {
            "sentence_level_positive_f1": (
                "Deduplicate predictions by cited transcript sentence before "
                "computing TP/FP/FN. Raw candidate count is workload, never FP."
            ),
            "raw_candidates": (
                "Pre-routing candidate workload; multiple candidates may cite "
                "the same sentence and must not be substituted for sentence FP."
            ),
            "recall_gold_coverage": (
                "Recall-only diagnostics over raw anchors, explicit anchor+support "
                "evidence, and evidence retained after draft/hint routing. Support "
                "is not a predicted positive sentence, so these surfaces have no "
                "precision or F1."
            ),
            "candidate_reference_workload": (
                "Anchor and support unit-reference counts are reported beside "
                "candidate counts. This makes broad support enumeration visible "
                "instead of rewarding it as free recall."
            ),
            "amc_zero_label_meetings": (
                "Meetings with no AMC-A positive sentence labels. This is not "
                "proof that the meeting has no action item under the broader "
                "product definition."
            ),
        },
        "results": results,
    }
