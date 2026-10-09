import type { Report } from "./types";
import { IconAlert, IconCheck, IconDash, IconQuestion } from "./icons";
import { pct, words } from "./util";

const STATUS = {
  supported: { label: "Supported", icon: <IconCheck />, cls: "ok" },
  inconclusive: { label: "Inconclusive", icon: <IconQuestion />, cls: "warn" },
  no_incident: { label: "No incident", icon: <IconDash />, cls: "quiet" },
} as const;

export default function DiagnosisCard({ report }: { report: Report }) {
  const d = report.diagnosis;
  const st = STATUS[d.status] ?? { label: words(d.status), icon: <IconAlert />, cls: "quiet" };
  const conf = Math.max(0, Math.min(1, d.confidence ?? 0));
  const llm = report.narrative_source === "llm";
  const headline =
    d.status === "no_incident"
      ? "No incident found in this window"
      : d.service
        ? `${d.service}: ${words(d.fault_type) || "unknown fault"}`
        : "Cause not determined";
  const plain =
    d.status === "supported" && d.service
      ? `Root cause is ${d.service}, ${words(d.fault_type) ? `a ${words(d.fault_type)} fault` : "fault type unknown"}.`
      : d.status === "inconclusive"
        ? "The evidence does not single out one cause."
        : "";

  return (
    <section className={`card diag diag-${st.cls}`} aria-labelledby="diag-h">
      <div className="diag-top">
        <span className={`badge badge-${st.cls}`}>{st.icon}{st.label}</span>
        <div className="meter-wrap">
          <span className="meter-label" id="conf-l">Confidence</span>
          <div className="meter" role="meter" aria-labelledby="conf-l" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(conf * 100)} aria-valuetext={pct(conf)}>
            <span style={{ width: `${conf * 100}%` }} />
          </div>
          <strong className="meter-num">{pct(conf)}</strong>
        </div>
      </div>

      <h2 id="diag-h" className="headline">{headline}</h2>
      {plain && <p className="plain">{plain}</p>}
      {d.propagation && d.propagation.length > 1 && (
        <p className="path-line"><span className="muted">Failure travelled:</span> {d.propagation.map((s, i) => (<span key={s}>{i > 0 && <span className="arrow" aria-label="then"> → </span>}<strong>{s}</strong></span>))}</p>
      )}

      <p className="summary">{d.summary}</p>

      {report.narrative && (
        <figure className="narrative">
          <figcaption className={`source ${llm ? "source-llm" : "source-ev"}`}>
            {llm ? "LLM-written, checked against evidence" : "Written from evidence"}
          </figcaption>
          <blockquote>{report.narrative}</blockquote>
        </figure>
      )}

      <div className="diag-cols">
        {d.runner_up && (
          <div>
            <h3>Runner-up</h3>
            <p><strong>{d.runner_up.service}</strong> <span className="muted">{words(d.runner_up.fault_type)}</span>{d.runner_up.score != null && <span className="muted"> (score {d.runner_up.score})</span>}</p>
          </div>
        )}
        {d.ruled_out && d.ruled_out.length > 0 && (
          <details className="ruled" open={d.ruled_out.length <= 3}>
            <summary><h3>Ruled out <span className="count">{d.ruled_out.length}</span></h3></summary>
            <ul>{d.ruled_out.map((r, i) => <li key={i}>{r}</li>)}</ul>
          </details>
        )}
      </div>
    </section>
  );
}
