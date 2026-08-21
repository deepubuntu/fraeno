import { expect, test } from "@playwright/test";

test("loads the product story without layout overflow", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveTitle(/Fraeno/);
  await expect(
    page.getByRole("heading", {
      name: "Catch dangerous robot behavior before deployment.",
    }),
  ).toBeVisible();

  const timing = await page.evaluate(() => {
    const navigation = performance.getEntriesByType("navigation")[0];
    return navigation?.domContentLoadedEventEnd ?? 0;
  });
  expect(timing).toBeLessThan(2_500);

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("opens and closes the access request dialog", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await page.getByRole("button", { name: "Request access" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Request access" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("Name", { exact: true })).toBeFocused();

  await page.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
});

test("opens the access request directly from the private beta link", async ({ page }) => {
  await page.goto("/#access", { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("dialog", { name: "Request access" })).toBeVisible();
  await expect(page.getByLabel("GitHub username")).toBeVisible();
});

test("shows the complete protected admin sign-in surface", async ({ page }) => {
  await page.goto("/admin/", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveTitle("Fraeno operations");
  await expect(page.getByRole("heading", { name: "Admin Login" })).toBeVisible();
  await expect(page.getByLabel("Email Address")).toBeVisible();
  await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign In with Email" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign In with Google" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Forgot password?" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Back to Home" })).toBeVisible();
});
