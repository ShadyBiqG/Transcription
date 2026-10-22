(() => {
  "use strict";

  const api = "/api/v1";
  const state = {
    csrf: "", editor: null, user: null, generation: 0, editorGeneration: 0,
    jobsTimer: null, jobsGeneration: 0, pendingRuns: new Set(), authBusy: false,
  };
  const byId = (id) => document.getElementById(id);
  const statusLabels = {
    uploaded: "Загружена", queued: "В очереди", processing: "Обрабатывается",
    ready: "Готова", failed: "Ошибка обработки", cancelled: "Отменена", stalled: "Обработка прервана",
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
    byId("groups-list").replaceChildren();
    byId("transcript-list").replaceChildren();
  }

  function clearSession(message = "Войдите или зарегистрируйтесь") {
    state.generation += 1;
    state.csrf = "";
    state.user = null;
    state.pendingRuns.clear();
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
      throw new Error((body.message || "Не удалось выполнить запрос.") + suffix);
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
      const actions = document.createElement("div");
      actions.className = "job-actions";
      if (["uploaded", "failed", "cancelled", "stalled"].includes(job.status)) {
        const run = text("button", state.pendingRuns.has(job.id) ? "Запуск…" : "Транскрибировать");
        run.type = "button";
        run.disabled = state.pendingRuns.has(job.id);
        run.addEventListener("click", () => startRun(job, run));
        actions.append(run);
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
    const data = await request("/jobs");
    requireGeneration(generation);
    if (load !== state.jobsGeneration) return;
    renderJobs(data.items || []);
    if ((data.items || []).some((job) => ["queued", "processing"].includes(job.status))) {
      state.jobsTimer = setTimeout(() => { loadJobs().catch(showError); }, 4000);
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
    byId("admin-overview").textContent = JSON.stringify(overview, null, 2);
    renderAdminUsers(users.items || []);
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
      seekButton.disabled = !timing;
      if (timing) seekButton.addEventListener("click", () => seek(timing.start_ms));
      const body = document.createElement("div");
      body.className = "transcript-body";
      const speaker = editor.groupById.get(segment.primary_group_id);
      body.append(text("small", speaker?.label || "Неизвестный говорящий"));
      const input = document.createElement("textarea");
      input.className = "transcript-edit";
      input.dataset.segmentId = segment.source_segment_id;
      input.rows = Math.max(2, Math.ceil(segment.text.length / 72));
      input.value = segment.text;
      input.setAttribute("aria-label", "Текст реплики");
      body.append(input);
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
      enroll.disabled = Boolean(group.merged_into_group_id);
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
        } catch (error) {
          showError(error);
        } finally {
          if (state.editor === editor) enroll.disabled = Boolean(group.merged_into_group_id);
        }
      });
      row.append(name, profile, enroll);
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

  async function openJob(job) {
    if (!state.user) return;
    const generation = state.generation;
    clearEditor();
    const editorGeneration = state.editorGeneration;
    const isCurrent = () => generation === state.generation && editorGeneration === state.editorGeneration;
    byId("editor-title").textContent = job.source_name;
    byId("editor-status").textContent = statusLabels[job.status] || "Неизвестный статус";
    byId("audio-player").src = api + "/jobs/" + encodeURIComponent(job.id) + "/audio";
    activateView("editor");
    try {
      const [source, profiles] = await Promise.all([
        request("/jobs/" + job.id + "/source"),
        request("/voice-profiles"),
      ]);
      if (!isCurrent()) return;
      let transcript = null;
      let unavailable = "";
      try {
        transcript = await request("/jobs/" + job.id + "/transcript");
      } catch (error) {
        if (error instanceof StaleSessionError || !isCurrent()) return;
        unavailable = error.message;
      }
      if (!isCurrent()) return;
      state.editor = {
        jobId: job.id,
        profiles: profiles.items || [],
        revision: transcript,
        timingBySegment: new Map((source.segments || []).map((segment) => [segment.id, segment])),
        groupById: new Map((transcript?.groups || []).map((group) => [group.speaker_group_id, group])),
        unavailable,
      };
      renderGroups();
      renderTranscript();
      byId("save-editor-button").disabled = !transcript;
      byId("editor-help").textContent = transcript
        ? "Правки сохраняются отдельной ревизией. Аудио и субтитры синхронизированы."
        : unavailable || "Дождитесь публикации результата обработки.";
    } catch (error) {
      if (error instanceof StaleSessionError || !isCurrent()) return;
      state.editor = { unavailable: error.message };
      renderGroups();
      renderTranscript();
      byId("save-editor-button").disabled = true;
      showError(error);
    }
  }

  async function saveEditor() {
    const editor = state.editor;
    if (!editor?.revision || byId("save-editor-button").disabled) return;
    const segmentTexts = {};
    editor.revision.segments.forEach((segment) => {
      const input = document.querySelector(".transcript-edit[data-segment-id='" + CSS.escape(segment.source_segment_id) + "']");
      if (input && input.value !== segment.text) segmentTexts[segment.source_segment_id] = input.value;
    });
    const groupUpdates = {};
    editor.revision.groups.forEach((group) => {
      const row = document.querySelector(".group-editor[data-group-id='" + CSS.escape(group.speaker_group_id) + "']");
      const label = row?.querySelector(".group-label-input")?.value;
      const profileId = row?.querySelector(".group-profile-select")?.value || null;
      if (label && (label !== group.label || profileId !== group.profile_id)) {
        groupUpdates[group.speaker_group_id] = {
          label_snapshot: label,
          profile_id: profileId,
          assignment_status: profileId ? "confirmed" : "unknown",
          provenance: "manual",
        };
      }
    });
    if (!Object.keys(segmentTexts).length && !Object.keys(groupUpdates).length) {
      notify("Нет изменений для сохранения.");
      return;
    }
    byId("save-editor-button").disabled = true;
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
      renderGroups();
      renderTranscript();
      notify("Правки сохранены как ревизия " + result.revision.number + ".");
    } catch (error) {
      if (state.editor === editor) showError(error);
    } finally {
      if (state.editor === editor) byId("save-editor-button").disabled = false;
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
    const timeMs = event.currentTarget.currentTime * 1000;
    document.querySelector(".waveform span").style.width = Math.min(100, Math.max(0, timeMs / 1000)) + "%";
    renderSubtitle(timeMs);
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
