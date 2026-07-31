import { expect, test } from "@playwright/test";

test("primary navigation and empty states are usable", async ({ page }) => {
  await page.route("**/v1/**", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [], total: 0 }) }));
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
  for (const name of ["Sources", "Memories", "Search", "Jobs", "Backups", "Settings"]) {
    await page.getByRole("link", { name }).click();
    await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
    await expect(page.getByRole("link", { name })).toHaveAttribute("aria-current", "page");
  }
});

test("destructive settings action requires explicit confirmation", async ({ page }) => {
  await page.route("**/v1/**", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [], total: 0 }) }));
  await page.goto("/settings");
  await page.getByRole("button", { name: "Delete all data" }).click();
  const confirm = page.getByRole("button", { name: "Permanently delete" });
  await expect(confirm).toBeDisabled();
  await page.getByLabel(/Type DELETE ALL/).fill("DELETE ALL");
  await expect(confirm).toBeEnabled();
});
