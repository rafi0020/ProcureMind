"use client";

import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading } from "@/components/ui";
import { useApi } from "@/lib/api";
import { day, pct, usd } from "@/lib/format";

interface AgentRun {
  name: string; model: string; split: string; at: string; n: number; note?: string; regraded_at?: string;
  metrics: { pass_rate: number; outcome_accuracy: number; cite_rate: number | null; completed_rate: number; unsupported_numbers_per_report: number; mean_steps: number; mean_cost_usd: number; p50_latency_s: number; p95_latency_s: number };
  by_kind: Record<string, { n: number; passed: number }>;
}
interface Quality { documents: number; field_accuracy: number; missing: number; misread: number; lines_complete: number; classified_invoice: number }
interface ExtractionRun { name: string; mode: string; model: string | null; at: string; documents: number; field_accuracy: number; invented: number; by_quality: Record<string, Quality>; by_field: Record<string, Record<string, number>> }
interface MatchingRun { name: string; scale: number; scans: boolean; at: string; documents: number; status_accuracy: number; findings_precision: number | null; findings_recall: number | null; per_code: Record<string, { tp: number; fp: number; fn: number; precision: number | null; recall: number | null }> }
interface AttackRun { model: string; at: string; note?: string; cases: { id: string; name: string; passed: boolean; result?: "held" | "failed" | "inconclusive"; detail: string }[] }
interface Unavailable { name: string; model: string; at: string; n: number; note: string }
const CHIP = { held: "chip--ok", failed: "chip--bad", inconclusive: "" } as const;
const QUALITY: Record<string, string> = { digital: "Digital PDF", clean: "Clean scan", medium: "Worn scan", photo: "Phone photo" };
const words = (s: string) => s.replace(/_/g, " ");
const maybe = (v: number | null | undefined) => (v == null ? "" : pct(v));


/** The headline figures, taken from the recorded results. Nothing here is estimated: each is a number the runs stored. */
function figureList(data: { results: AgentRun[]; attacks: AttackRun[]; extraction: ExtractionRun[]; matching: MatchingRun[] }) {
  const out: { label: string; value: string; note: string }[] = [];
  const reading = [...data.extraction].sort((a, b) => Number(a.mode === "rules") - Number(b.mode === "rules"))[0];
  if (reading) out.push({ label: "Fields read right", value: pct(reading.field_accuracy), note: `${reading.documents} held-out invoices, ${reading.mode === "rules" ? "rules alone" : "rules and a model"}` });
  const checks = data.matching[0];
  if (checks) out.push({ label: "Ended in the right state", value: pct(checks.status_accuracy), note: `${checks.documents} documents through the whole pipeline` });
  const agent = [...data.results].sort((a, b) => Number(b.split === "test") - Number(a.split === "test"))[0];
  if (agent) out.push({ label: "The agent's right outcome", value: pct(agent.metrics.outcome_accuracy), note: `${agent.n} cases, ${agent.split === "test" ? "held out" : "development"}` });
  const attacks = data.attacks[0];
  if (attacks) {
    const held = attacks.cases.filter((c) => (c.result ?? (c.passed ? "held" : "failed")) === "held").length;
    out.push({ label: "Attacks held", value: `${held} of ${attacks.cases.length}`, note: attacks.model });
  }
  return out;
}

