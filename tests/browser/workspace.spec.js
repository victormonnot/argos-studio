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

async function prepareExperimentReference(page, name) {
  const created = await page.request.post("/api/sessions", {
    data: {
      name,
      objective:
        "Vérifier un effet de réception sur deux acquisitions synthétiques.",
    },
  });
  expect(created.status()).toBe(201);
  const session = await created.json();
  await expect
    .poll(async () => {
      const detail = await (
        await page.request.get(`/api/sessions/${session.id}`)
      ).json();
      return detail.session.sample_count;
    })
    .toBeGreaterThan(4);
  await page.request.post(`/api/sessions/${session.id}/stop`, { data: {} });
  const investigated = await page.request.post(
    `/api/sessions/${session.id}/investigations`,
    { data: {} },
  );
  expect(investigated.status()).toBe(201);
  return { session, report: await investigated.json() };
}

test("an explicit synthetic experiment preserves both captures and links its comparison to the reference", async ({
  page,
}) => {
  test.setTimeout(45_000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const { session, report } = await prepareExperimentReference(
    page,
    "Référence de l’essai comparatif",
  );
  let experiment;
  try {
    await page.goto("/");
    await expect(page.locator("#investigation-history")).toHaveValue(report.id);
    await expect(page.locator("#experiment-prepare")).toBeEnabled();
    const [prepared] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().endsWith(`/investigations/${report.id}/experiments`) &&
          response.request().method() === "POST",
      ),
      page.locator("#experiment-prepare").click(),
    ]);
    expect(prepared.status()).toBe(201);
    experiment = await prepared.json();
    expect(experiment.status).toBe("proposed");
    expect(experiment.control_session_id).toBeNull();
    await expect(page.locator("#experiment-state")).toHaveText(
      "Protocole prêt · lancement à confirmer",
    );
    await expect(page.locator("#experiment-phases")).toContainText("Témoin");
    await expect(page.locator("#experiment-phases")).toContainText("20,00 s");
    await expect(page.locator("#active-experiment")).toBeHidden();
    await expect(page.locator("#experiment-start")).toBeEnabled();
    await page.locator("#experiment-start").click();
    await expect(page.locator("#active-experiment")).toBeVisible();
    await expect(page.locator("#active-experiment-state")).toContainText(
      "1 / 2",
    );
    await expect(page.locator("#start-button")).toBeDisabled();
    await expect(page.locator("#experiment-session-links button")).toHaveCount(
      1,
    );
    await page.locator("#experiment-session-links button").first().click();
    await expect(page.locator("#active-session-title")).toContainText("Témoin");
    await expect(page.locator("#dropout-button")).toBeDisabled();
    await expect(page.locator("#stop-button")).toHaveText(
      "Annuler l’expérience",
    );
    await page.locator("#active-experiment-open").click();
    await expect(page.locator("#active-session-title")).toHaveText(
      session.name,
    );
    await expect(page.locator("#active-experiment-state")).toContainText(
      "2 / 2",
      { timeout: 9000 },
    );
    await expect(page.locator("#experiment-session-links button")).toHaveCount(
      2,
    );
    await expect(page.locator("#experiment-state")).toHaveText(
      "Expérience terminée",
      { timeout: 14000 },
    );
    await expect(page.locator("#active-experiment")).toBeHidden();
    await expect(page.locator("#start-button")).toBeEnabled();
    await expect(page.locator("#experiment-outcome")).toHaveText(
      "Effet de l’intervention observé",
    );
    await expect(page.locator("#experiment-checks .check-unmet")).toHaveCount(
      0,
    );
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#experiment-export").click(),
    ]);
    const exported = JSON.parse(await readFile(await download.path(), "utf8"));
    expect(exported.id).toBe(experiment.id);
    expect(exported.status).toBe("completed");
    expect(exported.result.outcome).toBe("supported");
    expect(exported.result.reference.report_id).toBe(report.id);
    expect(exported.result.reference.snapshot_sha256).toBe(
      report.snapshot.sha256,
    );
    expect(exported.result.control.gap_count).toBe(0);
    expect(exported.result.perturbed.gap_count).toBe(1);
    await page.locator("#experiment-gap").click();
    await expect(page.locator("#active-session-title")).toContainText(
      "Interruption",
    );
    const gap = exported.result.intervention.gap;
    await expect(page.locator("#window-start")).toHaveValue(
      String(gap.start_s),
    );
    await expect(page.locator("#window-end")).toHaveValue(String(gap.end_s));
    await expect(page.locator("#experiment-history")).toHaveValue(
      experiment.id,
    );
    await page.reload();
    await expect(page.locator("#experiment-history")).toHaveValue(
      experiment.id,
    );
    await expect(page.locator("#experiment-outcome")).toHaveText(
      "Effet de l’intervention observé",
    );
    await page.locator("#experiment-origin").click();
    await expect(page.locator("#active-session-title")).toHaveText(
      session.name,
    );
    await expect(page.locator("#investigation-history")).toHaveValue(report.id);
    await page
      .locator(".comparison-panel")
      .screenshot({ path: "test-results/experiment-desktop.png" });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(390);
    await page
      .locator(".comparison-panel")
      .screenshot({ path: "test-results/experiment-mobile.png" });
    expect(errors).toEqual([]);
  } finally {
    if (experiment)
      await page.request.post(`/api/experiments/${experiment.id}/cancel`, {
        data: {},
      });
  }
});

