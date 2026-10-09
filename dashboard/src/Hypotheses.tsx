import { useMemo, useState } from "react";
import type { Evidence, Hypothesis } from "./types";
import { kindIcon } from "./icons";
import { pct, words } from "./util";

const rank = (h: Hypothesis) => (h.status === "supported" ? 0 : h.status === "open" ? 1 : 2);

function EvList({ title, items, onOpen }: { title: string; items: Evidence[]; onOpen: (r: string) => void }) {
  return (
    <div className="ev-col">
      <h4>{title} <span className="count">{items.length}</span></h4>
      {items.length === 0 ? (
        <p className="muted small">None recorded.</p>
      ) : (
        <ul className="ev-list">
          {items.map((e, i) => (
            <li key={i}>
              <div className="ev-top">
                <span className="ev-kind">{kindIcon(e.kind)}{e.kind}</span>
                <span className="chip chip-sm">{e.service}</span>
                <span className="weight" title="Evidence weight">weight {e.weight}</span>
                {e.decisive && <span className="pill pill-decisive">decisive</span>}
              </div>
              <p>{e.summary}</p>
              {e.ref && <button className="ref-btn" onClick={() => onOpen(e.ref!)}>{e.ref}</button>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Item({ h, onOpen }: { h: Hypothesis; onOpen: (r: string) => void }) {
  const [open, setOpen] = useState(false);
  const sup = h.evidence.filter((e) => e.supports);
  const con = h.evidence.filter((e) => !e.supports);
  const id = `hyp-${h.service}-${h.fault_type}`;
  return (
    <li className={`hyp hyp-${h.status}`}>
      <button className="hyp-head" aria-expanded={open} aria-controls={id} onClick={() => setOpen((o) => !o)}>
        <span className="caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
        <span className="hyp-title"><strong>{h.service}</strong> <span className="muted">{words(h.fault_type)}</span></span>
        <span className={`pill pill-h-${h.status}`}>{h.status}</span>
        <span className="hyp-stats">confidence {pct(h.confidence)} · score {h.score}</span>
      </button>
      {h.status === "rejected" && h.rejected_because && (
        <p className="rejected-why"><strong>Rejected because:</strong> {h.rejected_because}</p>
      )}
      {open && (
        <div id={id} className="ev-grid">
          <EvList title="Supports" items={sup} onOpen={onOpen} />
          <EvList title="Contradicts" items={con} onOpen={onOpen} />
        </div>
      )}
    </li>
  );
}

export default function Hypotheses({ hypotheses, onOpen }: { hypotheses: Hypothesis[]; onOpen: (r: string) => void }) {
  const [showRejected, setShowRejected] = useState(false);
  const sorted = useMemo(() => [...hypotheses].sort((a, b) => rank(a) - rank(b) || b.score - a.score || b.confidence - a.confidence), [hypotheses]);
  const live = sorted.filter((h) => h.status !== "rejected");
  const rejected = sorted.filter((h) => h.status === "rejected");

  return (
    <section className="card" aria-labelledby="hyp-h">
      <div className="card-head">
        <h2 id="hyp-h">Competing hypotheses</h2>
        <p className="sub">Every candidate cause was tested against the same evidence. Expand one to see what supports and what contradicts it.</p>
      </div>
      {hypotheses.length === 0 && <p className="muted">No hypotheses in this report.</p>}
      {live.length > 0 && <ul className="hyp-list">{live.map((h) => <Item key={`${h.service}/${h.fault_type}`} h={h} onOpen={onOpen} />)}</ul>}
      {rejected.length > 0 && (
        <div className="rejected-block">
          <button className="btn btn-ghost" aria-expanded={showRejected} onClick={() => setShowRejected((s) => !s)}>
            {showRejected ? "Hide" : "Show"} {rejected.length} rejected {rejected.length === 1 ? "hypothesis" : "hypotheses"}
          </button>
          {showRejected && <ul className="hyp-list">{rejected.map((h) => <Item key={`${h.service}/${h.fault_type}`} h={h} onOpen={onOpen} />)}</ul>}
        </div>
      )}
    </section>
  );
}
