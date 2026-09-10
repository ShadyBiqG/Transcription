const authShell = document.querySelector("#auth-shell");
const appShell = document.querySelector("#app-shell");
const authForm = document.querySelector("#auth-form");
const authMessage = document.querySelector("#auth-message");
const currentUser = document.querySelector("#current-user");
const form = document.querySelector("#upload-form");
const jobsNode = document.querySelector("#jobs");
const messageNode = document.querySelector("#message");
const healthNode = document.querySelector("#health");
let refreshTimer = null;
let healthTimer = null;
const attributionRuns = new Map();

function showAuth() {
  appShell.hidden = true;
  authShell.hidden = false;
  currentUser.textContent = "";
  clearInterval(refreshTimer);
  clearInterval(healthTimer);
}

function showApp(user) {
  authShell.hidden = true;
  appShell.hidden = false;
  currentUser.textContent = user.email;
  document.querySelectorAll(".admin-only").forEach((node) => { node.hidden = !user.is_admin; });
  document.querySelector("#main-tabs").hidden = !user.is_admin;
  clearInterval(refreshTimer);
  clearInterval(healthTimer);
  refreshJobs();
  loadPersonalUsage();
  refreshHealth();
  refreshTimer = setInterval(refreshJobs, 3000);
  healthTimer = setInterval(refreshHealth, 15000);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* нет JSON */ }
    if (response.status === 401 && !path.startsWith("/api/v1/auth/")) showAuth();
    throw new Error(detail);
  }
  return response;
}

