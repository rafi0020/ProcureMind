import { expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

// The stack runs with LLM_PROVIDER=stub by default, so the Exception Agent uses the built-in script: these journeys
// exercise the whole system (API, workers, OCR, database, live events, UI) without calling a model.
export const KESTREL = {
  admin: ["admin@kestrel.demo", "Admin#Demo2026"],
  approver: ["approver@kestrel.demo", "Approver#Demo2026"],
  clerk: ["clerk@kestrel.demo", "Clerk#Demo2026"],
  viewer: ["viewer@kestrel.demo", "Viewer#Demo2026"],
} as const;
export const RIDGEWAY_CLERK = ["clerk@ridgeway.demo", "Clerk#Demo2026"] as const;
export const ACCOUNTS = [KESTREL.admin, KESTREL.approver, KESTREL.clerk, KESTREL.viewer, RIDGEWAY_CLERK];
export const AUTH_DIR = "e2e/.auth";
export const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3300";
export const tag = () => Date.now().toString(36).toUpperCase();

/** Open the app as this account, using the session created once in global setup. */
export async function signIn(page: Page, [email]: readonly [string, string]) {
  const state = JSON.parse(readFileSync(`${AUTH_DIR}/${email}.json`, "utf8"));
  await page.context().addCookies(state.cookies);
  await page.goto("/inbox/");
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();
}

export async function csrf(page: Page) {
  const me = await (await page.request.get("/api/auth/me")).json();
  return { "X-CSRF-Token": me.csrf_token as string };
}

/** A write as the signed-in person, failing the test with the server's message on an error. */
export async function send<T = Record<string, unknown>>(page: Page, method: "post" | "put" | "patch", path: string, data?: unknown): Promise<T> {
  const response = await page.request[method](path, { data, headers: await csrf(page) });
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${response.status()} ${await response.text()}`).toBeTruthy();
  return (await response.json()) as T;
}

/** A vendor (as an administrator) and a purchase order for it, received in full (as a clerk). */
export async function vendorAndOrder(admin: Page, clerk: Page, { name, po, item, qty, price }: { name: string; po: string; item: [string, string]; qty: number; price: string }) {
  const vendor = await send<{ id: string }>(admin, "post", "/api/vendors", { name, default_currency: "USD" });
  // The account the invoices print, on file and approved (a sole administrator may decide their own request), so the
  // bank-account check has something to compare with.
  const account = await send<{ id: string }>(admin, "post", `/api/vendors/${vendor.id}/bank-accounts`, { iban_or_account: "GB00E2EB00000000000001", bank_name: "Journey Bank" });
  await send(admin, "post", `/api/vendors/${vendor.id}/bank-accounts/${account.id}/decide`, { decision: "approved" });
  const order = await send<{ id: string; lines: { id: string }[] }>(clerk, "post", "/api/orders", {
    vendor_id: vendor.id, po_number: po, currency: "USD", lines: [{ item_code: item[0], description: item[1], quantity: String(qty), unit_price: price }],
  });
  await send(clerk, "post", `/api/orders/${order.id}/receipts`, { receipt_no: `GRN-${po}`, lines: [{ po_line_id: order.lines[0].id, quantity: String(qty) }] });
  return { vendor: vendor.id, order: order.id };
}

/** An invoice document rendered by the backend's own generator, as a supplier would send it. */
export function invoiceFile(spec: { number: string; vendor: string; po: string | null; item: [string, string]; qty: number; price: string; quality?: "digital" | "clean" }): string {
  mkdirSync("test-results/documents", { recursive: true });
  const out = resolve(`test-results/documents/${spec.number.replace(/\W/g, "_")}.pdf`);
  const script = `
import json, os, sys
from datetime import date, timedelta
from decimal import Decimal as D
from seed.documents import DocSpec, Line, render_pdf, render_scan
s = json.loads(os.environ["SPEC"])
spec = DocSpec("invoice", s["number"], date(2026, 11, 10), s["vendor"], "Kestrel Foods", "USD", [Line(s["item"][0], s["item"][1], D(s["qty"]), D(s["price"]))],
               po_number=s["po"], due_date=date(2026, 12, 10), bank_account="GB00E2EB00000000000001")
body = render_scan(spec, "clean") if s.get("quality") == "clean" else render_pdf(spec)
open(os.environ["OUT"], "wb").write(body)
`;
  execFileSync("uv", ["run", "--quiet", "python", "-c", script], { cwd: "../backend", env: { ...process.env, SPEC: JSON.stringify(spec), OUT: out }, stdio: "pipe" });
  return out;
}

/** Upload through the Inbox page, as a person would. */
export async function upload(page: Page, file: string) {
  await page.goto("/inbox/");
  await page.locator('input[type="file"]').setInputFiles(file);
  await expect(page.getByText(/uploaded|received|queued/i).first()).toBeVisible({ timeout: 15_000 });
}

/** Poll until `check` returns something truthy: the worker processes a document a few seconds after upload. */
export async function eventually<T>(check: () => Promise<T | null | undefined | false>, seconds = 60): Promise<T> {
  const until = Date.now() + seconds * 1000;
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() > until) throw new Error("condition not met in time");
    await new Promise((r) => setTimeout(r, 1000));
  }
}

/** The invoice made from an uploaded document, found by its printed number, once the pipeline has run. */
export async function invoiceNumbered(page: Page, number: string, statuses: string[]) {
  return eventually(async () => {
    const { invoices } = await (await page.request.get("/api/invoices?limit=500")).json();
    return invoices.find((i: { invoice_number: string; status: string }) => i.invoice_number === number && statuses.includes(i.status)) as
      | { id: string; status: string; total: string }
      | undefined;
  });
}
