import { expect, test } from "@playwright/test";
import { csrf, invoiceFile, invoiceNumbered, KESTREL, RIDGEWAY_CLERK, signIn, tag, upload, vendorAndOrder } from "./helpers";

test("whoever corrected an invoice cannot approve it, in the screen or through the API", async ({ browser }) => {
  const [admin, clerk, approver] = await Promise.all([1, 2, 3].map(async () => (await browser.newContext()).newPage()));
  await signIn(admin, KESTREL.admin);
  await signIn(clerk, KESTREL.clerk);
  await signIn(approver, KESTREL.approver);
  const t = tag();
  const vendor = `Duty Supplies ${t}`;
  await vendorAndOrder(admin, clerk, { name: vendor, po: `PO-Y${t}`, item: ["DS-1", "Hand soap 5L"], qty: 4, price: "12.00" });
  const number = `INV-Y${t}`;
  await upload(clerk, invoiceFile({ number, vendor, po: `PO-Y${t}`, item: ["DS-1", "Hand soap 5L"], qty: 4, price: "12.00" }));
  const invoice = await invoiceNumbered(clerk, number, ["pending_approval"]);

  // The administrator sends it back, corrects the due date on the review screen, then matches it again.
  await admin.goto(`/invoices/?id=${invoice.id}`);
  await expect(admin.getByRole("button", { name: "Correct" })).toHaveCount(0); // waiting for approval: not editable
  await admin.getByLabel("Why it is sent back").fill("Due date is wrong");
  await admin.getByRole("button", { name: "Send back for correction" }).click();
  await expect(admin.getByText("Sent back. Correct it, then match it again.")).toBeVisible();
  await admin.getByRole("button", { name: "Correct" }).nth(2).click();
  await admin.getByLabel("Due date").fill("2026-12-31");
  await admin.getByRole("button", { name: "Save" }).click();
  await expect(admin.getByText("Saved. The invoice is checked again.")).toBeVisible();
  await admin.getByRole("button", { name: "Match again" }).click();
  await invoiceNumbered(admin, number, ["pending_approval"]);

  await admin.goto("/approvals/");
  await expect(admin.getByRole("link", { name: new RegExp(number) })).toHaveCount(0); // not in "Mine to decide"
  await admin.getByRole("tab", { name: "Everything waiting" }).click();
  await admin.getByRole("link", { name: new RegExp(number) }).click();
  await expect(admin.getByText("someone else decides")).toBeVisible();
  await expect(admin.getByText(/entered or corrected this invoice/)).toBeVisible();
  await expect(admin.getByRole("button", { name: "Approve" })).toHaveCount(0); // no decision offered to the person who corrected it
  const refused = await admin.request.post(`/api/ap-approvals/${invoice.id}/decide`, { data: { decision: "approved" }, headers: await csrf(admin) });
  expect(refused.status()).toBe(403);
  expect((await refused.json()).detail).toMatch(/corrected/);

  await approver.goto("/approvals/");
  await approver.getByRole("link", { name: new RegExp(number) }).click();
  await approver.getByRole("button", { name: "Approve" }).click();
  await expect(approver.getByText("Approved for payment.")).toBeVisible();
});

test("one organisation cannot open another's invoice, by URL or through the API", async ({ browser }) => {
  const kestrel = await (await browser.newContext()).newPage();
  await signIn(kestrel, KESTREL.clerk);
  const { invoices } = await (await kestrel.request.get("/api/invoices?limit=1")).json();
  const id = invoices[0].id as string;

  const ridgeway = await (await browser.newContext()).newPage();
  await signIn(ridgeway, RIDGEWAY_CLERK);
  for (const path of [`/api/invoices/${id}`, `/api/invoices/${id}/match`]) expect((await ridgeway.request.get(path)).status(), path).toBe(404);
  await ridgeway.goto(`/invoices/?id=${id}`);
  await expect(ridgeway.getByText(/not found/i)).toBeVisible();
  await expect(ridgeway.getByRole("heading", { name: new RegExp(invoices[0].invoice_number ?? "no-such") })).toHaveCount(0);
});
