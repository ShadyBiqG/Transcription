(() => {
  "use strict";

  const api = "/api/v1";
  const supportedMediaExtensions = new Set([
    ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".mp4", ".mov",
  ]);
  const supportedMediaLabel = ".webm, .mp3, .wav, .m4a, .aac, .ogg, .opus, .mp4 или .mov";
  const state = {
    csrf: "", editor: null, user: null, generation: 0, editorGeneration: 0,
    jobsTimer: null, jobsGeneration: 0, jobs: [], pendingRuns: new Set(), authBusy: false,
    uploadBusy: false,
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
    byId("show-login").setAttribute("aria-selected", String(login));
    byId("show-register").setAttribute("aria-selected", String(!login));
    byId("show-login").tabIndex = login ? 0 : -1;
    byId("show-register").tabIndex = login ? -1 : 0;
  }

  function setAuthBusy(busy) {
    state.authBusy = busy;
    document.querySelectorAll("#auth-panel button").forEach((button) => { button.disabled = busy; });
  }

  function isSupportedMediaFile(file) {
    const name = String(file?.name || "").trim().toLowerCase();
    return supportedMediaExtensions.has(name.slice(name.lastIndexOf(".")));
  }

  function updateSelectedMedia() {
    const file = byId("media-input").files[0];
    const supported = isSupportedMediaFile(file);
    const form = byId("upload-form");
    const dropzone = byId("recording-dropzone");
    if (file && state.user) activateView("jobs");
    form.hidden = !file;
    dropzone.classList.toggle("has-draft", Boolean(file));
    byId("selected-media").textContent = file
      ? file.name + " · " + new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(file.size / 1024 / 1024) +
        " МБ" + (supported ? "" : " · неподдерживаемый формат")
      : "Файл не выбран";
    byId("upload-and-start-button").disabled = !file || !supported || state.uploadBusy;
  }

  function resetUploadDraft() {
    byId("upload-form").reset();
    updateSelectedMedia();
  }

  function selectMediaFile(file) {
    if (!file) return;
    const input = byId("media-input");
    try {
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
    } catch (_) {
      notify("Не удалось принять файл из области перетаскивания. Выберите его кнопкой «Добавить».", true);
      return;
    }
    updateSelectedMedia();
  }

  function openMediaPicker() {
    if (!state.user || state.uploadBusy) return;
    activateView("jobs");
    byId("media-input").click();
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
    byId("audio-help").hidden = true;
    byId("audio-help").textContent = "";
    document.querySelector(".waveform span").style.width = "0%";
    byId("save-editor-button").disabled = true;
    byId("download-text-button").hidden = true;
    byId("editor-conflict").hidden = true;
    byId("speaker-panel").replaceChildren();
    byId("open-speakers-button").textContent = "Говорящие · 0";
    byId("open-speakers-button").disabled = true;
    byId("speakers-dialog").close();
    byId("transcript-list").replaceChildren();
  }

  function clearSession(message = "Войдите или зарегистрируйтесь") {
    state.generation += 1;
    state.csrf = "";
    state.user = null;
    state.pendingRuns.clear();
    state.uploadBusy = false;
    state.jobs = [];
    clearTimeout(state.jobsTimer);
    clearEditor();
    ["jobs-list", "vocabulary-list", "admin-users", "admin-overview"].forEach((id) => byId(id).replaceChildren());
    ["login-form", "register-form", "upload-form", "vocabulary-form"].forEach((id) => byId(id).reset());
    document.querySelectorAll("#vocabulary-form button, #admin-retry-index").forEach((button) => { button.disabled = false; });
    updateSelectedMedia();
    byId("media-input").disabled = false;
    byId("notice").hidden = true;
    byId("notice").textContent = "";
    byId("workspace").hidden = true;
    byId("auth-panel").hidden = false;
    document.querySelectorAll(".admin-only").forEach((item) => { item.hidden = true; });
    byId("add-recording-button").hidden = true;
    byId("workspace-menu").hidden = true;
    byId("workspace-menu").open = false;
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
      const active = item.dataset.view === name;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    byId("workspace-menu").open = false;
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
    document.querySelectorAll(".admin-only").forEach((item) => { item.hidden = user.role !== "admin"; });
    byId("add-recording-button").hidden = false;
    byId("workspace-menu").hidden = false;
  }

  function formatJobDate(value) {
    const date = new Date(value || "");
    if (Number.isNaN(date.getTime())) return value || "Дата неизвестна";
    return new Intl.DateTimeFormat("ru-RU", {
      day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    }).format(date);
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
      const draft = editor.segmentDrafts.get(segment.source_segment_id);
      if (draft !== undefined && draft !== segment.text) segmentTexts[segment.source_segment_id] = draft;
    });
    return { segmentTexts, groupUpdates: {} };
  }

  function hasIdentityDrafts(editor = state.editor) {
    if (!editor?.revision) return false;
    return editor.revision.groups.some((group) => {
      const draft = editor.identityDrafts.get(group.speaker_group_id);
      return draft !== undefined && draft.trim() !== group.label;
    });
  }

  function hasEditorChanges(editor = state.editor) {
    const changes = editorChanges(editor);
    return Boolean(Object.keys(changes.segmentTexts).length || Object.keys(changes.groupUpdates).length ||
      hasIdentityDrafts(editor));
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
      const icon = text("span", "♫");
      icon.className = "job-file-icon";
      icon.setAttribute("aria-hidden", "true");
      const detail = document.createElement("div");
      detail.className = "job-detail";
      const openFromCard = () => openJob(job).catch(showError);
      detail.tabIndex = 0;
      detail.setAttribute("role", "button");
      detail.setAttribute("aria-label", "Открыть запись «" + job.source_name + "» в редакторе");
      detail.addEventListener("click", openFromCard);
      detail.addEventListener("keydown", (event) => {
        if (!["Enter", " "].includes(event.key)) return;
        event.preventDefault();
        openFromCard();
      });
      const status = text("small", (statusLabels[job.status] || "Неизвестный статус") + " · " + formatJobDate(job.updated_at || job.created_at));
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
      const menu = document.createElement("details");
      menu.className = "job-overflow";
      const menuToggle = document.createElement("summary");
      menuToggle.setAttribute("aria-label", "Действия с записью «" + job.source_name + "»");
      menuToggle.title = "Дополнительные действия";
      menuToggle.textContent = "⋮";
      const menuPanel = document.createElement("div");
      menuPanel.className = "job-overflow-panel";
      const open = text("button", "Открыть в редакторе");
      open.type = "button";
      open.className = "menu-item";
      open.addEventListener("click", () => {
        menu.open = false;
        openFromCard();
      });
      menuPanel.append(open);
      if (["queued", "processing"].includes(job.status)) {
        const cancel = text("button", run?.cancel_requested ? "Отмена запрошена" : "Отменить");
        cancel.type = "button";
        cancel.className = "menu-item menu-item-danger";
        cancel.disabled = Boolean(run?.cancel_requested);
        cancel.addEventListener("click", async () => {
          cancel.disabled = true;
          try {
            await request("/jobs/" + encodeURIComponent(job.id) + "/cancel", { method: "POST" });
            menu.open = false;
            notify("Отмена обработки запрошена.");
            await loadJobs();
          } catch (error) {
            showError(error);
            cancel.disabled = false;
          }
        });
        menuPanel.append(cancel);
      }
      menu.append(menuToggle, menuPanel);
      actions.append(menu);
      row.append(icon, detail, actions);
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

  async function enqueueRun(job) {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    const key = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    await request("/jobs/" + encodeURIComponent(job.id) + "/runs", {
      method: "POST", headers: { "Idempotency-Key": key },
    });
  }

  async function startRun(job, button) {
    if (!state.user || state.pendingRuns.has(job.id)) return;
    const generation = state.generation;
    state.pendingRuns.add(job.id);
    button.disabled = true;
    button.textContent = "Запуск…";
    let started = false;
    let refreshFailed = false;
    try {
      await enqueueRun(job);
      started = true;
      state.pendingRuns.delete(job.id);
      try {
        await loadJobs();
      } catch (error) {
        if (error instanceof StaleSessionError) throw error;
        refreshFailed = true;
      }
      requireGeneration(generation);
      notify(refreshFailed
        ? "Запись поставлена в очередь, но список не обновился. Нажмите «Обновить список»."
        : "Запись поставлена в очередь транскрибации.", refreshFailed);
    } catch (error) {
      showError(error);
    } finally {
      if (generation === state.generation) {
        state.pendingRuns.delete(job.id);
        button.disabled = started;
        button.textContent = started ? "В очереди · обновите список" :
          job.status === "uploaded" ? "Транскрибировать" : "Повторить обработку";
      }
    }
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

  function editSegment(editor, segmentId) {
    if (editor.saving) return;
    editor.editingSegmentId = segmentId;
    renderTranscript();
    requestAnimationFrame(() => document.querySelector(".transcript-edit")?.focus());
  }

  function effectiveGroupId(editor, segment) {
    let groupId = segment.primary_group_id || segment.speaker_group_ids?.[0] || null;
    const visited = new Set();
    while (groupId && !visited.has(groupId)) {
      visited.add(groupId);
      const group = editor.groupById.get(groupId);
      if (!group?.merged_into_group_id) break;
      groupId = group.merged_into_group_id;
    }
    return groupId;
  }

  function groupLabel(editor, groupId) {
    return groupId ? (editor.groupById.get(groupId)?.label || "Неизвестный говорящий") : "Неизвестный говорящий";
  }

  function isGenericGroupLabel(label) {
    return /^Говорящий\s+\d+$/u.test(String(label || "").trim());
  }

  function groupDisplayLabel(editor, groupId) {
    const label = groupLabel(editor, groupId);
    return isGenericGroupLabel(label) ? "Не подписан" : label;
  }

  function visibleGroups(editor) {
    return editor.revision.groups.filter((group) => !group.merged_into_group_id);
  }

  function groupSegments(editor, groupId) {
    return editor.revision.segments.filter((segment) => effectiveGroupId(editor, segment) === groupId);
  }

  function groupDuration(editor, groupId) {
    return groupSegments(editor, groupId).reduce((total, segment) => {
      const timing = editor.timingBySegment.get(segment.source_segment_id);
      return total + Math.max(0, (timing?.end_ms || 0) - (timing?.start_ms || 0));
    }, 0);
  }

  function sampleSegments(editor, groupId) {
    const all = groupSegments(editor, groupId).filter((segment) => editor.timingBySegment.has(segment.source_segment_id));
    const clean = all.filter((segment) => !segment.requires_review && (segment.speaker_group_ids?.length || 0) <= 1);
    const pool = clean.length ? clean : all;
    if (pool.length <= 5) return pool;
    const positions = [0, .25, .5, .75, 1];
    const used = new Set();
    return positions.map((position) => {
      let index = Math.round(position * (pool.length - 1));
      while (used.has(index) && index < pool.length - 1) index += 1;
      used.add(index);
      return pool[index];
    });
  }

  function openSpeakers(editor = state.editor, groupId = null) {
    if (!editor?.revision) return;
    const groups = visibleGroups(editor);
    editor.selectedGroupId = groupId && groups.some((group) => group.speaker_group_id === groupId)
      ? groupId
      : (editor.selectedGroupId && groups.some((group) => group.speaker_group_id === editor.selectedGroupId)
        ? editor.selectedGroupId : groups[0]?.speaker_group_id || null);
    renderSpeakerPanel();
    renderTranscript();
    const dialog = byId("speakers-dialog");
    if (!dialog.open) dialog.showModal();
  }

  function closeSpeakers() {
    stopSamplePlayback();
    byId("speakers-dialog").close();
    const editor = state.editor;
    if (editor) {
      editor.selectedGroupId = null;
      renderTranscript();
    }
  }

  function stopSamplePlayback() {
    const editor = state.editor;
    if (!editor || !Number.isFinite(editor.sampleStopAt)) return;
    editor.sampleStopAt = null;
    const player = byId("audio-player");
    if (!player.paused) player.pause();
  }

  function playSample(editor, segment) {
    const timing = editor.timingBySegment.get(segment.source_segment_id);
    if (!timing) return;
    editor.sampleStopAt = timing.end_ms / 1000;
    seek(timing.start_ms);
  }

  function renderTranscript() {
    const list = byId("transcript-list");
    list.replaceChildren();
    const editor = state.editor;
    if (!editor?.revision) {
      list.append(text("p", editor?.unavailable || "Реплики ещё не опубликованы."));
      return;
    }
    let turn = null;
    const turns = [];
    editor.revision.segments.forEach((segment) => {
      const groupId = effectiveGroupId(editor, segment);
      const separate = segment.requires_review || (segment.speaker_group_ids?.length || 0) > 1;
      if (!turn || turn.groupId !== groupId || turn.separate || separate) {
        turn = { groupId, separate, segments: [] };
        turns.push(turn);
      }
      turn.segments.push(segment);
    });
    turns.forEach((currentTurn) => {
      const first = currentTurn.segments[0];
      const timing = editor.timingBySegment.get(first.source_segment_id);
      const row = document.createElement("article");
      row.className = "transcript-turn";
      row.dataset.groupId = currentTurn.groupId || "";
      if (currentTurn.separate) row.classList.add("requires-review");
      if (byId("speakers-dialog").open && editor.selectedGroupId === currentTurn.groupId) row.classList.add("speaker-selected");
      const seekButton = text("button", timing ? formatTime(timing.start_ms) : "—");
      seekButton.type = "button";
      seekButton.className = "timestamp";
      seekButton.disabled = !timing;
      if (timing) {
        seekButton.addEventListener("click", () => {
          stopSamplePlayback();
          seek(timing.start_ms);
        });
      }
      const body = document.createElement("div");
      body.className = "transcript-body";
      const speaker = text("button", groupDisplayLabel(editor, currentTurn.groupId));
      speaker.type = "button";
      speaker.className = "turn-speaker";
      speaker.addEventListener("click", () => openSpeakers(editor, currentTurn.groupId));
      body.append(speaker);
      if (currentTurn.separate) {
        const badge = text("span", "Одновременная речь — проверить");
        badge.className = "review-badge";
        body.append(badge);
      }
      const paragraph = document.createElement("p");
      paragraph.className = "transcript-text";
      currentTurn.segments.forEach((segment, index) => {
        const draft = editor.segmentDrafts.get(segment.source_segment_id) ?? segment.text;
        if (editor.editingSegmentId === segment.source_segment_id) {
          const input = document.createElement("textarea");
          input.className = "transcript-edit";
          input.dataset.segmentId = segment.source_segment_id;
          input.rows = Math.max(2, Math.ceil(draft.length / 84));
          input.value = draft;
          input.setAttribute("aria-label", "Текст фрагмента");
          input.addEventListener("input", () => { editor.segmentDrafts.set(segment.source_segment_id, input.value); });
          input.addEventListener("blur", () => {
            if (editor.editingSegmentId === segment.source_segment_id) {
              editor.editingSegmentId = null;
              renderTranscript();
            }
          });
          paragraph.append(input);
        } else {
          const content = text("span", draft);
          content.className = "transcript-segment";
          content.dataset.segmentId = segment.source_segment_id;
          content.tabIndex = 0;
          content.setAttribute("role", "button");
          content.setAttribute("aria-label", "Изменить фрагмент");
          content.addEventListener("click", () => editSegment(editor, segment.source_segment_id));
          content.addEventListener("keydown", (event) => {
            if (!["Enter", " "].includes(event.key)) return;
            event.preventDefault();
            editSegment(editor, segment.source_segment_id);
          });
          paragraph.append(content);
          if (index < currentTurn.segments.length - 1) paragraph.append(document.createTextNode(" "));
        }
      });
      body.append(paragraph);
      const suggestions = currentTurn.segments.flatMap((segment) =>
        (editor.suggestions || []).filter((item) => item.source_segment_id === segment.source_segment_id)
      );
      if (suggestions.length) {
        const suggestionMenu = document.createElement("details");
        suggestionMenu.className = "turn-suggestions";
        suggestionMenu.append(text("summary", "Термины: " + suggestions.length));
        const suggestionList = document.createElement("div");
        suggestionList.className = "turn-suggestions-list";
        suggestions.forEach((suggestion) => {
          const button = text("button", "Заменить «" + suggestion.matched_variant + "» на «" + suggestion.canonical + "»");
          button.type = "button";
          button.className = "menu-item";
          button.addEventListener("click", () => {
            const segment = editor.revision.segments.find((item) => item.source_segment_id === suggestion.source_segment_id);
            if (!segment || editor.saving) return;
            const current = editor.segmentDrafts.get(segment.source_segment_id) ?? segment.text;
            const start = suggestion.start_offset;
            const end = suggestion.end_offset;
            if (Number.isInteger(start) && Number.isInteger(end) && current.slice(start, end) === suggestion.matched_variant) {
              editor.segmentDrafts.set(segment.source_segment_id, current.slice(0, start) + suggestion.canonical + current.slice(end));
            } else if (!Number.isInteger(start) && current.includes(suggestion.matched_variant)) {
              editor.segmentDrafts.set(segment.source_segment_id, current.replace(suggestion.matched_variant, suggestion.canonical));
            } else {
              notify("Текст фрагмента уже изменён. Проверьте подсказку вручную.", true);
              return;
            }
            renderTranscript();
            notify("Подсказка внесена в черновик. Сохраните правки.");
          });
          suggestionList.append(button);
        });
        suggestionMenu.append(suggestionList);
        body.append(suggestionMenu);
      }
      row.append(seekButton, body);
      list.append(row);
    });
  }

  async function mergeSpeakerGroup(editor, sourceGroupId, targetGroupId, button) {
    if (!targetGroupId || sourceGroupId === targetGroupId || editor.saving) return;
    button.disabled = true;
    try {
      const revision = await request("/jobs/" + encodeURIComponent(editor.jobId) + "/transcript/groups/merge", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_group_id: sourceGroupId, target_group_id: targetGroupId, expected_revision_number: editor.revision.revision.number }),
      });
      if (state.editor !== editor) return;
      applyEditorRevision(editor, revision);
      editor.selectedGroupId = targetGroupId;
      renderSpeakerPanel();
      renderTranscript();
      notify("Группы объединены. При необходимости это можно отменить в этой панели.");
    } catch (error) {
      showError(error);
    } finally {
      if (state.editor === editor) button.disabled = false;
    }
  }

  async function unmergeSpeakerGroup(editor, sourceGroupId, button) {
    if (editor.saving) return;
    button.disabled = true;
    try {
      const revision = await request("/jobs/" + encodeURIComponent(editor.jobId) + "/transcript/groups/unmerge", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_group_id: sourceGroupId, expected_revision_number: editor.revision.revision.number }),
      });
      if (state.editor !== editor) return;
      applyEditorRevision(editor, revision);
      editor.selectedGroupId = sourceGroupId;
      renderSpeakerPanel();
      renderTranscript();
      notify("Объединение групп отменено.");
    } catch (error) {
      showError(error);
    } finally {
      if (state.editor === editor) button.disabled = false;
    }
  }

  function renderSpeakerPanel() {
    const panel = byId("speaker-panel");
    panel.replaceChildren();
    const editor = state.editor;
    if (!editor?.revision) {
      panel.append(text("p", "Говорящие станут доступны после публикации транскрипции."));
      return;
    }
    const groups = visibleGroups(editor);
    byId("open-speakers-button").textContent = "Говорящие · " + groups.length;
    byId("open-speakers-button").disabled = !groups.length;
    if (!groups.length) {
      panel.append(text("p", "Группы говорящих не найдены."));
      return;
    }
    const groupId = groups.some((group) => group.speaker_group_id === editor.selectedGroupId)
      ? editor.selectedGroupId : groups[0].speaker_group_id;
    editor.selectedGroupId = groupId;
    const group = editor.groupById.get(groupId);
    const context = groupSegments(editor, groupId);
    const groupIndex = groups.findIndex((item) => item.speaker_group_id === groupId);
    const selectLocalGroup = (nextGroupId) => {
      editor.selectedGroupId = nextGroupId;
      renderSpeakerPanel();
      renderTranscript();
    };
    const groupSwitcher = document.createElement("section");
    groupSwitcher.className = "speaker-local-group";
    groupSwitcher.append(text("h3", "Группа " + (groupIndex + 1) + " из " + groups.length));
    const switcherBody = document.createElement("div");
    switcherBody.className = "speaker-local-group-controls";
    const previous = text("button", "←");
    previous.type = "button";
    previous.className = "secondary icon-button";
    previous.disabled = groups.length < 2;
    previous.setAttribute("aria-label", "Предыдущая группа этой записи");
    previous.title = "Предыдущая группа этой записи";
    previous.addEventListener("click", () => {
      const previousIndex = (groupIndex - 1 + groups.length) % groups.length;
      selectLocalGroup(groups[previousIndex].speaker_group_id);
    });
    const localGroupSummary = text(
      "p",
      groupDisplayLabel(editor, groupId) + " · " + context.length + " реплик в этой записи",
    );
    localGroupSummary.className = "speaker-local-group-summary";
    const next = text("button", "→");
    next.type = "button";
    next.className = "secondary icon-button";
    next.disabled = groups.length < 2;
    next.setAttribute("aria-label", "Следующая группа этой записи");
    next.title = "Следующая группа этой записи";
    next.addEventListener("click", () => {
      const nextIndex = (groupIndex + 1) % groups.length;
      selectLocalGroup(groups[nextIndex].speaker_group_id);
    });
    switcherBody.append(previous, localGroupSummary, next);
    groupSwitcher.append(switcherBody);
    const metrics = text("p", context.length + " реплик · " + formatTime(groupDuration(editor, groupId)) + " речи");
    metrics.className = "speaker-metrics";
    const name = document.createElement("input");
    name.className = "group-label-input";
    name.value = editor.identityDrafts.get(groupId) ?? (isGenericGroupLabel(group.label) ? "" : group.label);
    name.maxLength = 240;
    name.placeholder = "Имя и фамилия";
    name.setAttribute("aria-label", "Имя и фамилия говорящего");
    const identity = text("button", "Сохранить имя");
    identity.type = "button";
    identity.disabled = !name.value.trim() || editor.saving;
    name.addEventListener("input", () => {
      editor.identityDrafts.set(groupId, name.value);
      identity.disabled = !name.value.trim() || editor.saving;
    });
    identity.addEventListener("click", () => saveGroupIdentity(editor, group, name, identity));
    const identityRow = document.createElement("div");
    identityRow.className = "speaker-identity";
    identityRow.append(name, identity);
    panel.append(groupSwitcher, metrics, identityRow);
    const samples = sampleSegments(editor, groupId);
    const samplesBlock = document.createElement("section");
    samplesBlock.className = "speaker-samples";
    samplesBlock.append(text("h3", "Фрагменты для проверки"));
    const samplesHelp = text("p", "Прослушайте несколько разных мест записи перед тем, как подтверждать имя.");
    samplesHelp.className = "muted";
    samplesBlock.append(samplesHelp);
    samples.forEach((segment, index) => {
      const timing = editor.timingBySegment.get(segment.source_segment_id);
      if (!timing) return;
      const row = document.createElement("article");
      row.className = "speaker-sample";
      const play = text("button", "Слушать " + formatTime(timing.start_ms) + "—" + formatTime(timing.end_ms));
      play.type = "button";
      play.className = "secondary";
      play.addEventListener("click", () => playSample(editor, segment));
      const replay = text("button", "Повторить");
      replay.type = "button";
      replay.className = "secondary";
      replay.addEventListener("click", () => playSample(editor, segment));
      row.append(text("strong", "Фрагмент " + (index + 1)), text("p", segment.text), play, replay);
      samplesBlock.append(row);
    });
    panel.append(samplesBlock);
    const merge = document.createElement("details");
    merge.className = "speaker-merge";
    merge.append(text("summary", "Объединить локальные группы этой записи"));
    const mergeBody = document.createElement("div");
    mergeBody.append(text("p", "Если это один человек, выберите вторую группу из этой же записи."));
    if (groups.length < 2) {
      mergeBody.append(text("p", "В этой записи других групп нет."));
    } else {
      const choices = document.createElement("div");
      choices.className = "speaker-merge-choices";
      groups.filter((item) => item.speaker_group_id !== groupId).forEach((item) => {
        const targetPosition = groups.findIndex((candidate) => candidate.speaker_group_id === item.speaker_group_id) + 1;
        const button = text(
          "button",
          "Объединить с группой " + targetPosition + " · " + groupDisplayLabel(editor, item.speaker_group_id),
        );
        button.type = "button";
        button.className = "secondary";
        button.setAttribute(
          "aria-label",
          "Объединить текущую группу с группой " + targetPosition + " этой записи",
        );
        button.addEventListener("click", () => mergeSpeakerGroup(editor, groupId, item.speaker_group_id, button));
        choices.append(button);
      });
      mergeBody.append(choices);
    }
    merge.append(mergeBody);
    panel.append(merge);
    const mergedGroups = editor.revision.groups.filter((item) => item.merged_into_group_id);
    if (mergedGroups.length) {
      const restore = document.createElement("details");
      restore.className = "speaker-merge";
      restore.append(text("summary", "Отменить локальное объединение (" + mergedGroups.length + ")"));
      const restoreBody = document.createElement("div");
      mergedGroups.forEach((item) => {
        const itemRow = document.createElement("div");
        const button = text("button", "Разъединить «" + item.label + "»");
        button.type = "button";
        button.className = "secondary";
        button.addEventListener("click", () => unmergeSpeakerGroup(editor, item.speaker_group_id, button));
        itemRow.append(text("span", item.label + " → " + groupLabel(editor, item.merged_into_group_id)), button);
        restoreBody.append(itemRow);
      });
      restore.append(restoreBody);
      panel.append(restore);
    }
    if (group.assignment_status === "confirmed" && group.label.trim()) {
      const sample = document.createElement("details");
      sample.className = "sample-control";
      sample.append(text("summary", "Дополнительно: добавить голосовой образец"));
      const sampleBody = document.createElement("div");
      const consent = document.createElement("input");
      consent.type = "checkbox";
      const consentLabel = document.createElement("label");
      consentLabel.className = "sample-consent";
      consentLabel.append(consent, text("span", "Я проверил(а) фрагменты и подтверждаю добавление образца"));
      const enroll = text("button", "Добавить образец");
      enroll.type = "button";
      enroll.className = "secondary enroll-sample";
      enroll.disabled = true;
      consent.addEventListener("change", () => { enroll.disabled = !consent.checked || editor.saving; });
      enroll.addEventListener("click", () => enrollSpeakerSample(editor, group, consent, enroll));
      sampleBody.append(consentLabel, enroll);
      sample.append(sampleBody);
      panel.append(sample);
    }
  }

  function applyEditorRevision(editor, revision) {
    editor.revision = revision;
    editor.groupById = new Map(revision.groups.map((group) => [group.speaker_group_id, group]));
    editor.decisions.clear();
  }

  function hasChangesOutsideGroup(editor, groupId) {
    const changes = editorChanges(editor);
    if (Object.keys(changes.segmentTexts).length) return true;
    return editor.revision.groups.some((group) => {
      if (group.speaker_group_id === groupId) return false;
      const draft = editor.identityDrafts.get(group.speaker_group_id);
      return draft !== undefined && draft.trim() !== group.label;
    });
  }

  async function saveGroupIdentity(editor, group, input, button) {
    if (state.editor !== editor || editor.saving) return;
    const displayName = input.value.trim();
    if (!displayName) {
      notify("Укажите имя и фамилию говорящего.", true);
      input.focus();
      return;
    }
    if (hasChangesOutsideGroup(editor, group.speaker_group_id)) {
      notify("Сначала сохраните или отмените другие правки транскрипции.", true);
      return;
    }
    if (displayName === group.label) {
      editor.identityDrafts.delete(group.speaker_group_id);
      renderSpeakerPanel();
      return;
    }
    button.disabled = true;
    try {
      const revision = await request(
        "/jobs/" + encodeURIComponent(editor.jobId) + "/transcript/groups/" +
          encodeURIComponent(group.speaker_group_id) + "/identity",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            display_name: displayName,
            expected_revision_number: editor.revision.revision.number,
          }),
        },
      );
      if (state.editor !== editor) return;
      applyEditorRevision(editor, revision);
      editor.identityDrafts.delete(group.speaker_group_id);
      renderSpeakerPanel();
      renderTranscript();
      byId("editor-help").textContent = "Ревизия " + revision.revision.number + ". Имя применено ко всем репликам группы.";
      notify("Имя говорящего сохранено для всей группы.");
    } catch (error) {
      showError(error);
      button.disabled = false;
    }
  }

  async function enrollSpeakerSample(editor, group, consent, button) {
    if (state.editor !== editor || editor.saving || !consent.checked) return;
    button.disabled = true;
    try {
      const result = await request(
        "/jobs/" + encodeURIComponent(editor.jobId) + "/transcript/groups/" +
          encodeURIComponent(group.speaker_group_id) + "/samples",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
      );
      if (state.editor !== editor) return;
      consent.checked = false;
      notify(
        result.indexing_state === "active"
          ? "Образец голоса добавлен для автоматического сопоставления."
          : "Образец голоса сохранён и будет добавлен после проверки качества.",
      );
    } catch (error) {
      showError(error);
    } finally {
      if (state.editor === editor) button.disabled = !consent.checked || editor.saving;
    }
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
    document.querySelectorAll(".transcript-segment").forEach((row) => {
      const id = row.dataset.segmentId;
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
      const source = await request(path + "/source");
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
      editor.revision = transcript;
      editor.timingBySegment = new Map((source.segments || []).map((segment) => [segment.id, segment]));
      editor.groupById = new Map((transcript?.groups || []).map((group) => [group.speaker_group_id, group]));
      editor.identityDrafts.clear();
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
      renderSpeakerPanel();
      renderTranscript();
      byId("save-editor-button").disabled = !transcript;
      byId("download-text-button").hidden = !transcript;
      byId("editor-help").textContent = transcript
        ? "Ревизия " + transcript.revision.number + ". Правки сохраняются новой ревизией; отмеченные наложения требуют проверки."
        : unavailable || "Дождитесь публикации результата обработки.";
    } catch (error) {
      if (error instanceof StaleSessionError || state.editor !== editor) return;
      editor.unavailable = error.message;
      if (!editor.revision && !hasEditorChanges(editor)) {
        renderSpeakerPanel();
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
      jobId: job.id, job, revision: null, timingBySegment: new Map(),
      groupById: new Map(), decisions: new Map(), suggestions: [], segmentDrafts: new Map(), identityDrafts: new Map(), editingSegmentId: null,
      unavailable: "", loading: false, saving: false,
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
      notify(hasIdentityDrafts(editor)
        ? "Сохраните имя говорящего в соответствующей строке."
        : "Нет изменений для сохранения.");
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
      applyEditorRevision(editor, result);
      editor.suggestions = [];
      editor.segmentDrafts.clear();
      editor.editingSegmentId = null;
      byId("editor-conflict").hidden = true;
      renderSpeakerPanel();
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

  async function downloadTranscriptText() {
    const editor = state.editor;
    const button = byId("download-text-button");
    if (!editor?.revision || button.disabled) return;
    const generation = state.generation;
    button.disabled = true;
    try {
      const response = await fetch(
        api + "/jobs/" + encodeURIComponent(editor.jobId) + "/transcript/export?format=txt",
        { credentials: "same-origin" },
      );
      requireGeneration(generation);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.message || "Не удалось подготовить текстовый файл.");
      }
      const blob = await response.blob();
      requireGeneration(generation);
      const link = document.createElement("a");
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = String(editor.job.source_name || "transcription").replace(/\.[^.]+$/, "") + ".txt";
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      showError(error);
    } finally {
      if (generation === state.generation) button.disabled = false;
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
    const button = byId("upload-and-start-button");
    if (button.disabled || !state.user || state.uploadBusy) return;
    const file = byId("media-input").files[0];
    if (!isSupportedMediaFile(file)) {
      notify("Выберите запись в формате " + supportedMediaLabel + ".", true);
      return;
    }
    const generation = state.generation;
    const body = new FormData(form);
    let uploadedJob = null;
    state.uploadBusy = true;
    button.disabled = true;
    button.textContent = "Загрузка…";
    byId("media-input").disabled = true;
    try {
      const created = await request("/jobs", { method: "POST", body });
      requireGeneration(generation);
      uploadedJob = created.job;
      state.pendingRuns.add(uploadedJob.id);
      button.textContent = "Запуск обработки…";
      let runError = null;
      try {
        await enqueueRun(uploadedJob);
      } catch (error) {
        if (error instanceof StaleSessionError) throw error;
        runError = error;
      } finally {
        state.pendingRuns.delete(uploadedJob.id);
      }
      resetUploadDraft();
      let refreshError = null;
      try {
        await loadJobs();
      } catch (error) {
        if (error instanceof StaleSessionError) throw error;
        refreshError = error;
      }
      requireGeneration(generation);
      if (runError) {
        notify("Файл «" + uploadedJob.source_name + "» загружен, но обработка не запустилась: " + runError.message +
          " Обновите список и нажмите «Транскрибировать» у этой записи.", true);
      } else if (refreshError) {
        notify("Файл «" + uploadedJob.source_name + "» поставлен в очередь. Список не обновился: " +
          refreshError.message + " Нажмите «Обновить список».", true);
      } else {
        notify("Файл «" + uploadedJob.source_name + "» загружен и поставлен в очередь транскрибации.");
      }
    } catch (error) {
      showError(error);
    } finally {
      if (generation === state.generation) {
        state.uploadBusy = false;
        byId("media-input").disabled = false;
        button.textContent = "Загрузить и транскрибировать";
        if (uploadedJob) resetUploadDraft();
        updateSelectedMedia();
      }
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
  byId("download-text-button").addEventListener("click", downloadTranscriptText);
  byId("open-speakers-button").addEventListener("click", () => openSpeakers());
  byId("close-speakers-button").addEventListener("click", closeSpeakers);
  byId("speakers-dialog").addEventListener("cancel", () => {
    stopSamplePlayback();
    const editor = state.editor;
    if (editor) {
      editor.selectedGroupId = null;
      requestAnimationFrame(renderTranscript);
    }
  });
  byId("copy-editor-draft").addEventListener("click", async () => {
    const editor = state.editor;
    if (!editor?.revision) return;
    const lines = editor.revision.segments.map((segment) => {
      return editor.segmentDrafts.get(segment.source_segment_id) ?? segment.text;
    });
    const groups = visibleGroups(editor).map((group) =>
      editor.identityDrafts.get(group.speaker_group_id) ?? group.label
    );
    try {
      await navigator.clipboard.writeText("Говорящие:\n" + groups.join("\n") + "\n\nТекст:\n" + lines.join("\n\n"));
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
  document.querySelectorAll("#auth-panel [role='tab']").forEach((tab, index, tabs) => {
    tab.addEventListener("keydown", (event) => {
      const moves = {
        ArrowLeft: -1,
        ArrowRight: 1,
        Home: -index,
        End: tabs.length - index - 1,
      };
      if (!(event.key in moves)) return;
      event.preventDefault();
      const next = tabs[(index + moves[event.key] + tabs.length) % tabs.length];
      next.focus();
      authMode(next.id === "show-login" ? "login" : "register");
    });
  });
  byId("media-input").addEventListener("change", updateSelectedMedia);
  const goToJobs = () => {
    if (!state.user) return;
    activateView("jobs");
    loadJobs().catch(showError);
  };
  byId("go-to-jobs").addEventListener("click", goToJobs);
  byId("go-to-jobs").addEventListener("keydown", (event) => {
    if (!['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    goToJobs();
  });
  byId("add-recording-button").addEventListener("click", openMediaPicker);
  byId("replace-media-button").addEventListener("click", openMediaPicker);
  byId("discard-media-button").addEventListener("click", resetUploadDraft);
  byId("recording-dropzone").addEventListener("click", openMediaPicker);
  byId("recording-dropzone").addEventListener("keydown", (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    openMediaPicker();
  });
  byId("recording-dropzone").addEventListener("dragover", (event) => {
    event.preventDefault();
    if (!state.uploadBusy) event.currentTarget.classList.add("is-dragging");
  });
  byId("recording-dropzone").addEventListener("dragleave", (event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.classList.remove("is-dragging");
  });
  byId("recording-dropzone").addEventListener("drop", (event) => {
    event.preventDefault();
    event.currentTarget.classList.remove("is-dragging");
    if (state.uploadBusy) return;
    selectMediaFile(event.dataTransfer?.files?.[0]);
  });
  byId("refresh-jobs-button").addEventListener("click", () => loadJobs().catch(showError));
  byId("back-to-jobs-button").addEventListener("click", () => {
    activateView("jobs");
    loadJobs().catch(showError);
  });

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!state.user) return;
      activateView(button.dataset.view);
      try {
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
    if (state.editor?.sampleStopAt && player.currentTime >= state.editor.sampleStopAt) {
      player.pause();
      player.currentTime = state.editor.sampleStopAt;
      state.editor.sampleStopAt = null;
    }
    const timeMs = player.currentTime * 1000;
    const percentage = Number.isFinite(player.duration) && player.duration > 0
      ? player.currentTime / player.duration * 100 : 0;
    document.querySelector(".waveform span").style.width = Math.min(100, Math.max(0, percentage)) + "%";
    renderSubtitle(timeMs);
  });
  byId("audio-player").addEventListener("loadedmetadata", () => {
    byId("audio-help").hidden = true;
    byId("audio-help").textContent = "";
  });
  byId("audio-player").addEventListener("ended", stopSamplePlayback);
  byId("audio-player").addEventListener("error", () => {
    if (!state.editor) return;
    const player = byId("audio-player");
    const code = player.error?.code;
    const detail = code === 4
      ? "Формат аудио не поддерживается браузером."
      : "Браузер не смог загрузить аудиопоток.";
    const help = byId("audio-help");
    help.textContent = detail + " Обновите запись; если ошибка повторится, перезагрузите страницу и сообщите администратору название записи.";
    help.hidden = false;
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
