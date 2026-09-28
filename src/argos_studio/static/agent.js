const STATUS = {
  running: "Investigation en cours",
  completed: "Investigation terminée",
  cancelled: "Investigation annulée",
  interrupted: "Investigation interrompue",
  failed: "Investigation échouée",
  limited: "Limite de l’investigation atteinte",
};

export function initAgent(hooks) {
  const { getState, api, post, element, formatDate, seconds } = hooks;
  const $ = (id) => document.getElementById(id);
  const text = (id, value) => {
    $(id).textContent = value ?? "";
  };
  const path = (sessionId, runId = "", suffix = "") =>
    `/api/sessions/${encodeURIComponent(sessionId)}/agent-runs${runId ? `/${encodeURIComponent(runId)}` : ""}${suffix}`;
  const drafts = new Map();
  const observationDrafts = new Map();
  let configuration = null;
  let sessionRevision = 0;
  let selectionRevision = 0;
  let mutationRevision = 0;
  let healthRevision = 0;
  let runs = [];
  let selected = null;
  let active = null;
  let submitting = false;
  let cancelling = false;
  let polling = false;
  let healthPolling = false;
  let loading = false;
  let rendered = null;
  let activeError = "";

  function status(message, error = false) {
    text("agent-status", message);
    $("agent-status").classList.toggle("is-error", error);
  }

  function sync() {
    const state = getState();
    const configured = configuration?.available === true;
    text(
      "agent-provider",
      configuration?.provider
        ? `${configuration.provider} · ${configuration.model ?? "modèle non renseigné"}`
        : "Fournisseur non configuré",
    );
    text(
      "agent-availability",
      configured
        ? "Disponible sur demande. Aucun envoi continu des acquisitions au modèle."
        : configuration?.reason ||
            "Configurez un fournisseur sur le service local pour activer l’agent. Les outils d’analyse restent utilisables ci-dessous.",
    );
    text(
      "agent-disclosure",
      !configured
        ? "Aucune requête au modèle ne sera envoyée tant que le fournisseur n’est pas configuré."
        : configuration.sends_data_off_machine
          ? `En envoyant cette demande, votre question, le contexte de cette session et les extraits consultés par les outils seront transmis à ${configuration.provider}.`
          : `Cette demande et les extraits consultés seront transmis au fournisseur configuré : ${configuration.provider}.`,
    );
    const limits = configuration?.limits;
    text(
      "agent-limits",
      limits
        ? `Limites : ${limits.max_rounds} appels au modèle · ${limits.max_tool_calls} appels d’outils · ${Number(limits.max_total_output_tokens).toLocaleString("fr-FR")} tokens de sortie · ${limits.deadline_s} s`
        : "",
    );
    text(
      "agent-window",
      `Fenêtre demandée : ${state.start === null ? "début de session" : seconds(state.start)} → ${state.end === null ? "dernière observation au lancement" : seconds(state.end)}. La fenêtre est figée au lancement ; les résultats des outils sont conservés.`,
    );
    const observation = observationDrafts.get(state.id);
    $("agent-observation").hidden = !observation;
    text(
      "agent-observation",
      observation
        ? `Demande préparée depuis l’observation ${observation.id}. Aucun appel au modèle n’a encore été lancé par cette préparation.`
        : "",
    );
    $("agent-submit").disabled =
      !configured || !state.detail || submitting || Boolean(active);
    text(
      "agent-submit",
      submitting ? "Envoi de la demande…" : "Demander à l’agent",
    );
    $("agent-history").disabled = loading || runs.length === 0;
    $("agent-export").disabled = !selected || selected.status === "running";
    $("agent-cancel").hidden = selected?.status !== "running";
    $("agent-cancel").disabled = cancelling;
    $("active-agent").hidden = !active;
    $("active-agent-cancel").disabled = cancelling;
    if (active) {
      const session = state.sessions.find(
        (item) => item.id === active.session_id,
      );
      text(
        "active-agent-state",
        activeError ||
          `${session?.name ?? "Session enregistrée"} · outils et réponse en cours. L’annulation reste disponible pendant la navigation.`,
      );
    }
  }

  function syncHealth(value) {
    configuration = value ?? null;
    const pending = value?.active_run;
    if (pending) active = { id: pending.id, session_id: pending.session_id };
    // A terminal health response does not discard a known run before its final trace is read.
    sync();
  }

  function history() {
    const placeholder = element(
      "option",
      "",
      runs.length
        ? "Choisir une investigation de l’agent"
        : "Aucune investigation de l’agent",
    );
    placeholder.value = "";
    $("agent-history").replaceChildren(
      placeholder,
      ...runs.map((run) => {
        const option = element(
          "option",
          "",
          `${formatDate(run.created_at)} · ${STATUS[run.status] ?? run.status} · ${run.prompt}`,
        );
        option.value = run.id;
        return option;
      }),
    );
    $("agent-history").value = selected?.id ?? "";
    sync();
  }

  async function list(sessionId, revision, selectLatest = false) {
    const requestSelection = selectionRevision;
    const requestMutation = mutationRevision;
    try {
      const result = await api(path(sessionId));
      if (revision !== sessionRevision || requestMutation !== mutationRevision)
        return;
      runs = result;
      history();
      if (selectLatest && runs.length && requestSelection === selectionRevision)
        await select(runs[0].id);
      else if (!runs.length && !selected)
        status("Aucune investigation de l’agent pour cette session.");
    } catch (error) {
      if (revision === sessionRevision && requestMutation === mutationRevision)
        status(error.message, true);
    }
  }

  async function selectionChanged() {
    const revision = ++sessionRevision;
    selectionRevision += 1;
    runs = [];
    selected = null;
    rendered = null;
    loading = false;
    $("agent-detail").hidden = true;
    $("agent-prompt").value = drafts.get(getState().id) ?? "";
    windowChanged();
    status("Chargement des investigations de l’agent…");
    history();
    await list(getState().id, revision, true);
  }

  async function select(id) {
    if (!id) return;
    const sessionId = getState().id;
    const revision = sessionRevision;
    const selection = ++selectionRevision;
    const mutation = mutationRevision;
    loading = true;
    status("Chargement de la trace…");
    sync();
    try {
      const run = await api(path(sessionId, id));
      if (
        revision !== sessionRevision ||
        selection !== selectionRevision ||
        mutation !== mutationRevision
      )
        return;
      selected = run;
      if (run.status === "running")
        active = { id: run.id, session_id: run.session_id };
      render();
      history();
    } catch (error) {
      if (revision === sessionRevision && selection === selectionRevision)
        status(error.message, true);
    } finally {
      if (revision === sessionRevision && selection === selectionRevision) {
        loading = false;
        sync();
      }
    }
  }

  async function navigate(run, action) {
    let revision = getState().investigationSessionRevision;
    if (getState().id !== run.session_id) {
      const selection = hooks.selectSession(run.session_id);
      revision = getState().investigationSessionRevision;
      await selection;
    }
    if (
      getState().id !== run.session_id ||
      getState().investigationSessionRevision !== revision
    )
      return;
    await action();
  }

  function evidence(run) {
    const links = [];
    const seen = new Set();
    function button(key, title, action) {
      if (seen.has(key)) return;
      seen.add(key);
      const link = element("button", "report-evidence-link", title);
      link.type = "button";
      link.addEventListener("click", () =>
        navigate(run, action).catch((error) => status(error.message, true)),
      );
      links.push(link);
    }
    for (const step of run.steps ?? []) {
      const payload = step.payload;
      if (step.kind !== "tool_result" || payload?.ok !== true) continue;
      const result = payload.result;
      if (!result || typeof result !== "object") continue;
      const window = result.window_s;
      if (
        window &&
        Number.isFinite(window.start_s) &&
        Number.isFinite(window.end_s) &&
        window.start_s >= 0 &&
        window.end_s >= window.start_s
      )
        button(
          `window:${window.start_s}:${window.end_s}`,
          `Mesures : ${seconds(window.start_s)} → ${seconds(window.end_s)}`,
          async () => {
            await hooks.setWindow(window.start_s, window.end_s);
            if (getState().id === run.session_id)
              $("signal-title").scrollIntoView({ block: "start" });
          },
        );
      if (
        ["investigate_reception", "read_investigation"].includes(
          payload.name,
        ) &&
        typeof result.id === "string"
      )
        button(
          `report:${result.id}`,
          "Ouvrir l’investigation de réception",
          async () => {
            await hooks.selectInvestigation(result.id);
            if (getState().id === run.session_id)
              $("investigation-title").scrollIntoView({ block: "start" });
          },
        );
      if (
        ["prepare_synthetic_experiment", "read_experiment_result"].includes(
          payload.name,
        ) &&
        typeof result.id === "string"
      )
        button(
          `experiment:${result.id}`,
          "Examiner le protocole et son résultat",
          async () => {
            await hooks.refreshExperiments();
            if (getState().id !== run.session_id) return;
            $("experiment-history").value = result.id;
            $("experiment-history").dispatchEvent(new Event("change"));
            $("comparison-title").scrollIntoView({ block: "start" });
          },
        );
    }
    $("agent-evidence").replaceChildren(...links);
  }

  function render() {
    if (!selected) return;
    const signature = JSON.stringify(selected);
    if (signature === rendered) {
      sync();
      return;
    }
    rendered = signature;
    const run = selected;
    $("agent-detail").hidden = false;
    text("agent-run-state", STATUS[run.status] ?? run.status);
    text(
      "agent-run-meta",
      `${run.provider} · ${run.model} · ${formatDate(run.created_at)}`,
    );
    const window = run.context?.window_s;
    text(
      "agent-run-window",
      window
        ? `Fenêtre conservée : ${seconds(window.start_s)} → ${seconds(window.end_s)}.${run.context?.observation_id ? ` Observation d’origine : ${run.context.observation_id}.` : ""}`
        : "Fenêtre conservée dans le contexte de l’investigation.",
    );
    text("agent-run-prompt", run.prompt);
    text(
      "agent-answer",
      run.answer ||
        (run.status === "running"
          ? "L’agent consulte les outils. La réponse s’affichera à la fin de l’investigation."
          : "Aucune réponse finale conservée. Les étapes déjà réalisées restent consultables."),
    );
    $("agent-answer-note").hidden = !run.answer;
    $("agent-run-error").hidden = !run.error;
    text(
      "agent-run-error",
      typeof run.error === "string"
        ? run.error
        : run.error
          ? JSON.stringify(run.error)
          : "",
    );
    const steps = run.steps ?? [];
    const callCount = steps.filter((step) => step.kind === "tool_call").length;
    text(
      "agent-trace-summary",
      `${callCount} appel${callCount === 1 ? "" : "s"} d’outils · trace et consommation`,
    );
    $("agent-steps").replaceChildren(
      ...steps.map((step) => {
        const item = element("li");
        const detail = element("details");
        const label =
          step.kind === "tool_call"
            ? "Appel"
            : step.kind === "tool_result"
              ? step.payload?.ok
                ? "Résultat"
                : "Erreur d’outil"
              : "Consommation du fournisseur";
        detail.append(
          element(
            "summary",
            "",
            `${step.seq + 1}. ${label}${step.payload?.name ? ` · ${step.payload.name}` : ""}`,
          ),
        );
        detail.append(
          element(
            "pre",
            "evidence-data",
            JSON.stringify(step.payload, null, 2),
          ),
        );
        item.append(detail);
        return item;
      }),
    );
    text(
      "agent-usage",
      run.usage
        ? JSON.stringify(run.usage, null, 2)
        : "Consommation non renseignée par le fournisseur.",
    );
    evidence(run);
    status(
      run.status === "running"
        ? "La trace se complète pendant l’investigation. Vous pouvez naviguer ou annuler."
        : "Trace enregistrée. Aucun redémarrage automatique de l’agent.",
    );
    sync();
  }

  async function poll() {
    if (polling || !active) return;
    const target = { ...active };
    const revision = mutationRevision;
    const session = sessionRevision;
    const selection = selectionRevision;
    polling = true;
    try {
      const run = await api(path(target.session_id, target.id));
      if (revision !== mutationRevision || active?.id !== target.id) return;
      activeError = "";
      if (run.status !== "running") {
        active = null;
        healthRevision += 1;
      }
      if (getState().id === run.session_id && session === sessionRevision) {
        runs = [run, ...runs.filter((item) => item.id !== run.id)];
        if (selection === selectionRevision && selected?.id === run.id) {
          selected = run;
          render();
        }
        history();
      }
      if (run.status !== "running") {
        await hooks.refreshExperiments();
        await hooks.reloadInvestigations();
      }
      sync();
    } catch (error) {
      if (revision !== mutationRevision) return;
      activeError =
        "Le suivi de l’agent est indisponible. Le service conserve ses limites d’exécution ; vous pouvez réessayer l’annulation.";
      text("active-agent-state", activeError);
      if (getState().id === target.session_id) status(error.message, true);
    } finally {
      polling = false;
    }
  }

  async function cancel(target) {
    if (!target || cancelling) return;
    cancelling = true;
    activeError = "";
    mutationRevision += 1;
    healthRevision += 1;
    const revision = sessionRevision;
    const selection = selectionRevision;
    sync();
    try {
      const run = await post(path(target.session_id, target.id, "/cancel"));
      if (active?.id === run.id && run.status !== "running") active = null;
      if (getState().id === run.session_id && revision === sessionRevision) {
        runs = [run, ...runs.filter((item) => item.id !== run.id)];
        if (selection === selectionRevision && selected?.id === run.id) {
          selected = run;
          render();
        }
        history();
      }
      if (run.status !== "running") {
        await hooks.refreshExperiments();
        await hooks.reloadInvestigations();
      }
    } catch (error) {
      status(error.message, true);
      activeError = `Annulation non confirmée : ${error.message}`;
    } finally {
      cancelling = false;
      mutationRevision += 1;
      healthRevision += 1;
      sync();
    }
  }

  $("agent-prompt").addEventListener("input", () =>
    drafts.set(getState().id, $("agent-prompt").value),
  );
  function windowChanged() {
    const state = getState();
    const observation = observationDrafts.get(state.id);
    if (
      observation &&
      (state.start !== observation.start_s || state.end !== observation.end_s)
    )
      observationDrafts.delete(state.id);
    sync();
  }

  async function prepareObservation(observation) {
    if (getState().id !== observation.session_id) return;
    const revision = sessionRevision;
    await hooks.setWindow(observation.start_s, observation.end_s);
    if (revision !== sessionRevision) return;
    const state = getState();
    if (state.start !== observation.start_s || state.end !== observation.end_s)
      return;
    const prompt = `Examine l’observation ${observation.id} : intervalle de réception de ${observation.duration_s} s entre les échantillons #${observation.before_seq} et #${observation.after_seq}, de ${observation.start_s} à ${observation.end_s} s.${observation.investigation_id ? ` Consulte le rapport conservé ${observation.investigation_id}.` : ""} Appuie tes conclusions sur les preuves de cette fenêtre, distingue les hypothèses des faits et propose la prochaine vérification utile.`;
    observationDrafts.set(state.id, observation);
    drafts.set(state.id, prompt);
    $("agent-prompt").value = prompt;
    sync();
    $("agent-title").scrollIntoView({ block: "start" });
    $("agent-prompt").focus({ preventScroll: true });
  }
  $("agent-history").addEventListener("change", () =>
    select($("agent-history").value),
  );
  $("agent-cancel").addEventListener("click", () => cancel(selected));
  $("active-agent-cancel").addEventListener("click", () => cancel(active));
  $("active-agent-open").addEventListener("click", async () => {
    const target = active && { ...active };
    if (!target) return;
    await navigate(target, async () => {
      await select(target.id);
      if (getState().id === target.session_id)
        $("agent-title").scrollIntoView({ block: "start" });
    });
  });
  $("agent-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || active || !configuration?.available) return;
    const state = getState();
    const prompt = $("agent-prompt").value.trim();
    if (!state.detail || !prompt) return;
    const sessionId = state.id;
    const revision = sessionRevision;
    const selection = ++selectionRevision;
    const generation = state.generation;
    windowChanged();
    const observation = observationDrafts.get(sessionId);
    submitting = true;
    activeError = "";
    mutationRevision += 1;
    healthRevision += 1;
    status("Envoi de la demande à l’agent…");
    sync();
    try {
      const run = await post(path(sessionId), {
        prompt,
        start_s: state.start,
        end_s: state.end,
        ...(observation ? { observation_id: observation.id } : {}),
      });
      if (run.status === "running")
        active = { id: run.id, session_id: sessionId };
      if (revision === sessionRevision) {
        runs = [run, ...runs.filter((item) => item.id !== run.id)];
        if (
          selection === selectionRevision &&
          generation === getState().generation
        ) {
          selected = run;
          render();
        } else
          status(
            "Demande enregistrée pour la fenêtre précédente. Retrouvez-la dans l’historique.",
          );
        history();
      }
      if (run.status !== "running") {
        await hooks.refreshExperiments();
        await hooks.reloadInvestigations();
      }
    } catch (error) {
      if (revision === sessionRevision) status(error.message, true);
    } finally {
      submitting = false;
      mutationRevision += 1;
      healthRevision += 1;
      sync();
    }
  });
  $("agent-export").addEventListener("click", async () => {
    if (!selected || selected.status === "running") return;
    const run = selected;
    try {
      const data = await api(path(run.session_id, run.id, "/export"));
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
      );
      const link = element("a");
      link.href = url;
      link.download = `argos-studio-agent-${run.id}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      status(error.message, true);
    }
  });
  setInterval(poll, 750);
  setInterval(async () => {
    if (healthPolling || submitting || cancelling) return;
    healthPolling = true;
    const revision = healthRevision;
    try {
      const health = await api("/api/health");
      if (revision === healthRevision) syncHealth(health.agent);
    } catch {
      /* The selected session and active-run polls expose connection failures. */
    } finally {
      healthPolling = false;
    }
  }, 5000);
  return { selectionChanged, sync, syncHealth, prepareObservation, windowChanged };
}
