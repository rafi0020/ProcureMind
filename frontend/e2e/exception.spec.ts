import { expect, test } from "@playwright/test";
import { eventually, invoiceFile, invoiceNumbered, KESTREL, signIn, tag, upload, vendorAndOrder } from "./helpers";

async function exceptionFor(page: import("@playwright/test").Page, invoiceId: string) {
  return eventually(async () => {
    const { exceptions } = await (await page.request.get("/api/exceptions?status=open")).json();
    return exceptions.find((e: { invoice_id: string }) => e.invoice_id === invoiceId) as { id: string } | undefined;
  });
}

test("a price over the order becomes an exception; the agent recommends; an approver accepts", async ({ browser }) => {
  const [admin, clerk, approver] = await Promise.all([1, 2, 3].map(async () => (await browser.newContext()).newPage()));
  await signIn(admin, KESTREL.admin);
  await signIn(clerk, KESTREL.clerk);
  await signIn(approver, KESTREL.approver);
  const t = tag();
  const vendor = `Price Metals ${t}`;
  await vendorAndOrder(admin, clerk, { name: vendor, po: `PO-X${t}`, item: ["PM-1", "Steel sheet 2mm"], qty: 10, price: "20.00" });
  const number = `INV-X${t}`;
  await upload(clerk, invoiceFile({ number, vendor, po: `PO-X${t}`, item: ["PM-1", "Steel sheet 2mm"], qty: 10, price: "23.00" }));
  const invoice = await invoiceNumbered(clerk, number, ["exception"]);
  const exception = await exceptionFor(clerk, invoice.id);

  await clerk.goto(`/exceptions/?id=${exception.id}`);
  await expect(clerk.getByText(/price over tolerance/)).toBeVisible();
  await clerk.getByRole("button", { name: "Ask the agent to investigate" }).click();
  await expect(clerk.getByRole("heading", { name: /The agent recommends: Hold/ })).toBeVisible({ timeout: 60_000 });
  await expect(clerk.getByRole("button", { name: "Accept this recommendation" })).toHaveCount(0); // clerks do not decide

  await approver.goto(`/exceptions/?id=${exception.id}`);
  await approver.getByRole("button", { name: "Accept this recommendation" }).click();
  await expect(approver.getByText(/Resolved:\s*Hold/)).toBeVisible();
  await invoiceNumbered(clerk, number, ["on_hold"]);
});

test("a duplicate sent again under another spelling is caught and rejected", async ({ browser }) => {
  const [admin, clerk, approver] = await Promise.all([1, 2, 3].map(async () => (await browser.newContext()).newPage()));
  await signIn(admin, KESTREL.admin);
  await signIn(clerk, KESTREL.clerk);
  await signIn(approver, KESTREL.approver);
  const t = tag();
  const vendor = `Repeat Tools ${t}`;
  await vendorAndOrder(admin, clerk, { name: vendor, po: `PO-D${t}`, item: ["RT-1", "Torque wrench"], qty: 100, price: "45.00" });
  const n = String(100 + (Date.now() % 800));
  await upload(clerk, invoiceFile({ number: `INV-00${n}`, vendor, po: `PO-D${t}`, item: ["RT-1", "Torque wrench"], qty: 10, price: "45.00" }));
  const first = await invoiceNumbered(clerk, `INV-00${n}`, ["pending_approval"]);
  await upload(clerk, invoiceFile({ number: `inv ${n}`, vendor, po: `PO-D${t}`, item: ["RT-1", "Torque wrench"], qty: 10, price: "45.00" }));
  const again = await invoiceNumbered(clerk, `inv ${n}`, ["exception"]);
  const exception = await exceptionFor(clerk, again.id);

  await approver.goto(`/exceptions/?id=${exception.id}`);
  await expect(approver.getByText(/duplicate number/)).toBeVisible();
  await approver.getByRole("link", { name: "Related invoice" }).first().click();
  await expect(approver).toHaveURL(new RegExp(first.id));
  await approver.goto(`/exceptions/?id=${exception.id}`);
  await approver.getByLabel("Outcome").selectOption({ label: "Reject as a duplicate" });
  await approver.getByRole("button", { name: "Record the decision" }).click();
  await expect(approver.getByText(/Resolved:\s*Reject as a duplicate/)).toBeVisible();
  await invoiceNumbered(clerk, `inv ${n}`, ["rejected"]);
});
