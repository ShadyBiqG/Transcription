(() => {
  "use strict";

  const api = "/api/v1";
  const state = { csrf: "", editor: null, user: null };
  const byId = (id) => document.getElementById(id);

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
    const headers = new Headers(options.headers || {});
    const method = (options.method || "GET").toUpperCase();
    if (state.csrf && !["GET", "HEAD"].includes(method)) {
      headers.set("X-CSRF-Token", state.csrf);
    }
    const response = await fetch(api + path, {
      ...options,
      credentials: "same-origin",
      headers,
    });
    if (response.status === 204) return null;
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const revision = body.details?.current_revision_number;
      const suffix = revision ? " Текущая ревизия: " + revision + "." : "";
      throw new Error((body.message || "Не удалось выполнить запрос.") + suffix);
    }
    return body;
  }

  function activateView(name) {
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
    state.user = user;
    state.csrf = csrf || state.csrf;
    byId("auth-panel").hidden = true;
    byId("workspace").hidden = false;
    byId("session-state").textContent = user.login + " · " + user.role;
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
      detail.append(text("strong", job.source_name), text("small", job.status + " · " + (job.updated_at || "")));
      const button = text("button", "Открыть");
      button.type = "button";
      button.addEventListener("click", () => openJob(job));
      row.append(detail, button);
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
    const data = await request("/jobs");
    renderJobs(data.items || []);
  }

  async function loadProfiles() {
    const data = await request("/voice-profiles");
    renderProfiles(data.items || []);
    return data.items || [];
  }

  async function loadVocabulary() {
    const data = await request("/vocabulary");
    renderVocabulary(data.items || []);
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
    editor.revision.groups.forEach((group) => {
      const row = document.createElement("article");
      row.className = "group-editor";
      row.dataset.groupId = group.speaker_group_id;
      row.append(text("small", group.merged_into_group_id ? "Объединена с " + group.merged_into_group_id : "Локальная группа"));
      const name = document.createElement("input");
      name.className = "group-label-input";
      name.value = group.label;
      name.maxLength = 240;
      name.setAttribute("aria-label", "Метка группы");
      const profile = document.createElement("select");
      profile.className = "group-profile-select";
      profileOptions(profile, group.profile_id);
      row.append(name, profile);
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
    byId("editor-title").textContent = job.source_name;
    byId("editor-status").textContent = job.status;
    byId("audio-player").src = api + "/jobs/" + encodeURIComponent(job.id) + "/audio";
    activateView("editor");
    try {
      const [source, profiles] = await Promise.all([
        request("/jobs/" + job.id + "/source"),
        request("/voice-profiles"),
      ]);
      let transcript = null;
      let unavailable = "";
      try {
        transcript = await request("/jobs/" + job.id + "/transcript");
      } catch (error) {
        unavailable = error.message;
      }
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
      state.editor = { unavailable: error.message };
      renderGroups();
      renderTranscript();
      byId("save-editor-button").disabled = true;
      notify(error.message, true);
    }
  }

  async function saveEditor() {
    const editor = state.editor;
    if (!editor?.revision) return;
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
      editor.revision = result;
      editor.groupById = new Map(result.groups.map((group) => [group.speaker_group_id, group]));
      renderGroups();
      renderTranscript();
      notify("Правки сохранены как ревизия " + result.revision.number + ".");
    } catch (error) {
      notify(error.message, true);
    }
  }

  byId("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await request("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))),
      });
      setAuthenticated(result.user, csrfCookie());
      await loadJobs();
      notify("Вход выполнен.");
    } catch (error) {
      notify(error.message, true);
    }
  });

  byId("register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await request("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))),
      });
      notify("Учётная запись «" + result.user.login + "» создана. Теперь войдите.");
      event.currentTarget.reset();
    } catch (error) {
      notify(error.message, true);
    }
  });

  byId("upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await request("/jobs", { method: "POST", body: new FormData(event.currentTarget) });
      event.currentTarget.reset();
      await loadJobs();
      notify("Запись принята в приватную очередь.");
    } catch (error) {
      notify(error.message, true);
    }
  });

  byId("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await request("/voice-profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))),
      });
      event.currentTarget.reset();
      await loadProfiles();
      notify("Общий профиль создан без добавления образца голоса.");
    } catch (error) {
      notify(error.message, true);
    }
  });

  byId("vocabulary-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    const variants = String(values.variants || "").split(",").map((item) => item.trim()).filter(Boolean);
    try {
      await request("/vocabulary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ canonical: values.canonical, variants }),
      });
      event.currentTarget.reset();
      await loadVocabulary();
      notify("Термин добавлен в общий словарь будущих запусков.");
    } catch (error) {
      notify(error.message, true);
    }
  });

  byId("save-editor-button").addEventListener("click", saveEditor);

  byId("logout-button").addEventListener("click", async () => {
    try {
      await request("/auth/logout", { method: "POST" });
    } catch (_) {
      // Cookie будет очищена браузером при следующем входе.
    }
    state.csrf = "";
    state.editor = null;
    state.user = null;
    byId("workspace").hidden = true;
    byId("auth-panel").hidden = false;
    byId("session-state").textContent = "Сессия завершена";
  });

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", async () => {
      activateView(button.dataset.view);
      try {
        if (button.dataset.view === "profiles") await loadProfiles();
        if (button.dataset.view === "vocabulary") await loadVocabulary();
        if (button.dataset.view === "admin") {
          byId("admin-overview").textContent = JSON.stringify(await request("/admin/overview"), null, 2);
        }
      } catch (error) {
        notify(error.message, true);
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
    } catch (_) {
      byId("session-state").textContent = "Войдите или зарегистрируйтесь";
    }
  })();
})();
