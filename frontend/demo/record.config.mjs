// What to record for the ProcureMind demo. Run with the stack up and a model connected (see README, "Adding an AI model").
import path from "node:path";

const user = (role) => `${role}@kestrel.demo`;
const PASSWORDS = { clerk: "Clerk#Demo2026", approver: "Approver#Demo2026", admin: "Admin#Demo2026", viewer: "Viewer#Demo2026" };
const SCAN = path.resolve("../samples/resent-invoice-scanned.pdf");

/** A step that may fail without losing the rest of the recording. */
async function step(name, fn) {
  try { await fn(); } catch (e) { console.log(`  step "${name}" skipped: ${String(e.message).split("\n")[0]}`); }
}

async function openException(page, h, invoice) {
  await page.goto("/exceptions/");
  await h.settle(600);
  await page.getByRole("link", { name: invoice, exact: true }).first().click({ timeout: 8000 }).catch(async () => {
    await page.getByText(invoice, { exact: true }).first().click({ timeout: 8000 });
  });
  await h.settle(900);
}

async function investigate(page, h, invoice) {
  await openException(page, h, invoice);
  await page.getByRole("button", { name: "Ask the agent to investigate" }).click({ timeout: 8000 });
  await page.getByRole("heading", { name: /The agent recommends/ }).waitFor({ timeout: 240_000 });
  await h.settle(1500);
}

export default {
  project: "procuremind",
  baseUrl: process.env.DEMO_BASE_URL ?? "http://localhost:3300",
  loginPath: "/login/",
  loginApi: "/api/auth/login",
  logoutApi: "/api/auth/logout",
  crawlStart: "/inbox/",
  crawlMax: 45,
  crawlSkip: /[?&]run=/,
  poke: true,
  pokeRows: 3,
  sequence: [/^\/api\/exceptions\/[^/]+$/, /^\/api\/investigations\/[^/]+$/, /^\/api\/documents\/[^/]+$/],
  users: [
    { email: user("clerk"), password: PASSWORDS.clerk, label: "Clerk" },
    { email: user("approver"), password: PASSWORDS.approver, label: "Approver" },
    { email: user("admin"), password: PASSWORDS.admin, label: "Administrator" },
    { email: user("viewer"), password: PASSWORDS.viewer, label: "Viewer" },
  ],
  journeys: {
    [user("clerk")]: async (page, h) => {
      for (const invoice of ["INV-P1001", "INV-P5003", "INV-P4001"]) await step(`investigate ${invoice}`, () => investigate(page, h, invoice));
      await step("upload a scanned duplicate", async () => {
        await page.goto("/inbox/");
        await h.settle(600);
        await page.locator("input[type=file]").first().setInputFiles(SCAN);
        await page.getByText(/extracted|needs review|exception/i).first().waitFor({ timeout: 180_000 });
        await h.settle(4000);
      });
      await step("open an invoice and highlight a field", async () => {
        await page.goto("/invoices/");
        await h.settle(600);
        await page.getByRole("link", { name: "INV-P5001", exact: true }).first().click({ timeout: 8000 });
        await h.settle(1500);
        await page.getByRole("button", { name: /Total/ }).first().click({ timeout: 5000 });
        await h.settle(1500);
      });
    },
  },
  after: {
    [user("approver")]: async (page, h) => {
      await step("accept a recommendation", async () => {
        await openException(page, h, "INV-P5003");
        await h.mute(async () => {
          await page.getByRole("button", { name: "Accept this recommendation" }).click({ timeout: 8000 });
          await h.settle(2500);
        });
      });
      await step("approve an invoice", async () => {
        await page.goto("/approvals/");
        await h.settle(800);
        await page.locator("a, tr").filter({ hasText: /INV-/ }).first().click({ timeout: 8000 });
        await h.settle(1200);
        await h.mute(async () => {
          await page.getByRole("button", { name: "Approve" }).first().click({ timeout: 8000 });
          await h.settle(2500);
        });
      });
    },
  },
};
