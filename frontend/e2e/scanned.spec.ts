import { expect, test } from "@playwright/test";
import { invoiceFile, invoiceNumbered, KESTREL, signIn, tag, upload, vendorAndOrder } from "./helpers";

test("a scanned invoice under a trading name is put right on the review screen and matched", async ({ browser }) => {
  test.setTimeout(150_000); // OCR is the slowest step; on a busy machine it can take far longer than the 13 s it takes alone
  const [admin, clerk] = await Promise.all([1, 2].map(async () => (await browser.newContext()).newPage()));
  await signIn(admin, KESTREL.admin);
  await signIn(clerk, KESTREL.clerk);
  const t = tag();
  const vendor = `Harbourline Stationers ${t}`;
  await vendorAndOrder(admin, clerk, { name: vendor, po: `PO-S${t}`, item: ["ST-1", "Envelopes C4"], qty: 20, price: "3.50" });
  const number = `INV-S${t}`;
  // The scan prints a trading name the system has never seen, so the vendor is left for a person.
  await upload(clerk, invoiceFile({ number, vendor: `QPC Trading ${t}`, po: `PO-S${t}`, item: ["ST-1", "Envelopes C4"], qty: 20, price: "3.50", quality: "clean" }));
  const invoice = await invoiceNumbered(clerk, number, ["needs_review"], );

  await clerk.goto(`/invoices/?id=${invoice.id}`);
  await expect(clerk.getByText("Read by OCR from a scan.")).toBeVisible();
  await clerk.getByLabel("Vendor", { exact: true }).selectOption({ label: vendor });
  await expect(clerk.getByText("Saved. The invoice is checked again.")).toBeVisible();
  await clerk.getByRole("button", { name: "Match again" }).click();
  await expect(clerk.getByText("waiting for approval").first()).toBeVisible();
  await invoiceNumbered(clerk, number, ["pending_approval"]);
});
