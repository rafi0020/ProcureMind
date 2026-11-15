"use client";

import { useState } from "react";
import { duration } from "@/lib/format";
import type { Run, Step } from "@/lib/types";

const TOOL_LABEL: Record<string, string> = {
  list_tables: "Listed the tables", describe_table: "Read a table's columns", run_sql: "Ran a query", compute: "Calculated",
  chart: "Drew a chart", record_finding: "Recorded a finding", ask_user: "Asked you a question", propose_alert: "Proposed an alert",
  finish: "Wrote the report",
};

function describe(step: Step): string {
  const input = step.input ?? {};
  if (step.name === "run_sql" && typeof input.purpose === "string") return input.purpose;
  if (step.name === "compute") return `${String(input.op ?? "").replace(/_/g, " ")}`;
  if (step.name === "describe_table") return String(input.table ?? "");
  if (step.name === "record_finding" && typeof input.claim === "string") return input.claim;
  if (step.name === "ask_user" && typeof input.question === "string") return input.question;
  if (step.name === "chart" && typeof input.title === "string") return input.title;
  if (step.name === "propose_alert") return `${input.metric} ${input.comparison} ${input.threshold}${input.region ? ` in ${input.region}` : ""}, per ${input.window}`;
  return "";
}

function Item({ step, onEvidence }: { step: Step; onEvidence: (handle: string) => void }) {
  const [open, setOpen] = useState(false);
  const active = step.state === "started";
  const waiting = step.state === "awaiting_approval" || step.state === "awaiting_user";
  const answered = step.name === "ask_user" && step.state === "done" && step.summary.startsWith("The user answered");
  return (
    <>
      <li className="trail__item" data-state={step.error ? "error" : active ? "active" : undefined}>
        <div className="trail__row">
          <span className="instrument">{TOOL_LABEL[step.name] ?? step.name}</span>
          {waiting && <span className="chip chip--open">{step.state === "awaiting_approval" ? "waiting for an administrator" : "waiting for you"}</span>}
          {step.handle && <button className="fig" onClick={() => onEvidence(step.handle!)}>{step.handle}</button>}
          <span className="trail__meta measured">{step.latency_ms > 0 && duration(step.latency_ms)}</span>
        </div>
        {describe(step) && <div className="ink-2">{describe(step)}</div>}
        {step.error && <div className="bad">{step.error.replace(/^invalid_call: /, "The model called this tool incorrectly: ")}</div>}
        {step.input && Object.keys(step.input).length > 0 && (
          <button className="trail__detail" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide what was sent" : "Show what was sent"}</button>
        )}
        {open && <div className="well code">{typeof step.input?.sql === "string" ? step.input.sql : JSON.stringify(step.input, null, 2)}</div>}
      </li>
      {answered && (
        <li className="trail__item" data-who="person">
          <div className="trail__row"><span className="person">You answered</span></div>
          <div className="ink-2">{step.summary.replace("The user answered: ", "")}</div>
        </li>
      )}
    </>
  );
}

/** One spine for the whole run: what the agent did, in cool; what people asked or decided, in amber. */
export function RunTrail({ run, onEvidence }: { run: Run; onEvidence: (handle: string) => void }) {
  const steps = (run.steps ?? []).filter((s) => s.kind === "tool");
  const thinking = run.status === "running" && (run.steps ?? []).every((s) => s.state === "done");
  return (
    <ol className="trail" aria-label="What the agent did">
      <li className="trail__item" data-who="person">
        <div className="trail__row"><span className="person">{run.asked_by ?? "Someone"} asked</span></div>
        <div className="ink-2">{run.question}</div>
      </li>
      {steps.map((step) => (<Item key={step.idx} step={step} onEvidence={onEvidence} />))}
      {run.status === "queued" && <li className="trail__item" data-state="active"><span className="muted">Waiting for a worker</span></li>}
      {thinking && <li className="trail__item" data-state="active"><span className="muted">Deciding the next step</span></li>}
    </ol>
  );
}