async function authenticate(action) {
  authMessage.textContent = action === "register" ? "Регистрация…" : "Вход…";
  const body = {
    email: document.querySelector("#auth-email").value,
    password: document.querySelector("#auth-password").value,
  };
  try {
    const response = await api(`/api/v1/auth/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const user = await response.json();
    authForm.reset();
    authMessage.textContent = "";
    showApp(user);
  } catch (error) {
    authMessage.textContent = error.message;
  }
}

authForm.addEventListener("submit", (event) => {
  event.preventDefault();
  authenticate("login");
});

document.querySelector("#register").addEventListener("click", () => {
  if (authForm.reportValidity()) authenticate("register");
});

document.querySelector("#logout").addEventListener("click", async () => {
  try { await api("/api/v1/auth/logout", { method: "POST" }); } finally { showAuth(); }
});

async function refreshHealth() {
  try {
    const data = await (await fetch("/api/v1/health")).json();
    healthNode.textContent = data.noscribe === "ready"
      ? `Готово · модели: ${data.models.join(", ")}`
      : "noScribe недоступен";
    healthNode.dataset.ok = data.noscribe === "ready";
  } catch (error) {
    healthNode.textContent = `Сервис недоступен: ${error.message}`;
    healthNode.dataset.ok = "false";
  }
}

function formatSize(bytes) {
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatDateTime(value, fallback = "—") {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("ru-RU", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function renderJob(job) {
  const card = document.createElement("article");
  card.className = "job";
  const downloads = job.status === "completed"
    ? `<a data-open="${job.transcript_url}" href="#">Открыть транскрипцию</a>`
      + `<a data-download="${job.transcript_url}" href="#">Скачать файл</a>`
    : "";
  const completedFallback = job.status === "running" ? "выполняется" : "—";
  const run = attributionRuns.get(job.id);
  const attribution = job.status === "completed" ? `
    <div class="attribution"><button data-attribution="${job.id}" type="button">Определить подписи по видео</button>
    ${run ? `<span class="badge ${run.status}">${escapeHtml(run.status)}</span>` : ""}
    ${run?.attributed_transcript_url ? `<a data-open="${run.attributed_transcript_url}" href="#">Результат с подписями</a>` : ""}</div>
    ${run?.segments?.length ? `<div class="segments">${run.segments.map((segment) => `<button class="secondary" data-segment="${segment.id}" data-run="${run.id}" data-job="${job.id}" type="button">${escapeHtml(segment.manual_label || segment.speaker_label || "unknown")} · ${Math.floor(segment.start_ms / 1000)}с</button>`).join("")}</div>` : ""}` : "";
  card.innerHTML = `
    <div><strong>${escapeHtml(job.original_filename)}</strong><span>${formatSize(job.size_bytes)}</span></div>
    <span class="badge ${job.status}">${job.status}</span>
    <p>${job.error_message ? escapeHtml(job.error_message) : `${job.language} · ${job.model} · говорящие: ${escapeHtml(job.speaker_detection)}`}</p>
    <div class="job-times">
      <span><b>Создано:</b> ${formatDateTime(job.created_at)}</span>
      <span><b>Начало:</b> ${formatDateTime(job.started_at, "ожидает запуска")}</span>
      <span><b>Окончание:</b> ${formatDateTime(job.completed_at, completedFallback)}</span>
    </div>
    ${attribution}<footer>${downloads}<a data-download="${job.manifest_url}" href="#">Manifest</a></footer>`;
  card.querySelector("[data-attribution]")?.addEventListener("click", () => startAttribution(job.id));
  card.querySelectorAll("[data-segment]").forEach((button) => button.addEventListener("click", () => editSpeaker(button)));
  card.querySelectorAll("[data-download]").forEach((link) => {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      await download(link.dataset.download);
    });
  });
  card.querySelectorAll("[data-open]").forEach((link) => {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      await openTranscript(link.dataset.open);
    });
  });
  return card;
}

async function editSpeaker(button) {
  const speakerLabel = window.prompt("Введите подпись говорящего", button.textContent.split(" · ")[0]);
  if (!speakerLabel?.trim()) return;
  try {
    const path = `/api/v1/jobs/${button.dataset.job}/attribution-runs/${button.dataset.run}/segments/${button.dataset.segment}`;
    await api(path, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ speaker_label: speakerLabel.trim() }) });
    await pollAttribution(button.dataset.job, button.dataset.run);
  } catch (error) { messageNode.textContent = error.message; }
}

async function startAttribution(jobId) {
  const consent = window.confirm("Внешнему провайдеру будут отправлены только кадры видео. Аудио и текст не передаются. Продолжить?");
  if (!consent) return;
  try {
    const response = await api(`/api/v1/jobs/${jobId}/attribution-runs`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ external_processing_consent: true }),
    });
    const run = await response.json();
    attributionRuns.set(jobId, run);
    pollAttribution(jobId, run.id);
    await refreshJobs();
  } catch (error) { messageNode.textContent = error.message; }
}

async function pollAttribution(jobId, runId) {
  try {
    const run = await (await api(`/api/v1/jobs/${jobId}/attribution-runs/${runId}`)).json();
    attributionRuns.set(jobId, run);
    await refreshJobs();
    if (["queued", "running"].includes(run.status)) setTimeout(() => pollAttribution(jobId, runId), 2500);
  } catch (error) { messageNode.textContent = error.message; }
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value;
  return node.innerHTML;
}

async function download(path) {
  try {
    const response = await api(path);
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const match = disposition.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i);
    const name = match ? decodeURIComponent(match[1].replace(/\"/g, "")) : "download";
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    anchor.click();
    URL.revokeObjectURL(url);
  } catch (error) { messageNode.textContent = error.message; }
}

async function openTranscript(path) {
  const preview = window.open("about:blank", "_blank");
  try {
    if (!preview) throw new Error("Браузер заблокировал новое окно");
    preview.opener = null;
    const response = await api(path);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    preview.location.href = url;
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) {
    if (preview) preview.close();
    messageNode.textContent = error.message;
  }
}

async function refreshJobs() {
  try {
    const jobs = await (await api("/api/v1/jobs")).json();
    jobsNode.replaceChildren(...jobs.map(renderJob));
    if (!jobs.length) jobsNode.textContent = "Заданий пока нет.";
  } catch (error) {
    if (!appShell.hidden) jobsNode.textContent = error.message;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  messageNode.textContent = "Загрузка…";
  const body = new FormData(form);
  try {
    const job = await (await api("/api/v1/jobs", { method: "POST", body })).json();
    messageNode.textContent = `Задание ${job.id} принято.`;
    form.reset();
    document.querySelector("#language").value = "ru";
    document.querySelector("#speaker_detection").value = "auto";
    await refreshJobs();
  } catch (error) { messageNode.textContent = error.message; }
});

document.querySelector("#refresh").addEventListener("click", refreshJobs);

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", async () => {
  document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("active", node === tab));
  document.querySelectorAll(".app-section").forEach((node) => { node.hidden = node.id !== tab.dataset.section; });
  if (tab.dataset.section === "admin-overview") await loadOverview();
  if (tab.dataset.section === "admin-models") await loadUsage();
  if (tab.dataset.section === "admin-settings") await loadAdminSettings();
}));

function showMetrics(selector, values) {
  document.querySelector(selector).innerHTML = Object.entries(values).map(([label, value]) => `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value ?? 0))}</strong></div>`).join("");
}

function usageTotalsRow(data, columns = 9) {
  return `<tfoot><tr><th>Итого</th><th>${data.calls ?? 0}</th><th>${data.successful_calls ?? 0}</th><th>${data.failed_calls ?? 0}</th><th>${data.retries ?? 0}</th><th>${data.images ?? 0}</th><th>${data.input_units ?? 0}</th><th>${data.output_units ?? 0}</th><th colspan="${Math.max(1, columns - 8)}">${escapeHtml(String(data.confirmed_cost ?? "0"))} ₽</th></tr></tfoot>`;
}

function usageByUserTable(data) {
  if (!data.by_user?.length) return "Внешних обращений пока нет.";
  const rows = data.by_user.map((item) => `<tr><td>${escapeHtml(item.email)}</td><td>${item.calls}</td><td>${item.successful_calls}</td><td>${item.failed_calls}</td><td>${item.retries}</td><td>${item.images}</td><td>${item.input_units}</td><td>${item.output_units}</td><td>${escapeHtml(item.confirmed_cost)} ₽</td></tr>`).join("");
  return `<table><thead><tr><th>Пользователь</th><th>Вызовы</th><th>Успешно</th><th>Ошибки</th><th>Повторы</th><th>Кадры</th><th>Вход</th><th>Выход</th><th>Стоимость</th></tr></thead><tbody>${rows}</tbody>${usageTotalsRow(data)}</table>`;
}

async function loadPersonalUsage() {
  const cards = document.querySelector("#personal-usage-cards"); cards.textContent = "Загрузка…";
  try {
    const data = await (await api("/api/v1/external-usage")).json();
    showMetrics("#personal-usage-cards", { Вызовы: data.calls, Успешно: data.successful_calls, Ошибки: data.failed_calls, Кадры: data.images, "Стоимость, ₽": data.confirmed_cost });
    document.querySelector("#personal-usage-table").innerHTML = usageByUserTable(data);
  } catch (error) { cards.textContent = `Не удалось загрузить статистику: ${error.message}`; }
}

async function loadOverview() {
  const node = document.querySelector("#overview-cards"); node.textContent = "Загрузка…";
  try { const data = await (await api("/api/v1/admin/statistics/overview")).json();
    showMetrics("#overview-cards", { Пользователи: data.users, Задания: data.jobs, Завершено: data.completed_jobs, Ошибки: data.failed_jobs, "В очереди/работе": data.active_jobs, "Хранилище, байт": data.storage_bytes });
    const rows = data.by_user.map((item) => `<tr><td>${escapeHtml(item.email)}</td><td>${escapeHtml(item.role)}</td><td>${item.jobs}</td><td>${item.completed_jobs}</td><td>${item.failed_jobs}</td><td>${item.active_jobs}</td><td>${formatSize(item.source_bytes)}</td></tr>`).join("");
    document.querySelector("#overview-table").innerHTML = `<table><thead><tr><th>Пользователь</th><th>Роль</th><th>Задания</th><th>Завершено</th><th>Ошибки</th><th>В очереди/работе</th><th>Объём</th></tr></thead><tbody>${rows}</tbody><tfoot><tr><th colspan="2">Итого</th><th>${data.jobs}</th><th>${data.completed_jobs}</th><th>${data.failed_jobs}</th><th>${data.active_jobs}</th><th>${formatSize(data.source_bytes)}</th></tr></tfoot></table>`;
  } catch (error) { node.textContent = `Не удалось загрузить статистику: ${error.message}`; }
}

async function loadUsage() {
  const node = document.querySelector("#usage-cards"); node.textContent = "Загрузка…";
  let data;
  try { data = await (await api("/api/v1/admin/external-usage")).json(); }
  catch (error) { node.textContent = `Не удалось загрузить расходы: ${error.message}`; return; }
  showMetrics("#usage-cards", { Вызовы: data.calls, Успешно: data.successful_calls, Ошибки: data.failed_calls, Кадры: data.images, "Стоимость, ₽": data.confirmed_cost });
  document.querySelector("#usage-table").innerHTML = usageByUserTable(data);
  document.querySelector("#usage-details").innerHTML = data.items.length ? `<table><thead><tr><th>Время</th><th>Провайдер</th><th>Модель</th><th>Статус</th><th>Кадры</th><th>Стоимость</th></tr></thead><tbody>${data.items.map((item) => `<tr><td>${formatDateTime(item.created_at)}</td><td>${escapeHtml(item.provider)}</td><td>${escapeHtml(item.actual_model || item.requested_model)}</td><td>${escapeHtml(item.status)}</td><td>${item.image_count}</td><td>${item.provider_cost ?? "—"}</td></tr>`).join("")}</tbody></table>` : "Обращений нет.";
}

async function loadAdminSettings() {
  const message = document.querySelector("#admin-message"); message.textContent = "Загрузка…";
  const settings = await (await api("/api/v1/admin/settings")).json();
  const catalog = await (await api("/api/v1/admin/models")).json();
  document.querySelector("#provider-enabled").checked = settings.provider_enabled;
  document.querySelector("#provider-name").value = settings.provider_name;
  document.querySelector("#provider-base-url").value = settings.provider_base_url;
  document.querySelector("#global-budget").value = settings.global_budget || "";
  document.querySelector("#job-budget").value = settings.default_job_budget || "";
  const options = catalog.models.map((model) => `<option value="${escapeHtml(model.model_id)}">${escapeHtml(model.name)}</option>`).join("");
  document.querySelector("#model-options").innerHTML = options;
  document.querySelector("#primary-model").value = settings.primary_model_id;
  document.querySelector("#fallback-model").value = settings.fallback_model_id;
  document.querySelector("#allowed-models").value = settings.allowed_model_ids.join("\n");
  message.textContent = catalog.stale ? `Список моделей недоступен: ${catalog.last_error || "ошибка провайдера"}. Введите идентификаторы моделей вручную.` : catalog.models.length ? `Список моделей получен: ${formatDateTime(catalog.fetched_at)}. Можно выбрать вариант или ввести свой.` : "Список моделей пуст. Введите идентификаторы моделей вручную.";
}

document.querySelector("#admin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = { provider_enabled: document.querySelector("#provider-enabled").checked, provider_name: document.querySelector("#provider-name").value.trim(), provider_base_url: document.querySelector("#provider-base-url").value.trim(), primary_model_id: document.querySelector("#primary-model").value.trim(), fallback_model_id: document.querySelector("#fallback-model").value.trim(), allowed_model_ids: document.querySelector("#allowed-models").value.split(/\r?\n|,/).map((value) => value.trim()).filter(Boolean), global_budget: document.querySelector("#global-budget").value || null, default_job_budget: document.querySelector("#job-budget").value || null };
  const key = document.querySelector("#routerai-key").value; if (key) body.api_key = key;
  try { await api("/api/v1/admin/settings", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); document.querySelector("#admin-message").textContent = "Настройки сохранены"; }
  catch (error) { document.querySelector("#admin-message").textContent = error.message; }
});

document.querySelector("#refresh-catalog").addEventListener("click", async () => { try { await api("/api/v1/admin/models/refresh", { method: "POST" }); await loadAdminSettings(); } catch (error) { document.querySelector("#admin-message").textContent = `${error.message} Введите модели вручную.`; } });
document.querySelector("#test-routerai").addEventListener("click", async () => { try { const data = await (await api("/api/v1/admin/settings/test", { method: "POST" })).json(); document.querySelector("#admin-message").textContent = data.ok ? "Провайдер и модель доступны" : "API-ключ не настроен или отклонён"; } catch (error) { document.querySelector("#admin-message").textContent = `Проверка не пройдена: ${error.message}`; } });
document.querySelectorAll("[data-admin-refresh]").forEach((node) => node.addEventListener("click", () => node.dataset.adminRefresh === "overview" ? loadOverview() : loadUsage()));
document.querySelector("#refresh-personal-usage").addEventListener("click", loadPersonalUsage);

async function initialize() {
  try {
    const user = await (await api("/api/v1/auth/me")).json();
    showApp(user);
  } catch (_) {
    showAuth();
  }
}

initialize();
