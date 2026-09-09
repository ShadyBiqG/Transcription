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
  clearInterval(refreshTimer);
  clearInterval(healthTimer);
  refreshJobs();
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
  card.innerHTML = `
    <div><strong>${escapeHtml(job.original_filename)}</strong><span>${formatSize(job.size_bytes)}</span></div>
    <span class="badge ${job.status}">${job.status}</span>
    <p>${job.error_message ? escapeHtml(job.error_message) : `${job.language} · ${job.model} · говорящие: ${escapeHtml(job.speaker_detection)}`}</p>
    <div class="job-times">
      <span><b>Создано:</b> ${formatDateTime(job.created_at)}</span>
      <span><b>Начало:</b> ${formatDateTime(job.started_at, "ожидает запуска")}</span>
      <span><b>Окончание:</b> ${formatDateTime(job.completed_at, completedFallback)}</span>
    </div>
    <footer>${downloads}<a data-download="${job.manifest_url}" href="#">Manifest</a></footer>`;
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

async function initialize() {
  try {
    const user = await (await api("/api/v1/auth/me")).json();
    showApp(user);
  } catch (_) {
    showAuth();
  }
}

initialize();
