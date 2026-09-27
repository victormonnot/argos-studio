const $ = (id) => document.getElementById(id);
const state = {
  sessions: [],
  id: null,
  detail: null,
  analysis: null,
  health: null,
  start: null,
  end: null,
  generation: 0,
  inFlight: null,
  follow: true,
  cursor: 0,
  playing: false,
  playbackAt: 0,
  busy: new Set(),
  connectionFailed: false,
  chart: null,
};
const number = (value, digits = 1) =>
  Number.isFinite(value)
    ? value.toLocaleString("fr-FR", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })
    : "—";
const seconds = (value) =>
  Number.isFinite(value) ? `${number(value, 2)} s` : "—";
const sourceLabel = (session) =>
  session.source === "simulation"
    ? "Simulation synthétique"
    : session.source === "argos-recording"
      ? "Enregistrement ARGOS"
      : "Source inconnue";
const statusLabel = (session) =>
  ({ live: "En direct", completed: "Terminée", interrupted: "Interrompue" })[
    session.status
  ] || session.status;
const text = (id, value) => {
  $(id).textContent = value ?? "—";
};
const element = (tag, className, content) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
};
const svg = (tag, attributes, content) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes))
    node.setAttribute(key, String(value));
  if (content !== undefined) node.textContent = content;
  return node;
};
function showError(error, origin = "action") {
  $("error-message").dataset.origin = origin;
  text("error-message", error instanceof Error ? error.message : String(error));
  $("error-message").hidden = false;
}
function clearError() {
  $("error-message").hidden = true;
}
let noticeTimer;
function notify(message) {
  clearTimeout(noticeTimer);
  text("success-message", message);
  $("success-message").hidden = false;
  noticeTimer = setTimeout(() => {
    $("success-message").hidden = true;
  }, 6000);
}
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { cache: "no-store", ...options });
  } catch {
    throw new Error(
      "Le service local est injoignable. Les mesures affichées ne sont plus actualisées.",
    );
  }
  if (!response.ok) {
    let message = `La requête a échoué (${response.status}).`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
    } catch {
      /* An HTTP error may not contain JSON. */
    }
    throw new Error(message);
  }
  return response.json();
}
const post = (path, payload = {}) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
function sessionPath(suffix = "", id = state.id) {
  return `/api/sessions/${encodeURIComponent(id)}${suffix}`;
}
function windowQuery() {
  const query = new URLSearchParams();
  if (state.start !== null) query.set("start_s", state.start);
  if (state.end !== null) query.set("end_s", state.end);
  return query.size ? `?${query}` : "";
}
function renderControls() {
  const session = state.detail?.session;
  const live = session?.status === "live";
  const anyLive = state.sessions.some((item) => item.status === "live");
  $("start-button").disabled = anyLive || state.busy.has("start");
  $("start-button").title = anyLive
    ? "Arrêtez la session active avant de démarrer un nouvel essai."
    : "";
  $("stop-button").hidden = !live;
  $("stop-button").disabled = !live || state.busy.has("stop");
  $("dropout-button").disabled =
    !live ||
    session?.source !== "simulation" ||
    state.detail?.live?.gap_active ||
    state.busy.has("dropout");
  $("dropout-button").textContent = state.detail?.live?.gap_active
    ? "Interruption en cours…"
    : "Provoquer une interruption de 2 s";
  $("export-button").disabled = !session || state.busy.has("export");
  $("import-file").disabled =
    !state.health?.argos_import?.available ||
    anyLive ||
    state.busy.has("import");
  $("import-file").title = anyLive
    ? "Arrêtez la session synthétique avant un import."
    : "";
  if (state.health?.argos_import?.available) {
    text(
      "import-status",
      anyLive
        ? "Arrêtez la session active pour importer un enregistrement."
        : "Adaptateur ARGOS disponible. Les données importées sont consultées en rejeu.",
    );
  }
  const hasSamples = !!state.detail?.samples?.length;
  $("play-button").disabled = !hasSamples;
  $("replay-cursor").disabled = !hasSamples;
  $("live-button").disabled = !hasSamples;
}
function renderSessions() {
  text("session-count", state.sessions.length);
  const nodes = state.sessions.map((session) => {
    const button = element(
      "button",
      `session-item${session.id === state.id ? " active" : ""}`,
    );
    button.type = "button";
    button.setAttribute(
      "aria-current",
      session.id === state.id ? "true" : "false",
    );
    button.append(element("span", "session-item-name", session.name));
    const meta = element("span", "session-item-meta");
    meta.append(
      element("span", "", statusLabel(session)),
      element("span", "", `${session.sample_count ?? 0} pts`),
    );
    button.append(
      meta,
      element("span", "session-item-source", sourceLabel(session)),
    );
    button.addEventListener("click", () => selectSession(session.id));
    return button;
  });
  if (!nodes.length)
    nodes.push(element("p", "small muted", "Aucune session enregistrée."));
  $("session-list").replaceChildren(...nodes);
  renderControls();
}
async function loadSessions() {
  state.sessions = await api("/api/sessions");
  renderSessions();
}
async function selectSession(id) {
  pausePlayback();
  state.id = id;
  state.generation += 1;
  state.start = null;
  state.end = null;
  state.follow = true;
  state.detail = null;
  state.analysis = null;
  $("window-start").value = "";
  $("window-end").value = "";
  $("session-workspace").hidden = true;
  $("empty-state").hidden = true;
  clearError();
  renderSessions();
  await refreshSelection();
}
async function refreshSelection() {
  if (!state.id || state.inFlight === state.generation) return;
  const generation = state.generation;
  const id = state.id;
  const query = windowQuery();
  state.inFlight = generation;
  try {
    const detail = await api(sessionPath("", id) + query);
    const analysis = detail.analysis;
    if (generation !== state.generation) return;
    const previousStatus = state.detail?.session.status;
    const selectedId = !state.follow
      ? state.detail?.samples[state.cursor]?.id
      : null;
    state.detail = detail;
    state.analysis = analysis;
    if (
      state.connectionFailed &&
      $("error-message").dataset.origin === "connection"
    )
      clearError();
    state.connectionFailed = false;
    if (state.follow) state.cursor = Math.max(0, detail.samples.length - 1);
    else if (selectedId !== null) {
      const retainedIndex = detail.samples.findIndex(
        (sample) => sample.id === selectedId,
      );
      state.cursor =
        retainedIndex >= 0
          ? retainedIndex
          : Math.min(state.cursor, Math.max(0, detail.samples.length - 1));
    }
    state.sessions = state.sessions.map((session) =>
      session.id === id ? detail.session : session,
    );
    $("empty-state").hidden = true;
    $("session-workspace").hidden = false;
    renderSession();
    renderSessions();
    if (previousStatus === "live" && detail.session.status !== "live")
      notify("Session arrêtée. Les mesures restent disponibles en rejeu.");
  } catch (error) {
    if (generation !== state.generation) return;
    state.connectionFailed = true;
    showError(error, "connection");
    renderFreshness();
  } finally {
    if (state.inFlight === generation) state.inFlight = null;
  }
}
function renderSession() {
  const { session, samples, events } = state.detail;
  const simulated = session.source === "simulation";
  text("active-session-title", session.name);
  text("active-objective", session.objective || "Aucun objectif renseigné.");
  text("source-badge", sourceLabel(session));
  $("source-badge").classList.toggle("imported", !simulated);
  text("session-status", statusLabel(session));
  text(
    "source-notice",
    simulated
      ? "Signal synthétique de développement ; aucune physique d’autopilote simulée. Aucun matériel connecté, aucun modèle de langage actif."
      : `Rejeu d’un fichier ARGOS. Origine déclarée : ${session.metadata?.environment === "simulation" ? "simulation" : session.metadata?.environment === "real" ? "matériel (déclaré)" : "non établie"}. Aucun flux matériel actif ; les horloges originales restent distinctes.`,
  );
  text("provenance-id", session.id);
  text("provenance-source", sourceLabel(session));
  text("provenance-created", formatDate(session.created_at));
  text("provenance-metadata", JSON.stringify(session.metadata ?? {}, null, 2));
  text("sample-value", samples.length.toLocaleString("fr-FR"));
  text(
    "sample-scope",
    `sur ${session.sample_count ?? samples.length} conservés`,
  );
  text(
    "footer-status",
    `${statusLabel(session)} · ${seconds(state.detail.live?.elapsed_s ?? session.elapsed_s)}`,
  );
  renderFreshness();
  renderChart();
  renderCursor();
  renderEvents(events);
  renderAnalysis();
  renderControls();
}
function formatDate(value) {
  if (value === null || value === undefined) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.valueOf())
    ? String(value)
    : date.toLocaleString("fr-FR");
}
function renderFreshness() {
  const live = state.detail?.live;
  const allowed = ["fresh", "stale", "offline", "empty"];
  const freshness = state.connectionFailed
    ? "offline"
    : allowed.includes(live?.freshness)
      ? live.freshness
      : "empty";
  $("freshness").className = `freshness ${freshness}`;
  const age = Number.isFinite(live?.age_s)
    ? ` · ${number(live.age_s, 1)} s`
    : "";
  const label = state.connectionFailed
    ? "Service injoignable"
    : {
        fresh: `Flux reçu${age}`,
        stale: `Mesure périmée${age}`,
        offline: "Rejeu · hors ligne",
        empty: "En attente de mesures",
      }[freshness];
  text("freshness-text", label);
}
function renderChart() {
  const chart = $("signal-chart");
  const samples = state.detail.samples;
  const left = 53,
    right = 878,
    top = 18,
    bottom = 231;
  const first = samples[0]?.elapsed_s ?? state.start ?? 0;
  const last = samples.at(-1)?.elapsed_s ?? state.end ?? first + 1;
  const minTime = state.start ?? Math.min(0, first);
  const maxTime = Math.max(minTime + 1, state.end ?? last);
  const values = samples
    .flatMap((sample) => [sample.roll_deg, sample.pitch_deg])
    .filter(Number.isFinite);
  let low = values.length ? values.reduce((a, b) => Math.min(a, b)) : -30;
  let high = values.length ? values.reduce((a, b) => Math.max(a, b)) : 30;
  const pad = Math.max(2, (high - low) * 0.18);
  low -= pad;
  high += pad;
  const x = (value) =>
    left + ((value - minTime) / (maxTime - minTime)) * (right - left);
  const y = (value) => bottom - ((value - low) / (high - low)) * (bottom - top);
  const nodes = [];
  for (let i = 0; i <= 4; i += 1) {
    const value = low + ((high - low) * i) / 4;
    const yy = y(value);
    nodes.push(
      svg("line", { x1: left, y1: yy, x2: right, y2: yy, class: "chart-grid" }),
    );
    nodes.push(
      svg(
        "text",
        { x: left - 11, y: yy + 4, "text-anchor": "end", class: "chart-axis" },
        number(value, 0),
      ),
    );
  }
  for (let i = 0; i <= 5; i += 1) {
    const value = minTime + ((maxTime - minTime) * i) / 5;
    nodes.push(
      svg(
        "text",
        {
          x: x(value),
          y: bottom + 24,
          "text-anchor": "middle",
          class: "chart-axis",
        },
        number(value, 1),
      ),
    );
  }
  nodes.push(svg("text", { x: 20, y: 12, class: "chart-unit" }, "°"));
  const gapThreshold = state.analysis?.gap_threshold_s ?? 0.25;
  for (const [key, className] of [
    ["roll_deg", "chart-roll"],
    ["pitch_deg", "chart-pitch"],
  ]) {
    let path = "",
      previous = null;
    for (const sample of samples) {
      if (!Number.isFinite(sample[key])) {
        previous = null;
        continue;
      }
      const connect =
        previous &&
        sample.elapsed_s > previous.elapsed_s &&
        sample.elapsed_s - previous.elapsed_s <= gapThreshold;
      path += `${connect ? "L" : "M"}${x(sample.elapsed_s).toFixed(2)},${y(sample[key]).toFixed(2)} `;
      previous = sample;
    }
    nodes.push(svg("path", { d: path, class: className }));
  }
  nodes.push(svg("g", { id: "chart-cursor-group" }));
  chart.replaceChildren(...nodes);
  $("chart-empty").hidden = samples.length > 0;
  state.chart = { x, y, top, bottom, left, right, minTime, maxTime };
}
function annotationPosition() {
  const followsLive =
    state.follow &&
    state.end === null &&
    state.detail?.session.status === "live";
  const at = followsLive
    ? (state.detail?.live?.elapsed_s ?? state.detail?.session.elapsed_s ?? 0)
    : (state.detail?.samples[state.cursor]?.elapsed_s ??
      state.start ??
      state.detail?.session.elapsed_s ??
      0);
  return { at, followsLive };
}
function renderCursor() {
  const samples = state.detail?.samples ?? [];
  const sample = samples[state.cursor];
  const latest =
    state.follow &&
    state.detail?.session.status === "live" &&
    sample?.elapsed_s === state.detail?.live?.last_sample_elapsed_s;
  text(
    "measurement-context",
    sample
      ? `${latest ? "Dernière mesure reçue" : "Mesure au curseur · rejeu"} · ${seconds(sample.elapsed_s)}`
      : "Aucune mesure dans cette fenêtre",
  );
  $("measurement-context").classList.toggle("historical", !!sample && !latest);
  $("replay-cursor").max = Math.max(0, samples.length - 1);
  $("replay-cursor").value = state.cursor;
  text("roll-value", number(sample?.roll_deg));
  text("pitch-value", number(sample?.pitch_deg));
  text("gyro-value", number(sample?.gyro_x_deg_s));
  text("cursor-value", seconds(sample?.elapsed_s));
  const annotation = annotationPosition();
  text(
    "annotation-time",
    annotation.followsLive
      ? `Repère à ${seconds(annotation.at)}, temps de session observé au dernier rafraîchissement.`
      : `Repère à ${seconds(annotation.at)}, ${sample ? "position du curseur" : "début de la fenêtre ou fin de session"}.`,
  );
  text(
    "inspected-record",
    sample
      ? `Échantillon #${sample.seq} · t = ${seconds(sample.elapsed_s)} · temps source ${seconds(sample.source_time_s)} · identifiant ${sample.id}`
      : "Aucun échantillon à inspecter dans cette fenêtre.",
  );
  const group = $("chart-cursor-group");
  if (group && sample && state.chart) {
    const { x, y, top, bottom } = state.chart;
    const nodes = [
      svg("line", {
        x1: x(sample.elapsed_s),
        x2: x(sample.elapsed_s),
        y1: top,
        y2: bottom,
        class: "chart-cursor",
      }),
    ];
    if (Number.isFinite(sample.roll_deg))
      nodes.push(
        svg("circle", {
          cx: x(sample.elapsed_s),
          cy: y(sample.roll_deg),
          r: 4,
          class: "chart-point",
        }),
      );
    group.replaceChildren(...nodes);
  } else group?.replaceChildren();
}
function renderEvents(events) {
  text("event-count", events.length);
  const nodes = [...events].reverse().map((event) => {
    const li = element("li");
    const button = element("button", "event-button");
    button.type = "button";
    const label = element("span", "", event.text);
    const kindLabels = {
      annotation: "Observation",
      source_started: "Acquisition démarrée",
      source_stopped: "Acquisition arrêtée",
      dropout_started: "Interruption synthétique",
      dropout_ended: "Réception rétablie",
      duration_limit: "Durée maximale atteinte",
      source_error: "Erreur de la source",
      imported: "Import d’un enregistrement",
      recovered: "Session récupérée après arrêt du service",
    };
    label.append(element("small", "", kindLabels[event.kind] ?? "Événement"));
    button.append(element("time", "", seconds(event.at_s)), label);
    button.addEventListener("click", () =>
      setWindow(Math.max(0, event.at_s - 2), event.at_s + 2),
    );
    li.append(button);
    return li;
  });
  if (!nodes.length)
    nodes.push(
      element(
        "li",
        "event-empty",
        "Marquez une observation pour retrouver cet instant en rejeu.",
      ),
    );
  $("event-list").replaceChildren(...nodes);
}
function renderAnalysis() {
  const analysis = state.analysis;
  const conclusion = analysis.gaps?.length
    ? `${analysis.gaps.length} intervalle(s) entre réceptions dépassent ${seconds(analysis.gap_threshold_s)}. Examinez les échantillons associés ; la cause reste indéterminée.`
    : analysis.sample_count < 2
      ? "Moins de deux échantillons dans cette fenêtre : la continuité de réception ne peut pas être évaluée."
      : `Aucun intervalle observé entre deux échantillons consécutifs ne dépasse ${seconds(analysis.gap_threshold_s)} dans cette fenêtre.`;
  text("analysis-conclusion", conclusion);
  text(
    "median-interval",
    Number.isFinite(analysis.median_interval_s)
      ? `${number(analysis.median_interval_s * 1000, 0)} ms`
      : "—",
  );
  text("maximum-gap", seconds(analysis.max_interval_s));
  text(
    "observed-duration",
    seconds(analysis.observed_duration_s ?? analysis.duration_s),
  );
  const nodes = (analysis.gaps ?? []).slice(0, 20).map((gap) => {
    const button = element("button", "gap-button");
    button.type = "button";
    button.append(
      element("strong", "", `${seconds(gap.duration_s)} sans observation`),
    );
    button.append(
      element("span", "", `${seconds(gap.start_s)} → ${seconds(gap.end_s)}`),
    );
    button.append(
      element(
        "small",
        "",
        `#${gap.before_seq} → #${gap.after_seq} · Examiner l’intervalle ↗`,
      ),
    );
    button.addEventListener("click", () =>
      setWindow(Math.max(0, gap.start_s - 0.5), gap.end_s + 0.5),
    );
    return button;
  });
  if ((analysis.gaps?.length ?? 0) > 20)
    nodes.push(
      element(
        "p",
        "gap-more",
        `${analysis.gaps.length - 20} autres intervalles. Réduisez la fenêtre pour les examiner.`,
      ),
    );
  $("gap-list").replaceChildren(...nodes);
  const translatedLimits = {
    "Receipt intervals use elapsed_s; source and receipt clocks are not assumed synchronized.":
      "Les intervalles utilisent le temps écoulé à la réception. Les horloges de source et de réception ne sont pas supposées synchronisées.",
    "An interval without samples does not identify a physical cause or prove packet loss.":
      "Un intervalle sans échantillon n’identifie pas une cause physique et ne prouve pas une perte de paquets.",
    "Leading and trailing silence cannot be bounded by two samples and is not counted as a gap.":
      "Le silence avant le premier ou après le dernier échantillon n’est pas encadré par deux mesures ; il n’est pas compté comme interruption.",
    "Gaps retain their full adjacent-sample interval even when the selected window clips it.":
      "Les interruptions conservent leur intervalle complet entre échantillons, même si la fenêtre sélectionnée en exclut une partie.",
    "Measurements are synthetic simulation data; no hardware was measured.":
      "Les mesures sont synthétiques. Aucun matériel n’a été mesuré.",
    "No roll measurements are available in this window.":
      "Aucune mesure de roulis n’est disponible dans cette fenêtre.",
  };
  const limits = Array.isArray(analysis.limitations)
    ? analysis.limitations
    : [analysis.limitations];
  $("analysis-limitations").replaceChildren(
    ...limits
      .filter(Boolean)
      .map((limit) => element("li", "", translatedLimits[limit] ?? limit)),
  );
}
async function setWindow(start, end) {
  if (
    (start !== null && (!Number.isFinite(start) || start < 0)) ||
    (end !== null && (!Number.isFinite(end) || end < 0)) ||
    (start !== null && end !== null && end < start)
  ) {
    showError(
      new Error(
        "La fin de la fenêtre doit être supérieure ou égale au début, avec des temps positifs.",
      ),
    );
    return;
  }
  pausePlayback();
  clearError();
  state.start = start;
  state.end = end;
  state.generation += 1;
  state.follow = true;
  $("window-start").value =
    start === null ? "" : String(Number(start.toFixed(3)));
  $("window-end").value = end === null ? "" : String(Number(end.toFixed(3)));
  await refreshSelection();
}
function pausePlayback() {
  state.playing = false;
  text("play-button", "Lire");
  $("play-button").setAttribute("aria-label", "Lire les mesures enregistrées");
}
function playbackFrame(now) {
  if (!state.playing) return;
  const samples = state.detail?.samples ?? [];
  const elapsed = (now - state.playbackAt) / 1000;
  const target = state.playbackOrigin + elapsed;
  while (
    state.cursor + 1 < samples.length &&
    samples[state.cursor + 1].elapsed_s <= target
  )
    state.cursor += 1;
  renderCursor();
  if (state.cursor >= samples.length - 1) pausePlayback();
  else requestAnimationFrame(playbackFrame);
}
async function action(name, callback) {
  if (state.busy.has(name)) return;
  state.busy.add(name);
  clearError();
  renderControls();
  try {
    await callback();
  } catch (error) {
    showError(error);
  } finally {
    state.busy.delete(name);
    renderControls();
  }
}
$("start-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action("start", async () => {
    const session = await post("/api/sessions", {
      name: $("session-name").value.trim(),
      objective: $("session-objective").value.trim(),
    });
    await loadSessions();
    await selectSession(session.id);
    notify(
      "Acquisition synthétique démarrée. Les mesures sont enregistrées localement.",
    );
  });
});
$("stop-button").addEventListener("click", () =>
  action("stop", async () => {
    await post(sessionPath("/stop"));
    await refreshSelection();
    await loadSessions();
  }),
);
$("dropout-button").addEventListener("click", () =>
  action("dropout", async () => {
    await post(sessionPath("/dropout"));
    notify(
      "Interruption synthétique demandée : 2 secondes. Son effet sera visible dans les observations.",
    );
    await refreshSelection();
  }),
);
$("annotation-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action("annotation", async () => {
    const { at } = annotationPosition();
    await post(sessionPath("/annotations"), {
      text: $("annotation-text").value.trim(),
      at_s: at,
    });
    $("annotation-text").value = "";
    await refreshSelection();
    notify(`Observation enregistrée à ${seconds(at)}.`);
  });
});
$("window-form").addEventListener("submit", (event) => {
  event.preventDefault();
  setWindow(
    $("window-start").value === "" ? null : Number($("window-start").value),
    $("window-end").value === "" ? null : Number($("window-end").value),
  );
});
$("reset-window").addEventListener("click", () => setWindow(null, null));
$("replay-cursor").addEventListener("input", () => {
  pausePlayback();
  state.follow = false;
  state.cursor = Number($("replay-cursor").value);
  renderCursor();
});
$("live-button").addEventListener("click", () => {
  pausePlayback();
  state.follow = true;
  state.cursor = Math.max(0, (state.detail?.samples.length ?? 0) - 1);
  renderCursor();
});
$("play-button").addEventListener("click", () => {
  if (state.playing) {
    pausePlayback();
    return;
  }
  const samples = state.detail?.samples ?? [];
  if (!samples.length) return;
  state.follow = false;
  if (state.cursor >= samples.length - 1) state.cursor = 0;
  state.playing = true;
  state.playbackAt = performance.now();
  state.playbackOrigin = samples[state.cursor].elapsed_s;
  text("play-button", "Pause");
  $("play-button").setAttribute("aria-label", "Mettre le rejeu en pause");
  requestAnimationFrame(playbackFrame);
});
$("signal-chart").addEventListener("click", (event) => {
  if (!state.chart || !state.detail?.samples.length) return;
  const rect = $("signal-chart").getBoundingClientRect();
  const pixel = ((event.clientX - rect.left) / rect.width) * 900;
  const target =
    state.chart.minTime +
    ((pixel - state.chart.left) / (state.chart.right - state.chart.left)) *
      (state.chart.maxTime - state.chart.minTime);
  let index = 0;
  state.detail.samples.forEach((sample, i) => {
    if (
      Math.abs(sample.elapsed_s - target) <
      Math.abs(state.detail.samples[index].elapsed_s - target)
    )
      index = i;
  });
  pausePlayback();
  state.follow = false;
  state.cursor = index;
  renderCursor();
});
$("export-button").addEventListener("click", () =>
  action("export", async () => {
    const id = state.id;
    const data = await api(sessionPath("/export", id));
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
    const link = element("a");
    link.href = url;
    link.download = `argos-studio-${id}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }),
);
$("import-file").addEventListener("change", () =>
  action("import", async () => {
    const file = $("import-file").files[0];
    if (!file) return;
    try {
      if (file.size > (state.health?.limits?.max_import_bytes ?? 10485760))
        throw new Error("Le fichier dépasse la limite d’import de 10 Mio.");
      const session = await api(
        `/api/import/argos?filename=${encodeURIComponent(file.name)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: file,
        },
      );
      await loadSessions();
      await selectSession(session.id);
      notify("Enregistrement importé. La session est disponible en rejeu.");
    } finally {
      $("import-file").value = "";
    }
  }),
);
async function initialize() {
  try {
    const [health, sessions] = await Promise.all([
      api("/api/health"),
      api("/api/sessions"),
    ]);
    state.health = health;
    state.sessions = sessions;
    text("version", health.version);
    if (!health.argos_import?.available)
      text(
        "import-status",
        health.argos_import?.reason ||
          "L’adaptateur ARGOS n’est pas configuré sur ce service.",
      );
    renderSessions();
    if (sessions.length)
      await selectSession(
        sessions.find((session) => session.status === "live")?.id ??
          sessions[0].id,
      );
    else $("empty-state").hidden = false;
  } catch (error) {
    showError(error);
    text(
      "session-list",
      "Le service local est indisponible. Rechargez la page après son démarrage.",
    );
  }
}
setInterval(async () => {
  if (
    state.detail?.session.status === "live" ||
    (state.connectionFailed && state.id)
  )
    await refreshSelection();
  else if (state.sessions.some((session) => session.status === "live")) {
    try {
      await loadSessions();
    } catch (error) {
      showError(error);
    }
  }
}, 500);
initialize();
