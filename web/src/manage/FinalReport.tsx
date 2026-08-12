import type { FinalDeliverable, Task } from "../manage-types";
import { Chip } from "./TaskCard";

/** The organized final, as something a person reads.
 *
 *  The whole point of this deliverable is that every sentence in it can be
 *  traced to an accepted version of a task -- so a section that names its
 *  source is not decoration, it is the claim the system is making about
 *  itself. Rendering only a task checklist threw that away and left the report
 *  looking like a list of names.
 *
 *  How it was produced is stated too. "A model wrote this", "a template
 *  assembled this" and "the model was unavailable so a template stood in" are
 *  three different claims, and a reader deciding whether to trust a summary is
 *  entitled to know which one is on screen.
 */
export function FinalReport({
  final,
  tasks,
}: {
  final: FinalDeliverable;
  tasks: Task[];
}) {
  const report = final.payload?.organized_report;
  if (!report) return null;
  const processing = final.payload?.processing;
  const titleOf = (versionId?: string) =>
    tasks.find(
      (task) =>
        task.current_version?.version_id === versionId ||
        task.latest_version?.version_id === versionId,
    )?.title;

  return (
    <article className="mt-3 overflow-hidden rounded-md border border-rule-2">
      <header className="flex flex-wrap items-baseline gap-2 border-b border-rule-2 bg-sunk px-3.5 py-2.5">
        <h3 className="text-[0.92rem] font-semibold">
          {report.title || "会议成果汇总"}
        </h3>
        <Chip tone={MODE_TONE[processing?.mode ?? ""] ?? "plain"}>
          {MODE_LABEL[processing?.mode ?? ""] ?? processing?.mode ?? "未知来源"}
        </Chip>
        {processing?.model ? (
          <span className="font-mono text-[0.68rem] text-ink-3">
            {processing.model}
          </span>
        ) : null}
      </header>

      <div className="px-3.5 py-3">
        {report.executive_summary ? (
          <p className="text-[0.85rem]">{report.executive_summary}</p>
        ) : null}

        {report.key_findings?.length ? (
          <section className="mt-3">
            <h4 className="mb-1 text-[0.82rem] font-semibold">关键结论</h4>
            <ul className="grid gap-1">
              {report.key_findings.map((finding, index) => (
                <li key={index} className="text-[0.81rem]">
                  <span className="text-ink-2">· {finding.text}</span>
                  {finding.source_version_ids?.map((versionId) => (
                    <span
                      key={versionId}
                      title={versionId}
                      className="ml-1.5 font-mono text-[0.68rem] text-ink-3"
                    >
                      [{titleOf(versionId) || "来源版本"}]
                    </span>
                  ))}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {report.sections?.length ? (
          <section className="mt-3 grid gap-2 border-t border-rule-2 pt-3">
            {report.sections.map((section, index) => (
              <div key={index}>
                <h4 className="text-[0.82rem] font-semibold">
                  {section.heading}
                  <span className="ml-1.5 font-mono text-[0.68rem] font-normal text-ink-3">
                    [{titleOf(section.source_version_id) || "来源版本"}]
                  </span>
                </h4>
                {section.summary ? (
                  <p className="mt-0.5 text-[0.81rem] text-ink-2">
                    {section.summary}
                  </p>
                ) : null}
                {section.links?.length ? (
                  <ul className="mt-1 flex flex-wrap gap-2">
                    {section.links.map((link) => (
                      <li key={link}>
                        <a
                          href={link}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-block max-w-[22rem] truncate font-mono text-[0.7rem] text-accent underline"
                        >
                          🔗 {link}
                        </a>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ))}
          </section>
        ) : null}

        {report.risks_or_gaps?.length ? (
          <section className="mt-3 border-t border-rule-2 pt-3">
            <h4 className="mb-1 text-[0.82rem] font-semibold">风险与缺口</h4>
            <ul className="grid gap-1">
              {report.risks_or_gaps.map((risk, index) => (
                <li key={index} className="text-[0.81rem] text-warn">
                  · {risk.text}
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </div>
    </article>
  );
}

const MODE_LABEL: Record<string, string> = {
  bailian: "模型整理",
  deterministic_template: "本地模板整理",
  deterministic_fallback: "模型不可用，模板兜底",
};

const MODE_TONE: Record<string, "ok" | "plain" | "warn"> = {
  bailian: "ok",
  deterministic_template: "plain",
  deterministic_fallback: "warn",
};
