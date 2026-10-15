"use strict";

const $ = (sel) => document.querySelector(sel);

// Прозрачный GIF 1x1: подставляем в <img>, чтобы оборвать MJPEG-соединение
const BLANK_GIF = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";

async function api(url, options) {
  const r = await fetch(url, options);
  if (r.status === 401) {
    location.href = "/login";
    throw new Error("Нужен вход");
  }
  if (!r.ok) {
    let detail = r.statusText || "Ошибка " + r.status;
    try {
      detail = (await r.json()).detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return r;
}

function fmtSize(n) {
  if (n == null) return "";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + units[i];
}

function fmtDate(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

// ------------------------------------------------------------------ статус

async function refreshStatus() {
  try {
    const s = await (await api("/api/status")).json();
    $("#dot").className = "dot " + (s.online ? "on" : "off");
    $("#host").textContent = s.online
      ? s.hostname
      : s.reason === "agent_auth"
        ? "ошибка токена агента"
        : "не в сети";
  } catch {
    $("#dot").className = "dot off";
    $("#host").textContent = "нет связи с сервером";
  }
}

// ------------------------------------------------------------------ файлы

let currentPath = "";

function renderCrumbs(path) {
  const box = $("#crumbs");
  box.replaceChildren();
  const parts = path ? path.split("/") : [];
  const add = (label, target, isLast) => {
    const el = document.createElement(isLast ? "span" : "button");
    el.className = "crumb" + (isLast ? " current" : "");
    el.textContent = label;
    if (!isLast) el.addEventListener("click", () => loadDir(target));
    box.append(el);
  };
  add("Папки", "", parts.length === 0);
  parts.forEach((part, i) => {
    const sep = document.createElement("span");
    sep.className = "muted";
    sep.textContent = "/";
    box.append(sep);
    add(part, parts.slice(0, i + 1).join("/"), i === parts.length - 1);
  });
}

async function loadDir(path) {
  const errorBox = $("#files-error");
  errorBox.hidden = true;
  try {
    const data = await (await api("/api/files/list?path=" + encodeURIComponent(path))).json();
    currentPath = data.path;
    renderCrumbs(currentPath);
    $("#upload").disabled = !currentPath;
    $("#upload-label").classList.toggle("disabled", !currentPath);

    const rows = $("#rows");
    rows.replaceChildren();
    if (data.entries.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.className = "muted";
      td.textContent = "Пусто";
      tr.append(td);
      rows.append(tr);
    }
    for (const e of data.entries) {
      const full = currentPath ? currentPath + "/" + e.name : e.name;
      const tr = document.createElement("tr");

      const nameTd = document.createElement("td");
      if (e.is_dir) {
        const b = document.createElement("button");
        b.className = "link";
        b.textContent = "📁 " + e.name;
        b.addEventListener("click", () => loadDir(full));
        nameTd.append(b);
      } else {
        nameTd.textContent = "📄 " + e.name;
      }

      const sizeTd = document.createElement("td");
      sizeTd.className = "num";
      sizeTd.textContent = fmtSize(e.size);

      const dateTd = document.createElement("td");
      dateTd.className = "date";
      dateTd.textContent = fmtDate(e.mtime);

      const actionTd = document.createElement("td");
      if (!e.is_dir) {
        const a = document.createElement("a");
        a.href = "/api/files/download?path=" + encodeURIComponent(full);
        a.textContent = "Скачать";
        actionTd.append(a);
      }

      tr.append(nameTd, sizeTd, dateTd, actionTd);
      rows.append(tr);
    }
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.hidden = false;
  }
}

function uploadOne(file, overwrite) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const q = new URLSearchParams({
      path: currentPath,
      name: file.name,
      overwrite: overwrite ? "true" : "false",
    });
    xhr.open("POST", "/api/files/upload?" + q);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        setUploadStatus(`${file.name}: ${Math.round((ev.loaded / ev.total) * 100)}%`);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve("ok");
      if (xhr.status === 401) location.href = "/login";
      let detail = "Ошибка " + xhr.status;
      try {
        detail = JSON.parse(xhr.responseText).detail || detail;
      } catch {}
      if (xhr.status === 409 && !overwrite) return resolve("exists");
      reject(new Error(`${file.name}: ${detail}`));
    };
    xhr.onerror = () => reject(new Error(`${file.name}: нет связи`));
    xhr.send(file);
  });
}

