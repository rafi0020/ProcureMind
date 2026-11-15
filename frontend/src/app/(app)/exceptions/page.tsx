"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { mutate as revalidate } from "swr";
import { EvidenceDrawer } from "@/components/Evidence";
import { PageViewer } from "@/components/PageViewer";
import { ReportView } from "@/components/ReportView";
import { RunTrail } from "@/components/RunTrail";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { amount, day, STATUS_LABEL, when } from "@/lib/format";
import { followRun } from "@/lib/sse";
import type { Exception, Invoice, Provenance, Run } from "@/lib/types";

const TABS: [string, string][] = [["open", "Open"], ["recommended", "Recommended"], ["escalated", "Escalated"], ["resolved", "Resolved"]];
const OUTCOMES: Record<string, string> = {
  approve_with_variance: "Approve with the variance", hold: "Hold", request_credit_note: "Ask the supplier for a credit note",
  request_corrected_invoice: "Ask for a corrected invoice", reject_duplicate: "Reject as a duplicate", escalate: "Escalate to an administrator",
};
const EXC_STATUS: Record<string, string> = { open: "open", investigating: "agent working", recommended: "recommendation ready", escalated: "escalated", resolved: "resolved" };
const EXC_CHIP = (s: string) => (s === "resolved" ? "done" : s === "escalated" ? "bad" : "waiting");
const words = (code: string) => code.replace(/_/g, " ");

function useExceptionList(tab: string) {
  return useApi<{ exceptions: Exception[] }>(`/api/exceptions?status=${tab}`);
}

