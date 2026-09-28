import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { createSocket } from "node:dgram";

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

async function unusedLoopbackPort() {
  const socket = createSocket("udp4");
  try {
    await new Promise((resolve, reject) => {
      socket.once("error", reject);
      socket.bind(0, "127.0.0.1", resolve);
    });
    return socket.address().port;
  } finally {
    socket.close();
  }
}

test("passive MAVLink waits, receives, ages and preserves capture for replay", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const port = await unusedLoopbackPort();
  let sender;
  let finished;
  let sessionId;
  try {
    await page.goto("/");
    await expect(
      page.locator('#source-kind option[value="mavlink-udp"]'),
    ).toBeEnabled();
    await page.locator("#source-kind").selectOption("mavlink-udp");
    await expect(page.locator("#mavlink-settings")).toBeVisible();
    await expect(page.locator("#listen-host")).toHaveValue("127.0.0.1");
    await page.locator("#listen-port").fill(String(port));
    await page
      .locator("#session-name")
      .fill("MAVLink synthétique via UDP local");
    await page
      .locator("#session-objective")
      .fill(
        "Conserver les réceptions et distinguer écoute, présence et fraîcheur ATTITUDE",
      );
    await page.locator("#start-button").click();
    await expect(page.locator("#source-badge")).toHaveText("MAVLink UDP");
    await expect(page.locator("#receiver-status")).toHaveText(
      "Écoute ouverte · en attente",
    );
    await expect(page.locator("#receiver-identity")).toHaveText(
      "Système 1 · composant 1",
    );
    await expect(page.locator("#heartbeat-age")).toHaveText("Jamais observé");
    await expect(page.locator("#sample-value")).toHaveText("0");
    await expect(page.locator("#experiment-panel")).toBeHidden();
    await expect(page.locator("#capture-button")).toBeDisabled();
    await expect(page.locator("#source-notice")).toContainText(
      "non authentifiée",
    );
    const sessions = await (await page.request.get("/api/sessions")).json();
    sessionId = sessions.find((session) => session.status === "live").id;

    sender = spawn(
      ".venv/bin/python",
      [
        "tests/browser/emit_mavlink.py",
        "--port",
        String(port),
        "--duration",
        "2",
      ],
      {
        env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let stderr = "";
    sender.stderr.on("data", (data) => {
      stderr += data;
    });
    finished = new Promise((resolve) => {
      sender.once("error", (error) =>
        resolve({ code: -1, error: error.message }),
      );
      sender.once("close", (code) => resolve({ code, error: stderr }));
    });
    await expect(page.locator("#receiver-status")).toHaveText(
      "Réception en cours",
    );
    await expect(page.locator("#freshness-text")).toContainText(
      "ATTITUDE reçue",
    );
    await expect(page.locator("#heartbeat-age")).toContainText("Reçu il y a");
    await expect(page.locator("#receiver-peer")).toContainText("127.0.0.1:");
    await expect
      .poll(async () =>
        Number(await page.locator("#sample-value").textContent()),
      )
      .toBeGreaterThan(5);
    await expect(page.locator("#capture-rejected")).toHaveText("1 / 0");
    await expect(page.locator("#capture-foreign")).toHaveText("1 / 0");
    expect(await finished).toEqual({ code: 0, error: "" });
    await expect(page.locator("#freshness-text")).toContainText(
      "ATTITUDE périmée",
    );
    await expect(page.locator("#receiver-status")).toHaveText(
      "Écoute ouverte · réception périmée",
      { timeout: 5000 },
    );
    await expect(page.locator("#heartbeat-age")).toContainText("Périmé");
    await expect(page.locator("#stop-button")).toBeVisible();
    await page
      .locator("#annotation-text")
      .fill("Émetteur de test arrêté ; écoute encore ouverte");
    await page.locator("#annotation-form button").click();
    await expect(page.locator("#event-list")).toContainText(
      "Émetteur de test arrêté ; écoute encore ouverte",
    );
    await page.locator("#stop-button").click();
    await expect(page.locator("#receiver-status")).toHaveText(
      "Écoute arrêtée · rejeu",
    );
    await expect(page.locator("#capture-button")).toBeEnabled();
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#capture-button").click(),
    ]);
    const capture = JSON.parse(await readFile(await download.path(), "utf8"));
    expect(capture.session.source).toBe("mavlink-udp");
    expect(capture.session.metadata.environment).toBe("simulation");
    expect(capture.capture.datagram_count).toBe(capture.datagrams.length);
    expect(capture.capture.dispositions.invalid).toBe(1);
    expect(capture.capture.dispositions.foreign_source).toBe(1);
    expect(
      capture.datagrams.every(
        (datagram) => Buffer.from(datagram.raw_base64, "base64").length > 0,
      ),
    ).toBe(true);
    const count = await page.locator("#capture-count").textContent();
    await page.reload();
    await expect(page.locator("#capture-count")).toHaveText(count);
    await expect(page.locator("#source-badge")).toHaveText("MAVLink UDP");
    await expect(page.locator("#receiver-status")).toHaveText(
      "Écoute arrêtée · rejeu",
    );
    await expect(page.locator("#experiment-panel")).toBeHidden();
    await page.screenshot({
      path: "test-results/mavlink-desktop.png",
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator("#source-kind").selectOption("mavlink-udp");
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(390);
    await page.screenshot({
      path: "test-results/mavlink-mobile.png",
      fullPage: true,
    });
    expect(errors).toEqual([]);
  } finally {
    if (sender && sender.exitCode === null) sender.kill("SIGTERM");
    if (finished) await finished;
    if (sessionId) {
      const detail = await (
        await page.request.get(`/api/sessions/${sessionId}`)
      ).json();
      if (detail.session.status === "live")
        await page.request.post(`/api/sessions/${sessionId}/stop`, {
          data: {},
        });
    }
  }
});

test("a reception investigation freezes live evidence, preserves context and reopens exact windows", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let sessionId;
  try {
    await page.goto("/");
    await page.locator("#source-kind").selectOption("simulation");
    await page
      .locator("#session-name")
      .fill("Investigation de réception synthétique");
    await page
      .locator("#session-objective")
      .fill(
        "Relier une interruption aux observations et conserver le raisonnement",
      );
    await page.locator("#start-button").click();
    await expect
      .poll(async () =>
        Number(await page.locator("#sample-value").textContent()),
      )
      .toBeGreaterThan(5);
    const sessions = await (await page.request.get("/api/sessions")).json();
    sessionId = sessions.find((session) => session.status === "live").id;
    const context =
      '<img src=x onerror="alert(1)"> Vérifier la coupure, sans conclure à une perte de paquets.';
    await page.locator("#investigation-context").fill(context);
    await page.locator("#dropout-button").click();
    await expect(page.locator("#gap-list button")).toHaveCount(1, {
      timeout: 8000,
    });
    const [response] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response
            .url()
            .endsWith(`/api/sessions/${sessionId}/investigations`) &&
          response.request().method() === "POST",
      ),
      page.locator("#investigate-button").click(),
    ]);
    expect(response.status()).toBe(201);
    const report = await response.json();
    await expect(page.locator("#investigation-report")).toBeVisible();
    await expect(page.locator("#report-summary")).toHaveText(report.summary);
    await expect(page.locator("#report-version")).toHaveText(
      "reception-quality/1",
    );
    await expect(page.locator("#report-context-text")).toHaveText(context);
    await expect(page.locator("#investigation-report img")).toHaveCount(0);
    const frozenLabel = await page.locator("#report-snapshot").textContent();
    const draft = "Hypothèse à affiner au prochain passage";
    await page.locator("#investigation-context").fill(draft);
    await expect
      .poll(async () =>
        Number(await page.locator("#sample-value").textContent()),
      )
      .toBeGreaterThan(report.snapshot.sample_count + 5);
    await expect(page.locator("#report-snapshot")).toHaveText(frozenLabel);
    await expect(page.locator("#investigation-context")).toHaveValue(draft);
    await expect(page.locator("#investigation-history")).toHaveValue(report.id);

    const referencedIds = report.findings.flatMap(
      (finding) => finding.evidence,
    );
    const evidenceIndex = report.evidence.findIndex(
      (item) => referencedIds.includes(item.id) && item.window_s,
    );
    expect(evidenceIndex).toBeGreaterThanOrEqual(0);
    const evidence = report.evidence[evidenceIndex];
    await page
      .getByRole("button", { name: `Preuve · ${evidence.title}`, exact: true })
      .first()
      .click();
    await expect(page.locator("#window-start")).toHaveValue(
      String(evidence.window_s.start_s),
    );
    await expect(page.locator("#window-end")).toHaveValue(
      String(evidence.window_s.end_s),
    );
    await expect(
      page.locator(`#report-evidence-${evidenceIndex}`),
    ).toHaveAttribute("open", "");
    await expect(
      page.locator(`#report-evidence-${evidenceIndex} pre`),
    ).toHaveText(JSON.stringify(evidence.data, null, 2));
    await expect(page.locator("#report-snapshot")).toHaveText(frozenLabel);
    await page.locator("#stop-button").click();
    await expect(page.locator("#stop-button")).toBeHidden();
    await page.reload();
    await expect(page.locator("#investigation-history")).toHaveValue(report.id);
    await expect(page.locator("#report-summary")).toHaveText(report.summary);
    await expect(page.locator("#report-context-text")).toHaveText(context);
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#investigation-export").click(),
    ]);
    const exported = JSON.parse(await readFile(await download.path(), "utf8"));
    expect(exported).toEqual(report);
    await page.locator(".report-context summary").click();
    await page.locator("#reuse-report-context").click();
    await expect(page.locator("#investigation-context")).toHaveValue(context);
    await page
      .locator(".investigation-panel")
      .screenshot({ path: "test-results/investigation-desktop.png" });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(390);
    await page
      .locator(".investigation-panel")
      .screenshot({ path: "test-results/investigation-mobile.png" });
    expect(errors).toEqual([]);
  } finally {
    if (sessionId) {
      const detail = await (
        await page.request.get(`/api/sessions/${sessionId}`)
      ).json();
      if (detail.session.status === "live")
        await page.request.post(`/api/sessions/${sessionId}/stop`, {
          data: {},
        });
    }
  }
});

