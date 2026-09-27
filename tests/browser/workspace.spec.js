import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("capture, mark, interrupt, inspect evidence, export and replay", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto("/");
  await expect(page.locator("#empty-state")).toBeVisible();
  await page.locator("#session-name").fill("Réception au banc");
  await page
    .locator("#session-objective")
    .fill("Vérifier les preuves d’une coupure de réception");
  await page.locator("#start-button").click();
  await expect(page.locator("#active-session-title")).toHaveText(
    "Réception au banc",
  );
  await expect
    .poll(async () => Number(await page.locator("#sample-value").textContent()))
    .toBeGreaterThan(5);
  const note = "<img src=x onerror=alert(1)> Observation conservée";
  await page.locator("#annotation-text").fill(note);
  await page.locator("#annotation-form button").click();
  await expect(page.locator("#event-list")).toContainText(note);
  await expect(page.locator("#event-list img")).toHaveCount(0);
  await page.locator("#dropout-button").click();
  await expect(page.locator("#dropout-button")).toBeDisabled();
  await expect(page.locator("#freshness")).toHaveClass(/stale/);
  await page.locator("#annotation-text").fill("Observation pendant la coupure");
  await page.locator("#annotation-form button").click();
  await expect(page.locator("#event-list")).toContainText(
    "Observation pendant la coupure",
  );
  await expect(page.locator("#gap-list button")).toHaveCount(1, {
    timeout: 8_000,
  });
  await page.locator("#stop-button").click();
  await expect(page.locator("#stop-button")).toBeHidden();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#export-button").click(),
  ]);
  const exported = JSON.parse(await readFile(await download.path(), "utf8"));
  expect(exported.session.status).toBe("completed");
  expect(exported.session.sample_count).toBe(exported.samples.length);
  expect(exported.analysis.gaps).toHaveLength(1);
  expect(exported.analysis.gaps[0].duration_s).toBeGreaterThanOrEqual(2);
  const gapNote = exported.events.find(
    (event) => event.text === "Observation pendant la coupure",
  );
  expect(gapNote.at_s).toBeGreaterThan(
    exported.analysis.gaps[0].start_s + 0.25,
  );
  expect(gapNote.at_s).toBeLessThan(exported.analysis.gaps[0].end_s);
  await page.reload();
  await expect(page.locator("#active-session-title")).toHaveText(
    "Réception au banc",
  );
  await expect(page.locator("#event-list")).toContainText(note);
  await page.locator("#gap-list button").click();
  await expect(page.locator("#window-start")).not.toHaveValue("");
  await expect(page.locator("#window-end")).not.toHaveValue("");
  await page.locator("#reset-window").click();
  await page.locator("#replay-cursor").fill("0");
  await page.locator("#play-button").click();
  await expect
    .poll(async () => Number(await page.locator("#replay-cursor").inputValue()))
    .toBeGreaterThan(0);
  await page.locator("#play-button").click();
  await page.screenshot({
    path: "test-results/workspace-desktop.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("mobile layout and backend failure remain usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.locator("#active-session-title")).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: "test-results/workspace-mobile.png",
    fullPage: true,
  });
  await page.route("**/api/sessions**", (route) => route.abort());
  await page.reload();
  await expect(page.locator("#error-message")).toBeVisible();
  await page.unroute("**/api/sessions**");
});

test("live telemetry recovers after a temporary connection failure", async ({
  page,
}) => {
  await page.goto("/");
  await page.locator("#session-name").fill("Reconnexion du navigateur");
  await page
    .locator("#session-objective")
    .fill("Conserver les mesures pendant une perte d’affichage");
  await page.locator("#start-button").click();
  await expect
    .poll(async () => Number(await page.locator("#sample-value").textContent()))
    .toBeGreaterThan(3);
  await page.route("**/api/sessions/*", (route) => route.abort());
  await expect(page.locator("#freshness-text")).toHaveText(
    "Service injoignable",
  );
  await expect(page.locator("#error-message")).toBeVisible();
  await page.unroute("**/api/sessions/*");
  await expect(page.locator("#freshness-text")).toContainText("Flux reçu");
  await expect(page.locator("#error-message")).toBeHidden();
  await page.locator("#stop-button").click();
  await expect(page.locator("#stop-button")).toBeHidden();
});