function ExceptionQueue() {
  const router = useRouter();
  const [tab, setTab] = useState("open");
  const list = useExceptionList(tab);
  return (
    <Page title="Exceptions" lede="Invoices held back from payment until a person decides. The agent can look into each one and recommend an outcome; people decide.">
      <div className="tabs" role="tablist" aria-label="Filter">
        {TABS.map(([v, l]) => <button key={v} role="tab" className="tab" aria-selected={tab === v} onClick={() => setTab(v)}>{l}</button>)}
      </div>
      {list.error && <ErrorState error={list.error} retry={() => list.mutate()} />}
      {list.isLoading && <div className="panel"><Loading rows={6} /></div>}
      {list.data && list.data.exceptions.length === 0 && <div className="panel"><Empty title="Nothing here">No exceptions in this state.</Empty></div>}
      {!!list.data?.exceptions.length && (
        <div className="panel table-wrap">
          <table className="table">
            <thead><tr><th>Invoice</th><th>Vendor</th><th>Why</th><th className="num">Total</th><th>Raised</th><th>Status</th></tr></thead>
            <tbody>{list.data.exceptions.map((e) => (
              <tr key={e.id} className="cursor-pointer" onClick={() => router.push(`/exceptions/?id=${e.id}`)}>
                <td><Link href={`/exceptions/?id=${e.id}`}>{e.invoice.number ?? "no number"}</Link></td>
                <td>{e.invoice.vendor ?? <span className="missing">unknown</span>}</td>
                <td className="ink-2">{[...e.reasons.map((r) => words(r.code)), ...e.findings.map((f) => words(f.code))].filter((v, i, a) => a.indexOf(v) === i).join(", ")}</td>
                <td className="num">{amount(e.invoice.total, e.invoice.currency)}</td>
                <td className="muted">{day(e.created_at)}</td>
                <td><StatusChip status={EXC_CHIP(e.status)} label={EXC_STATUS[e.status]} /></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </Page>
  );
}

/** The agent's report and its trail, one above the other in the decision pane. */
function AgentRun({ exceptionId, runId }: { exceptionId: string; runId: string | null }) {
  const runs = useApi<{ runs: Run[] }>(runId ? null : "/api/investigations?limit=50");
  const id = runId ?? runs.data?.runs.find((r) => r.question.includes(exceptionId))?.id ?? null;
  const run = useApi<Run>(id ? `/api/investigations/${id}` : null);
  const [evidence, setEvidence] = useState<string | null>(null);
  const unfinished = run.data ? !["completed", "failed", "cancelled", "budget_exceeded"].includes(run.data.status) : false;
  useEffect(() => (id && unfinished ? followRun(id, () => void run.mutate()) : undefined), [id, unfinished, run]);
  if (!id) return null;
  if (!run.data) return <section className="sec"><Loading rows={4} label="Loading the agent's work" /></section>;
  return (
    <section className="sec" aria-label="The agent's work">
      <div className="sec__head"><h2>What the agent did</h2><StatusChip status={run.data.status} label={STATUS_LABEL[run.data.status]} /></div>
      {run.data.report ? <ReportView report={run.data.report} onEvidence={setEvidence} /> : (
        unfinished ? <Loading rows={4} label="The agent is working" /> : <Empty title="No report">{run.data.error ?? "The run ended without a report."}</Empty>
      )}
      <div className="grid gap-3">
        <h3>The steps</h3>
        <RunTrail run={run.data} onEvidence={setEvidence} />
      </div>
      {evidence && <EvidenceDrawer runId={id} handle={evidence} onClose={() => setEvidence(null)} />}
    </section>
  );
}

function ExceptionCase({ id }: { id: string }) {
  const { user } = useMe();
  const toast = useToast();
  const exc = useApi<Exception>(`/api/exceptions/${id}`);
  const drafts = useApi<{ drafts: { id: string; kind: string; body: string; created_at: string }[] }>(`/api/exceptions/${id}/drafts`);
  const invoice = useApi<Invoice & { provenance: Provenance[] }>(exc.data ? `/api/invoices/${exc.data.invoice_id}` : null);
  const [tab, setTab] = useState("open");
  const queue = useExceptionList(tab);
  const [outcome, setOutcome] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(1);
  const [showDoc, setShowDoc] = useState(false);
  const invoiceNumbers = useApi<{ invoices: { id: string; invoice_number: string | null }[] }>("/api/invoices?limit=500");
  const numbers = new Map((invoiceNumbers.data?.invoices ?? []).map((i) => [i.id, i.invoice_number]));
  useEffect(() => {
    if (exc.data?.status !== "investigating") return;
    const t = setInterval(() => { void exc.mutate(); void drafts.mutate(); }, 4000);  // the run's own stream refreshes the trail
    return () => clearInterval(t);
  }, [exc, drafts]);
  if (exc.error) return <Page title="Exception"><ErrorState error={exc.error} retry={() => exc.mutate()} /></Page>;
  if (!exc.data) return <Page title="Exception" flush><Loading rows={8} /></Page>;
  const e = exc.data;
  const open = e.status !== "resolved";
  const decider = user?.role === "approver" || user?.role === "admin";
  const worker = decider || user?.role === "clerk";
  const boxes = (invoice.data?.provenance ?? []).filter((p: Provenance) => p.located && p.bbox && p.page).map((p: Provenance) => ({ page: p.page!, bbox: p.bbox!, label: p.field }));
  const act = async (path: string, json: unknown, done: string) => {
    setBusy(true);
    try { await api(path, { method: "POST", json }); toast(done, "ok"); await exc.mutate(); await drafts.mutate(); void revalidate("/api/exceptions?status=open"); }
    catch (err) { toast(err instanceof ApiError ? err.message : "That did not work.", "bad"); }
    finally { setBusy(false); }
  };

  return (
    <Page flush title={`Exception on ${e.invoice.kind === "credit_note" ? "credit note" : "invoice"} ${e.invoice.number ?? ""}`}
      lede={[e.invoice.vendor, amount(e.invoice.total, e.invoice.currency)].filter(Boolean).join(", ")}
      actions={<>
        <StatusChip status={EXC_CHIP(e.status)} label={EXC_STATUS[e.status]} />
        <Link className="btn btn--sm" href={`/invoices/?id=${e.invoice_id}`}>Open the invoice</Link>
        <Link className="btn btn--sm" href="/exceptions/">All exceptions</Link>
      </>}>
      <div className="workspace">
        <section className="pane pane--list" aria-label="Exceptions">
          <div className="queue-head">
            <label className="field"><span className="label">Show</span>
              <select className="select input--sm" value={tab} onChange={(ev) => setTab(ev.target.value)} aria-label="Show">
                {TABS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
          </div>
          <ul className="queue">
            {queue.data?.exceptions.map((x) => (
              <li key={x.id}>
                <Link className="queue__item" href={`/exceptions/?id=${x.id}`} aria-current={x.id === id ? "true" : undefined}>
                  <span className="queue__top"><span className="queue__ref">{x.invoice.number ?? "no number"}</span><StatusChip status={EXC_CHIP(x.status)} label={EXC_STATUS[x.status]} /></span>
                  <span className="queue__sub"><span>{x.invoice.vendor ?? "unknown vendor"}</span><span className="queue__amt">{amount(x.invoice.total, x.invoice.currency)}</span></span>
                </Link>
              </li>
            ))}
          </ul>
        </section>

        <section className="pane pane--doc" aria-label="The invoice as it was received" data-shown={showDoc ? "true" : "false"}>
          <button type="button" className="btn btn--sm doc-toggle" aria-expanded={showDoc} onClick={() => setShowDoc((v) => !v)}>{showDoc ? "Hide the document" : "Show the document"}</button>
          <div className="sheet-stick">
            {invoice.data?.document_id ? <PageViewer documentId={invoice.data.document_id} boxes={boxes} page={page} onPage={setPage} /> : <div className="panel p-5 muted">{invoice.data ? "Entered without a document." : "Loading the invoice…"}</div>}
          </div>
        </section>

        <section className="pane pane--decide" aria-label="Decision">
          <section className="sec">
            <div className="sec__head"><h2>Why it was held</h2></div>
            <ul className="grid gap-3">
              {e.reasons.map((r, i) => <li key={`r${i}`}><strong>{words(r.code)}</strong>: <span className="ink-2">{r.detail}</span></li>)}
              {e.findings.map((f, i) => (
                <li key={`f${i}`}>
                  <strong>{words(f.code)}</strong> <span className="chip">score {Math.round(f.score * 100)}</span>: <span className="ink-2">{f.reason}</span>
                  {f.related_ids.length > 0 && <span className="flex flex-wrap gap-2 mt-1">{f.related_ids.map((r) => <Link key={r} className="btn btn--sm btn--quiet" href={`/invoices/?id=${r}`}>Related invoice {numbers.get(r) ?? r.slice(0, 8)}</Link>)}</span>}
                </li>
              ))}
            </ul>
          </section>

          {e.status === "resolved" ? (
            <div className="banner m-4" role="status">Resolved: <strong>{OUTCOMES[e.resolution ?? ""] ?? e.resolution}</strong>{e.resolved_at ? ` on ${when(e.resolved_at)}` : ""}{e.note ? `. ${e.note}` : "."}</div>
          ) : e.recommendation ? (
            <section className="sec">
              <div className="sec__head"><h2>The agent recommends: {OUTCOMES[e.recommendation.outcome] ?? e.recommendation.outcome}</h2></div>
              <p className="prose">{e.recommendation.rationale}</p>
              {decider ? (
                <div className="flex flex-wrap gap-2"><button className="btn btn--primary" disabled={busy} onClick={() => act(`/api/exceptions/${id}/accept-recommendation`, undefined, "Recommendation accepted.")}>Accept this recommendation</button></div>
              ) : <p className="hint">An approver or administrator decides. Whoever started the investigation does not accept its recommendation.</p>}
            </section>
          ) : open && (
            <div className="banner banner--open m-4" role="status">
              {e.status === "investigating" ? <span>The agent is looking into this. It reads the invoice, the order, receipts and the vendor&apos;s history; it changes nothing.</span>
                : <span>No recommendation yet.</span>}
              {worker && e.status !== "investigating" && <button className="btn btn--sm btn--primary" disabled={busy} onClick={() => act(`/api/exceptions/${id}/investigate`, undefined, "The agent has started.")}>Ask the agent to investigate</button>}
            </div>
          )}

          <AgentRun exceptionId={id} runId={e.recommendation?.run_id ?? null} />

          {open && decider && (
            <form className="sec" onSubmit={(ev) => { ev.preventDefault(); if (outcome) void act(`/api/exceptions/${id}/resolve`, { outcome, note: note || null }, "Decision recorded."); }}>
              <div className="sec__head"><h2>{e.recommendation ? "Or decide something else" : "Decide"}</h2></div>
              <label className="field"><span className="label">Outcome</span>
                <select className="select" value={outcome} onChange={(ev) => setOutcome(ev.target.value)} aria-label="Outcome">
                  <option value="">Choose an outcome…</option>
                  {Object.entries(OUTCOMES).filter(([k]) => !(k === "escalate" && e.status === "escalated")).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </select>
              </label>
              <label className="field"><span className="label">A note for the record</span>
                <input className="input" value={note} maxLength={1000} onChange={(ev) => setNote(ev.target.value)} placeholder="Optional" aria-label="Note" />
              </label>
              <div><button className="btn" disabled={!outcome || busy}>Record the decision</button></div>
              {e.status === "escalated" && user?.role !== "admin" && <p className="hint">This exception was escalated; an administrator decides it.</p>}
            </form>
          )}

          {!!drafts.data?.drafts.length && (
            <section className="sec">
              <div className="sec__head"><h2>Drafts for the supplier</h2><span className="label">Written by the agent. Nothing is sent; copy and send it yourself.</span></div>
              {drafts.data.drafts.map((d) => (
                <article key={d.id} className="grid gap-2">
                  <div className="flex items-center justify-between gap-2"><strong>{words(d.kind)}</strong>
                    <button className="btn btn--sm" onClick={() => { void navigator.clipboard?.writeText(d.body); toast("Copied.", "ok"); }}>Copy</button></div>
                  <pre className="draft">{d.body}</pre>
                </article>
              ))}
            </section>
          )}
        </section>
      </div>
    </Page>
  );
}

function ExceptionsRoute() {
  const id = useSearchParams().get("id");
  return id ? <ExceptionCase key={id} id={id} /> : <ExceptionQueue />;
}

export default function Exceptions() {
  return <Suspense fallback={<Loading />}><ExceptionsRoute /></Suspense>;
}