export default function EvaluationPage() {
  const { data, error, isLoading, mutate } = useApi<{ results: AgentRun[]; attacks: AttackRun[]; unavailable?: Unavailable[]; extraction: ExtractionRun[]; matching: MatchingRun[] }>("/api/eval/results");
  const empty = data && !data.results.length && !data.attacks.length && !data.extraction.length && !data.matching.length;
  return (
    <Page title="Evaluation" lede="How well documents are read, invoices matched and exceptions handled, measured on data the system was not tuned on. Results are shown as recorded, failures included.">
      {error && <ErrorState error={error} retry={() => mutate()} />}
      {isLoading && <div className="panel"><Loading rows={6} /></div>}
      {empty && <div className="panel"><Empty title="No evaluation recorded on this installation">See docs/EVALUATION.md for the commands (make eval-extraction, eval-matching, eval-agent, eval-attacks).</Empty></div>}

      {!!data && !empty && (
        <div className="figures" aria-label="Headline figures">
          {figureList(data).map((f) => (
            <div key={f.label} className="figure">
              <span className="figure__value">{f.value}</span>
              <span className="figure__label">{f.label}</span>
              <span className="figure__note">{f.note}</span>
            </div>
          ))}
        </div>
      )}

      {[...(data?.extraction ?? [])].sort((a, b) => a.mode.localeCompare(b.mode)).map((x) => (
        <section key={x.name} className="grid gap-2">
          <h2>Reading documents: {x.mode === "rules" ? "rules alone" : <>rules and <span className="measured">{x.model}</span></>}</h2>
          <div className="panel table-wrap">
            <table className="table">
              <thead><tr><th>Quality</th><th className="num">Documents</th><th className="num">Fields right</th><th className="num">Left for a person</th><th className="num">Misread</th><th className="num">All lines read</th></tr></thead>
              <tbody>{Object.entries(x.by_quality).map(([q, v]) => (
                <tr key={q}><td>{QUALITY[q] ?? q}</td><td className="num">{v.documents}</td><td className="num">{pct(v.field_accuracy)}</td><td className="num">{pct(v.missing)}</td><td className="num">{pct(v.misread)}</td><td className="num">{pct(v.lines_complete)}</td></tr>
              ))}</tbody>
            </table>
          </div>
          <p className="label">{x.documents} held-out invoices, each read four ways; {pct(x.field_accuracy)} of fields right overall. Values stored that are not on the page: <strong>{x.invented}</strong>. Run on {day(x.at)}.</p>
        </section>
      ))}

      {data?.matching.map((m) => (
        <section key={m.name} className="grid gap-2">
          <h2>Matching and checks</h2>
          <div className="panel table-wrap">
            <table className="table">
              <thead><tr><th>Check</th><th className="num">Found</th><th className="num">Raised wrongly</th><th className="num">Missed</th><th className="num">Precision</th><th className="num">Recall</th></tr></thead>
              <tbody>{Object.entries(m.per_code).map(([code, v]) => (
                <tr key={code}><td>{words(code)}</td><td className="num">{v.tp}</td><td className="num">{v.fp}</td><td className="num">{v.fn}</td><td className="num">{maybe(v.precision)}</td><td className="num">{maybe(v.recall)}</td></tr>
              ))}</tbody>
            </table>
          </div>
          <p className="label">{m.documents} documents through the real pipeline{m.scans ? ", scans included" : ""}: {pct(m.status_accuracy)} ended in the state the answer key gives. Run on {day(m.at)}.</p>
        </section>
      ))}

      {!!data?.results.length && (
        <section className="grid gap-2">
          <h2>The Exception Agent</h2>
          <div className="panel table-wrap">
            <table className="table">
              <thead><tr><th>Model</th><th>Set</th><th className="num">Cases</th><th className="num">Passed</th><th className="num">Right outcome</th><th className="num">Stated the figure</th><th className="num">Unsupported figures</th><th className="num">Steps</th><th className="num">Cost per case</th><th className="num">p50 / p95</th><th>Run on</th></tr></thead>
              <tbody>{data.results.map((r) => (
                <tr key={r.name}>
                  <td className="measured">{r.model}</td><td>{r.split === "test" ? "held out" : "development"}</td><td className="num">{r.n}</td><td className="num">{pct(r.metrics.pass_rate)}</td>
                  <td className="num">{pct(r.metrics.outcome_accuracy)}</td><td className="num">{maybe(r.metrics.cite_rate)}</td><td className="num">{r.metrics.unsupported_numbers_per_report.toFixed(2)}</td>
                  <td className="num">{r.metrics.mean_steps.toFixed(1)}</td><td className="num">{usd(r.metrics.mean_cost_usd)}</td><td className="num">{r.metrics.p50_latency_s.toFixed(0)} s / {r.metrics.p95_latency_s.toFixed(0)} s</td><td className="muted">{day(r.at)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <p className="label">Right outcome: the recommendation stored on the exception is one an AP lead would accept for that kind of case. Stated the figure: the report gives the deciding number (the variance, the quantity received, the duplicate&apos;s total, the limit).</p>
          {data.results.filter((r) => r.note || r.regraded_at).map((r) => <p key={r.name} className="ink-2"><span className="measured">{r.model}</span>: {r.note ?? `graded again on ${day(r.regraded_at!)}; the answers are the ones first given.`}</p>)}
          {data.unavailable?.map((u) => <p key={u.name} className="ink-2"><span className="measured">{u.model}</span> was not measured: {u.note}</p>)}
          <div className="grid gap-4 lg:grid-cols-2">{data.results.map((r) => (
            <div key={r.name} className="panel table-wrap">
              <div className="panel__head"><h3 className="measured">{r.model}</h3><span className="label">{r.split === "test" ? "held out" : "development"}, by kind of exception</span></div>
              <table className="table"><tbody>{Object.entries(r.by_kind).map(([k, v]) => <tr key={k}><td>{words(k)}</td><td className="num">{v.passed} of {v.n}</td></tr>)}</tbody></table>
            </div>
          ))}</div>
        </section>
      )}

      {data?.attacks.map((a) => (
        <section key={a.model + a.at} className="grid gap-2">
          <h2>Attacks, <span className="measured">{a.model}</span></h2>
          <div className="panel table-wrap">
            <table className="table">
              <thead><tr><th>Attack</th><th>Result</th><th>What the database shows</th></tr></thead>
              <tbody>{a.cases.map((c) => {
                const result = c.result ?? (c.passed ? "held" : "failed");
                return <tr key={c.id}><td>{c.name}</td><td><span className={`chip ${CHIP[result]}`}>{result}</span></td><td className="ink-2">{c.detail}</td></tr>;
              })}</tbody>
            </table>
          </div>
          <p className="label">Run on {day(a.at)}.{a.note ? ` ${a.note}` : ""}</p>
        </section>
      ))}
    </Page>
  );
}
