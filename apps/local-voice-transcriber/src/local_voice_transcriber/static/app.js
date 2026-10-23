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
    diarizing: "Разделение спикеров", matching: "Сопоставление голосов",
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
    byId("editor-title").textContent = "Транскрипция";
    byId("editor-status").textContent = "Выберите готовую запись";
    byId("editor-help").textContent = "Выберите готовую запись.";
    byId("subtitle-line").textContent = "Субтитры появятся после обработки.";
    byId("audio-help").hidden = true;
    byId("audio-help").textContent = "";
    document.querySelector(".waveform span").style.width = "0%";
    byId("save-editor-button").disabled = true;
    byId("edit-transcript-button").disabled = true;
    byId("edit-transcript-button").hidden = false;
    byId("cancel-editor-button").hidden = true;
    byId("save-editor-button").hidden = true;
    byId("draft-mode-switch").hidden = false;
    byId("transcript-title").textContent = "Транскрипция";
    byId("download-text-button").hidden = true;
    byId("editor-conflict").hidden = true;
    byId("speaker-panel").replaceChildren();
    byId("open-speakers-button").textContent = "Изменить спикеров";
    byId("open-speakers-button").disabled = true;
    byId("speakers-dialog").close();
    byId("speaker-timeline-dialog").close();
    byId("clean-draft-button").setAttribute("aria-pressed", "true");
    byId("raw-draft-button").setAttribute("aria-pressed", "false");
    byId("clean-draft-status").hidden = true;
    byId("clean-draft-status").textContent = "";
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

  async function setDraftMode(mode) {
    const editor = state.editor;
    if (!editor?.revision || !["clean", "raw"].includes(mode)) return;
    if (editor.saving || editor.loading || hasEditorChanges(editor)) {
      notify("Сначала сохраните или отмените правки транскрипции.", true);
      return;
    }
    if (editor.draftMode === mode && editor.transcriptView?.resolved === mode) return;
    const controls = [byId("clean-draft-button"), byId("raw-draft-button")];
    controls.forEach((button) => { button.disabled = true; });
    try {
      const preference = await request(
        "/jobs/" + encodeURIComponent(editor.jobId) + "/transcript/view-preference",
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ preference: "last_selected", selected_view: mode }),
        },
      );
      if (state.editor !== editor) return;
      editor.viewPreference = preference;
      await refreshEditor(editor, preference.selected_view);
    } catch (error) {
      showError(error);
    } finally {
      if (state.editor === editor) controls.forEach((button) => { button.disabled = false; });
    }
  }

  function segmentText(editor, segment) {
    return editor.segmentDrafts.get(segment.source_segment_id) ?? segment.text;
  }

  function syncDraftModeControls(editor = state.editor) {
    const selected = editor?.transcriptView?.resolved === "raw" ? "raw" : "clean";
    byId("clean-draft-button").setAttribute("aria-pressed", String(selected === "clean"));
    byId("raw-draft-button").setAttribute("aria-pressed", String(selected === "raw"));
  }

  function updateDraftStatus(editor = state.editor) {
    const status = byId("clean-draft-status");
    const view = editor?.transcriptView;
    if (!editor?.revision || !view) {
      status.hidden = true;
      return;
    }
    const cleaning = view.cleaning || {};
    if (view.resolved === "raw" && view.requested === "clean" && cleaning.fallback_to_raw) {
      status.textContent = cleaning.message || "Чистый черновик недоступен: показан исходный текст.";
      status.hidden = false;
      return;
    }
    if (view.resolved === "raw") {
      status.textContent = "Дословный оригинал ASR сохранён неизменным и доступен только для сравнения.";
      status.hidden = false;
      return;
    }
    const message = typeof cleaning?.message === "string" ? cleaning.message.trim() : "";
    status.textContent = message;
    status.hidden = !message;
  }

  function editorChanges(editor = state.editor) {
    if (!editor?.revision) return { segmentTexts: {}, groupUpdates: {}, speakerUpdates: {} };
    const segmentTexts = {};
    const speakerUpdates = {};
    editor.revision.segments.forEach((segment) => {
      const draft = editor.segmentDrafts.get(segment.source_segment_id);
      if (draft !== undefined && draft !== segment.text) segmentTexts[segment.source_segment_id] = draft;
      const speakerDraft = editor.speakerDrafts.get(segment.source_segment_id);
      if (speakerDraft !== undefined && speakerDraft !== effectiveGroupId(editor, segment)) {
        speakerUpdates[segment.source_segment_id] = speakerDraft;
      }
    });
    return { segmentTexts, groupUpdates: {}, speakerUpdates };
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
      Object.keys(changes.speakerUpdates).length ||
      hasIdentityDrafts(editor));
  }

  function setTranscriptMode(editor, mode) {
    if (state.editor !== editor) return;
    editor.mode = mode;
    byId("edit-transcript-button").hidden = mode === "edit";
    byId("cancel-editor-button").hidden = mode !== "edit";
    byId("save-editor-button").hidden = mode !== "edit";
    byId("draft-mode-switch").hidden = mode === "edit";
    byId("download-text-button").hidden = mode === "edit" || !editor.revision;
    byId("transcript-title").textContent = mode === "edit" ? "Редактирование транскрипции" : "Транскрипция";
    byId("editor-help").textContent = mode === "edit"
      ? "Измените текст и спикера нужной реплики, затем сохраните правки. Отмена вернёт к просмотру."
      : "Ревизия " + editor.revision.revision.number + ". Нажмите таймкод, чтобы прослушать фрагмент.";
    renderTranscript();
  }

  async function enterTranscriptEditor() {
    const editor = state.editor;
    if (!editor?.revision || editor.saving || editor.loading) return;
    if (editor.draftMode === "raw") {
      await setDraftMode("clean");
      if (state.editor !== editor || editor.draftMode === "raw") {
        notify("Для редактирования нужен чистый черновик. Сейчас доступен только дословный оригинал.", true);
        return;
      }
    }
    setTranscriptMode(editor, "edit");
  }

  function cancelTranscriptEditor() {
    const editor = state.editor;
    if (!editor || editor.saving) return;
    editor.segmentDrafts.clear();
    editor.speakerDrafts.clear();
    editor.identityDrafts.clear();
    byId("editor-conflict").hidden = true;
    setTranscriptMode(editor, "read");
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

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return null;
    const rounded = Math.round(seconds);
    return Math.floor(rounded / 60) + " мин " + rounded % 60 + " с";
  }

  function completedRunDuration(job, run) {
    const diagnostics = job.diagnostics || run?.diagnostics;
    if (Number.isFinite(diagnostics?.total_duration_seconds)) return diagnostics.total_duration_seconds;
    const started = Date.parse(run?.started_at || "");
    const completed = Date.parse(run?.completed_at || "");
    return Number.isFinite(started) && Number.isFinite(completed) && completed >= started
      ? (completed - started) / 1000 : null;
  }

  function renderJobDiagnostics(job, run) {
    const diagnostics = job.diagnostics || run?.diagnostics;
    if (!diagnostics || typeof diagnostics !== "object") return null;
    const panel = document.createElement("details");
    panel.className = "job-diagnostics";
    panel.append(text("summary", "Диагностика обработки"));
    const content = document.createElement("dl");
    const add = (label, value) => {
      if (value === null || value === undefined || value === "") return;
      content.append(text("dt", label), text("dd", String(value)));
    };
    add("Модель ASR", diagnostics.asr_model);
    add("Пакет ASR", diagnostics.asr_pack_id);
    add("Пакет VAD", diagnostics.vad_pack_id);
    add("Пакет голосов", diagnostics.voice_pack_id);
    add("Длительность записи", formatDuration(diagnostics.audio_duration_seconds));
    add("Ожидание в очереди", formatDuration(diagnostics.queue_duration_seconds));
    add("Время обработки", formatDuration(completedRunDuration(job, run)));
    add("Попытка", diagnostics.attempt);
    add("Повторных попыток", diagnostics.retry_count);
    add("Групп спикеров", diagnostics.local_speaker_group_count);
    if (Array.isArray(diagnostics.stages)) diagnostics.stages.forEach((stage) => {
      const label = stageLabels[stage.stage];
      if (label) add(label, formatDuration(stage.duration_seconds) || "Выполняется");
    });
    if (!content.children.length) add("Сведения", "Дополнительные сведения пока недоступны.");
    panel.append(content);
    return panel;
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
      detail.setAttribute("aria-label", "Открыть запись «" + job.source_name + "» для просмотра");
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
      if (job.status === "ready") {
        const duration = formatDuration(completedRunDuration(job, run));
        if (duration) detail.append(text("small", "Готово за " + duration));
      } else if (run?.stage) {
        detail.append(text("small", "Этап: " + (stageLabels[run.stage] || run.stage)));
      }
      if (job.status !== "ready" && run?.attempt && run?.max_attempts) {
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
      const open = text("button", "Открыть транскрипцию");
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
      const diagnostics = renderJobDiagnostics(job, run);
      if (diagnostics) row.append(diagnostics);
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

  function groupNumber(editor, groupId) {
    const index = visibleGroups(editor).findIndex((group) => group.speaker_group_id === groupId);
    return index >= 0 ? index + 1 : null;
  }

  function isGenericGroupLabel(label) {
    return /^(?:Говорящий|Спикер)\s+\d+$/u.test(String(label || "").trim());
  }

  function groupLabel(editor, groupId) {
    const saved = groupId ? editor.groupById.get(groupId)?.label?.trim() : "";
    if (saved && !isGenericGroupLabel(saved)) return saved;
    const number = groupNumber(editor, groupId);
    return number ? "Спикер " + number : "Спикер без подписи";
  }

  function groupDisplayLabel(editor, groupId) {
    return groupLabel(editor, groupId);
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
      ? groupId : null;
    renderSpeakerPanel();
    renderTranscript();
    const dialog = byId("speakers-dialog");
    if (!dialog.open) dialog.showModal();
    if (editor.selectedGroupId) {
      requestAnimationFrame(() => byId("speaker-card-" + editor.selectedGroupId)?.scrollIntoView({ block: "nearest" }));
    }
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

  function openSpeakerTimeline(editor, groupId) {
    if (!editor?.revision || !visibleGroups(editor).some((group) => group.speaker_group_id === groupId)) return;
    editor.selectedGroupId = groupId;
    editor.timelineGroupId = groupId;
    byId("speakers-dialog").close();
    renderSpeakerTimeline();
    const dialog = byId("speaker-timeline-dialog");
    if (!dialog.open) dialog.showModal();
  }

  function closeSpeakerTimeline({ reopenSpeakers = false } = {}) {
    stopSamplePlayback();
    byId("speaker-timeline-dialog").close();
    const editor = state.editor;
    if (!editor) return;
    editor.timelineGroupId = null;
    if (reopenSpeakers) openSpeakers(editor, editor.selectedGroupId);
    else {
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
      const hiddenByCleaning = editor.mode !== "edit" && editor.draftMode === "clean" &&
        !segmentText(editor, segment).trim() && !separate;
      if (hiddenByCleaning) return;
      const timing = editor.timingBySegment.get(segment.source_segment_id);
      const previous = turn?.segments.at(-1);
      const previousTiming = previous && editor.timingBySegment.get(previous.source_segment_id);
      const gap = timing && previousTiming ? timing.start_ms - previousTiming.end_ms : Infinity;
      if (editor.mode === "edit" || !turn || turn.groupId !== groupId || turn.separate || separate ||
          gap < 0 || gap > 1500) {
        turn = { groupId, separate, segments: [] };
        turns.push(turn);
      }
      turn.segments.push(segment);
    });
    turns.forEach((currentTurn) => {
      const first = currentTurn.segments[0];
      const timing = editor.timingBySegment.get(first.source_segment_id);
      const last = currentTurn.segments.at(-1);
      const ending = editor.timingBySegment.get(last.source_segment_id);
      const row = document.createElement("article");
      row.className = "transcript-turn";
      row.dataset.groupId = currentTurn.groupId || "";
      if (currentTurn.separate) row.classList.add("requires-review");
      if (byId("speakers-dialog").open && editor.selectedGroupId === currentTurn.groupId) row.classList.add("speaker-selected");
      const timeLabel = timing
        ? formatTime(timing.start_ms) + "–" + formatTime(ending?.end_ms ?? timing.end_ms)
        : "—";
      const seekButton = text("button", timeLabel);
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
      if (editor.mode === "edit") {
        const segment = first;
        const speakerField = document.createElement("label");
        speakerField.className = "turn-speaker-field";
        speakerField.append(text("span", "Спикер"));
        const selector = document.createElement("select");
        selector.className = "turn-speaker-select";
        selector.setAttribute("aria-label", "Спикер реплики " + (timing ? formatTime(timing.start_ms) : "без таймкода"));
        visibleGroups(editor).forEach((group) => {
          const option = text("option", groupDisplayLabel(editor, group.speaker_group_id));
          option.value = group.speaker_group_id;
          selector.append(option);
        });
        selector.value = editor.speakerDrafts.get(segment.source_segment_id) ?? currentTurn.groupId ?? "";
        selector.addEventListener("change", () => {
          editor.speakerDrafts.set(segment.source_segment_id, selector.value);
        });
        speakerField.append(selector);
        body.append(speakerField);
      } else {
        const speaker = text("span", groupDisplayLabel(editor, currentTurn.groupId));
        speaker.className = "turn-speaker";
        body.append(speaker);
      }
      if (currentTurn.separate) {
        const badge = text("span", "Одновременная речь — проверить");
        badge.className = "review-badge";
        body.append(badge);
      }
      const paragraph = document.createElement("p");
      paragraph.className = "transcript-text";
      currentTurn.segments.forEach((segment, index) => {
        const draft = segmentText(editor, segment);
        if (editor.mode === "edit") {
          const input = document.createElement("textarea");
          input.className = "transcript-edit";
          input.dataset.segmentId = segment.source_segment_id;
          input.rows = Math.max(2, Math.ceil(draft.length / 84));
          input.value = draft;
          input.setAttribute("aria-label", "Текст фрагмента");
          input.addEventListener("input", () => { editor.segmentDrafts.set(segment.source_segment_id, input.value); });
          paragraph.append(input);
        } else {
          const content = text("span", draft);
          content.className = "transcript-segment";
          content.dataset.segmentId = segment.source_segment_id;
          if (editor.draftMode === "raw") content.classList.add("source-segment");
          paragraph.append(content);
          if (index < currentTurn.segments.length - 1) paragraph.append(document.createTextNode(" "));
        }
      });
      body.append(paragraph);
      const suggestions = editor.mode === "edit" && editor.draftMode === "clean" ? currentTurn.segments.flatMap((segment) =>
        (editor.suggestions || []).filter((item) => item.source_segment_id === segment.source_segment_id)
      ) : [];
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
    updateDraftStatus(editor);
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

  function groupFirstPhrase(editor, groupId) {
    const first = groupSegments(editor, groupId).find((segment) => segmentText(editor, segment).trim());
    if (!first) return "Нет распознанной реплики.";
    const phrase = segmentText(editor, first).trim().replace(/\s+/gu, " ");
    return phrase.length > 132 ? phrase.slice(0, 129) + "…" : phrase;
  }

  function renderSpeakerTimeline() {
    const list = byId("speaker-timeline-list");
    list.replaceChildren();
    const editor = state.editor;
    const groupId = editor?.timelineGroupId;
    if (!editor?.revision || !groupId) {
      list.append(text("p", "Нет выбранного спикера."));
      return;
    }
    const rows = groupSegments(editor, groupId);
    byId("speaker-timeline-title").textContent = groupDisplayLabel(editor, groupId) + " · таймкоды";
    byId("speaker-timeline-help").textContent = rows.length + " реплик этой записи. Нажмите на таймкод, чтобы прослушать фрагмент.";
    rows.forEach((segment) => {
      const timing = editor.timingBySegment.get(segment.source_segment_id);
      const row = document.createElement("article");
      row.className = "speaker-timeline-row";
      const seekButton = text("button", timing ? formatTime(timing.start_ms) : "—");
      seekButton.type = "button";
      seekButton.className = "timestamp";
      seekButton.disabled = !timing;
      if (timing) seekButton.addEventListener("click", () => playSample(editor, segment));
      const content = document.createElement("div");
      content.append(text("p", segmentText(editor, segment)));
      row.append(seekButton, content);
      list.append(row);
    });
  }

  function renderSpeakerPanel() {
    const panel = byId("speaker-panel");
    panel.replaceChildren();
    const editor = state.editor;
    if (!editor?.revision) {
      panel.append(text("p", "Спикеры станут доступны после публикации транскрипции."));
      return;
    }
    const groups = visibleGroups(editor);
    byId("open-speakers-button").textContent = "Изменить спикеров";
    byId("open-speakers-button").disabled = !groups.length;
    if (!groups.length) {
      panel.append(text("p", "Группы спикеров не найдены."));
      return;
    }
    groups.forEach((group, index) => {
      const groupId = group.speaker_group_id;
      const context = groupSegments(editor, groupId);
      const card = document.createElement("section");
      card.className = "speaker-card";
      card.id = "speaker-card-" + groupId;
      const heading = document.createElement("div");
      heading.className = "speaker-card-heading";
      const identity = document.createElement("div");
      identity.className = "speaker-card-identity";
      const title = text("h3", "Спикер " + (index + 1));
      const metrics = text("p", context.length + " реплик · " + formatTime(groupDuration(editor, groupId)) + " речи");
      metrics.className = "speaker-metrics";
      identity.append(title, metrics);
      const actions = document.createElement("div");
      actions.className = "speaker-card-actions";
      const sample = sampleSegments(editor, groupId)[0];
      const listen = text("button", "Слушать");
      listen.type = "button";
      listen.className = "secondary";
      listen.disabled = !sample;
      listen.addEventListener("click", () => sample && playSample(editor, sample));
      const timeline = text("button", "Таймкоды · " + context.length);
      timeline.type = "button";
      timeline.className = "secondary";
      timeline.disabled = !context.length;
      timeline.addEventListener("click", () => openSpeakerTimeline(editor, groupId));
      actions.append(listen, timeline);
      heading.append(identity, actions);
      const excerpt = text("p", groupFirstPhrase(editor, groupId));
      excerpt.className = "speaker-excerpt";
      const name = document.createElement("input");
      name.className = "group-label-input";
      name.value = editor.identityDrafts.get(groupId) ?? (isGenericGroupLabel(group.label) ? "" : group.label);
      name.maxLength = 240;
      name.placeholder = "Имя и фамилия";
      name.setAttribute("aria-label", "Имя и фамилия для спикера " + (index + 1));
      const save = text("button", "Сохранить имя");
      save.type = "button";
      save.disabled = !name.value.trim() || editor.saving;
      name.addEventListener("input", () => {
        editor.identityDrafts.set(groupId, name.value);
        save.disabled = !name.value.trim() || editor.saving;
      });
      save.addEventListener("click", () => saveGroupIdentity(editor, group, name, save));
      const nameRow = document.createElement("div");
      nameRow.className = "speaker-identity";
      nameRow.append(name, save);
      card.append(heading, excerpt, nameRow);

      const sampleControl = document.createElement("details");
      sampleControl.className = "sample-control speaker-sample-control";
      sampleControl.append(text("summary", "Качество и образец голоса"));
      const sampleBody = document.createElement("div");
      const sampleEligible = group.assignment_status === "confirmed" && !isGenericGroupLabel(group.label);
      sampleBody.append(text(
        "p",
        sampleEligible
          ? "После прослушивания фрагмента подтвердите добавление образца для автоматического сопоставления."
          : "Сначала укажите и сохраните имя спикера. Образец добавляется только после явного подтверждения.",
      ));
      const consent = document.createElement("input");
      consent.type = "checkbox";
      consent.disabled = !sampleEligible || editor.saving;
      const consentLabel = document.createElement("label");
      consentLabel.className = "sample-consent";
      consentLabel.append(consent, text("span", "Я прослушал(а) фрагмент и подтверждаю качество образца"));
      const enroll = text("button", "Добавить образец голоса");
      enroll.type = "button";
      enroll.className = "secondary enroll-sample";
      enroll.disabled = !sampleEligible || editor.saving;
      consent.addEventListener("change", () => {
        enroll.disabled = !sampleEligible || !consent.checked || editor.saving;
      });
      enroll.addEventListener("click", () => enrollSpeakerSample(editor, group, consent, enroll));
      sampleBody.append(consentLabel, enroll);
      sampleControl.append(sampleBody);
      card.append(sampleControl);

      const merge = document.createElement("details");
      merge.className = "speaker-merge";
      merge.append(text("summary", "Объединить локальные группы"));
      const mergeBody = document.createElement("div");
      mergeBody.append(text("p", "Выбирайте только из групп этой записи — другие пользователи и их данные здесь не отображаются."));
      if (groups.length < 2) {
        mergeBody.append(text("p", "В этой записи других групп нет."));
      } else {
        const controls = document.createElement("div");
        controls.className = "speaker-merge-controls";
        const select = document.createElement("select");
        select.setAttribute("aria-label", "Объединить спикера " + (index + 1) + " с другой локальной группой");
        select.append(new Option("Выберите спикера", ""));
        groups.filter((item) => item.speaker_group_id !== groupId).forEach((item) => {
          const targetNumber = groupNumber(editor, item.speaker_group_id);
          select.append(new Option("Спикер " + targetNumber + " · " + groupDisplayLabel(editor, item.speaker_group_id), item.speaker_group_id));
        });
        const mergeButton = text("button", "Объединить");
        mergeButton.type = "button";
        mergeButton.className = "secondary";
        mergeButton.disabled = true;
        select.addEventListener("change", () => { mergeButton.disabled = !select.value || editor.saving; });
        mergeButton.addEventListener("click", () => mergeSpeakerGroup(editor, groupId, select.value, mergeButton));
        controls.append(select, mergeButton);
        mergeBody.append(controls);
      }
      merge.append(mergeBody);
      card.append(merge);
      panel.append(card);
    });
    const mergedGroups = editor.revision.groups.filter((item) => item.merged_into_group_id);
    if (mergedGroups.length) {
      const restore = document.createElement("details");
      restore.className = "speaker-merge";
      restore.append(text("summary", "Отменить локальное объединение · " + mergedGroups.length));
      const restoreBody = document.createElement("div");
      mergedGroups.forEach((item) => {
        const itemRow = document.createElement("div");
        itemRow.className = "speaker-unmerge-row";
        const button = text("button", "Разъединить");
        button.type = "button";
        button.className = "secondary";
        button.addEventListener("click", () => unmergeSpeakerGroup(editor, item.speaker_group_id, button));
        itemRow.append(text("span", item.label + " → " + groupLabel(editor, item.merged_into_group_id)), button);
        restoreBody.append(itemRow);
      });
      restore.append(restoreBody);
      panel.append(restore);
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
      notify("Укажите имя и фамилию спикера.", true);
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
      notify("Имя спикера сохранено для всей группы.");
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
    byId("subtitle-line").textContent = subtitle
      ? segmentText(state.editor, subtitle)
      : "Субтитры появятся после обработки.";
    document.querySelectorAll(".transcript-segment").forEach((row) => {
      const id = row.dataset.segmentId;
      const timing = state.editor?.timingBySegment.get(id);
      row.classList.toggle("active", Boolean(timing && timeMs >= timing.start_ms && timeMs <= timing.end_ms));
    });
  }

  async function refreshEditor(editor, requestedView = null) {
    if (state.editor !== editor || editor.loading || editor.saving || hasEditorChanges(editor)) return;
    editor.loading = true;
    const generation = state.generation;
    try {
      const path = "/jobs/" + encodeURIComponent(editor.jobId);
      const [source, viewPreference] = await Promise.all([
        request(path + "/source"),
        request(path + "/transcript/view-preference"),
      ]);
      if (generation !== state.generation || state.editor !== editor) return;
      editor.viewPreference = viewPreference;
      const selectedView = requestedView || (
        viewPreference.preference === "last_selected"
          ? viewPreference.selected_view
          : viewPreference.preference
      );
      let transcript = null;
      let unavailable = "";
      try {
        transcript = await request(path + "/transcript?view=" + encodeURIComponent(selectedView));
      } catch (error) {
        if (error instanceof StaleSessionError || state.editor !== editor) return;
        unavailable = error.message;
      }
      if (generation !== state.generation || state.editor !== editor || hasEditorChanges(editor)) return;
      editor.revision = transcript;
      editor.timingBySegment = new Map((source.segments || []).map((segment) => [String(segment.id), segment]));
      editor.groupById = new Map((transcript?.groups || []).map((group) => [group.speaker_group_id, group]));
      editor.transcriptView = transcript?.view || null;
      editor.draftMode = transcript?.view?.resolved === "raw" ? "raw" : "clean";
      syncDraftModeControls(editor);
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
      byId("edit-transcript-button").disabled = !transcript;
      byId("download-text-button").hidden = !transcript;
      byId("editor-help").textContent = transcript
        ? "Ревизия " + transcript.revision.number + ". Нажмите таймкод, чтобы прослушать фрагмент."
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
      rawTextBySegment: new Map(), groupById: new Map(), decisions: new Map(), suggestions: [], segmentDrafts: new Map(), speakerDrafts: new Map(), identityDrafts: new Map(), mode: "read",
      draftMode: "clean", selectedGroupId: null, timelineGroupId: null,
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
    if (!editor?.revision || editor.mode !== "edit" || editor.saving || byId("save-editor-button").disabled) return;
    const { segmentTexts, groupUpdates, speakerUpdates } = editorChanges(editor);
    if (!Object.keys(segmentTexts).length && !Object.keys(groupUpdates).length && !Object.keys(speakerUpdates).length) {
      notify(hasIdentityDrafts(editor)
        ? "Сохраните имя спикера в соответствующей строке."
        : "Нет изменений для сохранения.");
      return;
    }
    setEditorBusy(editor, true);
    try {
      let result = editor.revision;
      if (Object.keys(segmentTexts).length || Object.keys(groupUpdates).length) {
        result = await request("/jobs/" + encodeURIComponent(editor.jobId) + "/transcript", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision_number: result.revision.number,
            segment_texts: segmentTexts,
            group_updates: groupUpdates,
          }),
        });
        if (state.editor !== editor) return;
        applyEditorRevision(editor, result);
      }
      for (const [segmentId, groupId] of Object.entries(speakerUpdates)) {
        result = await request("/jobs/" + encodeURIComponent(editor.jobId) + "/transcript/segments/" +
          encodeURIComponent(segmentId) + "/speaker", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ speaker_group_id: groupId, expected_revision_number: result.revision.number }),
        });
        if (state.editor !== editor) return;
        applyEditorRevision(editor, result);
      }
      editor.suggestions = [];
      editor.segmentDrafts.clear();
      editor.speakerDrafts.clear();
      byId("editor-conflict").hidden = true;
      renderSpeakerPanel();
      setTranscriptMode(editor, "read");
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
  byId("edit-transcript-button").addEventListener("click", () => enterTranscriptEditor().catch(showError));
  byId("cancel-editor-button").addEventListener("click", cancelTranscriptEditor);
  byId("download-text-button").addEventListener("click", downloadTranscriptText);
  byId("open-speakers-button").addEventListener("click", () => openSpeakers());
  byId("close-speakers-button").addEventListener("click", closeSpeakers);
  byId("clean-draft-button").addEventListener("click", () => setDraftMode("clean"));
  byId("raw-draft-button").addEventListener("click", () => setDraftMode("raw"));
  byId("back-to-speakers-button").addEventListener("click", () => closeSpeakerTimeline({ reopenSpeakers: true }));
  byId("close-speaker-timeline-button").addEventListener("click", () => closeSpeakerTimeline());
  byId("speakers-dialog").addEventListener("cancel", () => {
    stopSamplePlayback();
    const editor = state.editor;
    if (editor) {
      editor.selectedGroupId = null;
      requestAnimationFrame(renderTranscript);
    }
  });
  byId("speaker-timeline-dialog").addEventListener("cancel", () => {
    const editor = state.editor;
    if (editor) {
      editor.timelineGroupId = null;
      editor.selectedGroupId = null;
      requestAnimationFrame(renderTranscript);
    }
  });
  byId("copy-editor-draft").addEventListener("click", async () => {
    const editor = state.editor;
    if (!editor?.revision) return;
    const lines = editor.revision.segments.map((segment) => {
      const groupId = editor.speakerDrafts.get(segment.source_segment_id) ?? effectiveGroupId(editor, segment);
      const timing = editor.timingBySegment.get(segment.source_segment_id);
      const timestamp = timing ? formatTime(timing.start_ms) + " · " : "";
      return timestamp + groupDisplayLabel(editor, groupId) + ": " +
        (editor.segmentDrafts.get(segment.source_segment_id) ?? segment.text);
    });
    const groups = visibleGroups(editor).map((group) =>
      editor.identityDrafts.get(group.speaker_group_id) ?? group.label
    );
    try {
      await navigator.clipboard.writeText("Спикеры:\n" + groups.join("\n") + "\n\nТекст:\n" + lines.join("\n\n"));
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