test("experiment cancellation remains available after navigation and reload without restarting the protocol", async ({
  page,
}) => {
  const { session, report } = await prepareExperimentReference(
    page,
    "Référence de l’essai annulé",
  );
  let experiment;
  try {
    await page.goto("/");
    await expect(page.locator("#investigation-history")).toHaveValue(report.id);
    const [prepared] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.url().endsWith(`/investigations/${report.id}/experiments`) &&
          response.request().method() === "POST",
      ),
      page.locator("#experiment-prepare").click(),
    ]);
    experiment = await prepared.json();
    await page.locator("#experiment-start").click();
    await expect(page.locator("#active-experiment")).toBeVisible();
    await page.reload();
    await expect(page.locator("#active-experiment")).toBeVisible();
    await expect(page.locator("#active-experiment-cancel")).toBeEnabled();
    const sessions = await (await page.request.get("/api/sessions")).json();
    const unrelated = sessions.find(
      (item) => item.id !== session.id && item.status !== "live",
    );
    expect(unrelated).toBeDefined();
    await page
      .locator(".session-item")
      .filter({ hasText: unrelated.name })
      .click();
    await expect(page.locator("#active-experiment")).toBeVisible();
    await page.locator("#active-experiment-cancel").click();
    await expect(page.locator("#active-experiment")).toBeHidden();
    const stopped = await (
      await page.request.get(`/api/experiments/${experiment.id}`)
    ).json();
    expect(stopped.status).toBe("cancelled");
    expect(stopped.perturbed_session_id).toBeNull();
    const capture = await (
      await page.request.get(`/api/sessions/${stopped.control_session_id}`)
    ).json();
    expect(capture.session.status).toBe("interrupted");
    await page
      .locator(".session-item")
      .filter({ hasText: session.name })
      .click();
    await expect(page.locator("#experiment-state")).toHaveText(
      "Expérience annulée",
    );
    await expect(page.locator("#experiment-start")).toBeHidden();
    await expect(page.locator("#experiment-status")).toContainText(
      "Aucun redémarrage automatique",
    );
    await page.reload();
    await page
      .locator(".session-item")
      .filter({ hasText: session.name })
      .click();
    await expect(page.locator("#experiment-history")).toHaveValue(
      experiment.id,
    );
    await expect(page.locator("#experiment-state")).toHaveText(
      "Expérience annulée",
    );
    await expect(page.locator("#active-experiment")).toBeHidden();
    await expect(page.locator("#start-button")).toBeEnabled();
    const recovered = await (
      await page.request.get(`/api/experiments/${experiment.id}`)
    ).json();
    expect(recovered.perturbed_session_id).toBeNull();
    expect(recovered.result).toBeNull();
  } finally {
    if (experiment)
      await page.request.post(`/api/experiments/${experiment.id}/cancel`, {
        data: {},
      });
  }
});

