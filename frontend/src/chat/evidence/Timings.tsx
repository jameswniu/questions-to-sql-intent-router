import { words } from "@/chat/evidence/model";
import { fmtMs } from "@/lib/format";
import type { StageEvent } from "@/lib/types";

/** Each step of the pipeline on one timeline, so where the time went shows at a glance. */
export function Timings({ stages }: { stages: StageEvent[] }) {
  const lengths = stages.map((stage) => Math.max(stage.ms || 0, 0));
  const total = lengths.reduce((sum, ms) => sum + ms, 0);
  // Each step starts where the one before it ended.
  const starts = lengths.map((_, index) => lengths.slice(0, index).reduce((sum, ms) => sum + ms, 0));
  return (
    <>
      <ol className="waterfall">
        {stages.map((stage, index) => {
          const ms = lengths[index] ?? 0;
          const left = total ? ((starts[index] ?? 0) / total) * 100 : (index / stages.length) * 100;
          const width = total ? Math.max((ms / total) * 100, 0.75) : 100 / stages.length;
          return (
            <li key={index}>
              <span className="name">{words(stage.name)}</span>
              <span className="track" aria-hidden="true">
                <span
                  className={index % 2 ? "tone-accent-light" : "tone-accent"}
                  style={{ left: `${left}%`, width: `${width}%` }}
                />
              </span>
              <span className="ms">{fmtMs(stage.ms)}</span>
            </li>
          );
        })}
      </ol>
      <p className="legend">
        {`${stages.map((stage) => `${words(stage.name)} ${fmtMs(stage.ms)}`).join(" · ")} · total ${fmtMs(total)}`}
      </p>
    </>
  );
}
