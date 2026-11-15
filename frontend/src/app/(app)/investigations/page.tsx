"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent } from "react";
import { EvidenceDrawer } from "@/components/Evidence";
import { ReportView } from "@/components/ReportView";
import { RunTrail } from "@/components/RunTrail";
import { Page } from "@/components/Shell";
import { Empty, ErrorState, Loading, StatusChip, useToast } from "@/components/ui";
import { api, ApiError, useApi, useMe } from "@/lib/api";
import { STATUS_LABEL, usd, when } from "@/lib/format";
import { followRun } from "@/lib/sse";
import type { Run } from "@/lib/types";

const EXAMPLES = [
  "Which vendors invoiced above their purchase-order prices this month?",
  "Are there invoices that look like duplicates of ones already approved?",
  "Which purchase orders have been invoiced for more than was received?",
  "Which vendors changed bank details recently, and were any invoices paid to the new account?",
];
const ACTIVE = ["queued", "running"];
const FINAL = ["completed", "failed", "cancelled", "budget_exceeded"];

/** The recent runs, one per row, the one open marked. */
function RunList({ picked }: { picked: string | null }) {
  const runs = useApi<{ runs: Run[] }>("/api/investigations?limit=15", { refreshInterval: (d) => (d?.runs.some((r) => ACTIVE.includes(r.status) || r.status.startsWith("waiting")) ? 4000 : 0) });
  return (
    <section className="pane pane--list" aria-label="Agent runs">
      {runs.isLoading && <Loading rows={6} label="Loading the runs" />}
      {runs.error && <div className="bar-note"><ErrorState error={runs.error} retry={() => runs.mutate()} /></div>}
      {runs.data?.runs.length === 0 && <Empty title="No questions yet">Ask one; it will appear here with its result.</Empty>}
      {!!runs.data?.runs.length && (
        <ul className="queue">
          {runs.data.runs.map((r) => (
            <li key={r.id}>
              <Link className="queue__item" href={`/investigations/?run=${r.id}`} aria-current={r.id === picked ? "true" : undefined}>
                <span className="queue__top"><span className="queue__ref queue__question">{r.question}</span></span>
                <span className="queue__sub"><span>{r.asked_by ?? "someone"} · {when(r.created_at)}</span><StatusChip status={r.status} label={STATUS_LABEL[r.status]} /></span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Not a run yet: the question, and examples to start from. In the middle, where the report will be. */
function AskPane() {
  const { user } = useMe();
  const router = useRouter();
  const toast = useToast();
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (question.trim().length < 3) return;
    setBusy(true);
    try {
      const { id } = await api<{ id: string }>("/api/investigations", { method: "POST", json: { question } });
      router.push(`/investigations/?run=${id}`);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Could not start the run.", "bad");
      setBusy(false);
    }
  }

  return (
    <section className="pane pane--doc" aria-label="Ask the agent">
      <div className="grid gap-5 min-w-0">
        <div className="grid gap-2">
          <h2>Ask the Exception Agent</h2>
          <p className="ink-2">It reads invoices, orders, receipts and vendors. It never changes anything, shows each step, and links every figure to the record it came from.</p>
        </div>
        {user?.role !== "viewer" && (
          <form className="panel" onSubmit={submit}>
            <div className="panel__body grid gap-3">
              <label className="field">
                <span className="label">Your question</span>
                <textarea className="textarea" rows={3} maxLength={2000} value={question} onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) e.currentTarget.form?.requestSubmit(); }}
                  placeholder="For example: which invoices from Harbor Metals were above the PO price?" />
              </label>
              <div className="flex flex-wrap items-center justify-between gap-3">
                <span className="hint">Ctrl + Enter to send.</span>
                <button className="btn btn--primary" disabled={busy || question.trim().length < 3} aria-busy={busy}>Ask the agent</button>
              </div>
            </div>
          </form>
        )}
        {user?.role !== "viewer" && (
          <div className="grid gap-2">
            <h3>Try one of these</h3>
            <div className="grid gap-2">
              {EXAMPLES.map((q) => (<button key={q} className="btn btn--example" onClick={() => setQuestion(q)}>{q}</button>))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function RunView({ id }: { id: string }) {
  const { user } = useMe();
  const toast = useToast();
  const { data: run, error, mutate } = useApi<Run>(`/api/investigations/${id}`);
  const [evidence, setEvidence] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const live = run ? ACTIVE.includes(run.status) : false;
  // A run waiting for an answer or an approval is not finished: keep listening, so the page moves on by
  // itself when someone else (an administrator approving, say) lets it continue.
  const unfinished = run ? !FINAL.includes(run.status) : false;

  // Until the run is finished, every event from the server refreshes it. Reconnection is automatic.
  useEffect(() => (unfinished ? followRun(id, () => void mutate()) : undefined), [id, unfinished, mutate]);

  if (error) return <section className="pane pane--doc"><ErrorState error={error} retry={() => mutate()} /></section>;
  if (!run) return <><section className="pane pane--doc"><Loading rows={8} label="Loading the run" /></section></>;
  const mine = user?.role === "admin" || user?.id === run.user_id;
  const act = async (path: string, json: unknown, done: string) => {
    try {
      await api(path, { method: "POST", json });
      toast(done);
      await mutate();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That did not work.", "bad");
    }
  };

  return (
    <>
      <section className="pane pane--doc" aria-label="The report">
        {run.status === "waiting_user" && (
          <form className="banner banner--open" onSubmit={(e) => { e.preventDefault(); void act(`/api/investigations/${id}/answer`, { text: answer }, "Answer sent; the agent is continuing."); setAnswer(""); }}>
            <div className="grid gap-2 w-full">
              <strong>The agent needs one thing from you: {run.pending?.question}</strong>
              {mine ? (
                <div className="flex gap-2">
                  <input className="input" value={answer} onChange={(e) => setAnswer(e.target.value)} maxLength={1000} aria-label="Your answer" autoFocus />
                  <button className="btn btn--primary" disabled={!answer.trim()}>Send answer</button>
                </div>
              ) : <span>Only the person who asked, or an administrator, can answer.</span>}
            </div>
          </form>
        )}
        {run.status === "waiting_approval" && (
          <div className="banner banner--open">
            <span>The agent proposed an action ({run.pending?.tool?.replace(/_/g, " ")}) that needs an administrator&apos;s approval. Nothing has been changed.</span>
            <a className="btn" href="/approvals/">Open approvals</a>
          </div>
        )}
        {run.error && <div className="banner banner--bad" role="alert">{run.error}</div>}
        <article className="sheet paper" aria-label="The agent's report">
          <h2 className="ink-2">{run.question}</h2>
          {run.report ? <ReportView report={run.report} onEvidence={setEvidence} /> : (
            live ? <Loading rows={5} label="The agent is working" /> : unfinished ? <Empty title="Paused">The report will appear here once the run continues.</Empty> : <Empty title="No report">{run.status === "cancelled" ? "This run was cancelled before it finished." : "The run ended without a report. The trail shows how far it got."}</Empty>
          )}
        </article>
      </section>

      <section className="pane pane--side" aria-label="The steps">
        <div className="decide">
          <div className="grid gap-1">
            <h2>Steps</h2>
            <span className="label measured">{run.steps_used} model steps, {run.tokens_used.toLocaleString()} tokens, est. {usd(run.cost_usd)}</span>
          </div>
          <RunTrail run={run} onEvidence={setEvidence} />
        </div>
      </section>
      {evidence && <EvidenceDrawer runId={id} handle={evidence} onClose={() => setEvidence(null)} />}
    </>
  );
}

function AskPage() {
  const params = useSearchParams();
  const run = params.get("run");
  const { user } = useMe();
  const toast = useToast();
  const current = useApi<Run>(run ? `/api/investigations/${run}` : null);
  const mine = !!run && !!current.data && (user?.role === "admin" || user?.id === current.data.user_id);
  const cancel = async () => {
    try {
      await api(`/api/investigations/${run}/cancel`, { method: "POST" });
      toast("Cancelling the run.");
      await current.mutate();
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "That did not work.", "bad");
    }
  };
  const open = run && current.data ? current.data : null;
  return (
    <Page flush title="Agent runs"
      lede={run ? "What the agent did, step by step, and the report it wrote from the records." : "Ask the Exception Agent about invoices, orders, receipts and vendors."}
      actions={open ? (
        <>
          <StatusChip status={open.status} label={STATUS_LABEL[open.status]} />
          {mine && (ACTIVE.includes(open.status) || open.status.startsWith("waiting")) && <button className="btn btn--danger btn--sm" onClick={cancel}>Cancel</button>}
          <Link className="btn btn--sm" href="/investigations/">New question</Link>
        </>
      ) : undefined}>
      <div className="workspace" data-picked={run ? "true" : "false"}>
        <RunList picked={run} />
        {run ? <RunView key={run} id={run} /> : <AskPane />}
      </div>
    </Page>
  );
}

export default function Ask() {
  return (
    <Suspense fallback={<Loading />}>
      <AskPage />
    </Suspense>
  );
}