function setUploadStatus(text) {
  const el = $("#upload-status");
  el.textContent = text;
  el.hidden = !text;
}

$("#upload").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  $("#files-error").hidden = true;
  let done = 0;
  try {
    for (const file of files) {
      let result = await uploadOne(file, false);
      if (result === "exists") {
        if (!confirm(`Файл «${file.name}» уже есть. Заменить?`)) continue;
        await uploadOne(file, true);
      }
      done++;
    }
    setUploadStatus(`Загружено файлов: ${done}`);
  } catch (err) {
    setUploadStatus("");
    $("#files-error").textContent = err.message;
    $("#files-error").hidden = false;
  }
  loadDir(currentPath);
});

// ------------------------------------------------------------------ экран

const screenImg = $("#screen");
const hint = $("#screen-hint");
let streaming = false;
let monitorsLoaded = false;

function screenParams() {
  return new URLSearchParams({
    fps: $("#fps").value,
    quality: $("#quality").value,
    scale: $("#scale").value,
    monitor: $("#monitor").value || "1",
  });
}

function startStream() {
  streaming = true;
  hint.hidden = true;
  screenImg.hidden = false;
  screenImg.src = "/api/screen/stream?" + screenParams() + "&t=" + Date.now();
  $("#toggle").textContent = "Стоп";
}

function stopStream() {
  streaming = false;
  screenImg.src = BLANK_GIF; // обрывает соединение
  screenImg.hidden = true;
  hint.hidden = false;
  hint.textContent = "Поток остановлен.";
  $("#toggle").textContent = "Старт";
}

// <img> не показывает текст ошибки, поэтому при сбое спрашиваем причину отдельным запросом
screenImg.addEventListener("error", async () => {
  if (!streaming) return;
  stopStream();
  try {
    await api("/api/screen/snapshot?" + screenParams());
    hint.textContent = "Поток прервался. Нажми «Старт» ещё раз.";
  } catch (err) {
    hint.textContent = err.message;
  }
});

$("#toggle").addEventListener("click", () => (streaming ? stopStream() : startStream()));
for (const id of ["fps", "quality", "scale", "monitor"]) {
  $("#" + id).addEventListener("change", () => streaming && startStream());
}
$("#fullscreen").addEventListener("click", () => screenImg.requestFullscreen?.());
$("#snap").addEventListener("click", () => {
  $("#snap").href = "/api/screen/snapshot?" + screenParams();
});

async function loadMonitors() {
  if (monitorsLoaded) return;
  try {
    const data = await (await api("/api/screen/monitors")).json();
    const select = $("#monitor");
    select.replaceChildren();
    for (const m of data.monitors) {
      const o = document.createElement("option");
      o.value = m.index;
      o.textContent = `${m.index} (${m.width}×${m.height})`;
      select.append(o);
    }
    $("#monitor-label").hidden = data.monitors.length < 2;
    monitorsLoaded = true;
  } catch (err) {
    hint.textContent = err.message;
  }
}

// ------------------------------------------------------------------ вкладки

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    const name = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $("#tab-files").hidden = name !== "files";
    $("#tab-screen").hidden = name !== "screen";
    if (name === "screen") loadMonitors();
    else if (streaming) stopStream(); // не гоняем трафик, пока экран не нужен
  });
}

$("#logout").addEventListener("click", async () => {
  try {
    await fetch("/api/logout", { method: "POST" });
  } finally {
    location.href = "/login";
  }
});

refreshStatus();
setInterval(refreshStatus, 10000);
loadDir("");