// These routes are explicit UI protocol doubles, never a real model response.
async function installAgentUiDouble(page) {
  const fixture = {
    runs: new Map(),
    requests: [],
    onStart: null,
    active() {
      return [...this.runs.values()].find((run) => run.status === "running");
    },
  };
  await page.route("**/api/health", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const active = fixture.active();
    body.agent = {
      available: true,
      provider: "test-double",
      model: "scripted-browser-fixture",
      reason: null,
      sends_data_off_machine: true,
      active_run: active
        ? { id: active.id, session_id: active.session_id }
        : null,
      limits: {
        max_rounds: 6,
        max_tool_calls: 8,
        max_output_tokens: 2000,
        max_total_output_tokens: 6000,
        deadline_s: 90,
      },
    };
    await route.fulfill({ response, json: body });
  });
  await page.route("**/api/sessions/*/agent-runs**", async (route) => {
    const match = new URL(route.request().url()).pathname.match(
      /^\/api\/sessions\/([^/]+)\/agent-runs(?:\/([^/]+))?(?:\/(cancel|export))?$/,
    );
    if (!match) return route.fallback();
    const [, sessionId, id, action] = match;
    const method = route.request().method();
    if (method === "POST" && !id) {
      const body = route.request().postDataJSON();
      fixture.requests.push({ sessionId, body });
      const run = {
        id: `test-double-run-${fixture.runs.size + 1}`,
        session_id: sessionId,
        prompt: body.prompt,
        provider: "test-double",
        model: "scripted-browser-fixture",
        status: "running",
        created_at: Date.now() / 1000,
        ended_at: null,
        context: {
          window_s: { start_s: body.start_s ?? 0, end_s: body.end_s ?? 0.3 },
        },
        answer: null,
        error: null,
        usage: null,
        steps: [],
      };
      fixture.runs.set(run.id, run);
      if (fixture.onStart) await fixture.onStart(run);
      await route.fulfill({ status: 202, json: run });
    } else if (!id) {
      await route.fulfill({
        json: [...fixture.runs.values()]
          .filter((run) => run.session_id === sessionId)
          .reverse(),
      });
    } else {
      const run = fixture.runs.get(id);
      if (!run || run.session_id !== sessionId)
        return route.fulfill({
          status: 404,
          json: { detail: "Test fixture missing" },
        });
      if (method === "POST" && action === "cancel") {
        run.status = "cancelled";
        run.ended_at = Date.now() / 1000;
      }
      await route.fulfill({ json: run });
    }
  });
  return fixture;
}

test("an unconfigured agent stays disabled while reception tools remain available", async ({
  page,
}) => {
  await prepareExperimentReference(page, "Agent non configuré");
  await page.goto("/");
  await expect(page.locator("#agent-provider")).toHaveText(
    "Fournisseur non configuré",
  );
  await expect(page.locator("#agent-submit")).toBeDisabled();
  await expect(page.locator("#agent-disclosure")).toContainText(
    "Aucune requête au modèle",
  );
  await expect(page.locator("#investigate-button")).toBeEnabled();
  await expect(page.locator("#source-notice")).not.toContainText(
    "modèle de langage actif",
  );
});

