import { expect, test } from "@playwright/test";
import { KESTREL, signIn } from "./helpers";

test("signing in through the form lands on the Inbox", async ({ page }) => {
  await page.goto("/login/");
  await page.getByLabel("E-mail").fill(KESTREL.viewer[0]);
  await page.getByLabel("Password").fill(KESTREL.viewer[1]);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();
});

for (const [role, account, approvals, admin, upload] of [
  ["viewer", KESTREL.viewer, false, false, false],
  ["clerk", KESTREL.clerk, false, false, true],
  ["approver", KESTREL.approver, true, false, true],
  ["admin", KESTREL.admin, true, true, true],
] as const) {
  test(`${role === "admin" ? "an" : "a"} ${role} sees what that role may use`, async ({ page }) => {
    await signIn(page, account);
    const nav = page.getByRole("navigation");
    await expect(nav.getByRole("link", { name: /Invoices/ })).toBeVisible();
    await expect(nav.getByRole("link", { name: /Approvals/ })).toHaveCount(approvals ? 1 : 0);
    await expect(nav.getByRole("link", { name: /Administration/ })).toHaveCount(admin ? 1 : 0);
    await expect(page.locator('input[type="file"]')).toHaveCount(upload ? 1 : 0);
  });
}
