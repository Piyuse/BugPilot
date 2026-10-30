import "./styles.css";
import { api } from "./api";
import { element, errorMessage } from "./dom";
import type { Session } from "./types";
import loginView from "./views/login.html?raw";
import dashboardView from "./views/dashboard.html?raw";

async function start(): Promise<void> {
  const app = element("#app");
  try {
    const session = await api<Session>("auth/session/");
    if (session.authenticated) {
      document.body.className = "";
      document.title = "BugPilot · Your bugs, handled";
      app.innerHTML = dashboardView;
      const { mountDashboard } = await import("./dashboard");
      mountDashboard(session);
      return;
    }
    document.body.className = "login-body";
    document.title = "Sign in · BugPilot";
    app.innerHTML = loginView;
    element<HTMLInputElement>("#id_username").focus();
    element<HTMLFormElement>("#login-form").addEventListener("submit", async event => {
      event.preventDefault();
      const button = element<HTMLButtonElement>("#login-form button[type=submit]");
      const error = element("#login-error");
      error.hidden = true;
      button.disabled = true;
      try {
        await api<Session>("auth/login/", {
          method: "POST",
          body: JSON.stringify({
            username: element<HTMLInputElement>("#id_username").value.trim(),
            password: element<HTMLInputElement>("#id_password").value,
          }),
        });
        // Start a fresh application lifecycle with the newly rotated CSRF cookie.
        window.location.reload();
      } catch (failure) {
        error.textContent = errorMessage(failure);
        error.hidden = false;
        button.disabled = false;
      }
    });
  } catch (failure) {
    document.body.className = "login-body";
    app.replaceChildren();
    const card = document.createElement("main");
    card.className = "login-card";
    const title = document.createElement("h1");
    title.textContent = "Cannot connect to BugPilot";
    const message = document.createElement("p");
    message.className = "form-error";
    message.textContent = errorMessage(failure);
    const retry = document.createElement("button");
    retry.className = "button primary";
    retry.textContent = "Try again";
    retry.addEventListener("click", () => { window.location.reload(); });
    card.append(title, message, retry);
    app.append(card);
  }
}

window.addEventListener("session-expired", () => { window.location.reload(); }, { once: true });
void start();