test("agent UI test-double keeps tool evidence navigable and model text inert across export and reload", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const { session, report } = await prepareExperimentReference(
    page,
    "Preuves de l’agent · doublure de test",
  );
  const prepared = await page.request.post(
    `/api/sessions/${session.id}/investigations/${report.id}/experiments`,
    { data: {} },
  );
  expect(prepared.status()).toBe(201);
  const experiment = await prepared.json();
  const fixture = await installAgentUiDouble(page);
  const unsafeText =
    "<img src=x onerror=alert(1)> [commande](javascript:alert(1))";
  fixture.onStart = async (run) => {
    run.status = "completed";
    run.ended_at = Date.now() / 1000;
    run.answer = `Doublure de test, aucun modèle appelé.\n${unsafeText}`;
    run.context.window_s = report.window_s;
    run.usage = { input_tokens: 12, output_tokens: 8 };
    run.steps = [
      {
        seq: 0,
        kind: "tool_call",
        payload: {
          call_id: "fixture-call",
          name: "investigate_reception",
          arguments: {},
        },
      },
      {
        seq: 1,
        kind: "tool_result",
        payload: {
          call_id: "fixture-call",
          name: "investigate_reception",
          ok: true,
          result: {
            id: report.id,
            session_id: session.id,
            window_s: report.window_s,
          },
        },
      },
      {
        seq: 2,
        kind: "tool_result",
        payload: {
          call_id: "fixture-proposal",
          name: "prepare_synthetic_experiment",
          ok: true,
          result: experiment,
        },
      },
    ];
  };
  try {
    await page.goto("/");
    await expect(page.locator("#agent-provider")).toContainText("test-double");
    await expect(page.locator("#agent-disclosure")).toContainText(
      "transmis à test-double",
    );
    await page.locator("#agent-prompt").fill(unsafeText);
    await page.locator("#agent-submit").click();
    await expect(page.locator("#agent-run-state")).toHaveText(
      "Investigation terminée",
    );
    await expect(page.locator("#agent-answer")).toContainText(unsafeText);
    await expect(
      page.locator("#agent-answer img, #agent-answer a, #agent-run-prompt img"),
    ).toHaveCount(0);
    await expect(page.locator("#agent-evidence button")).toHaveCount(3);
    await page
      .locator("#agent-evidence button")
      .filter({ hasText: "Mesures :" })
      .click();
    await expect(page.locator("#window-start")).toHaveValue(
      String(report.window_s.start_s),
    );
    await expect(page.locator("#window-end")).toHaveValue(
      String(report.window_s.end_s),
    );
    await page
      .locator("#agent-evidence button")
      .filter({ hasText: "Ouvrir l’investigation" })
      .click();
    await expect(page.locator("#investigation-history")).toHaveValue(report.id);
    await page
      .locator("#agent-evidence button")
      .filter({ hasText: "Examiner le protocole" })
      .click();
    await expect(page.locator("#experiment-history")).toHaveValue(
      experiment.id,
    );
    await expect(page.locator("#experiment-start")).toBeEnabled();
    const untouched = await (
      await page.request.get(`/api/experiments/${experiment.id}`)
    ).json();
    expect(untouched.status).toBe("proposed");
    expect(untouched.control_session_id).toBeNull();
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.locator("#agent-export").click(),
    ]);
    const exported = JSON.parse(await readFile(await download.path(), "utf8"));
    expect(exported.provider).toBe("test-double");
    expect(exported.answer).toContain(unsafeText);
    expect(exported.steps[1].payload.result.id).toBe(report.id);
    await page.reload();
    await expect(page.locator("#agent-history")).toHaveValue(exported.id);
    await expect(page.locator("#agent-answer")).toContainText(unsafeText);
    await page.locator(".agent-trace > summary").click();
    await expect(page.locator("#agent-steps")).toContainText(
      "investigate_reception",
    );
    await page
      .locator(".agent-panel")
      .screenshot({ path: "test-results/agent-desktop.png" });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(390);
    await page
      .locator(".agent-panel")
      .screenshot({ path: "test-results/agent-mobile.png" });
    expect(errors).toEqual([]);
  } finally {
    await page.request.post(`/api/experiments/${experiment.id}/cancel`, {
      data: {},
    });
  }
});

