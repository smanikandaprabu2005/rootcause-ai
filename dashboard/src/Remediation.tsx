import type { ReactNode } from "react";
import type { Remediation as R } from "./types";
import { IconAlert, IconCheck, IconDash, IconQuestion, IconX } from "./icons";
import { fmtNum, words } from "./util";

function Val({ v }: { v: unknown }): ReactNode {
  if (v == null) return <span className="muted">n/a</span>;
  if (typeof v === "string") return <>{v}</>;
  if (typeof v === "number") return <>{fmtNum(v)}</>;
  if (typeof v === "boolean") return <>{v ? "yes" : "no"}</>;
  if (Array.isArray(v)) {
    return (
      <ul className="plain-list">
        {v.map((x, i) => <li key={i}><Val v={x} /></li>)}
      </ul>
    );
  }
  return (
    <dl className="kv kv-tight">
      {Object.entries(v as Record<string, unknown>).map(([k, x]) => (
        <div key={k}><dt>{words(k)}</dt><dd><Val v={x} /></dd></div>
      ))}
    </dl>
  );
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

function MetricTable({ baseline, before, after }: { baseline: unknown; before: unknown; after: unknown }) {
  const cols = [["baseline", baseline], ["before fix", before], ["after fix", after]] as const;
  const present = cols.filter(([, v]) => v != null);
  if (present.length === 0) return null;
  if (!present.every(([, v]) => isObj(v))) {
    return <dl className="kv">{present.map(([k, v]) => <div key={k}><dt>{k}</dt><dd><Val v={v} /></dd></div>)}</dl>;
  }
  const keys = [...new Set(present.flatMap(([, v]) => Object.keys(v as object)))];
  return (
    <div className="table-wrap">
      <table className="metrics">
        <thead><tr><th scope="col">Metric</th>{present.map(([k]) => <th key={k} scope="col">{k}</th>)}</tr></thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k}>
              <th scope="row">{words(k)}</th>
              {present.map(([c, v]) => <td key={c}><Val v={(v as Record<string, unknown>)[k]} /></td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const META: Record<string, { label: string; icon: ReactNode; cls: string; note: string }> = {
  verified: { label: "Verified in sandbox", icon: <IconCheck />, cls: "ok", note: "The fix was applied in a disposable environment and the symptom went away." },
  failed: { label: "Verification failed", icon: <IconX />, cls: "bad", note: "The fix was tried in a sandbox and did not resolve the symptom." },
  unverified: { label: "Not verified", icon: <IconQuestion />, cls: "warn", note: "This is a suggestion only. It has not been tested." },
  no_action: { label: "No action proposed", icon: <IconDash />, cls: "quiet", note: "" },
};

export default function Remediation({ r }: { r: R }) {
  const meta = META[r.status] ?? { label: words(r.status), icon: <IconAlert />, cls: "quiet", note: "" };
  const p = r.proposal;
  const v = r.verification;
  const steps = v?.steps ?? [];
  const runbook = p?.runbook;
  const prod = r.production;

  return (
    <section className={`card remed remed-${meta.cls}`} aria-labelledby="rem-h">
      <div className="card-head">
        <h2 id="rem-h">Remediation</h2>
        <span className={`badge badge-${meta.cls}`}>{meta.icon}{meta.label}</span>
      </div>
      {meta.note && <p className="small muted">{meta.note}</p>}
      {r.reason && <p className={`remed-reason remed-reason-${meta.cls}`}>{r.reason}</p>}

      {p && (
        <div className="remed-block">
          <h3>Proposed action</h3>
          <p>
            {p.action && <code className="action">{p.action}</code>}{" "}
            {p.service && <>on <strong>{p.service}</strong></>}
            {p.fault_type && <span className="muted"> ({words(p.fault_type)})</span>}
          </p>
          {p.description && <p>{p.description}</p>}
          {runbook != null && (
            <div className="runbook"><h4>Runbook</h4><Val v={runbook} /></div>
          )}
        </div>
      )}

      {(r.sandbox != null || v) && (
        <div className="remed-block">
          <h3>Sandbox verification</h3>
          {r.sandbox != null && (
            <p className="small">Sandbox: {typeof r.sandbox === "string" ? <strong>{r.sandbox}</strong> : <Val v={r.sandbox} />}</p>
          )}
          {v && (
            <>
              {v.verified != null && <p className="small">Result: <strong>{v.verified ? "symptom cleared" : "symptom not cleared"}</strong>{v.reason ? ` (${v.reason})` : ""}</p>}
              {steps.length > 0 && <ol className="steps">{steps.map((s, i) => <li key={i}>{s}</li>)}</ol>}
              <MetricTable baseline={v.baseline} before={v.before} after={v.after} />
            </>
          )}
        </div>
      )}

      {prod != null && (
        <p className="prod-line"><strong>Production:</strong> {typeof prod === "string" ? prod : <Val v={prod} />}</p>
      )}
    </section>
  );
}
