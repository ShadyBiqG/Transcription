const tokenInput = document.querySelector("#token");
const form = document.querySelector("#upload-form");
const jobsNode = document.querySelector("#jobs");
const messageNode = document.querySelector("#message");
const healthNode = document.querySelector("#health");

tokenInput.value = localStorage.getItem("transcription-api-token") || "";
tokenInput.addEventListener("change", () => {
  localStorage.setItem("transcription-api-token", tokenInput.value.trim());
  refreshJobs();
});

function headers() {
  const token = tokenInput.value.trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(), ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* noop */ }
    throw new Error(detail);
  }
  return response;
}

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

function renderJob(job) {
  const card = document.createElement("article");
  card.className = "job";
  const downloads = job.status === "completed"
    ? `<a data-download="${job.transcript_url}" href="#">Скачать VTT</a>`
    : "";
  card.innerHTML = `
    <div><strong>${escapeHtml(job.original_filename)}</strong><span>${formatSize(job.size_bytes)}</span></div>
    <span class="badge ${job.status}">${job.status}</span>
    <p>${job.error_message ? escapeHtml(job.error_message) : `${job.language} · ${job.model}`}</p>
    <footer>${downloads}<a data-download="${job.manifest_url}" href="#">Manifest</a></footer>`;
  card.querySelectorAll("[data-download]").forEach((link) => {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      await download(link.dataset.download);
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

async function refreshJobs() {
  try {
    const jobs = await (await api("/api/v1/jobs")).json();
    jobsNode.replaceChildren(...jobs.map(renderJob));
    if (!jobs.length) jobsNode.textContent = "Заданий пока нет.";
  } catch (error) {
    jobsNode.textContent = error.message;
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
    await refreshJobs();
  } catch (error) { messageNode.textContent = error.message; }
});

document.querySelector("#refresh").addEventListener("click", refreshJobs);
refreshHealth();
refreshJobs();
setInterval(refreshJobs, 3000);
setInterval(refreshHealth, 15000);
