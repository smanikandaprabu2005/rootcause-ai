import { useCallback, useEffect, useState } from "react";
import { getInvestigation, getStatus, investigate, listInvestigations, loadSample } from "./api";
import type { Report, SavedInvestigation, StatusInfo } from "./types";
import DiagnosisCard from "./DiagnosisCard";
import DependencyGraph from "./DependencyGraph";
import Timeline from "./Timeline";
import Hypotheses from "./Hypotheses";
import Remediation from "./Remediation";
import EvidenceDrawer from "./EvidenceDrawer";
import { windowSeconds } from "./util";

type Backend = { s: "checking" } | { s: "online"; info: StatusInfo } | { s: "offline" };

export default function App() {
  const [backend, setBackend] = useState<Backend>({ s: "checking" });
  const [report, setReport] = useState<Report | null>(null);
  const [demo, setDemo] = useState(false);
  const [busy, setBusy] = useState<null | "investigate" | "sample" | "saved">(null);
  const [error, setError] = useState<string | null>(null);
  const [ref, setRef] = useState<string | null>(null);
  const [saved, setSaved] = useState<SavedInvestigation[]>([]);

  const showSample = useCallback(async () => {
    setBusy("sample");
    setError(null);
    try {
      setReport(await loadSample());
      setDemo(true);
    } catch (e) {
      setError(`Could not load the sample report: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  }, []);

  const refreshStatus = useCallback(async () => {
    try {
      const info = await getStatus();
      setBackend({ s: "online", info });
      listInvestigations().then(setSaved).catch(() => setSaved([]));
      return true;
    } catch {
      setBackend({ s: "offline" });
      setSaved([]);
      return false;
    }
  }, []);

  useEffect(() => {
    // Demo mode: with no backend, open straight onto the sample report.
    refreshStatus().then((ok) => {
      if (!ok) void showSample();
    });
  }, [refreshStatus, showSample]);

  const run = async () => {
    setBusy("investigate");
    setError(null);
    try {
      setReport(await investigate());
      setDemo(false);
      setRef(null);
      void refreshStatus();
    } catch (e) {
      setError(`Investigation failed: ${e instanceof Error ? e.message : String(e)}${backend.s === "offline" ? ". The backend appears to be offline; the sample report still works." : ""}`);
    } finally {
      setBusy(null);
    }
  };

  const openSaved = async (id: string) => {
    if (!id) return;
    setBusy("saved");
    setError(null);
    try {
      setReport(await getInvestigation(id));
      setDemo(false);
      setRef(null);
    } catch (e) {
      setError(`Could not load investigation ${id}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const closeDrawer = useCallback(() => setRef(null), []);
  const win = windowSeconds(report?.window);

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true">
            <rect width="32" height="32" rx="7" className="logo-bg" />
            <circle cx="9" cy="16" r="3.5" className="logo-a" />
            <circle cx="23" cy="9" r="3" className="logo-b" />
            <circle cx="23" cy="23" r="3" className="logo-b" />
            <path d="M12 15l8-5M12 17l8 5" className="logo-l" />
          </svg>
          <div>
            <h1>RootCause AI</h1>
            <p className="tagline">Evidence-backed incident reports</p>
          </div>
        </div>

        <div className="status" role="status" aria-live="polite">
          {backend.s === "checking" && <span className="dot dot-wait" />}
          {backend.s === "online" && (
            <>
              <span className="dot dot-ok" aria-hidden="true" />
              <span><strong>Backend online</strong></span>
              <span className="stat-items">
                <span>{backend.info.services?.length ?? 0} services</span>
                <span>{(backend.info.counts?.spans ?? 0).toLocaleString()} spans</span>
                <span>{(backend.info.counts?.logs ?? 0).toLocaleString()} logs</span>
                <span>{(backend.info.counts?.metrics ?? 0).toLocaleString()} metrics</span>
              </span>
            </>
          )}
          {backend.s === "offline" && (
            <>
              <span className="dot dot-off" aria-hidden="true" />
              <span><strong>Backend offline</strong> <span className="muted">demo mode available</span></span>
              <button className="link-btn" onClick={() => void refreshStatus()}>Retry</button>
            </>
          )}
          {backend.s === "checking" && <span className="muted">Checking backend...</span>}
        </div>

        <div className="actions">
          {saved.length > 0 && (
            <label className="saved">
              <span className="sr-only">Open a saved investigation</span>
              <select value="" onChange={(e) => void openSaved(e.target.value)} disabled={busy !== null}>
                <option value="">Saved ({saved.length})</option>
                {saved.map((s) => <option key={s.id} value={s.id}>{s.id}</option>)}
              </select>
            </label>
          )}
          <button className="btn btn-primary" onClick={() => void run()} disabled={busy !== null}>
            {busy === "investigate" ? "Investigating..." : "Investigate latest incident"}
          </button>
          <button className="btn" onClick={() => void showSample()} disabled={busy !== null}>
            {busy === "sample" ? "Loading..." : "Load sample report"}
          </button>
        </div>
      </header>

      <main>
        {error && <div className="banner banner-err" role="alert">{error}</div>}
        {busy && !report && <p className="loading" role="status"><span className="spinner" aria-hidden="true" />Working...</p>}
        {busy === "investigate" && report && <div className="banner" role="status"><span className="spinner" aria-hidden="true" />Investigating the latest 90 seconds of telemetry. The current report stays visible until the new one is ready.</div>}

        {!report && !busy && (
          <div className="empty card">
            <h2>No report loaded</h2>
            <p className="muted">Investigate the latest incident from the backend, or load the bundled sample report.</p>
          </div>
        )}

        {report && (
          <>
            {demo && (
              <p className="demo-line"><span className="badge badge-demo">Demo data</span> Showing the bundled sample report, not live telemetry.</p>
            )}
            <DiagnosisCard report={report} />
            <div className="two-col">
              <DependencyGraph report={report} />
              {report.remediation && <Remediation r={report.remediation} />}
            </div>
            <Hypotheses hypotheses={report.hypotheses} onOpen={setRef} />
            <Timeline entries={report.timeline} onOpen={setRef} />
          </>
        )}
      </main>

      {report && (
        <footer className="foot">
          <span>Investigation <code>{report.id}</code></span>
          {report.investigation_seconds != null && <span>Took {report.investigation_seconds.toFixed(2)} s</span>}
          {win != null && <span>Window {win.toFixed(0)} s</span>}
          {report.window?.baseline_start_ns != null && report.window && (
            <span>Baseline {((report.window.start_ns - report.window.baseline_start_ns) / 1e9).toFixed(0)} s before window</span>
          )}
        </footer>
      )}

      <EvidenceDrawer evidenceRef={ref} demo={demo} onClose={closeDrawer} />
    </div>
  );
}
