import type { SourcedPoint, Version } from "../manage-types";
import { Button, Chip } from "./TaskCard";

/** What the processor made of a submission, shown to whoever has to judge it.
 *
 *  This runs by itself between submission and acceptance, and until now none
 *  of it reached the screen: a coordinator read the raw submission and decided
 *  alone while a digest with citations and a recommendation sat unread in the
 *  database. On this meeting's own data that mattered -- one task was accepted
 *  while the processor had judged its evidence INSUFFICIENT and advised
 *  sending it back.
 *
 *  It advises and never decides. The verdict below is a reading of the
 *  evidence, the buttons above it belong to a person, and nothing here moves
 *  a task. Every claim carries the source it came from, so disagreeing with it
 *  means opening the attachment rather than arguing with a black box.
 */
export function ProcessingReview({
  version,
  attachments,
  onRetry,
}: {
  version: Version;
  attachments: { name?: string }[];
  onRetry?: () => void;
}) {
  const status = version.processing_status;
  if (!status || status === "NOT_REQUIRED") return null;

  if (status !== "READY") {
    return (
      <div className="mt-3 rounded border border-rule-2 bg-ground px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <Chip tone={status === "FAILED" ? "bad" : "plain"}>
            {status === "FAILED" ? "整理失败" : "正在整理"}
          </Chip>
          <span className="text-[0.79rem] text-ink-2">
            {status === "FAILED"
              ? version.processing_error_detail || "没有留下原因"
              : "系统正在读取正文与附件"}
          </span>
        </div>
        {status === "FAILED" && onRetry ? (
          <div className="mt-2">
            <Button tone="ghost" onClick={onRetry}>
              重新整理
            </Button>
          </div>
        ) : null}
      </div>
    );
  }

  const review = version.processing_result;
  if (!review) return null;
  const alignment = review.task_alignment;
  const advice = review.acceptance_advice;
  const gaps = review.gaps ?? [];
  const digest = review.evidence_digest;
  const result = review.normalized_result;

  return (
    <div className="mt-3 rounded border border-rule-2 bg-ground px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone={ALIGNMENT_TONE[alignment?.status ?? ""] ?? "plain"}>
          {ALIGNMENT_LABEL[alignment?.status ?? ""] ?? alignment?.status}
        </Chip>
        {advice ? (
          <Chip tone={advice.decision === "ACCEPT" ? "ok" : "warn"}>
            建议{advice.decision === "ACCEPT" ? "验收" : "退回"}
          </Chip>
        ) : null}
        <span className="ml-auto font-mono text-[0.68rem] text-ink-3">
          系统整理，供参考
        </span>
      </div>

      {alignment?.reason ? (
        <p className="mt-1.5 text-[0.8rem] text-ink-2">{alignment.reason}</p>
      ) : null}

      {result ? (
        <section className="mt-2.5">
          <h4 className="text-[0.82rem] font-semibold">{result.title}</h4>
          {result.executive_summary ? (
            <p className="mt-0.5 text-[0.8rem] text-ink-2">
              {result.executive_summary}
            </p>
          ) : null}
          <Points points={result.key_points} attachments={attachments} />
        </section>
      ) : digest ? (
        <section className="mt-2.5">
          {/* No deliverable was written, so what the evidence *is* stands in
              its place -- stating a result the evidence does not support is
              the failure this whole step exists to avoid. */}
          <h4 className="text-[0.82rem] font-semibold">
            {digest.title || "证据摘要"}
          </h4>
          {digest.summary ? (
            <p className="mt-0.5 text-[0.8rem] text-ink-2">{digest.summary}</p>
          ) : null}
          <Points points={digest.key_points} attachments={attachments} />
        </section>
      ) : null}

      {gaps.length ? (
        <section className="mt-2.5 border-t border-rule-2 pt-2">
          <h4 className="mb-1 text-[0.8rem] font-semibold">
            跟任务要求的差距
          </h4>
          <ul className="grid gap-1">
            {gaps.map((gap, index) => (
              <li key={index} className="flex items-start gap-2 text-[0.79rem]">
                <Chip tone={gap.severity === "BLOCKING" ? "bad" : "warn"}>
                  {SEVERITY[gap.severity] ?? gap.severity}
                </Chip>
                <span className="flex-1 text-ink-2">{gap.issue}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {review.source_coverage?.unreadable_or_uninspected?.length ? (
        <p className="mt-2 text-[0.75rem] text-ink-3">
          没读到：
          {review.source_coverage.unreadable_or_uninspected
            .map((ref) => label(ref, attachments))
            .join("、")}
        </p>
      ) : null}
    </div>
  );
}

function Points({
  points,
  attachments,
}: {
  points?: SourcedPoint[];
  attachments: { name?: string }[];
}) {
  if (!points?.length) return null;
  return (
    <ul className="mt-1.5 grid gap-1">
      {points.map((point, index) => (
        <li key={index} className="text-[0.8rem]">
          <span className="text-ink-2">· {point.text}</span>
          {point.source_refs?.length ? (
            <span className="ml-1.5 font-mono text-[0.68rem] text-ink-3">
              {point.source_refs.map((ref) => label(ref, attachments)).join(" ")}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** `attachment:0` is an index into what was submitted, so it is shown as the
 *  file's own name -- a reader checking a claim needs to know which file to
 *  open, not which array slot it occupied. */
function label(ref: string, attachments: { name?: string }[]): string {
  const attachment = /^attachment:(\d+)$/.exec(ref);
  if (attachment) {
    const file = attachments[Number(attachment[1])];
    return file?.name ? `[${file.name}]` : `[附件 ${attachment[1]}]`;
  }
  const link = /^link:(\d+)$/.exec(ref);
  if (link) return `[链接 ${link[1]}]`;
  return `[${REF_LABEL[ref] ?? ref}]`;
}

const REF_LABEL: Record<string, string> = {
  "task:contract": "任务定义",
  "submission:claim": "提交自述",
  "decision:votes": "投票结果",
};

const ALIGNMENT_LABEL: Record<string, string> = {
  ALIGNED: "对得上任务",
  PARTIAL: "只覆盖一部分",
  MISALIGNED: "跟任务对不上",
  INSUFFICIENT: "证据不足以判断",
};

const ALIGNMENT_TONE: Record<string, "ok" | "warn" | "bad" | "plain"> = {
  ALIGNED: "ok",
  PARTIAL: "warn",
  MISALIGNED: "bad",
  INSUFFICIENT: "warn",
};

const SEVERITY: Record<string, string> = {
  BLOCKING: "阻塞",
  MAJOR: "重要",
  MINOR: "次要",
};
