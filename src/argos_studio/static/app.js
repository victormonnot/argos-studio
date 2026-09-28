import { initAgent } from "./agent.js";

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
  investigations: [],
  report: null,
  investigationDrafts: new Map(),
  investigationSessionRevision: 0,
  investigationListRevision: 0,
  reportRevision: 0,
  investigationLoading: false,
  experiments: [],
  experimentId: null,
  experimentRevision: 0,
  experimentsInFlight: false,
  experimentsLoaded: false,
  experimentRendered: null,
  experimentsHistoryRendered: null,
  experimentsFailed: false,
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
      : session.source === "mavlink-udp"
        ? "MAVLink UDP"
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
function renderSourceSettings() {
  const mavlink = $("source-kind").value === "mavlink-udp";
  $("mavlink-settings").hidden = !mavlink;
  $("mavlink-settings").disabled = !mavlink || state.busy.has("start");
  text(
    "source-description",
    mavlink
      ? "MAVLink local · SITL déclaré · 3 min maximum"
      : "Source synthétique · un véhicule · 3 min maximum",
  );
  text(
    "source-help",
    mavlink
      ? "Un émetteur local, une identité attendue, aucune émission du Studio."
      : "Signal synthétique de développement, sans physique d’autopilote.",
  );
}
function renderControls() {
  const session = state.detail?.session;
  const live = session?.status === "live";
  const anyLive = state.sessions.some((item) => item.status === "live");
  const experimentActive = activeExperiment();
  const experimentChild =
    experimentActive &&
    [
      experimentActive.control_session_id,
      experimentActive.perturbed_session_id,
    ].includes(session?.id);
  const mavlinkSelected = $("source-kind").value === "mavlink-udp";
  const mavlinkAvailable = state.health?.mavlink?.available === true;
  $("source-kind").querySelector('option[value="mavlink-udp"]').disabled =
    !mavlinkAvailable;
  $("source-kind").disabled = state.busy.has("start");
  $("start-button").disabled =
    anyLive ||
    experimentActive ||
    state.busy.has("start") ||
    (mavlinkSelected && !mavlinkAvailable);
  renderSourceSettings();
  $("start-button").title = experimentActive
    ? "L’expérience conserve deux acquisitions successives. Attendez sa fin ou annulez-la."
    : anyLive
      ? "Arrêtez la session active avant de démarrer un nouvel essai."
      : "";
  $("stop-button").hidden = !live;
  $("stop-button").disabled = !live || state.busy.has("stop");
  $("stop-button").textContent = experimentChild
    ? "Annuler l’expérience"
    : "Arrêter la session";
  $("experiment-panel").hidden = session?.source !== "simulation";
  $("capture-button").hidden = session?.source !== "mavlink-udp";
  $("capture-button").disabled = !session || live || state.busy.has("capture");
  $("capture-button").title = live
    ? "Arrêtez la réception pour exporter la capture brute complète."
    : "Télécharger les datagrammes conservés et leurs métadonnées.";
  $("dropout-button").disabled =
    !live ||
    experimentChild ||
    session?.source !== "simulation" ||
    state.detail?.live?.gap_active ||
    state.busy.has("dropout");
  $("dropout-button").textContent = state.detail?.live?.gap_active
    ? "Interruption en cours…"
    : "Provoquer une interruption de 2 s";
  $("export-button").disabled = !session || state.busy.has("export");
  $("import-file").disabled =
    !state.health?.argos_import?.available ||
    experimentActive ||
    anyLive ||
    state.busy.has("import");
  $("import-file").title = experimentActive
    ? "Attendez la fin de l’expérience ou annulez-la avant un import."
    : anyLive
      ? "Arrêtez la session active avant un import."
      : "";
  if (state.health?.argos_import?.available) {
    text(
      "import-status",
      experimentActive
        ? "Une expérience est en cours ; l’import sera disponible à sa fin."
        : anyLive
          ? "Arrêtez la session active pour importer un enregistrement."
          : "Adaptateur ARGOS disponible. Les données importées sont consultées en rejeu.",
    );
  }
  const hasSamples = !!state.detail?.samples?.length;
  $("play-button").disabled = !hasSamples;
  $("replay-cursor").disabled = !hasSamples;
  $("live-button").disabled = !hasSamples;
  renderInvestigationControls();
  renderExperimentControls();
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
  state.investigationSessionRevision += 1;
  state.reportRevision += 1;
  state.investigations = [];
  state.report = null;
  state.investigationLoading = false;
  $("investigation-context").value = state.investigationDrafts.get(id) ?? "";
  $("investigation-report").hidden = true;
  setInvestigationStatus("Chargement des rapports enregistrés…");
  renderInvestigationHistory();
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
  renderExperiments();
  await Promise.all([
    refreshSelection(),
    loadInvestigations(id, state.investigationSessionRevision, true),
    agentWorkspace.selectionChanged(),
  ]);
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
    renderReceiver();
  } finally {
    if (state.inFlight === generation) state.inFlight = null;
  }
}
function renderSession() {
  const { session, samples, events } = state.detail;
  const simulated = session.source === "simulation";
  const mavlink = session.source === "mavlink-udp";
  text("active-session-title", session.name);
  text("active-objective", session.objective || "Aucun objectif renseigné.");
  text("source-badge", sourceLabel(session));
  $("source-badge").classList.toggle("imported", !simulated);
  text("session-status", statusLabel(session));
  text(
    "source-notice",
    simulated
      ? "Signal synthétique de développement ; aucune physique d’autopilote simulée. Aucun matériel connecté."
      : mavlink
        ? "Réception MAVLink passive locale. Origine déclarée : simulation SITL, non authentifiée ; une adresse locale ne la prouve pas. Aucun ordre envoyé au véhicule. Les datagrammes bruts sont conservés."
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
  renderReceiver();
  renderChart();
  renderCursor();
  renderEvents(events);
  renderAnalysis();
  renderControls();
  agentWorkspace.sync();
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
        fresh: `${state.detail?.session.source === "mavlink-udp" ? "ATTITUDE reçue" : "Flux reçu"}${age}`,
        stale: `${state.detail?.session.source === "mavlink-udp" ? "ATTITUDE périmée" : "Mesure périmée"}${age}`,
        offline: "Rejeu · hors ligne",
        empty: "En attente de mesures",
      }[freshness];
  text("freshness-text", label);
}
function renderReceiver() {
  const detail = state.detail;
  const mavlink = detail?.session.source === "mavlink-udp";
  $("receiver-panel").hidden = !mavlink;
  if (!mavlink) return;
  const metadata = detail.session.metadata ?? {};
  const live = detail.live ?? {};
  const replay = detail.session.status !== "live";
  const status = state.connectionFailed
    ? "unavailable"
    : replay
      ? "offline"
      : (live.connection_status ?? "waiting");
  const labels = {
    waiting: "Écoute ouverte · en attente",
    receiving: "Réception en cours",
    stale: "Écoute ouverte · réception périmée",
    offline: "Écoute arrêtée · rejeu",
    unavailable: "État du service inconnu",
  };
  text("receiver-status", labels[status] ?? "État de réception inconnu");
  $("receiver-status").className =
    `receiver-status ${["waiting", "receiving", "stale", "offline", "unavailable"].includes(status) ? status : "unavailable"}`;
  text(
    "receiver-endpoint",
    `${metadata.listen_host ?? "127.0.0.1"}:${metadata.listen_port ?? "—"}`,
  );
  text(
    "receiver-identity",
    `Système ${live.system_id ?? metadata.vehicle?.system_id ?? "—"} · composant ${live.component_id ?? metadata.vehicle?.component_id ?? "—"}`,
  );
  text(
    "receiver-peer",
    live.peer
      ? `${live.peer.host}:${live.peer.port}`
      : replay
        ? "Non disponible en rejeu · voir capture"
        : "Non identifié",
  );
  text(
    "heartbeat-age",
    state.connectionFailed
      ? "État inconnu"
      : replay
        ? live.heartbeat
          ? "Conservé à l’arrêt · voir champs"
          : "Non disponible en rejeu · voir capture"
        : Number.isFinite(live.heartbeat_age_s)
          ? live.heartbeat_freshness === "stale"
            ? `Périmé · reçu il y a ${seconds(live.heartbeat_age_s)}`
            : `Reçu il y a ${seconds(live.heartbeat_age_s)}`
          : "Jamais observé",
  );
  $("heartbeat-details").hidden = !live.heartbeat;
  text(
    "heartbeat-fields",
    live.heartbeat ? JSON.stringify(live.heartbeat, null, 2) : "",
  );
  const capture = detail.capture;
  const dispositions = capture?.dispositions ?? {};
  text("capture-count", number(capture?.datagram_count ?? 0, 0));
  text("capture-accepted", number(dispositions.accepted ?? 0, 0));
  text(
    "capture-rejected",
    `${number(dispositions.invalid ?? 0, 0)} / ${number(dispositions.signed ?? 0, 0)}`,
  );
  text(
    "capture-foreign",
    `${number(dispositions.foreign_source ?? 0, 0)} / ${number(dispositions.foreign_peer ?? 0, 0)}`,
  );
  text(
    "capture-size",
    capture
      ? `${number(capture.raw_bytes ?? 0, 0)} octets bruts conservés · comptages sur la session entière, hors filtre temporel.`
      : "Qualité de capture non disponible.",
  );
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
      peer_identified: "Émetteur identifié",
      reception_stale: "Réception périmée",
      reception_resumed: "Réception rétablie",
      capture_limit: "Limite de capture atteinte",
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
  $("window-start").value = start === null ? "" : String(start);
  $("window-end").value = end === null ? "" : String(end);
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
$("source-kind").addEventListener("change", renderControls);
$("start-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action("start", async () => {
    const mavlink = $("source-kind").value === "mavlink-udp";
    const payload = {
      name: $("session-name").value.trim(),
      objective: $("session-objective").value.trim(),
    };
    if (mavlink)
      Object.assign(payload, {
        source: "mavlink-udp",
        listen_port: Number($("listen-port").value),
        system_id: Number($("system-id").value),
        component_id: Number($("component-id").value),
      });
    const session = await post("/api/sessions", payload);
    await loadSessions();
    await selectSession(session.id);
    notify(
      mavlink
        ? "Écoute UDP locale ouverte. En attente des messages de la source déclarée."
        : "Acquisition synthétique démarrée. Les mesures sont enregistrées localement.",
    );
  });
});
$("stop-button").addEventListener("click", () =>
  action("stop", async () => {
    await post(sessionPath("/stop"));
    await refreshSelection();
    await loadSessions();
    await refreshExperiments();
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
async function downloadSessionJson(suffix, filename) {
  const id = state.id;
  const data = await api(sessionPath(suffix, id));
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const link = element("a");
  link.href = url;
  link.download = `argos-studio-${id}${filename}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$("export-button").addEventListener("click", () =>
  action("export", () => downloadSessionJson("/export", "")),
);
$("capture-button").addEventListener("click", () =>
  action("capture", () => downloadSessionJson("/capture", "-capture")),
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
const agentWorkspace = initAgent({
  getState: () => state,
  api,
  post,
  element,
  formatDate,
  seconds,
  selectSession,
  setWindow,
  selectInvestigation,
  refreshExperiments,
  reloadInvestigations: () =>
    state.id
      ? loadInvestigations(state.id, state.investigationSessionRevision)
      : Promise.resolve(),
});

async function initialize() {
  try {
    const [health, sessions, experiments] = await Promise.all([
      api("/api/health"),
      api("/api/sessions"),
      api("/api/experiments"),
    ]);
    state.health = health;
    agentWorkspace.syncHealth(health.agent);
    state.sessions = sessions;
    state.experiments = experiments;
    state.experimentsLoaded = true;
    text("version", health.version);
    if (health.mavlink?.available) {
      $("listen-host").value = health.mavlink.listen_host ?? "127.0.0.1";
      $("listen-port").value = health.mavlink.default_port ?? 14580;
    }
    if (!health.argos_import?.available)
      text(
        "import-status",
        health.argos_import?.reason ||
          "L’adaptateur ARGOS n’est pas configuré sur ce service.",
      );
    renderSessions();
    renderExperiments();
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

function setInvestigationStatus(message, error = false) {
  text("investigation-status", message);
  $("investigation-status").classList.toggle("is-error", error);
}
function renderInvestigationControls() {
  const busy = state.busy.has("investigate");
  $("investigate-button").disabled =
    !state.detail || busy || state.investigationLoading;
  $("investigate-button").textContent = busy
    ? "Investigation en cours…"
    : "Investiguer cette fenêtre";
  $("investigation-history").disabled =
    !state.investigations.length || busy || state.investigationLoading;
  $("investigation-export").disabled =
    !state.report ||
    state.investigationLoading ||
    state.busy.has("investigation-export");
  $("reuse-report-context").disabled = !state.report;
  const start = state.start === null ? "début de session" : `${state.start} s`;
  const end =
    state.end === null ? "dernière observation au lancement" : `${state.end} s`;
  text(
    "investigation-window-label",
    `Fenêtre demandée : ${start} → ${end}. Le rapport conservera un instantané fixe.`,
  );
  renderExperimentControls();
}
function renderInvestigationHistory() {
  const placeholder = element(
    "option",
    "",
    state.investigations.length
      ? "Choisir un rapport enregistré"
      : "Aucun rapport enregistré",
  );
  placeholder.value = "";
  const options = state.investigations.map((report) => {
    const option = element(
      "option",
      "",
      `${formatDate(report.created_at)} · ${seconds(report.window_s?.start_s)} → ${seconds(report.window_s?.end_s)} · ${report.summary}`,
    );
    option.value = report.id;
    return option;
  });
  $("investigation-history").replaceChildren(placeholder, ...options);
  $("investigation-history").value = state.report?.id ?? "";
  renderInvestigationControls();
}
async function loadInvestigations(
  sessionId,
  sessionRevision,
  selectLatest = false,
) {
  const listRevision = ++state.investigationListRevision;
  try {
    const reports = await api(sessionPath("/investigations", sessionId));
    if (
      sessionRevision !== state.investigationSessionRevision ||
      listRevision !== state.investigationListRevision
    )
      return;
    state.investigations = reports;
    renderInvestigationHistory();
    if (selectLatest && reports.length)
      await selectInvestigation(reports[0].id);
    else if (!reports.length)
      setInvestigationStatus("Aucune investigation lancée pour cette session.");
  } catch (error) {
    if (
      sessionRevision === state.investigationSessionRevision &&
      listRevision === state.investigationListRevision
    ) {
      setInvestigationStatus(error.message, true);
    }
  }
}
async function selectInvestigation(reportId) {
  if (!reportId) return;
  const sessionId = state.id;
  const sessionRevision = state.investigationSessionRevision;
  const revision = ++state.reportRevision;
  state.investigationLoading = true;
  setInvestigationStatus("Chargement du rapport…");
  renderInvestigationControls();
  try {
    const report = await api(
      sessionPath(`/investigations/${encodeURIComponent(reportId)}`, sessionId),
    );
    if (
      sessionRevision !== state.investigationSessionRevision ||
      revision !== state.reportRevision
    )
      return;
    state.report = report;
    renderInvestigationReport();
    setInvestigationStatus(
      "Rapport enregistré : les nouvelles mesures et les changements de fenêtre ne le modifient pas.",
    );
  } catch (error) {
    if (
      sessionRevision === state.investigationSessionRevision &&
      revision === state.reportRevision
    ) {
      setInvestigationStatus(error.message, true);
      $("investigation-history").value = state.report?.id ?? "";
    }
  } finally {
    if (
      sessionRevision === state.investigationSessionRevision &&
      revision === state.reportRevision
    ) {
      state.investigationLoading = false;
      renderInvestigationControls();
    }
  }
}
function reportList(title, values, className) {
  const section = element("section", className);
  section.append(element("h5", "", title));
  const list = element("ul");
  list.append(...values.map((value) => element("li", "", value)));
  section.append(list);
  return section;
}
async function showReportEvidence(evidence, index, showChart = false) {
  const reportId = state.report?.id;
  const sessionRevision = state.investigationSessionRevision;
  if (evidence.window_s)
    await setWindow(evidence.window_s.start_s, evidence.window_s.end_s);
  if (
    reportId !== state.report?.id ||
    sessionRevision !== state.investigationSessionRevision
  )
    return;
  const details = $(`report-evidence-${index}`);
  if (!details) return;
  details.open = true;
  const target = showChart ? $("signal-title") : details;
  target.scrollIntoView({ block: "nearest", behavior: "auto" });
}
function renderInvestigationReport() {
  const report = state.report;
  if (!report) return;
  $("investigation-report").hidden = false;
  text("report-summary", report.summary);
  text("report-version", report.algorithm_version);
  text(
    "report-outcome",
    {
      insufficient_data: "Données insuffisantes",
      observations: "Observations à examiner",
      no_gap_observed: "Aucune interruption observée",
    }[report.outcome] ?? "Rapport de réception",
  );
  text(
    "report-snapshot",
    `Rapport figé le ${formatDate(report.created_at)} · fenêtre ${report.window_s.start_s} → ${report.window_s.end_s} s · ${report.snapshot.sample_count} échantillons, ${report.snapshot.datagram_count ?? 0} datagrammes, ${report.snapshot.event_count} événements · source : ${sourceLabel({ source: report.snapshot.source })}.`,
  );
  text(
    "report-context-text",
    report.context || "Aucun contexte fourni pour ce rapport.",
  );
  text(
    "report-fingerprint",
    `Empreinte SHA-256 des données analysées : ${report.snapshot.sha256}`,
  );
  $("investigation-history").value = report.id;
  const evidence = report.evidence ?? [];
  const findings = (report.findings ?? []).map((finding) => {
    const card = element("section", "report-finding");
    card.append(
      element("h4", "", finding.title),
      element("p", "finding-observation", finding.observation),
    );
    const links = element("div", "finding-evidence-links");
    for (const evidenceId of finding.evidence ?? []) {
      const index = evidence.findIndex((item) => item.id === evidenceId);
      if (index < 0) continue;
      const item = evidence[index];
      const button = element(
        "button",
        "report-evidence-link",
        `Preuve · ${item.title}`,
      );
      button.type = "button";
      button.addEventListener("click", () => showReportEvidence(item, index));
      links.append(button);
    }
    card.append(links);
    const interpretation = element("div", "finding-interpretation");
    if (finding.hypotheses?.length)
      interpretation.append(
        reportList(
          "Hypothèses à vérifier",
          finding.hypotheses,
          "finding-hypotheses",
        ),
      );
    if (finding.uncertainties?.length)
      interpretation.append(
        reportList(
          "Ce qui reste incertain",
          finding.uncertainties,
          "finding-uncertainties",
        ),
      );
    card.append(interpretation);
    if (finding.next_check) {
      const check = element("section", "finding-next-check");
      check.append(
        element("p", "eyebrow", "VÉRIFICATION PROPOSÉE · NON EXÉCUTÉE"),
        element("h5", "", finding.next_check.title),
      );
      const steps = element("ol");
      steps.append(
        ...(finding.next_check.steps ?? []).map((step) =>
          element("li", "", step),
        ),
      );
      check.append(
        steps,
        element(
          "p",
          "expected-evidence",
          `Preuve attendue : ${finding.next_check.expected_evidence}`,
        ),
      );
      card.append(check);
    }
    return card;
  });
  if (!findings.length)
    findings.push(
      element(
        "p",
        "small muted",
        "Aucun constat supplémentaire dans ce rapport.",
      ),
    );
  $("report-findings").replaceChildren(...findings);
  const evidenceNodes = evidence.map((item, index) => {
    const details = element("details", "report-evidence-item");
    details.id = `report-evidence-${index}`;
    details.append(element("summary", "", `${item.id} · ${item.title}`));
    if (item.window_s) {
      details.append(
        element(
          "p",
          "evidence-window",
          `Bornes exactes : ${item.window_s.start_s} → ${item.window_s.end_s} s`,
        ),
      );
      const button = element(
        "button",
        "button compact subtle evidence-window-button",
        "Afficher cette fenêtre",
      );
      button.type = "button";
      button.addEventListener("click", () =>
        showReportEvidence(item, index, true),
      );
      details.append(button);
    }
    details.append(
      element("pre", "evidence-data", JSON.stringify(item.data, null, 2)),
    );
    return details;
  });
  if (!evidenceNodes.length)
    evidenceNodes.push(
      element("p", "small muted", "Aucune pièce de preuve disponible."),
    );
  $("report-evidence").replaceChildren(...evidenceNodes);
  $("report-limitations").replaceChildren(
    ...(report.limitations ?? []).map((limit) => element("li", "", limit)),
  );
  $("report-tools").replaceChildren(
    ...(report.tools ?? []).map((tool) => {
      const details = element("details");
      details.append(
        element("summary", "", tool.name),
        element(
          "pre",
          "evidence-data",
          JSON.stringify(
            { parameters: tool.parameters, result: tool.result },
            null,
            2,
          ),
        ),
      );
      return details;
    }),
  );
  renderInvestigationControls();
}
$("investigation-context").addEventListener("input", () => {
  if (state.id)
    state.investigationDrafts.set(state.id, $("investigation-context").value);
});
$("investigation-history").addEventListener("change", () =>
  selectInvestigation($("investigation-history").value),
);
$("reuse-report-context").addEventListener("click", () => {
  if (!state.report) return;
  $("investigation-context").value = state.report.context ?? "";
  state.investigationDrafts.set(state.id, $("investigation-context").value);
  $("investigation-context").focus();
});
$("investigation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.detail || state.busy.has("investigate")) return;
  const sessionId = state.id;
  const sessionRevision = state.investigationSessionRevision;
  const generation = state.generation;
  const reportRevision = ++state.reportRevision;
  const payload = {
    start_s: state.start,
    end_s: state.end,
    context: $("investigation-context").value,
  };
  state.busy.add("investigate");
  setInvestigationStatus("Examen des observations et conservation du rapport…");
  renderInvestigationControls();
  try {
    const report = await post(
      sessionPath("/investigations", sessionId),
      payload,
    );
    if (sessionRevision !== state.investigationSessionRevision) return;
    await loadInvestigations(sessionId, sessionRevision);
    if (sessionRevision !== state.investigationSessionRevision) return;
    if (
      generation === state.generation &&
      reportRevision === state.reportRevision
    ) {
      state.report = report;
      renderInvestigationReport();
      setInvestigationStatus(
        "Rapport enregistré : les nouvelles mesures et les changements de fenêtre ne le modifient pas.",
      );
    } else
      setInvestigationStatus(
        "Rapport enregistré pour la fenêtre demandée au lancement. Retrouvez-le dans l’historique.",
      );
  } catch (error) {
    if (sessionRevision === state.investigationSessionRevision)
      setInvestigationStatus(error.message, true);
  } finally {
    state.busy.delete("investigate");
    renderInvestigationControls();
  }
});
$("investigation-export").addEventListener("click", () => {
  if (!state.report) return;
  const report = state.report;
  action("investigation-export", async () => {
    const data = await api(
      sessionPath(
        `/investigations/${encodeURIComponent(report.id)}/export`,
        report.session_id,
      ),
    );
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
    const link = element("a");
    link.href = url;
    link.download = `argos-studio-investigation-${report.id}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
});

function activeExperiment() {
  return (
    state.experiments.find((experiment) => experiment.status === "running") ??
    null
  );
}
function experimentPath(id, suffix = "") {
  return `/api/experiments/${encodeURIComponent(id)}${suffix}`;
}
function experimentStatusLabel(status) {
  return (
    {
      proposed: "Protocole prêt · lancement à confirmer",
      running: "Acquisitions en cours",
      completed: "Expérience terminée",
      cancelled: "Expérience annulée",
      interrupted: "Expérience interrompue",
      failed: "Expérience échouée",
      expired: "Proposition expirée",
    }[status] ?? status
  );
}
function relatedExperiments() {
  return state.experiments.filter((experiment) =>
    [
      experiment.origin_session_id,
      experiment.control_session_id,
      experiment.perturbed_session_id,
    ].includes(state.id),
  );
}
function selectedExperiment() {
  return (
    relatedExperiments().find(
      (experiment) => experiment.id === state.experimentId,
    ) ?? null
  );
}
function renderExperimentControls() {
  const report = state.report;
  const synthetic = report?.snapshot?.source === "simulation";
  const pending = [
    "experiment-prepare",
    "experiment-start",
    "experiment-cancel",
  ].some((key) => state.busy.has(key));
  const active = activeExperiment();
  const experiment = selectedExperiment();
  const anyLive = state.sessions.some((session) => session.status === "live");
  $("experiment-prepare").disabled =
    !synthetic || state.investigationLoading || pending;
  text(
    "experiment-availability",
    !report
      ? "Choisissez un rapport d’investigation pour préparer un essai."
      : synthetic
        ? "À partir du rapport sélectionné : préparer deux acquisitions synthétiques pour tester l’effet d’une interruption contrôlée."
        : "Ce protocole est disponible pour les rapports de sessions synthétiques.",
  );
  $("experiment-history").disabled = !relatedExperiments().length || pending;
  $("experiment-export").disabled =
    !experiment ||
    ["proposed", "running"].includes(experiment.status) ||
    state.busy.has("experiment-export");
  $("experiment-export").title = ["proposed", "running"].includes(
    experiment?.status,
  )
    ? "L’export est disponible après la fin ou l’annulation de l’expérience."
    : "Télécharger le protocole, les références de capture et le résultat conservé.";
  $("experiment-start").hidden = experiment?.status !== "proposed";
  $("experiment-start").disabled =
    !experiment ||
    !!active ||
    anyLive ||
    pending ||
    experiment.expires_at <= Date.now() / 1000;
  $("experiment-cancel").hidden = !["proposed", "running"].includes(
    experiment?.status,
  );
  $("experiment-cancel").disabled = pending;
  $("experiment-cancel").textContent =
    experiment?.status === "proposed"
      ? "Abandonner ce protocole"
      : "Annuler l’expérience";
  text(
    "experiment-start-help",
    experiment?.status !== "proposed"
      ? ""
      : experiment.expires_at <= Date.now() / 1000
        ? "La proposition a expiré. Préparez un nouveau protocole depuis le rapport."
        : active
          ? "Une expérience est déjà en cours. Attendez sa fin ou annulez-la."
          : anyLive
            ? "Arrêtez la session active avant de lancer ces deux acquisitions."
            : "Le lancement crée deux sessions successives. Vous pouvez annuler à tout moment ; les données déjà reçues restent conservées.",
  );
  $("active-experiment").hidden = !active;
  $("active-experiment-cancel").disabled = pending;
  if (active) {
    const elapsed =
      active.started_at == null
        ? null
        : Math.max(0, Date.now() / 1000 - active.started_at);
    text(
      "active-experiment-state",
      state.experimentsFailed
        ? "Suivi indisponible · état de l’expérience à vérifier après reconnexion."
        : `${active.perturbed_session_id ? "2 / 2 · Acquisition avec interruption" : "1 / 2 · Acquisition témoin"} · ${seconds(elapsed)} écoulées · limite ${seconds(active.plan.max_wall_duration_s)}.`,
    );
  }
}
function renderExperiments() {
  const related = relatedExperiments();
  if (!related.some((experiment) => experiment.id === state.experimentId))
    state.experimentId = related[0]?.id ?? null;
  const experiment = selectedExperiment();
  const historyKey = JSON.stringify([
    state.id,
    related.map((item) => [item.id, item.status]),
  ]);
  if (historyKey !== state.experimentsHistoryRendered) {
    const placeholder = element(
      "option",
      "",
      related.length ? "Choisir une expérience" : "Aucune expérience",
    );
    placeholder.value = "";
    $("experiment-history").replaceChildren(
      placeholder,
      ...related.map((item) => {
        const option = element(
          "option",
          "",
          `${formatDate(item.created_at)} · ${experimentStatusLabel(item.status)}`,
        );
        option.value = item.id;
        return option;
      }),
    );
    state.experimentsHistoryRendered = historyKey;
  }
  $("experiment-history").value = experiment?.id ?? "";
  $("experiment-detail").hidden = !experiment;
  if (!state.experimentsFailed) {
    text(
      "experiment-status",
      experiment
        ? `${experimentStatusLabel(experiment.status)}.${experiment.error ? ` ${experiment.error}` : ""}${["interrupted", "failed", "cancelled"].includes(experiment.status) ? " Les acquisitions conservées restent consultables. Aucun redémarrage automatique." : ""}`
        : "Aucune expérience préparée pour cette session.",
    );
    $("experiment-status").classList.remove("is-error");
  }
  renderExperimentControls();
  const renderKey = JSON.stringify(experiment);
  if (!experiment || renderKey === state.experimentRendered) return;
  state.experimentRendered = renderKey;
  const plan = experiment.plan;
  text("experiment-state", experimentStatusLabel(experiment.status));
  text("experiment-version", plan.version);
  text(
    "experiment-plan-description",
    `Source synthétique à ${number(plan.sample_rate_hz, 0)} Hz nominaux, ${seconds(plan.phase_duration_s)} par acquisition. L’essai vérifie la réception de ce générateur ; il ne reproduit pas une panne de véhicule.`,
  );
  $("experiment-phases").replaceChildren(
    element(
      "li",
      "",
      `Témoin : ${seconds(plan.phase_duration_s)} de réception sans intervention.`,
    ),
    element(
      "li",
      "",
      `Avec interruption : coupure de ${seconds(plan.dropout_duration_s)} demandée à ${seconds(plan.dropout_at_s)}, puis reprise du flux.`,
    ),
    element(
      "li",
      "",
      `Comparer les intervalles au seuil de ${seconds(plan.gap_threshold_s)}, avec une tolérance temporelle de ${seconds(plan.timing_tolerance_s)}. Arrêt au plus tard après ${seconds(plan.max_wall_duration_s)}.`,
    ),
  );
  text(
    "experiment-expiry",
    experiment.status === "proposed"
      ? `Proposition valable jusqu’au ${formatDate(experiment.expires_at)}. Aucune acquisition n’a encore été lancée.`
      : `Lancement : ${formatDate(experiment.started_at)} · fin : ${formatDate(experiment.ended_at)}.`,
  );
  const sessionLinks = [];
  for (const [id, label] of [
    [experiment.control_session_id, "Ouvrir l’acquisition témoin"],
    [experiment.perturbed_session_id, "Ouvrir l’acquisition avec interruption"],
  ]) {
    if (!id) continue;
    const button = element("button", "report-evidence-link", label);
    button.type = "button";
    button.addEventListener("click", () =>
      navigateExperimentSession(experiment, id),
    );
    sessionLinks.push(button);
  }
  $("experiment-session-links").replaceChildren(...sessionLinks);
  const metadata = [
    ["Expérience", experiment.id],
    ["Session d’origine", experiment.origin_session_id],
    ["Rapport d’origine", experiment.investigation_id],
    ["Préparée le", formatDate(experiment.created_at)],
    ["Lancée le", formatDate(experiment.started_at)],
    ["Terminée le", formatDate(experiment.ended_at)],
  ];
  if (experiment.result?.reference?.snapshot_sha256)
    metadata.push([
      "Empreinte des données de référence",
      experiment.result.reference.snapshot_sha256,
    ]);
  $("experiment-metadata").replaceChildren(
    ...metadata.flatMap(([label, value]) => [
      element("dt", "", label),
      element("dd", "", value),
    ]),
  );
  text("experiment-plan", JSON.stringify(plan, null, 2));
  renderExperimentResult(experiment);
}
function renderExperimentResult(experiment) {
  const result = experiment.result;
  $("experiment-result").hidden = !result;
  if (!result) return;
  text(
    "experiment-outcome",
    {
      supported: "Effet de l’intervention observé",
      not_reproduced: "Effet attendu non reproduit",
      inconclusive: "Essai non concluant",
    }[result.outcome] ?? "Comparaison enregistrée",
  );
  text("experiment-result-title", result.summary);
  const { reference, control, perturbed } = result;
  const maximumGap = (phase) =>
    phase?.gaps?.length
      ? Math.max(...phase.gaps.map((gap) => gap.duration_s))
      : 0;
  const rows = [
    [
      "Échantillons",
      number(reference?.sample_count, 0),
      number(control?.sample_count, 0),
      number(perturbed?.sample_count, 0),
    ],
    [
      "Intervalle médian",
      seconds(reference?.median_interval_s),
      seconds(control?.median_interval_s),
      seconds(perturbed?.median_interval_s),
    ],
    [
      "Intervalle maximal",
      seconds(reference?.max_interval_s),
      seconds(control?.max_interval_s),
      seconds(perturbed?.max_interval_s),
    ],
    [
      `Intervalles > ${seconds(experiment.plan.gap_threshold_s)}`,
      number(reference?.gap_count, 0),
      number(control?.gap_count, 0),
      number(perturbed?.gap_count, 0),
    ],
    [
      "Plus grand intervalle au-dessus du seuil",
      seconds(reference?.max_gap_s),
      seconds(maximumGap(control)),
      seconds(maximumGap(perturbed)),
    ],
  ];
  $("experiment-comparison").replaceChildren(
    ...rows.map(([label, ...values]) => {
      const row = element("tr");
      const heading = element("th", "", label);
      heading.scope = "row";
      row.append(heading, ...values.map((value) => element("td", "", value)));
      return row;
    }),
  );
  text(
    "experiment-difference",
    `Référence : fenêtre ${seconds(reference?.window_s?.start_s)} → ${seconds(reference?.window_s?.end_s)}. Écart d’intervalle maximal entre acquisitions : ${seconds(result.difference?.max_interval_s)}. ${number(result.difference?.gap_count, 0)} intervalle(s) supplémentaire(s) au-dessus du seuil.`,
  );
  $("experiment-checks").replaceChildren(
    ...(result.checks ?? []).map((check) => {
      const row = element(
        "li",
        check.passed === true ? "check-passed" : "check-unmet",
      );
      row.append(
        element(
          "span",
          "check-status",
          check.passed === true
            ? "Vérifié"
            : check.passed === false
              ? "Non vérifié"
              : "Indéterminé",
        ),
        element("span", "", check.explanation),
      );
      return row;
    }),
  );
  const passedChecks = (result.checks ?? []).filter(
    (check) => check.passed === true,
  ).length;
  text(
    "experiment-checks-title",
    `Critères de l’essai · ${passedChecks} / ${result.checks?.length ?? 0} vérifiés`,
  );
  $("experiment-checks-title").parentElement.open =
    result.outcome !== "supported";
  $("experiment-limitations").replaceChildren(
    ...(result.limitations ?? []).map((limit) => element("li", "", limit)),
  );
  const gap = result.intervention?.gap;
  $("experiment-gap").hidden = !gap;
  if (gap)
    text(
      "experiment-gap",
      `Inspecter l’intervalle : ${seconds(gap.start_s)} → ${seconds(gap.end_s)} · échantillons #${gap.before_seq} → #${gap.after_seq}`,
    );
}
async function refreshExperiments() {
  if (state.experimentsInFlight) return;
  const revision = state.experimentRevision;
  state.experimentsInFlight = true;
  try {
    const experiments = await api("/api/experiments");
    if (revision !== state.experimentRevision) return;
    const prior = state.experiments;
    state.experiments = experiments;
    state.experimentsLoaded = true;
    state.experimentsFailed = false;
    renderExperiments();
    renderControls();
    const changed =
      JSON.stringify(
        prior.map((item) => [
          item.id,
          item.status,
          item.control_session_id,
          item.perturbed_session_id,
        ]),
      ) !==
      JSON.stringify(
        experiments.map((item) => [
          item.id,
          item.status,
          item.control_session_id,
          item.perturbed_session_id,
        ]),
      );
    if (changed) await loadSessions();
  } catch (error) {
    if (revision !== state.experimentRevision) return;
    state.experimentsFailed = true;
    text(
      "experiment-status",
      "Le suivi des expériences n’est plus actualisé. Le service reste responsable de leur arrêt borné ; le suivi reprendra à la reconnexion.",
    );
    $("experiment-status").classList.add("is-error");
    if (activeExperiment())
      text(
        "active-experiment-state",
        "Suivi indisponible · état de l’expérience à vérifier après reconnexion.",
      );
  } finally {
    state.experimentsInFlight = false;
  }
}
function retainExperiment(experiment) {
  state.experimentRevision += 1;
  state.experiments = [
    experiment,
    ...state.experiments.filter((item) => item.id !== experiment.id),
  ];
  state.experimentsFailed = false;
  renderExperiments();
  renderControls();
}
async function navigateExperimentSession(
  experiment,
  sessionId,
  window = null,
  report = false,
) {
  const selection = selectSession(sessionId);
  const revision = state.investigationSessionRevision;
  await selection;
  if (revision !== state.investigationSessionRevision) return;
  state.experimentId = experiment.id;
  renderExperiments();
  if (window) await setWindow(window.start_s, window.end_s);
  if (revision !== state.investigationSessionRevision) return;
  if (report) await selectInvestigation(experiment.investigation_id);
  if (revision !== state.investigationSessionRevision) return;
  (report ? $("investigation-title") : $("signal-title")).scrollIntoView({
    block: "start",
    behavior: "auto",
  });
}
$("experiment-history").addEventListener("change", () => {
  state.experimentId = $("experiment-history").value;
  renderExperiments();
});
$("experiment-prepare").addEventListener("click", () => {
  const report = state.report;
  if (!report || report.snapshot.source !== "simulation") return;
  const revision = state.investigationSessionRevision;
  action("experiment-prepare", async () => {
    const experiment = await post(
      sessionPath(
        `/investigations/${encodeURIComponent(report.id)}/experiments`,
        report.session_id,
      ),
    );
    if (revision === state.investigationSessionRevision)
      state.experimentId = experiment.id;
    retainExperiment(experiment);
    notify(
      "Protocole enregistré. Consultez les deux acquisitions prévues avant de les lancer.",
    );
    if (revision === state.investigationSessionRevision)
      $("experiment-detail").scrollIntoView({
        block: "nearest",
        behavior: "auto",
      });
  });
});
$("experiment-start").addEventListener("click", () => {
  const experiment = selectedExperiment();
  if (!experiment) return;
  action("experiment-start", async () => {
    retainExperiment(await post(experimentPath(experiment.id, "/start")));
    await loadSessions();
    notify(
      "Expérience lancée : acquisition témoin, puis acquisition avec interruption. L’annulation reste accessible en haut de page.",
    );
  });
});
function cancelExperiment(experiment) {
  if (!experiment) return;
  action("experiment-cancel", async () => {
    const stopped = await post(experimentPath(experiment.id, "/cancel"));
    retainExperiment(stopped);
    await loadSessions();
    await refreshSelection();
    notify(
      stopped.status === "cancelled"
        ? "Expérience annulée. Les données déjà reçues restent conservées."
        : `${experimentStatusLabel(stopped.status)}. L’état enregistré a été actualisé.`,
    );
  });
}
$("experiment-cancel").addEventListener("click", () =>
  cancelExperiment(selectedExperiment()),
);
$("active-experiment-cancel").addEventListener("click", () =>
  cancelExperiment(activeExperiment()),
);
$("active-experiment-open").addEventListener("click", async () => {
  const experiment = activeExperiment();
  if (!experiment) return;
  await navigateExperimentSession(experiment, experiment.origin_session_id);
  if (state.id === experiment.origin_session_id)
    $("comparison-title").scrollIntoView({ block: "start", behavior: "auto" });
});
$("experiment-origin").addEventListener("click", () => {
  const experiment = selectedExperiment();
  if (experiment)
    navigateExperimentSession(
      experiment,
      experiment.origin_session_id,
      null,
      true,
    );
});
$("experiment-gap").addEventListener("click", () => {
  const experiment = selectedExperiment();
  const gap = experiment?.result?.intervention?.gap;
  if (gap)
    navigateExperimentSession(experiment, experiment.perturbed_session_id, gap);
});
$("experiment-export").addEventListener("click", () => {
  const experiment = selectedExperiment();
  if (!experiment) return;
  action("experiment-export", async () => {
    const data = await api(experimentPath(experiment.id, "/export"));
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
    const link = element("a");
    link.href = url;
    link.download = `argos-studio-experiment-${experiment.id}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
});
let experimentPolls = 0;
setInterval(() => {
  renderExperimentControls();
  if (
    activeExperiment() ||
    state.experimentsFailed ||
    ++experimentPolls % 5 === 0
  )
    refreshExperiments();
}, 1000);
