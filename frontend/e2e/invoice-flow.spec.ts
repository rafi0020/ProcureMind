import { expect, test } from "@playwright/test";
import { invoiceFile, invoiceNumbered, KESTREL, signIn, tag, upload, vendorAndOrder } from "./helpers";

test("an invoice is uploaded, read, matched and approved; above 1,000 it takes two people", async ({ browser }) => {
  const [admin, clerk, approver] = await Promise.all([1, 2, 3].map(async () => (await browser.newContext()).newPage()));
  await signIn(admin, KESTREL.admin);
  await signIn(clerk, KESTREL.clerk);
  await signIn(approver, KESTREL.approver);
  const t = tag();
  const name = `Flow Supplies ${t}`;
  await vendorAndOrder(admin, clerk, { name, po: `PO-F${t}-1`, item: ["FL-1", "Copier paper A4"], qty: 10, price: "45.00" });
  await vendorAndOrder(admin, clerk, { name: `${name} Two`, po: `PO-F${t}-2`, item: ["FL-2", "Office chair"], qty: 10, price: "150.00" });

  const small = `INV-F${t}-1`, large = `INV-F${t}-2`;
  await upload(clerk, invoiceFile({ number: small, vendor: name, po: `PO-F${t}-1`, item: ["FL-1", "Copier paper A4"], qty: 10, price: "45.00" }));
  await upload(clerk, invoiceFile({ number: large, vendor: `${name} Two`, po: `PO-F${t}-2`, item: ["FL-2", "Office chair"], qty: 10, price: "150.00" }));
  await invoiceNumbered(clerk, small, ["pending_approval"]);
  await invoiceNumbered(clerk, large, ["pending_approval"]);

  // 495.00: one approver is enough.
  await approver.goto("/approvals/");
  await approver.getByRole("link", { name: new RegExp(small) }).click();
  await approver.getByRole("button", { name: "Approve" }).click();
  await expect(approver.getByText("Approved for payment.")).toBeVisible();

  // 1,650.00: the second tier needs two approvals.
  await approver.getByRole("link", { name: new RegExp(large) }).click();
  await approver.getByRole("button", { name: "Approve" }).click();
  await expect(approver.getByText(/Still waiting for 1 more approval/)).toBeVisible();
  await admin.goto("/approvals/");
  await admin.getByRole("link", { name: new RegExp(large) }).click();
  await admin.getByRole("button", { name: "Approve" }).click();
  await expect(admin.getByText("Approved for payment.")).toBeVisible();
  await invoiceNumbered(clerk, small, ["approved"]);
  await invoiceNumbered(clerk, large, ["approved"]);
});
