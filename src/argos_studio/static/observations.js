export function initObservations(hooks) {
  const { getState, api, post, element, seconds, sourceLabel } = hooks;
  const $ = (id) => document.getElementById(id);
  const path = (sessionId, id = "", action = "") =>
    `/api/sessions/${encodeURIComponent(sessionId)}/observations${id ? `/${encodeURIComponent(id)}` : ""}${action ? `/${action}` : ""}`;
  let revision = 0;
  let mutationRevision = 0;
  let requestId = 0;
  let inFlight = null;
  let data = null;
  const busy = new Set();
  const cards = new Map();

  function status(message, error = false) {
    $("observations-status").textContent = message;
    $("observations-status").classList.toggle("is-error", error);
  }

  function current(sessionId, selection) {
    return getState().id === sessionId && revision === selection;
  }

  async function act(observation, action) {
    const sessionId = observation.session_id;
    const selection = revision;
    const generation = getState().generation;
    const reportRevision = getState().reportRevision;
    if (!current(sessionId, selection) || busy.has(observation.id)) return;
    busy.add(observation.id);
    mutationRevision += 1;
    syncButtons();
    status(action === "investigate" ? "Ouverture de l’investigation locale…" : "");
    try {
      if (action === "window") {
        await hooks.setWindow(observation.start_s, observation.end_s);
        if (current(sessionId, selection))
          $("signal-title").scrollIntoView({ block: "start" });
      } else if (action === "agent") {
        await hooks.prepareObservation(observation);
      } else {
        let updated = observation;
        if (action !== "investigate" || !observation.investigation_id)
          updated = await post(path(sessionId, observation.id, action));
        if (!current(sessionId, selection)) return;
        data.items = data.items.map((item) =>
          item.id === updated.id ? updated : item,
        );
        render();
        if (action === "investigate") {
          await hooks.reloadInvestigations();
          if (
            !current(sessionId, selection) ||
            getState().generation !== generation ||
            getState().reportRevision !== reportRevision
          )
            return;
          await hooks.selectInvestigation(updated.investigation_id);
          if (
            current(sessionId, selection) &&
            getState().generation === generation
          ) {
            status("Rapport conservé. Les nouvelles mesures ne modifient pas ses conclusions.");
            $("investigation-title").scrollIntoView({ block: "start" });
          }
        } else {
          status(
            action === "dismiss"
              ? "Observation écartée. Ses preuves restent conservées."
              : "Observation remise à examiner.",
          );
        }
      }
    } catch (error) {
      if (current(sessionId, selection)) status(error.message, true);
    } finally {
      busy.delete(observation.id);
      mutationRevision += 1;
      if (current(sessionId, selection)) syncButtons();
    }
  }

  function button(observation, action, label, className = "button subtle") {
    const node = element("button", className, label);
    node.type = "button";
    node.dataset.action = action;
    node.addEventListener("click", () => act(observation, action));
    return node;
  }

  function card(observation) {
    const node = element("article", "observation-card");
    node.dataset.observationId = observation.id;
    const header = element("div", "observation-heading");
    header.append(
      element("h4", "", `Intervalle de réception · ${seconds(observation.duration_s)}`),
      element(
        "span",
        "observation-disposition",
        observation.disposition === "dismissed" ? "Écartée" : "À examiner",
      ),
    );
    node.append(
      header,
      element(
        "p",
        "small muted",
        `${sourceLabel(observation)} · ${seconds(observation.start_s)} → ${seconds(observation.end_s)} · échantillons #${observation.before_seq} et #${observation.after_seq}`,
      ),
    );
    const actions = element("div", "observation-actions");
    actions.append(
      button(observation, "window", "Voir les mesures"),
      button(
        observation,
        "investigate",
        observation.investigation_id ? "Ouvrir le rapport conservé" : "Investiguer localement",
      ),
      button(observation, "agent", "Préparer une question à l’agent"),
      button(
        observation,
        observation.disposition === "dismissed" ? "reopen" : "dismiss",
        observation.disposition === "dismissed" ? "Remettre à examiner" : "Écarter",
        "text-button",
      ),
    );
    const evidence = element("details", "observation-evidence");
    evidence.append(
      element("summary", "", "Preuves et règle de détection"),
      element("p", "small muted", `${observation.rule_version} · observation ${observation.id}`),
      element("pre", "evidence-data", JSON.stringify(observation.evidence, null, 2)),
    );
    node.append(actions, evidence);
    return node;
  }

  function syncButtons() {
    for (const [id, item] of cards)
      for (const node of item.node.querySelectorAll("button"))
        node.disabled = busy.has(id);
  }

  function render() {
    if (!data) return;
    const { items, scan, monitor } = data;
    const open = items.filter((item) => item.disposition === "open").length;
    $("observations-count").textContent = open;
    const showDismissed = $("observations-show-dismissed").checked;
    const visible = items.filter(
      (item) => showDismissed || item.disposition !== "dismissed",
    );
    const retained = new Set(items.map((item) => item.id));
    for (const [id, item] of cards)
      if (!retained.has(id)) {
        item.node.remove();
        cards.delete(id);
      }
    for (const observation of items) {
      const previous = cards.get(observation.id);
      const signature = JSON.stringify(observation);
      if (previous?.signature === signature) continue;
      const node = card(observation);
      const focused = previous?.node.contains(document.activeElement)
        ? document.activeElement.dataset.action
        : null;
      if (previous) {
        node.querySelector("details").open = previous.node.querySelector("details").open;
        previous.node.replaceWith(node);
      } else $("observations-list").append(node);
      cards.set(observation.id, { node, signature });
      if (focused)
        node.querySelector(`[data-action="${focused}"]`)?.focus({ preventScroll: true });
    }
    for (const observation of items)
      cards.get(observation.id).node.hidden =
        !showDismissed && observation.disposition === "dismissed";
    $("observations-empty").hidden = visible.length > 0;
    $("observations-empty").textContent = open === 0 && items.length
      ? `${items.length} observation(s) écartée(s). Activez le filtre pour les revoir.`
      : scan?.last_sample_seq < 1
        ? "Deux mesures consécutives sont nécessaires pour examiner un intervalle de réception."
        : scan?.complete
        ? "Aucun intervalle à examiner parmi les mesures parcourues. Le silence avant la première mesure ou après la dernière n’est pas évalué ici."
        : "Le parcours des mesures enregistrées est en cours.";
    const progress = scan?.last_sample_seq == null || scan.last_sample_seq < 0
      ? "En attente de mesures enregistrées."
      : scan.complete
        ? `Mesures parcourues jusqu’à l’échantillon #${scan.last_seq}.`
        : scan.last_seq < 0
          ? `Parcours en attente jusqu’à l’échantillon #${scan.last_sample_seq}.`
          : `Parcours en cours : échantillon #${scan.last_seq} sur #${scan.last_sample_seq}.`;
    const omitted = scan?.omitted_gap_count ?? 0;
    $("observations-scan").textContent =
      `${monitor?.available === false || monitor?.last_error ? "Suivi automatique indisponible. " : ""}${progress} ${items.length} observation(s) conservée(s) sur ${monitor?.max_per_session ?? 20} au maximum par session, y compris celles écartées.${omitted ? ` ${omitted} intervalle(s) supplémentaire(s) non conservé(s) : limite atteinte.` : ""}${monitor?.last_error ? ` ${monitor.last_error}` : ""}`;
    syncButtons();
  }

  async function refresh() {
    const sessionId = getState().id;
    const selection = revision;
    if (!sessionId || inFlight?.revision === selection) return;
    const request = ++requestId;
    const mutation = mutationRevision;
    inFlight = { revision: selection, request };
    try {
      const result = await api(path(sessionId));
      if (!current(sessionId, selection) || mutation !== mutationRevision) return;
      const first = !data;
      data = result;
      render();
      if (first || $("observations-status").classList.contains("is-error")) status("");
    } catch (error) {
      if (current(sessionId, selection) && mutation === mutationRevision)
        status(`Suivi des observations indisponible : ${error.message}`, true);
    } finally {
      if (inFlight?.request === request) inFlight = null;
    }
  }

  async function selectionChanged() {
    revision += 1;
    data = null;
    cards.clear();
    $("observations-list").replaceChildren();
    $("observations-count").textContent = "—";
    $("observations-scan").textContent = "";
    $("observations-empty").hidden = true;
    status("Chargement des observations conservées…");
    await refresh();
  }

  $("observations-show-dismissed").addEventListener("change", render);
  setInterval(refresh, 1000);
  return { selectionChanged };
}
