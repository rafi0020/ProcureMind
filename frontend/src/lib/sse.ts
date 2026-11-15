"use client";

import { API } from "./api";

export interface RunEvent { type: string; data: Record<string, unknown> }
const TYPES = ["run.status", "step.started", "step.finished", "approval.requested", "question.asked", "report.ready"];
const TERMINAL = ["completed", "failed", "cancelled", "budget_exceeded"];

/** Follow a run's live events. The browser reconnects on its own and resumes after the last event it saw. */
export function followRun(runId: string, onEvent: (event: RunEvent) => void): () => void {
  const source = new EventSource(`${API}/api/investigations/${runId}/events`, { withCredentials: true });
  const handle = (type: string) => (message: MessageEvent) => {
    const data = JSON.parse(message.data);
    onEvent({ type, data });
    if (type === "run.status" && TERMINAL.includes(data.status)) source.close();
  };
  TYPES.forEach((type) => source.addEventListener(type, handle(type)));
  return () => source.close();
}
