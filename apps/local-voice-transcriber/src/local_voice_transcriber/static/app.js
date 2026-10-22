(() => {
  "use strict";

  const api = "/api/v1";
  const state = {
    csrf: "", editor: null, user: null, generation: 0, editorGeneration: 0,
    jobsTimer: null, jobsGeneration: 0, jobs: [], pendingRuns: new Set(), authBusy: false,
  };
  const byId = (id) => document.getElementById(id);
  const statusLabels = {
    uploaded: "Загружена", queued: "В очереди", processing: "Обрабатывается",
    ready: "Готова", failed: "Ошибка обработки", cancelled: "Отменена", stalled: "Обработка прервана",
  };
  const stageLabels = {
    extracting_audio: "Подготовка аудио", asr: "Распознавание речи",
    diarizing: "Разделение говорящих", matching: "Сопоставление голосов",
    publishing: "Публикация результата",
  };
  const workerLabels = {
    running: "работает", degraded: "восстанавливается", idle: "ожидает",
    stopped: "остановлен", not_configured: "не настроен",
  };

  class StaleSessionError extends Error {}

  function requireGeneration(generation) {
    if (generation !== state.generation) throw new StaleSessionError();
  }

  function showError(error) {
    if (!(error instanceof StaleSessionError)) notify(error.message, true);
  }

  function authMode(mode) {
    const login = mode === "login";
    byId("login-form").hidden = !login;
    byId("register-form").hidden = login;
    byId("show-login").classList.toggle("active", login);
    byId("show-register").classList.toggle("active", !login);
    byId("show-login").setAttribute("aria-pressed", String(login));
    byId("show-register").setAttribute("aria-pressed", String(!login));
  }

  function setAuthBusy(busy) {
    state.authBusy = busy;
    document.querySelectorAll("#auth-panel button").forEach((button) => { button.disabled = busy; });
  }

  function updateSelectedMedia() {
    const file = byId("media-input").files[0];
    byId("selected-media").textContent = file
      ? file.name + " · " + new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(file.size / 1024 / 1024) + " МБ"
      : "Файл .webm не выбран";
    byId("add-job-button").disabled = !file;
  }

  function clearEditor() {
    state.editor = null;
    state.editorGeneration += 1;
    const player = byId("audio-player");
    player.pause();
    player.removeAttribute("src");
    player.load();
    byId("editor-title").textContent = "Редактор";
    byId("editor-status").textContent = "Выберите готовую запись";
    byId("editor-help").textContent = "Выберите готовую запись.";
    byId("subtitle-line").textContent = "Субтитры появятся после обработки.";
    document.querySelector(".waveform span").style.width = "0%";
    byId("save-editor-button").disabled = true;
    byId("editor-conflict").hidden = true;
    byId("groups-list").replaceChildren();
    byId("transcript-list").replaceChildren();
  }

  function clearSession(message = "Войдите или зарегистрируйтесь") {
    state.generation += 1;
    state.csrf = "";
    state.user = null;
    state.pendingRuns.clear();
    state.jobs = [];
    clearTimeout(state.jobsTimer);
    clearEditor();
    ["jobs-list", "profiles-list", "vocabulary-list", "admin-users", "admin-overview"].forEach((id) => byId(id).replaceChildren());
    ["login-form", "register-form", "upload-form", "profile-form", "vocabulary-form"].forEach((id) => byId(id).reset());
    document.querySelectorAll("#profile-form button, #vocabulary-form button, #admin-retry-index").forEach((button) => { button.disabled = false; });
    updateSelectedMedia();
    byId("choose-media").disabled = false;
    byId("media-input").disabled = false;
    byId("notice").hidden = true;
    byId("notice").textContent = "";
    byId("workspace").hidden = true;
    byId("auth-panel").hidden = false;
    document.querySelector(".admin-only").hidden = true;
    byId("session-state").textContent = message;
    activateView("jobs");
    authMode("login");
  }

  function text(tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    return node;
  }

  function notify(message, error = false) {
    const item = byId("notice");
    item.textContent = message;
    item.hidden = false;
    item.classList.toggle("error", error);
  }

  async function request(path, options = {}) {
    const generation = state.generation;
    const headers = new Headers(options.headers || {});
    const method = (options.method || "GET").toUpperCase();
    if (state.csrf && !["GET", "HEAD"].includes(method)) {
      headers.set("X-CSRF-Token", state.csrf);
    }
    let response;
    try {
      response = await fetch(api + path, {
        ...options,
        credentials: "same-origin",
        headers,
      });
    } catch (_) {
      requireGeneration(generation);
      throw new Error("Не удалось связаться с сервером. Проверьте соединение и повторите запрос.");
    }
    requireGeneration(generation);
    if (response.status === 401) {
      const hadSession = Boolean(state.user);
      clearSession(hadSession ? "Сессия истекла. Войдите снова." : "Войдите или зарегистрируйтесь");
      if (path !== "/auth/me") notify(hadSession ? "Сессия истекла. Войдите снова." : "Не удалось войти. Проверьте логин и пароль.", true);
      throw new StaleSessionError();
    }
    if (response.status === 204) return null;
    const body = await response.json().catch(() => ({}));
    requireGeneration(generation);
    if (!response.ok) {
      const revision = body.details?.current_revision_number;
      const suffix = revision ? " Текущая ревизия: " + revision + "." : "";
      const error = new Error((body.message || "Не удалось выполнить запрос.") + suffix);
      error.status = response.status;
      error.code = body.code;
      error.details = body.details;
      throw error;
    }
    return body;
  }

  function activateView(name) {
    if (name === "admin" && state.user?.role !== "admin") name = "jobs";
    document.querySelectorAll(".view").forEach((item) => {
      item.classList.toggle("active", item.id === name + "-view");
    });
    document.querySelectorAll("[data-view]").forEach((item) => {
      item.classList.toggle("active", item.dataset.view === name);
    });
  }

  function csrfCookie() {
    const prefix = "local_voice_transcriber_csrf=";
    const value = document.cookie.split("; ").find((item) => item.startsWith(prefix));
    return value ? decodeURIComponent(value.slice(prefix.length)) : "";
  }

  function setAuthenticated(user, csrf) {
    clearSession();
    state.user = user;
    state.csrf = csrf || state.csrf;
    byId("auth-panel").hidden = true;
    byId("workspace").hidden = false;
    byId("session-state").textContent = user.login + " · " + (user.role === "admin" ? "Администратор" : "Пользователь");
    document.querySelector(".admin-only").hidden = user.role !== "admin";
  }

  function formatTime(milliseconds) {
    const seconds = Math.floor((milliseconds || 0) / 1000);
    return String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
  }

  function seek(startMs) {
    const player = byId("audio-player");
    player.currentTime = startMs / 1000;
    player.play().catch(() => {});
  }

  function editorChanges(editor = state.editor) {
    if (!editor?.revision) return { segmentTexts: {}, groupUpdates: {} };
    const segmentTexts = {};
    editor.revision.segments.forEach((segment) => {
      const input = Array.from(document.querySelectorAll(".transcript-edit"))
        .find((item) => item.dataset.segmentId === segment.source_segment_id);
      if (input && input.value !== segment.text) segmentTexts[segment.source_segment_id] = input.value;
    });
    const groupUpdates = {};
    editor.revision.groups.forEach((group) => {
      const row = Array.from(document.querySelectorAll(".group-editor"))
        .find((item) => item.dataset.groupId === group.speaker_group_id);
      if (!row) return;
      const label = row.querySelector(".group-label-input").value.trim();
      const profileId = row.querySelector(".group-profile-select").value || null;
      const decision = editor.decisions.get(group.speaker_group_id);
      if (label && (label !== group.label || profileId !== group.profile_id || decision)) {
        groupUpdates[group.speaker_group_id] = {
          label_snapshot: label, profile_id: decision === "rejected" ? null : profileId,
          assignment_status: decision || (profileId !== group.profile_id
            ? (profileId ? "confirmed" : "unknown") : group.assignment_status),
          provenance: "manual",
        };
      }
    });
    return { segmentTexts, groupUpdates };
  }

  function hasEditorChanges(editor = state.editor) {
    const changes = editorChanges(editor);
    return Boolean(Object.keys(changes.segmentTexts).length || Object.keys(changes.groupUpdates).length);
  }

  function setEditorBusy(editor, busy) {
    if (state.editor !== editor) return;
    editor.saving = busy;
    const controls = document.querySelectorAll("#editor-view textarea, #editor-view input, #editor-view select, #editor-view button");
    if (busy) editor.disabledControls = new Map();
    controls.forEach((control) => {
      if (busy) {
        editor.disabledControls.set(control, control.disabled);
        control.disabled = true;
      } else if (editor.disabledControls.has(control)) {
        control.disabled = editor.disabledControls.get(control);
      }
    });
    if (!busy) editor.disabledControls = null;
  }

  function jobStatusText(job) {
    const stage = job.current_run?.stage;
    return (statusLabels[job.status] || "Неизвестный статус") +
      (stage && ["queued", "processing"].includes(job.status) ? " · " + (stageLabels[stage] || stage) : "");
  }

  function renderJobs(items) {
    const list = byId("jobs-list");
    list.replaceChildren();
    if (!items.length) {
      list.append(text("p", "Записей пока нет."));
      return;
    }
    items.forEach((job) => {
      const row = document.createElement("article");
      row.className = "job";
      const detail = document.createElement("div");
      const status = text("small", (statusLabels[job.status] || "Неизвестный статус") + " · " + (job.updated_at || ""));
      status.className = "status-" + job.status;
      detail.append(text("strong", job.source_name), status);
      const run = job.current_run || job.runs?.find((item) => item.id === job.current_run_id);
      if (run?.stage) detail.append(text("small", "Этап: " + (stageLabels[run.stage] || run.stage)));
      if (run?.attempt && run?.max_attempts) {
        detail.append(text("small", "Попытка " + run.attempt + " из " + run.max_attempts));
      }
      if (run?.error_code) detail.append(text("small", "Ошибка: " + run.error_code));
      const actions = document.createElement("div");
      actions.className = "job-actions";
      if (["uploaded", "failed", "cancelled", "stalled"].includes(job.status)) {
        const start = text("button", state.pendingRuns.has(job.id) ? "Запуск…" : job.status === "uploaded" ? "Транскрибировать" : "Повторить обработку");
        start.type = "button";
        start.disabled = state.pendingRuns.has(job.id);
        start.addEventListener("click", () => startRun(job, start));
        actions.append(start);
      }
      if (["queued", "processing"].includes(job.status)) {
        const cancel = text("button", run?.cancel_requested ? "Отмена запрошена" : "Отменить");
        cancel.type = "button";
        cancel.className = "secondary";
        cancel.disabled = Boolean(run?.cancel_requested);
        cancel.addEventListener("click", async () => {
          cancel.disabled = true;
          try {
            await request("/jobs/" + encodeURIComponent(job.id) + "/cancel", { method: "POST" });
            notify("Отмена обработки запрошена.");
            await loadJobs();
          } catch (error) {
            showError(error);
            cancel.disabled = false;
          }
        });
        actions.append(cancel);
      }
      const button = text("button", "Открыть");
      button.type = "button";
      button.className = "secondary";
      button.addEventListener("click", () => openJob(job));
      actions.append(button);
      row.append(detail, actions);
      list.append(row);
    });
  }

  function renderProfiles(items) {
    const list = byId("profiles-list");
    list.replaceChildren();
    if (!items.length) {
      list.append(text("p", "Подтверждённых профилей пока нет."));
      return;
    }
    items.forEach((profile) => {
      const row = document.createElement("article");
      row.className = "profile";
      const aliases = profile.aliases?.length ? " · " + profile.aliases.join(", ") : "";
      row.append(
        text("strong", profile.display_name),
        text("small", (profile.active_sample_count || 0) + " образцов · " + profile.state + aliases),
      );
      list.append(row);
    });
  }

  function renderVocabulary(items) {
    const list = byId("vocabulary-list");
    list.replaceChildren();
    if (!items.length) {
      list.append(text("p", "Специфичных терминов пока нет."));
      return;
    }
    items.forEach((term) => {
      const row = document.createElement("article");
      row.className = "profile";
      const variants = term.variants?.length ? " · " + term.variants.join(", ") : "";
      row.append(text("strong", term.canonical), text("small", (term.context_hint || "Без подсказки") + variants));
      list.append(row);
    });
  }

  async function loadJobs() {
    clearTimeout(state.jobsTimer);
    const generation = state.generation;
    const load = ++state.jobsGeneration;
    let failed = true;
    try {
      const items = [];
      let cursor = null;
      const seen = new Set();
      do {
        const data = await request("/jobs?limit=50" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
        requireGeneration(generation);
        items.push(...(Array.isArray(data) ? data : data.items || []));
        cursor = Array.isArray(data) ? null : data.next_cursor || null;
        if (cursor && seen.has(cursor)) throw new Error("Сервер повторил страницу списка записей.");
        if (cursor) seen.add(cursor);
      } while (cursor);
      if (load !== state.jobsGeneration) return;
      state.jobs = items;
      renderJobs(items);
      failed = false;
      const opened = state.editor;
      const current = items.find((job) => job.id === opened?.jobId);
      if (current) {
        opened.job = current;
        byId("editor-status").textContent = jobStatusText(current);
        if (current.status === "ready" && !opened.revision && !opened.loading && !hasEditorChanges(opened)) {
          refreshEditor(opened).catch(showError);
        }
      }
    } finally {
      if (load === state.jobsGeneration && generation === state.generation &&
          (failed || state.jobs.some((job) => ["queued", "processing"].includes(job.status)) ||
           ["queued", "processing"].includes(state.editor?.job?.status) ||
           (state.editor?.job?.status === "ready" && !state.editor.revision))) {
        state.jobsTimer = setTimeout(() => { loadJobs().catch(showError); }, 4000);
      }
    }
  }

  async function startRun(job, button) {
    if (!state.user || state.pendingRuns.has(job.id)) return;
    const generation = state.generation;
    state.pendingRuns.add(job.id);
    button.disabled = true;
    button.textContent = "Запуск…";
    try {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      const key = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
      await request("/jobs/" + encodeURIComponent(job.id) + "/runs", {
        method: "POST", headers: { "Idempotency-Key": key },
      });
      state.pendingRuns.delete(job.id);
      await loadJobs();
      requireGeneration(generation);
      notify("Запись поставлена в очередь транскрибации.");
    } catch (error) {
      showError(error);
    } finally {
      if (generation === state.generation) {
        state.pendingRuns.delete(job.id);
        button.disabled = false;
        button.textContent = "Транскрибировать";
      }
    }
  }

  async function loadProfiles() {
    const generation = state.generation;
    const data = await request("/voice-profiles");
    requireGeneration(generation);
    renderProfiles(data.items || []);
    return data.items || [];
  }

  async function loadVocabulary() {
    const generation = state.generation;
    const data = await request("/vocabulary");
    requireGeneration(generation);
    renderVocabulary(data.items || []);
  }

  async function loadAdmin() {
    const generation = state.generation;
    if (state.user?.role !== "admin") return;
    const [overview, users] = await Promise.all([request("/admin/overview"), request("/admin/users")]);
    requireGeneration(generation);
    if (state.user?.role !== "admin") return;
    renderAdminOverview(overview);
    renderAdminUsers(users.items || []);
  }

  function renderAdminOverview(overview) {
    const container = byId("admin-overview");
    container.replaceChildren();
    const cards = [
      ["Сервис", overview.checks?.database === "ok" ? "Доступен" : "Требует проверки", "База данных: " + (overview.checks?.database === "ok" ? "доступна" : "нет данных или ошибка")],
      ["Пользователи", String(overview.users_active ?? overview.users_count ?? "—") + " активных", "Всего: " + (overview.users_total ?? "—")],
      ["Записи", String(overview.recordings_total ?? "—"), Object.entries(overview.recordings_by_status || overview.by_status || {}).map(([key, value]) => (statusLabels[key] || key) + ": " + value).join(" · ")],
      ["Очередь", String(overview.queue?.queued ?? overview.by_status?.queued ?? "—") + " ожидают", "Обрабатываются: " + (overview.queue?.processing ?? overview.by_status?.processing ?? "—") + " · Обработчик: " + (workerLabels[overview.queue?.worker || overview.checks?.worker] || overview.queue?.worker || overview.checks?.worker || "нет данных")],
      ["ASR", overview.asr?.ready ? "Готов" : "Не готов", overview.asr?.reason || overview.asr?.pack_id || "Нет сведений о модели"],
      ["Диаризация", overview.diarization?.ready ? "Готова" : "Не готова", overview.diarization?.reason || overview.diarization?.pack_id || "Нет сведений о модели"],
      ["Голосовой индекс", overview.voice_index?.ready ? "Готов" : "Не готов", overview.voice_index?.reason || overview.voice_vector_store?.availability || "Нет сведений"],
      ["Голосовые профили", String(overview.active_voice_profiles ?? "—"), "Активных профилей"],
    ];
    cards.forEach(([title, value, detail]) => {
      const card = document.createElement("div");
      card.className = "overview-item";
      card.append(text("h4", title), text("strong", value));
      if (detail) card.append(text("small", detail));
      container.append(card);
    });
  }

  function renderAdminUsers(users) {
    const list = byId("admin-users");
    list.replaceChildren();
    users.forEach((user) => {
      const row = document.createElement("article");
      row.className = "profile admin-user";
      const detail = document.createElement("div");
      detail.append(text("strong", user.login));
      detail.append(text("small", (user.role === "admin" ? "Администратор" : "Пользователь") + " · " + (user.is_active ? "Активен" : "Отключён")));
      const stats = user.statistics;
      detail.append(text("p", stats
        ? "Всего записей: " + stats.recordings_total + ". " + Object.keys(statusLabels).map((status) => statusLabels[status] + ": " + (stats.by_status?.[status] || 0)).join(" · ")
        : "Статистика пока недоступна."));
      const button = text("button", user.role === "admin" ? "Снять права администратора" : "Назначить администратором");
      button.type = "button";
      button.className = "secondary";
      button.addEventListener("click", async () => {
        if (button.disabled) return;
        const generation = state.generation;
        button.disabled = true;
        try {
          const result = await request("/admin/users/" + encodeURIComponent(user.id), {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ role: user.role === "admin" ? "user" : "admin" }),
          });
          requireGeneration(generation);
          if (state.user.id === result.user.id && result.user.role !== "admin") {
            setAuthenticated(result.user, state.csrf);
            await loadJobs();
          } else {
            await loadAdmin();
          }
          notify("Роль пользователя обновлена.");
        } catch (error) {
          showError(error);
        } finally {
          if (generation === state.generation) button.disabled = false;
        }
      });
      row.append(detail, button);
      list.append(row);
    });
  }

  function renderTranscript() {
    const list = byId("transcript-list");
    list.replaceChildren();
    const editor = state.editor;
    if (!editor?.revision) {
      list.append(text("p", editor?.unavailable || "Реплики ещё не опубликованы."));
      return;
    }
    editor.revision.segments.forEach((segment) => {
      const timing = editor.timingBySegment.get(segment.source_segment_id);
      const row = document.createElement("article");
      row.className = "transcript-row";
      const seekButton = text("button", timing ? formatTime(timing.start_ms) : "—");
      seekButton.type = "button";
      seekButton.className = "timestamp";
      seekButton.dataset.segmentId = segment.source_segment_id;
      seekButton.disabled = !timing;
      if (timing) seekButton.addEventListener("click", () => seek(timing.start_ms));
      const body = document.createElement("div");
      body.className = "transcript-body";
      const groupIds = segment.speaker_group_ids?.length ? segment.speaker_group_ids :
        segment.primary_group_id ? [segment.primary_group_id] : [];
      body.append(text("small", groupIds.length
        ? "Говорящие: " + groupIds.map((id) => editor.groupById.get(id)?.label || id).join(" · ")
        : "Неизвестный говорящий"));
      if (segment.requires_review || groupIds.length > 1) {
        const badge = text("span", "Одновременная речь — проверить");
        badge.className = "review-badge";
        body.append(badge);
        row.classList.add("requires-review");
      }
      const input = document.createElement("textarea");
      input.className = "transcript-edit";
      input.dataset.segmentId = segment.source_segment_id;
      input.rows = Math.max(2, Math.ceil(segment.text.length / 72));
      input.value = segment.text;
      input.setAttribute("aria-label", "Текст реплики");
      body.append(input);
      const suggestions = editor.suggestions?.filter((item) => item.source_segment_id === segment.source_segment_id) || [];
      suggestions.forEach((suggestion) => {
        const button = text("button", "Подставить «" + suggestion.canonical + "» вместо «" + suggestion.matched_variant + "»");
        button.type = "button";
        button.className = "secondary suggestion-button";
        button.addEventListener("click", () => {
          if (editor.saving) return;
          const start = suggestion.start_offset;
          const end = suggestion.end_offset;
          if (Number.isInteger(start) && Number.isInteger(end) &&
              input.value.slice(start, end).localeCompare(
                suggestion.matched_variant, undefined, { sensitivity: "accent" },
              ) === 0) {
            input.value = input.value.slice(0, start) + suggestion.canonical + input.value.slice(end);
          } else if (!Number.isInteger(start) && input.value.includes(suggestion.matched_variant)) {
            input.value = input.value.replace(suggestion.matched_variant, suggestion.canonical);
          } else {
            notify("Текст реплики уже изменён. Проверьте подсказку вручную.", true);
            return;
          }
          button.disabled = true;
          notify("Подсказка внесена в черновик. Сохраните правки.");
        });
        body.append(button);
      });
      row.append(seekButton, body);
      list.append(row);
    });
  }

  function profileOptions(select, selectedProfileId) {
    const empty = text("option", "Без общего профиля");
    empty.value = "";
    select.append(empty);
    state.editor.profiles.forEach((profile) => {
      const option = text("option", profile.display_name);
      option.value = profile.id;
      option.selected = profile.id === selectedProfileId;
      select.append(option);
    });
  }

  function renderGroups() {
    const list = byId("groups-list");
    list.replaceChildren();
    const editor = state.editor;
    if (!editor?.revision) {
      list.append(text("p", "Группы станут доступны после публикации транскрипции."));
      return;
    }
    const assignmentLabels = {
      unknown: "не определён",
      ambiguous: "несколько вариантов",
      candidate: "требуется проверка",
      auto_assigned: "распознано автоматически",
      confirmed: "подтверждено вручную",
      rejected: "отклонено",
    };
    editor.revision.groups.forEach((group) => {
      const row = document.createElement("article");
      row.className = "group-editor";
      row.dataset.groupId = group.speaker_group_id;
      row.append(
        text(
          "small",
          group.merged_into_group_id
            ? "Объединена с " + group.merged_into_group_id
            : "Локальная группа · " + (assignmentLabels[group.assignment_status] || "не определён"),
        ),
      );
      const context = editor.revision.segments.filter((segment) =>
        segment.speaker_group_ids?.includes(group.speaker_group_id));
      const overlaps = context.filter((segment) => segment.requires_review || segment.speaker_group_ids?.length > 1).length;
      row.append(text("small", "Реплик: " + context.length + " · На проверку: " + overlaps +
        " · Источник: " + (group.provenance || "не указан")));
      if (context.length) row.append(text("small", "Первый фрагмент: " + context[0].text));
      (group.candidates || []).slice(0, 3).forEach((candidate) => {
        const profileName = editor.profiles.find((item) => item.id === candidate.profile_id)?.display_name || "Профиль недоступен";
        const distance = Number.isFinite(candidate.distance) ? " · расстояние " + candidate.distance : "";
        row.append(text("small", "Кандидат: " + profileName + distance +
          (candidate.reason ? " · " + candidate.reason : "")));
      });
      const name = document.createElement("input");
      name.className = "group-label-input";
      name.value = group.label;
      name.maxLength = 240;
      name.setAttribute("aria-label", "Метка группы");
      const profile = document.createElement("select");
      profile.className = "group-profile-select";
      profileOptions(profile, group.profile_id);
      const enroll = text("button", "Добавить образец голоса");
      enroll.type = "button";
      enroll.className = "secondary enroll-sample";
      enroll.dataset.eligible = String(!group.merged_into_group_id && group.assignment_status === "confirmed" && Boolean(group.profile_id));
      const consent = document.createElement("input");
      consent.type = "checkbox";
      consent.checked = false;
      const consentLabel = document.createElement("label");
      consentLabel.className = "sample-consent";
      consentLabel.append(consent, text("span", "После проверки качества добавить мой фрагмент в общий профиль"));
      consent.disabled = enroll.dataset.eligible !== "true";
      enroll.disabled = true;
      consent.addEventListener("change", () => { enroll.disabled = !consent.checked || enroll.dataset.eligible !== "true"; });
      enroll.addEventListener("click", async () => {
        if (enroll.disabled || state.editor !== editor) return;
        const profileId = profile.value;
        if (!profileId) {
          notify("Сначала выберите общий голосовой профиль для этой группы.", true);
          return;
        }
        enroll.disabled = true;
        try {
          const result = await request(
            "/jobs/" + encodeURIComponent(editor.jobId) + "/transcript/groups/" +
              encodeURIComponent(group.speaker_group_id) + "/samples",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ profile_id: profileId }),
            },
          );
          await loadProfiles();
          if (state.editor !== editor) return;
          notify(
            result.indexing_state === "active"
              ? "Образец добавлен в общий голосовой индекс."
              : "Образец сохранён; общий голосовой индекс повторит обработку автоматически.",
          );
          consent.checked = false;
        } catch (error) {
          showError(error);
        } finally {
          if (state.editor === editor) enroll.disabled = !consent.checked || enroll.dataset.eligible !== "true";
        }
      });
      row.append(name, profile);
      profile.addEventListener("change", () => { editor.decisions.delete(group.speaker_group_id); });
      if (["candidate", "ambiguous", "auto_assigned"].includes(group.assignment_status)) {
        const decisions = document.createElement("div");
        decisions.className = "group-decisions";
        const pending = text("small", "Решение не подтверждено вручную.");
        const confirm = text("button", "Подтвердить");
        confirm.type = "button";
        confirm.addEventListener("click", () => {
          if (!profile.value) {
            notify("Выберите профиль перед подтверждением.", true);
            return;
          }
          editor.decisions.set(group.speaker_group_id, "confirmed");
          pending.textContent = "Подтверждение ожидает сохранения.";
        });
        const reject = text("button", "Отклонить");
        reject.type = "button";
        reject.className = "secondary";
        reject.addEventListener("click", () => {
          editor.decisions.set(group.speaker_group_id, "rejected");
          pending.textContent = "Отклонение ожидает сохранения.";
        });
        decisions.append(confirm, reject, pending);
        row.append(decisions);
      }
      row.append(consentLabel, enroll);
      list.append(row);
    });
  }

  function currentSubtitle(timeMs) {
    const editor = state.editor;
    if (!editor?.revision) return null;
    return editor.revision.segments.find((segment) => {
      const timing = editor.timingBySegment.get(segment.source_segment_id);
      return timing && timeMs >= timing.start_ms && timeMs <= timing.end_ms;
    });
  }

  function renderSubtitle(timeMs) {
    const subtitle = currentSubtitle(timeMs);
    byId("subtitle-line").textContent = subtitle ? subtitle.text : "Субтитры появятся после обработки.";
    document.querySelectorAll(".transcript-row").forEach((row) => {
      const id = row.querySelector("textarea")?.dataset.segmentId;
      const timing = state.editor?.timingBySegment.get(id);
      row.classList.toggle("active", Boolean(timing && timeMs >= timing.start_ms && timeMs <= timing.end_ms));
    });
  }

  async function refreshEditor(editor) {
    if (state.editor !== editor || editor.loading || editor.saving || hasEditorChanges(editor)) return;
    editor.loading = true;
    const generation = state.generation;
    try {
      const path = "/jobs/" + encodeURIComponent(editor.jobId);
      const [source, profiles] = await Promise.all([
        request(path + "/source"), request("/voice-profiles"),
      ]);
      if (generation !== state.generation || state.editor !== editor) return;
      let transcript = null;
      let unavailable = "";
      try {
        transcript = await request(path + "/transcript");
      } catch (error) {
        if (error instanceof StaleSessionError || state.editor !== editor) return;
        unavailable = error.message;
      }
      if (generation !== state.generation || state.editor !== editor || hasEditorChanges(editor)) return;
      editor.profiles = profiles.items || [];
      editor.revision = transcript;
      editor.timingBySegment = new Map((source.segments || []).map((segment) => [segment.id, segment]));
      editor.groupById = new Map((transcript?.groups || []).map((group) => [group.speaker_group_id, group]));
      editor.unavailable = unavailable;
      editor.decisions.clear();
      editor.suggestions = [];
      if (transcript?.revision?.number) {
        try {
          const suggestions = await request(path + "/vocabulary/suggestions?expected_revision_number=" + transcript.revision.number);
          if (state.editor === editor && generation === state.generation) editor.suggestions = suggestions.items || [];
        } catch (error) {
          if (error instanceof StaleSessionError || state.editor !== editor) return;
        }
      }
      if (state.editor !== editor || generation !== state.generation || hasEditorChanges(editor)) return;
      renderGroups();
      renderTranscript();
      byId("save-editor-button").disabled = !transcript;
      byId("editor-help").textContent = transcript
        ? "Ревизия " + transcript.revision.number + ". Правки сохраняются новой ревизией; отмеченные наложения требуют проверки."
        : unavailable || "Дождитесь публикации результата обработки.";
    } catch (error) {
      if (error instanceof StaleSessionError || state.editor !== editor) return;
      editor.unavailable = error.message;
      if (!editor.revision && !hasEditorChanges(editor)) {
        renderGroups();
        renderTranscript();
        byId("editor-help").textContent = error.message;
      }
      showError(error);
    } finally {
      editor.loading = false;
    }
  }

  async function openJob(job) {
    if (!state.user) return;
    if (state.editor?.jobId === job.id) {
      activateView("editor");
      return;
    }
    if (state.editor?.saving) {
      notify("Дождитесь окончания сохранения правок.", true);
      return;
    }
    if (hasEditorChanges() && !window.confirm("Есть несохранённые правки. Открыть другую запись и потерять их?")) return;
    clearEditor();
    const editor = {
      jobId: job.id, job, profiles: [], revision: null, timingBySegment: new Map(),
      groupById: new Map(), decisions: new Map(), suggestions: [], unavailable: "", loading: false, saving: false,
    };
    state.editor = editor;
    byId("editor-title").textContent = job.source_name;
    byId("editor-status").textContent = jobStatusText(job);
    byId("audio-player").src = api + "/jobs/" + encodeURIComponent(job.id) + "/audio";
    activateView("editor");
    await refreshEditor(editor);
  }

  async function saveEditor() {
    const editor = state.editor;
    if (!editor?.revision || editor.saving || byId("save-editor-button").disabled) return;
    const { segmentTexts, groupUpdates } = editorChanges(editor);
    if (!Object.keys(segmentTexts).length && !Object.keys(groupUpdates).length) {
      notify("Нет изменений для сохранения.");
      return;
    }
    setEditorBusy(editor, true);
    try {
      const result = await request("/jobs/" + editor.jobId + "/transcript", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision_number: editor.revision.revision.number,
          segment_texts: segmentTexts,
          group_updates: groupUpdates,
        }),
      });
      if (state.editor !== editor) return;
      editor.revision = result;
      editor.groupById = new Map(result.groups.map((group) => [group.speaker_group_id, group]));
      editor.decisions.clear();
      editor.suggestions = [];
      byId("editor-conflict").hidden = true;
      renderGroups();
      renderTranscript();
      notify("Правки сохранены как ревизия " + result.revision.number + ".");
    } catch (error) {
      if (state.editor === editor && error.status === 409) {
        byId("editor-conflict-message").textContent = "Запись изменилась в другой вкладке. Ваши правки остались в редакторе. Скопируйте их перед обновлением. " + error.message;
        byId("editor-conflict").hidden = false;
      }
      if (state.editor === editor) showError(error);
    } finally {
      if (state.editor === editor) setEditorBusy(editor, false);
    }
  }

  byId("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (state.authBusy) return;
    const values = Object.fromEntries(new FormData(form));
    clearSession();
    setAuthBusy(true);
    try {
      const result = await request("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      setAuthenticated(result.user, csrfCookie());
      await loadJobs();
      notify("Вход выполнен.");
    } catch (error) {
      showError(error);
    } finally {
      setAuthBusy(false);
    }
  });

  byId("register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (state.authBusy) return;
    state.generation += 1;
    setAuthBusy(true);
    try {
      const result = await request("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(form))),
      });
      notify("Учётная запись «" + result.user.login + "» создана. Теперь войдите.");
      form.reset();
      authMode("login");
      byId("login-form").elements.login.value = result.user.login;
    } catch (error) {
      showError(error);
    } finally {
      setAuthBusy(false);
    }
  });

  byId("upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = byId("add-job-button");
    if (button.disabled || !state.user) return;
    const file = byId("media-input").files[0];
    if (!file || !file.name.toLowerCase().endsWith(".webm")) {
      notify("Выберите файл .webm.", true);
      return;
    }
    const generation = state.generation;
    const body = new FormData(form);
    button.disabled = true;
    byId("choose-media").disabled = true;
    byId("media-input").disabled = true;
    try {
      await request("/jobs", { method: "POST", body });
      form.reset();
      updateSelectedMedia();
      await loadJobs();
      requireGeneration(generation);
      notify("Запись добавлена в список. Нажмите «Транскрибировать», чтобы начать обработку.");
    } catch (error) {
      showError(error);
    } finally {
      if (generation === state.generation) {
        byId("choose-media").disabled = false;
        byId("media-input").disabled = false;
        updateSelectedMedia();
      }
    }
  });

  byId("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type='submit']");
    if (button.disabled || !state.user) return;
    const generation = state.generation;
    button.disabled = true;
    try {
      await request("/voice-profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(form))),
      });
      form.reset();
      await loadProfiles();
      requireGeneration(generation);
      notify("Общий профиль создан без добавления образца голоса.");
    } catch (error) {
      showError(error);
    } finally {
      if (generation === state.generation) button.disabled = false;
    }
  });

  byId("vocabulary-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type='submit']");
    if (button.disabled || !state.user) return;
    const generation = state.generation;
    const values = Object.fromEntries(new FormData(form));
    const variants = String(values.variants || "").split(",").map((item) => item.trim()).filter(Boolean);
    button.disabled = true;
    try {
      await request("/vocabulary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ canonical: values.canonical, variants }),
      });
      form.reset();
      await loadVocabulary();
      requireGeneration(generation);
      notify("Термин добавлен в общий словарь будущих запусков.");
    } catch (error) {
      showError(error);
    } finally {
      if (generation === state.generation) button.disabled = false;
    }
  });

  byId("save-editor-button").addEventListener("click", saveEditor);
  byId("copy-editor-draft").addEventListener("click", async () => {
    const editor = state.editor;
    if (!editor?.revision) return;
    const lines = editor.revision.segments.map((segment) => {
      const input = Array.from(document.querySelectorAll(".transcript-edit"))
        .find((item) => item.dataset.segmentId === segment.source_segment_id);
      return (input?.value ?? segment.text);
    });
    const groups = Array.from(document.querySelectorAll(".group-editor")).map((row) => {
      const label = row.querySelector(".group-label-input")?.value || "";
      const profile = row.querySelector(".group-profile-select");
      return label + " — " + (profile?.selectedOptions[0]?.textContent || "Без общего профиля");
    });
    try {
      await navigator.clipboard.writeText("Группы:\n" + groups.join("\n") + "\n\nТекст:\n" + lines.join("\n\n"));
      notify("Ваши правки скопированы. Теперь можно обновить запись.");
    } catch (_) {
      notify("Не удалось скопировать автоматически. Выделите и скопируйте правки в полях редактора.", true);
    }
  });
  byId("reload-editor").addEventListener("click", () => {
    const editor = state.editor;
    if (!editor || editor.saving) return;
    if (hasEditorChanges(editor) && !window.confirm("Обновление удалит несохранённые правки. Продолжить?")) return;
    const job = editor.job;
    clearEditor();
    openJob(job).catch(showError);
  });

  byId("admin-retry-index").addEventListener("click", async () => {
    const button = byId("admin-retry-index");
    if (button.disabled || state.user?.role !== "admin") return;
    const generation = state.generation;
    button.disabled = true;
    try {
      const result = await request("/admin/voice-index/reconcile", { method: "POST" });
      await loadAdmin();
      requireGeneration(generation);
      notify(
        result.processed_operation_count
          ? "Голосовой индекс обновлён. Обработано операций: " + result.processed_operation_count + "."
          : "Голосовой индекс уже актуален.",
      );
    } catch (error) {
      showError(error);
    } finally {
      if (generation === state.generation) button.disabled = false;
    }
  });

  byId("logout-button").addEventListener("click", async () => {
    if (state.editor?.saving) {
      notify("Дождитесь окончания сохранения правок.", true);
      return;
    }
    if (hasEditorChanges() && !window.confirm("Есть несохранённые правки. Выйти и потерять их?")) return;
    const csrf = state.csrf;
    clearSession("Сессия завершена");
    setAuthBusy(true);
    try {
      await request("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": csrf } });
    } catch (error) {
      showError(error);
    } finally {
      setAuthBusy(false);
    }
  });

  byId("show-login").addEventListener("click", () => authMode("login"));
  byId("show-register").addEventListener("click", () => authMode("register"));
  byId("choose-media").addEventListener("click", () => byId("media-input").click());
  byId("media-input").addEventListener("change", updateSelectedMedia);

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!state.user) return;
      activateView(button.dataset.view);
      try {
        if (button.dataset.view === "profiles") await loadProfiles();
        if (button.dataset.view === "vocabulary") await loadVocabulary();
        if (button.dataset.view === "jobs") await loadJobs();
        if (button.dataset.view === "admin") await loadAdmin();
      } catch (error) {
        showError(error);
      }
    });
  });

  byId("audio-player").addEventListener("timeupdate", (event) => {
    const player = event.currentTarget;
    const timeMs = player.currentTime * 1000;
    const percentage = Number.isFinite(player.duration) && player.duration > 0
      ? player.currentTime / player.duration * 100 : 0;
    document.querySelector(".waveform span").style.width = Math.min(100, Math.max(0, percentage)) + "%";
    renderSubtitle(timeMs);
  });

  window.addEventListener("beforeunload", (event) => {
    if (!state.editor?.saving && !hasEditorChanges()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  (async () => {
    try {
      const result = await request("/auth/me");
      setAuthenticated(result.user, csrfCookie());
      await loadJobs();
    } catch (error) {
      showError(error);
    }
  })();
})();