test("late investigation responses cannot replace another window or session selection", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator("#active-session-title")).toHaveText(
    "Investigation de réception synthétique",
  );
  await expect(page.locator("#investigation-report")).toBeVisible();
  const previousId = await page.locator("#investigation-history").inputValue();
  const previousSummary = await page.locator("#report-summary").textContent();
  const sessions = await (await page.request.get("/api/sessions")).json();
  const sessionId = sessions.find(
    (session) => session.name === "Investigation de réception synthétique",
  ).id;
  let releasePost;
  let postFetched;
  const postResponseReady = new Promise((resolve) => {
    postFetched = resolve;
  });
  const postGate = new Promise((resolve) => {
    releasePost = resolve;
  });
  const collectionUrl = `**/api/sessions/${sessionId}/investigations`;
  await page.route(collectionUrl, async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    postFetched();
    await postGate;
    await route.fulfill({ response });
  });
  try {
    await page
      .locator("#investigation-context")
      .fill("Rapport demandé avant changement de fenêtre");
    await page.locator("#investigate-button").click();
    await postResponseReady;
    await page.locator("#window-start").fill("0.123456789");
    await page.locator("#window-end").fill("0.987654321");
    await page
      .locator("#window-form")
      .getByRole("button", { name: "Appliquer" })
      .click();
    await expect(page.locator("#window-start")).toHaveValue("0.123456789");
    releasePost();
    await expect(page.locator("#investigation-status")).toContainText(
      "fenêtre demandée au lancement",
    );
    await expect(page.locator("#investigation-history option")).toHaveCount(3);
    await expect(page.locator("#investigation-history")).toHaveValue(
      previousId,
    );
    await expect(page.locator("#report-summary")).toHaveText(previousSummary);
  } finally {
    releasePost();
    await page.unroute(collectionUrl);
  }

  const otherId = await page
    .locator("#investigation-history option")
    .evaluateAll(
      (options, previous) =>
        options.find((option) => option.value && option.value !== previous)
          .value,
      previousId,
    );
  let releaseGet;
  let getFetched;
  const getResponseReady = new Promise((resolve) => {
    getFetched = resolve;
  });
  const getGate = new Promise((resolve) => {
    releaseGet = resolve;
  });
  const reportUrl = `**/api/sessions/${sessionId}/investigations/${otherId}`;
  await page.route(reportUrl, async (route) => {
    const response = await route.fetch();
    getFetched();
    await getGate;
    await route.fulfill({ response });
  });
  try {
    await page.locator("#investigation-history").selectOption(otherId);
    await getResponseReady;
    await page
      .locator(".session-item")
      .filter({ hasText: "Réception au banc" })
      .click();
    await expect(page.locator("#active-session-title")).toHaveText(
      "Réception au banc",
    );
    await expect(page.locator("#investigation-status")).toContainText(
      "Aucune investigation",
    );
    releaseGet();
    await expect(page.locator("#investigation-report")).toBeHidden();
    await expect(page.locator("#investigation-history option")).toHaveCount(1);
  } finally {
    releaseGet();
    await page.unroute(reportUrl);
  }
});
