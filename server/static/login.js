"use strict";

const form = document.getElementById("form");
const errorBox = document.getElementById("error");
const codeLabel = document.getElementById("code-label");
const codeInput = document.getElementById("code");

function showError(text) {
  errorBox.textContent = text;
  errorBox.hidden = false;
}

fetch("/api/login-info")
  .then((r) => r.json())
  .then((info) => {
    if (info.totp_required) {
      codeLabel.hidden = false;
      codeInput.required = true;
    }
  })
  .catch(() => {}); // не критично: сервер всё равно проверит код при входе, если он нужен

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
        code: codeInput.value,
      }),
    });
    if (r.ok) {
      location.href = "/";
      return;
    }
    let message = "Ошибка входа";
    try {
      message = (await r.json()).detail || message;
    } catch {}
    showError(message);
  } catch {
    showError("Нет связи с сервером");
  } finally {
    button.disabled = false;
  }
});
