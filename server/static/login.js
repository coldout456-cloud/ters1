"use strict";

const form = document.getElementById("form");
const errorBox = document.getElementById("error");

function showError(text) {
  errorBox.textContent = text;
  errorBox.hidden = false;
}

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
