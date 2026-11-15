export type Role = "viewer" | "clerk" | "approver" | "admin";
export interface User { id: string; org_id: string; email: string; name: string; role: Role }
export interface Step {
  idx: number; kind: "model" | "tool" | "note"; name: string; state: string; error: string | null;
  input: Record<string, unknown> | null; summary: string; handle: string | null; tool_calls: string[];
  tokens: number; cost_usd?: number; latency_ms: number;
}
export interface NumberRef { text: string; handle: string }
export interface ChartSpec { kind: "line" | "bar" | "waterfall"; title: string; x: string; labels: string[]; series: { name: string; data: (number | null)[] }[]; handle?: string; chart_handle?: string }
export interface Report {
  outcome: "answered" | "cannot_answer"; summary: string; summary_numbers: NumberRef[];
  findings: { number: number; claim: string; evidence: string[]; numbers: NumberRef[] }[];
  recommendations: string[]; charts: ChartSpec[]; unsupported: string[];
  evidence: { handle: string; kind: string; purpose: string | null; sql: string | null; row_count: number }[];
}
export type RunStatus = "queued" | "running" | "waiting_approval" | "waiting_user" | "completed" | "failed" | "cancelled" | "budget_exceeded";
export interface Run {
  id: string; question: string; status: RunStatus; error: string | null; pending: { kind?: string; question?: string; tool?: string } | null;
  steps_used: number; tokens_used: number; cost_usd: number; asked_by: string | null; user_id: string | null;
  created_at: string; finished_at: string | null; steps?: Step[]; report?: Report | null;
}
export interface Evidence { handle: string; kind: string; sql: string | null; row_count: number; payload: Record<string, unknown> & { columns?: string[]; rows?: unknown[][]; rows_omitted?: number } }
export interface Approval { id: string; run_id: string; question: string; tool: string; arguments: Record<string, unknown>; status: string; reason: string | null; requested_by: string | null; created_at: string; decided_at: string | null }


export interface Money { total: string | null; currency: string | null }
export interface Invoice { id: string; document_id: string | null; vendor_id: string | null; vendor_name_printed: string | null; kind: "invoice" | "credit_note"; invoice_number: string | null; invoice_number_norm: string | null; invoice_date: string | null; due_date: string | null; po_number: string | null; currency: string | null; subtotal: string | null; tax: string | null; total: string | null; bank_account: string | null; status: string; issues: string[]; entered_by: string | null; corrected_by: string[]; created_at: string }
export interface InvoiceLine { id: string; line_no: number; item_code: string | null; description: string; quantity: string | null; unit_price: string | null; amount: string | null }
export interface Provenance { field: string; located: boolean; page: number | null; bbox: number[] | null; confidence: number | null; candidate: string | null; reason: string | null }
export interface Reason { code: string; detail: string; line_no?: number | null }
export interface Finding { code: string; score: number; reason: string; related_ids: string[] }
export interface Exception { id: string; invoice_id: string; status: string; reasons: Reason[]; findings: Finding[]; recommendation: { outcome: string; rationale: string; evidence: string[]; run_id: string } | null; resolution: string | null; note: string | null; resolved_by: string | null; resolved_at: string | null; created_at: string; invoice: { number: string | null; vendor_id: string | null; vendor: string | null; total: string | null; currency: string | null; kind: string; status: string } }
export interface Vendor { id: string; name: string; tax_id: string | null; email: string | null; default_currency: string; payment_terms_days: number; created_at: string }