test("agent UI test-double retains scoped drafts and global cancellation through navigation and reload", async ({
  page,
}) => {
  const { session: first } = await prepareExperimentReference(
    page,
    "Agent actif · doublure de test",
  );
  const { session: second } = await prepareExperimentReference(
    page,
    "Autre session de l’agent",
  );
  const fixture = await installAgentUiDouble(page);
  await page.goto("/");
  await page.locator("#agent-prompt").fill("Brouillon de l’autre session");
  await page.locator(".session-item").filter({ hasText: first.name }).click();
  await expect(page.locator("#agent-prompt")).toHaveValue("");
  await page.locator("#agent-prompt").fill("Demande de la session active");
  await page.locator("#agent-submit").click();
  await expect(page.locator("#active-agent")).toBeVisible();
  await expect(page.locator("#agent-export")).toBeDisabled();
  await page.locator(".session-item").filter({ hasText: second.name }).click();
  await expect(page.locator("#agent-prompt")).toHaveValue(
    "Brouillon de l’autre session",
  );
  await expect(page.locator("#agent-detail")).toBeHidden();
  await expect(page.locator("#active-agent")).toBeVisible();
  await page.reload();
  await expect(page.locator("#active-agent")).toBeVisible();
  let releaseStalePoll;
  let pollHeld = false;
  const stalePollGate = new Promise((resolve) => {
    releaseStalePoll = resolve;
  });
  const activeId = fixture.active().id;
  await page.route(
    `**/api/sessions/${first.id}/agent-runs/${activeId}`,
    async (route) => {
      if (pollHeld || route.request().method() !== "GET")
        return route.fallback();
      const stale = structuredClone(fixture.runs.get(activeId));
      pollHeld = true;
      await stalePollGate;
      await route.fulfill({ json: stale });
    },
  );
  await expect.poll(() => pollHeld).toBe(true);
  await page.locator("#active-agent-cancel").click();
  await expect(page.locator("#active-agent")).toBeHidden();
  const staleResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/agent-runs/${activeId}`),
  );
  releaseStalePoll();
  await staleResponse;
  await expect(page.locator("#active-agent")).toBeHidden();
  expect(fixture.active()).toBeUndefined();
  await page.locator(".session-item").filter({ hasText: first.name }).click();
  await expect(page.locator("#agent-run-state")).toHaveText(
    "Investigation annulée",
  );
  await expect(page.locator("#agent-export")).toBeEnabled();
  await page.reload();
  await expect(page.locator("#active-agent")).toBeHidden();
  expect(fixture.requests).toHaveLength(1);
});

test("agent UI test-double does not replace a new session with a delayed launch response", async ({
  page,
}) => {
  const { session: first } = await prepareExperimentReference(
    page,
    "Réponse agent retardée",
  );
  const { session: second } = await prepareExperimentReference(
    page,
    "Session conservée à l’écran",
  );
  const fixture = await installAgentUiDouble(page);
  let release;
  const responseGate = new Promise((resolve) => {
    release = resolve;
  });
  fixture.onStart = async () => responseGate;
  try {
    await page.goto("/");
    await page.locator(".session-item").filter({ hasText: first.name }).click();
    await page.locator("#agent-prompt").fill("Réponse différée");
    await page.locator("#agent-submit").click();
    await expect.poll(() => fixture.requests.length).toBe(1);
    await page
      .locator(".session-item")
      .filter({ hasText: second.name })
      .click();
    await page.locator("#agent-prompt").fill("Conserver ce brouillon");
    release();
    await expect(page.locator("#active-agent")).toBeVisible();
    await expect(page.locator("#active-session-title")).toHaveText(second.name);
    await expect(page.locator("#agent-detail")).toBeHidden();
    await expect(page.locator("#agent-prompt")).toHaveValue(
      "Conserver ce brouillon",
    );
    await page.locator("#active-agent-open").click();
    await expect(page.locator("#active-session-title")).toHaveText(first.name);
    await expect(page.locator("#agent-run-prompt")).toHaveText(
      "Réponse différée",
    );
    await page.locator("#active-agent-cancel").click();
    await expect(page.locator("#active-agent")).toBeHidden();
  } finally {
    release();
  }
});

async function prepareObservationSession(page, name) {
  const created = await page.request.post("/api/sessions", {
    data: { name, objective: "Examiner un intervalle de réception synthétique." },
  });
  expect(created.status()).toBe(201);
  const session = await created.json();
  try {
    await expect.poll(async () => {
      const result = await (await page.request.get(`/api/sessions/${session.id}`)).json();
      return result.session.sample_count;
    }).toBeGreaterThan(4);
    await page.request.post(`/api/sessions/${session.id}/dropout`, { data: {} });
    let observations;
    await expect.poll(async () => {
      const result = await page.request.get(`/api/sessions/${session.id}/observations`);
      expect(result.ok()).toBe(true);
      observations = await result.json();
      return observations.items.length;
    }, { timeout: 8000 }).toBe(1);
    return { session, observation: observations.items[0] };
  } finally {
    await page.request.post(`/api/sessions/${session.id}/stop`, { data: {} });
  }
}

test("local observations preserve measured evidence, reports and dismissal without calling an agent", async ({ page }) => {
  const errors = [];
  const agentRequests = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/agent-runs"))
      agentRequests.push(request.url());
  });
  const { session, observation } = await prepareObservationSession(
    page, "Observation locale de réception synthétique",
  );
  await page.goto("/");
  await expect(page.locator("#observations-count")).toHaveText("1");
  const card = page.locator(`[data-observation-id="${observation.id}"]`);
  await expect(card).toContainText("Simulation synthétique");
  await expect(card).toContainText(`#${observation.before_seq}`);
  await expect(card).toContainText(`#${observation.after_seq}`);
  await card.locator("summary").click();
  await expect(card.locator("pre")).toContainText("before_sample");
  const original = structuredClone(observation);
  await card.locator('[data-action="window"]').click();
  await expect(page.locator("#window-start")).toHaveValue(String(observation.start_s));
  await expect(page.locator("#window-end")).toHaveValue(String(observation.end_s));
  await card.locator('[data-action="investigate"]').click();
  await expect(page.locator("#investigation-report")).toBeVisible();
  const saved = await (await page.request.get(`/api/sessions/${session.id}/observations`)).json();
  const reportId = saved.items[0].investigation_id;
  expect(reportId).toBeTruthy();
  await expect(page.locator("#investigation-history")).toHaveValue(reportId);
  await expect(card.locator("details")).toHaveAttribute("open", "");
  await expect(card.locator('[data-action="investigate"]')).toHaveText("Ouvrir le rapport conservé");
  await card.locator('[data-action="investigate"]').click();
  const reports = await (await page.request.get(`/api/sessions/${session.id}/investigations`)).json();
  expect(reports).toHaveLength(1);
  expect(saved.items[0].evidence).toEqual(original.evidence);
  expect(saved.items[0].duration_s).toBe(original.duration_s);
  await card.locator('[data-action="agent"]').click();
  await expect(page.locator("#agent-prompt")).toHaveValue(new RegExp(observation.id));
  await expect(page.locator("#agent-prompt")).toHaveValue(new RegExp(reportId));
  await expect(page.locator("#agent-observation")).toContainText(observation.id);
  await expect(page.locator("#agent-submit")).toBeDisabled();
  expect(agentRequests).toEqual([]);
  await card.locator('[data-action="dismiss"]').click();
  await expect(page.locator("#observations-count")).toHaveText("0");
  await expect(card).toBeHidden();
  await page.reload();
  await expect(page.locator("#observations-count")).toHaveText("0");
  await page.locator("#observations-show-dismissed").check();
  await expect(card).toBeVisible();
  await expect(card).toContainText("Écartée");
  await card.locator('[data-action="reopen"]').click();
  await expect(page.locator("#observations-count")).toHaveText("1");
  await page.reload();
  await expect(card).toBeVisible();
  await expect(card.locator('[data-action="investigate"]')).toHaveText("Ouvrir le rapport conservé");
  const restored = await (await page.request.get(`/api/sessions/${session.id}/observations`)).json();
  expect(restored.items).toHaveLength(1);
  expect(restored.items[0].id).toBe(original.id);
  expect(restored.items[0].evidence).toEqual(original.evidence);
  await page.locator(".observations-panel").screenshot({ path: "test-results/observations-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.locator(".observations-panel").screenshot({ path: "test-results/observations-mobile.png" });
  expect(errors).toEqual([]);
  expect(agentRequests).toEqual([]);
});

test("an observation draft is linked only while its exact measurement window remains selected", async ({ page }) => {
  const { session, observation } = await prepareObservationSession(page, "Question issue d’une observation · doublure");
  const fixture = await installAgentUiDouble(page);
  fixture.onStart = async (run) => {
    run.status = "completed";
    run.answer = "Doublure de protocole navigateur : aucun modèle appelé.";
    run.ended_at = Date.now() / 1000;
  };
  await page.goto("/");
  const card = page.locator(`[data-observation-id="${observation.id}"]`);
  await card.locator('[data-action="agent"]').click();
  expect(fixture.requests).toHaveLength(0);
  await page.locator("#agent-submit").click();
  await expect.poll(() => fixture.requests.length).toBe(1);
  expect(fixture.requests[0]).toMatchObject({ sessionId: session.id, body: {
    observation_id: observation.id, start_s: observation.start_s, end_s: observation.end_s,
  } });
  await expect(page.locator("#agent-submit")).toBeEnabled();
  await page.locator("#window-start").fill("0");
  await page.locator("#window-form").getByRole("button", { name: "Appliquer" }).click();
  await expect(page.locator("#agent-observation")).toBeHidden();
  await page.locator("#agent-prompt").fill("Une autre question portant sur la fenêtre élargie.");
  await page.locator("#agent-submit").click();
  await expect.poll(() => fixture.requests.length).toBe(2);
  expect(fixture.requests[1].body).not.toHaveProperty("observation_id");
});

test("a delayed observation investigation preserves navigation and stale polls cannot undo dismissal", async ({ page }) => {
  const { session: first, observation } = await prepareObservationSession(page, "Observation avec réponse retardée");
  const { session: second } = await prepareExperimentReference(page, "Session conservée pendant l’investigation locale");
  let release;
  let received = false;
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route(`**/api/sessions/${first.id}/observations/${observation.id}/investigate`, async (route) => {
    const response = await route.fetch();
    received = true;
    await gate;
    await route.fulfill({ response });
  });
  try {
    await page.goto("/");
    await page.locator(".session-item").filter({ hasText: first.name }).click();
    const card = page.locator(`[data-observation-id="${observation.id}"]`);
    await card.locator('[data-action="investigate"]').click();
    await expect.poll(() => received).toBe(true);
    await page.locator(".session-item").filter({ hasText: second.name }).click();
    const pending = page.waitForResponse((response) => response.url().endsWith(`/${observation.id}/investigate`));
    release();
    await pending;
    await expect(page.locator("#active-session-title")).toHaveText(second.name);
    await expect(page.locator("#observations-count")).toHaveText("0");
    await page.locator(".session-item").filter({ hasText: first.name }).click();
    await expect(card.locator('[data-action="investigate"]')).toHaveText("Ouvrir le rapport conservé");

    let releasePoll;
    let held = false;
    const pollGate = new Promise((resolve) => { releasePoll = resolve; });
    await page.route(`**/api/sessions/${first.id}/observations`, async (route) => {
      if (held || route.request().method() !== "GET") return route.fallback();
      const response = await route.fetch();
      held = true;
      await pollGate;
      await route.fulfill({ response });
    });
    try {
      await expect.poll(() => held).toBe(true);
      await card.locator('[data-action="dismiss"]').click();
      await expect(page.locator("#observations-count")).toHaveText("0");
      const stale = page.waitForResponse((response) => response.url().endsWith(`/${first.id}/observations`));
      releasePoll();
      await stale;
      await expect(page.locator("#observations-count")).toHaveText("0");
      await expect(card).toBeHidden();
    } finally { releasePoll(); }
  } finally { release(); }
});

test("observation UI protocol fixture exposes scan backlog, omitted intervals and monitor failure", async ({ page }) => {
  const { session } = await prepareExperimentReference(page, "Progression du repérage · doublure de protocole");
  const fixture = {
    items: [],
    scan: { last_seq: 100, last_sample_seq: 1200, complete: false, omitted_gap_count: 7 },
    monitor: {
      available: false,
      max_per_session: 20,
      last_error: "Repérage temporairement indisponible · doublure de protocole.",
    },
  };
  await page.route(`**/api/sessions/${session.id}/observations`, (route) =>
    route.fulfill({ json: fixture }),
  );
  await page.goto("/");
  await expect(page.locator("#observations-scan")).toContainText("échantillon #100 sur #1200");
  await expect(page.locator("#observations-scan")).toContainText("7 intervalle(s) supplémentaire(s) non conservé(s)");
  await expect(page.locator("#observations-scan")).toContainText("Suivi automatique indisponible");
  await expect(page.locator("#observations-empty")).toContainText("en cours");
  fixture.scan.complete = true;
  fixture.scan.last_seq = 1200;
  fixture.monitor.available = true;
  fixture.monitor.last_error = null;
  await expect(page.locator("#observations-scan")).toContainText("parcourues jusqu’à l’échantillon #1200");
  await expect(page.locator("#observations-scan")).not.toContainText("indisponible");
  await expect(page.locator("#observations-scan")).toContainText("7 intervalle(s) supplémentaire(s)");
});
