import { api } from "./api";
import { element as $, elements as $$, errorMessage } from "./dom";
import type { Investigation, InvestigationDetail, Overview, Paginated, Repository, Session, Status } from "./types";

interface DashboardState {
  overview: Overview | null;
  submitting: boolean;
  repositories: Repository[];
  page: string;
  listPage: number;
  selected: string | null;
  selectedUpdated: string | null;
  refreshing: boolean;
  opener: HTMLElement | null;
}

export function mountDashboard(session: Session): void {
  const escapeHTML = (value: unknown): string => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" } as Record<string, string>)[char]!);
  const icon = (name: string): string => '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  const state: DashboardState = { overview: null, submitting: false, repositories: [], page: "overview", listPage: 1, selected: null, selectedUpdated: null, refreshing: false, opener: null };
  const labels: Record<Status, string> = { queued: "Queued", running: "In progress", pr_created: "Ready for review", needs_attention: "Needs attention" };
  const stageLabels: Record<string, string> = { fetching_ticket: "Reading ticket", preprocessing: "Preparing context", selecting_repository: "Finding repository", preparing_workspace: "Creating branch", preparing_validation: "Preparing test environment", fixing: "Investigating & fixing", validating: "Running checks", publishing: "Creating PR", linking_issue: "Commenting on GitHub issue", linking_jira: "Posting legacy issue comment", completed: "Completed", queued: "Waiting for worker" };

  function safeURL(value: string): string {
    try { const url = new URL(value); return url.protocol === "https:" ? escapeHTML(url.href) : "#"; } catch { return "#"; }
  }
  function timeAgo(value: string): string {
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (seconds < 60) return "Just now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function badge(run: Investigation): string {
    const label = labels[run.status] || run.status;
    return '<span class="badge ' + escapeHTML(run.status) + '"><span class="mini-dot"></span>' + escapeHTML(label) + "</span>";
  }
  let toastTimer: ReturnType<typeof setTimeout> | undefined;
  function toast(message: string): void {
    $("#toast").textContent = message;
    $("#toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 5000);
  }
  function showPage(page: string): void {
    if (!["overview", "investigations", "repositories", "integrations"].includes(page)) page = "overview";
    state.page = page;
    $$(".page").forEach((section) => { section.hidden = section.id !== "page-" + page; });
    $$(".nav-item").forEach((button) => { button.classList.toggle("active", button.dataset.page === page); button.setAttribute("aria-current", button.dataset.page === page ? "page" : "false"); });
    $("#breadcrumb-page").textContent = page.charAt(0).toUpperCase() + page.slice(1);
    $("#sidebar").classList.remove("open");
    if (location.hash !== "#" + page) history.replaceState(null, "", "#" + page);
    if (page === "investigations") loadList().catch(showGlobalError);
  }
  function showGlobalError(error: unknown): void { $("#loading-error").textContent = errorMessage(error); $("#loading-error").hidden = false; }

  function repositoryHint(): { value: string; match: Repository | undefined; url: string } {
    const value = $("#repository-input").value.trim();
    const canonical = value.toLowerCase().replace(/\/+$/, "").replace(/\.git$/, "");
    let match = state.repositories.find(repo => String(repo.id) === value || repo.name.toLowerCase() === value.toLowerCase() || repo.url.toLowerCase() === canonical);
    let url = "";
    try {
      const parsed = new URL(value || $("#github-issue").value.trim());
      const parts = parsed.pathname.split("/").filter(Boolean);
      if (parsed.protocol === "https:" && parsed.hostname === "github.com" && parts.length >= 2) {
        url = "https://github.com/" + parts.slice(0, 2).join("/").replace(/\.git$/, "");
      } else if (!value && parsed.protocol === "https:" && parsed.hostname === "api.github.com" && parts[0] === "repos" && parts.length >= 3) {
        url = "https://github.com/" + parts.slice(1, 3).join("/");
      }
    } catch { /* A registered repository name is also a supported hint. */ }
    if (!value && url) match = state.repositories.find(repo => repo.is_active && repo.url.toLowerCase() === url.toLowerCase());
    return { value, match, url };
  }
  function liveSetupIssues(): string[] {
    const config = state.overview?.pipeline;
    if (!config) return ["Cannot read live setup status. Check the backend connection and refresh setup status."];
    const issues = config.missing.length
      ? ["Missing backend credentials: " + config.missing.join(", ") + ". Add them to backend/.env, then restart the backend and worker."]
      : [];
    const hint = repositoryHint();
    if ((hint.value || !state.repositories.some(repo => repo.is_active)) && !hint.match) issues.push("The affected repository is not registered. Add its name and GitHub URL in Repositories.");
    else if (hint.match && !hint.match.is_active) issues.push("The selected repository is inactive. Enable it in Repositories.");
    return issues;
  }
  function syncSubmissionGuidance(): void {
    const issues = liveSetupIssues();
    $("#submission-guidance").hidden = !issues.length;
    $("#submission-guidance-text").textContent = issues.length ? "Before a live investigation can start:\n" + issues.map(issue => "• " + issue).join("\n") : "";
    $("#setup-integrations").hidden = !!state.overview?.pipeline.ready;
    const hint = repositoryHint();
    $("#setup-repository").hidden = !state.overview?.can_manage || !issues.length || (!hint.value && !hint.url);
    $("#setup-repository").textContent = hint.match ? "Edit repository configuration ↗" : "Register this repository ↗";
  }

  function syncLiveSetup(): void {
    const config = state.overview?.pipeline;
    $("#live-setup").textContent = !config ? "Checking live model configuration…" : config.ready
      ? "Live calls: OpenAI (" + config.preprocessing.model + ") → " + config.coding.model + ". Requires a real GitHub issue and repository."
      : "Live setup required in backend/.env: " + config.missing.join(", ") + ". Restart the backend and worker after adding credentials.";
    // Clicking an unconfigured live form must explain its blockers, not appear inert.
    // The API still enforces credentials and repository validation before queueing.
    $("#submit-ticket").disabled = state.submitting;
    syncSubmissionGuidance();
  }
  function renderOverview(data: Overview): void {
    state.overview = data;
    const counts = data.counts;
    $("#stat-total").textContent = String(Object.values(counts).reduce((sum, value) => sum + value, 0));
    $("#stat-running").textContent = String((counts.running || 0) + (counts.queued || 0));
    $("#stat-ready").textContent = String(counts.pr_created || 0);
    $("#stat-attention").textContent = String(counts.needs_attention || 0);
    $("#nav-running").textContent = String((counts.running || 0) + (counts.queued || 0));
    $("#nav-running").hidden = !(counts.running || counts.queued);
    $("#worker-dot").classList.toggle("offline", !data.worker_online);
    $("#worker-label").textContent = data.worker_online ? "Worker online · ready to help" : "Worker offline · start runworker";
    $("#ready-caption").textContent = "Pull requests ready for review";
    $("#add-repository").hidden = !data.can_manage;
    $("#integration-cards").innerHTML = data.integrations.map((provider) => '<article class="panel integration-card"><div class="card-top"><span class="integration-letter">' + escapeHTML(provider.name === "Jira" ? "◆" : provider.name === "OpenAI" ? "✳" : provider.name === "GitHub" ? "GH" : provider.name === "Codex" ? ">_" : "▤") + '</span><span class="badge ' + (provider.configured ? "pr_created" : "needs_attention") + '">' + (provider.configured ? "Configured" : "Setup required") + '</span></div><h3>' + escapeHTML(provider.name) + "</h3><p>" + escapeHTML(provider.description) + '</p><p class="integration-config">' + (provider.configured ? "Configuration present. Provider access is checked when the pipeline runs." : "Add the server configuration and restart the web server and worker.") + "</p></article>").join("");
    syncLiveSetup();
  }
  function renderTable(runs: Investigation[], container: string, emptyLabel = "No investigations yet") {
    if (!runs.length) {
      $(container).innerHTML = '<div class="empty-state">' + icon("bug") + "<h3>" + escapeHTML(emptyLabel) + "</h3><p>" + (emptyLabel === "No investigations yet" ? "The next late-night bug doesn't need to be your problem.<br>Submit a GitHub issue to start your first investigation." : "Try a different search or status filter.") + "</p></div>";
      return;
    }
    $(container).innerHTML = '<table><thead><tr><th>ISSUE</th><th>REPOSITORY</th><th>STATUS</th><th>CREATED</th><th><span class="muted">DETAILS</span></th></tr></thead><tbody>' + runs.map((run) => '<tr data-run="' + escapeHTML(run.id) + '" tabindex="0" aria-label="Open ' + escapeHTML(run.issue_key) + '"><td class="issue-cell"><span class="issue-key"><span class="issue-mark-inline">◆</span>' + escapeHTML(run.issue_key) + '</span><span class="issue-title">' + escapeHTML(run.title || "Waiting to read issue…") + '</span></td><td class="repo-cell">' + icon("repo") + escapeHTML(run.repository?.name || run.requested_repository || "Automatic discovery") + '</td><td>' + badge(run) + (run.status === "running" ? '<div class="field-help">' + escapeHTML(stageLabels[run.stage] || run.stage) + "</div>" : "") + '</td><td title="' + escapeHTML(new Date(run.created_at).toLocaleString()) + '">' + escapeHTML(timeAgo(run.created_at)) + '</td><td><button class="row-link" data-run="' + escapeHTML(run.id) + '" aria-label="View ' + escapeHTML(run.issue_key) + '">View ' + icon("arrow") + "</button></td></tr>").join("") + "</tbody></table>";
  }
  function renderRepositories(repositories: Repository[]): void {
    state.repositories = repositories;
    const eligible = repositories.filter(repo => repo.is_active);
    // Update suggestions only: background polling must never replace a pasted value.
    $("#repository-suggestions").innerHTML = eligible.map(repo =>
      '<option value="' + escapeHTML(repo.url) + '" label="' + escapeHTML(repo.name) + '"></option>' +
      '<option value="' + escapeHTML(repo.name) + '" label="' + escapeHTML(repo.url) + '"></option>'
    ).join("");
    $("#repository-cards").innerHTML = repositories.length ? repositories.map((repo) => '<article class="panel repo-card"><div class="card-top">' + icon("repo") + '<span class="badge ' + (repo.is_active ? "pr_created" : "") + '">' + (repo.is_active ? "Active" : "Inactive") + "</span></div><h3>" + escapeHTML(repo.name) + '</h3><p><a href="' + safeURL(repo.url) + '" target="_blank" rel="noopener noreferrer">' + escapeHTML(repo.url.replace("https://github.com/", "")) + ' ↗</a></p><p class="repo-description">' + escapeHTML(repo.description || "Add a description to improve repository discovery.") + '</p><div class="repo-meta"><span>⑂ ' + escapeHTML(repo.default_branch || "Default branch · automatic") + "</span><span>" + (repo.validation_commands.length ? repo.validation_commands.length + " validation " + (repo.validation_commands.length === 1 ? "command" : "commands") : "Tests · automatic detection") + '</span><span>' + escapeHTML(repo.validation_commands.length ? repo.runtime_image : "Runtime · prepared automatically") + '</span></div><div class="repo-actions">' + (state.overview?.can_manage ? '<button class="text-button" data-edit-repo="' + repo.id + '">Edit configuration ' + icon("arrow") + "</button>" : "") + "</div></article>").join("") : '<div class="panel empty-state">' + icon("repo") + "<h3>Give BugPilot a place to start</h3><p>Register your team's repositories to start investigating issues.</p></div>";
    syncSubmissionGuidance();
  }
  async function loadList() {
    const params = new URLSearchParams({ page: String(state.listPage) });
    if ($("#status-filter").value) params.set("status", $("#status-filter").value);
    if ($("#investigation-search").value.trim()) params.set("q", $("#investigation-search").value.trim());
    const data = await api<Paginated<Investigation>>("investigations/?" + params);
    renderTable(data.results, "#all-table", data.count ? "No investigations yet" : "No matching investigations");
    $("#page-info").textContent = data.count ? "Page " + state.listPage + " · " + data.count + " investigations" : "0 investigations";
    $("#previous-page").disabled = !data.previous;
    $("#next-page").disabled = !data.next;
  }
  async function refresh() {
    if (state.refreshing || document.hidden) return;
    state.refreshing = true;
    try {
      const [overview, recent, repositories] = await Promise.all([api<Overview>("overview/"), api<Paginated<Investigation>>("investigations/"), api<Repository[]>("repositories/")]);
      renderOverview(overview);
      renderRepositories(repositories);
      renderTable(recent.results.slice(0, 5), "#recent-table");
      $("#recent-count").textContent = String(recent.count);
      $("#loading-error").hidden = true;
      if (state.page === "investigations") await loadList();
      if (state.selected) await updateDrawer();
    } catch (error) { showGlobalError(error); }
    finally { state.refreshing = false; }
  }
  function closeDrawer() {
    $("#investigation-drawer").hidden = true;
    $("#drawer-backdrop").hidden = true;
    document.body.style.overflow = "";
    state.selected = null;
    state.selectedUpdated = null;
    state.opener?.focus();
  }
  async function openDrawer(id: string): Promise<void> {
    state.opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    state.selected = id;
    state.selectedUpdated = null;
    $("#drawer-content").innerHTML = '<h1 id="drawer-title">Loading investigation…</h1>';
    $("#investigation-drawer").hidden = false;
    $("#drawer-backdrop").hidden = false;
    document.body.style.overflow = "hidden";
    $("#close-drawer").focus();
    try { await updateDrawer(); }
    catch (error) { $("#drawer-content").innerHTML = '<h1 id="drawer-title">Unable to load investigation</h1><p class="form-error">' + escapeHTML(errorMessage(error)) + "</p>"; }
  }
  async function updateDrawer() {
    if (!state.selected) return;
    const id = state.selected;
    const run = await api<InvestigationDetail>("investigations/" + id + "/");
    if (state.selected !== id || state.selectedUpdated === run.updated_at) return;
    state.selectedUpdated = run.updated_at;
    const stages = state.overview?.stages || [];
    const index = run.stage === "completed" ? stages.length : stages.findIndex((item) => item.key === run.stage);
    let content = '<span class="issue-key">◆ ' + escapeHTML(run.issue_key) + '</span><h1 id="drawer-title">' + escapeHTML(run.title || "A fix starts with a first clue.") + '</h1><div class="drawer-meta">' + badge(run) + '<span class="muted" style="font-size:10px">Attempt ' + run.attempt + " · " + escapeHTML(timeAgo(run.created_at)) + "</span></div>";
    if (run.error) content += '<div class="form-error">' + escapeHTML(run.error) + '</div>';
    content += '<div class="drawer-actions">';
    if (run.pull_request_url) content += '<a class="button primary" href="' + safeURL(run.pull_request_url) + '" target="_blank" rel="noopener noreferrer">Review pull request ' + icon("out") + "</a>";
    if (run.issue_comment_url) content += '<a class="button secondary" href="' + safeURL(run.issue_comment_url) + '" target="_blank" rel="noopener noreferrer">View issue comment ' + icon("out") + "</a>";
    if (run.issue_url) content += '<a class="button secondary" href="' + safeURL(run.issue_url) + '" target="_blank" rel="noopener noreferrer">Open issue ' + icon("out") + "</a>";
    if (run.status === "needs_attention") content += '<button class="button primary" id="retry-investigation" data-id="' + escapeHTML(run.id) + '">Retry investigation ' + icon("arrow") + "</button>";
    content += "</div>";
    if (run.status === "needs_attention" && !run.pull_request_url && !["publishing", "linking_jira", "linking_issue"].includes(run.stage)) {
      content += '<label for="retry-repository">Optional: choose a repository before retrying</label><select id="retry-repository"><option value="">Keep current selection / automatic discovery</option>' + state.repositories.filter((repo) => repo.is_active).map((repo) => '<option value="' + repo.id + '">' + escapeHTML(repo.name) + "</option>").join("") + "</select>";
    }
    content += '<div class="drawer-section"><h2>Pipeline progress</h2><ol class="timeline">' + stages.map((item, i) => {
      const done = i < index || run.stage === "completed";
      const current = i === index;
      return '<li><span class="timeline-marker ' + (done ? "" : current ? "current" : "pending") + '">' + (done ? "✓" : i + 1) + '</span><div class="timeline-content"><strong>' + escapeHTML(item.label) + "</strong>" + (current ? "<small>" + (run.status === "needs_attention" ? "Paused · needs attention" : "Working on it…") + "</small>" : "") + "</div></li>";
    }).join("") + "</ol></div>";
    if (run.repository) content += '<div class="drawer-section"><h2>Affected repository</h2><p>' + icon("repo") + " " + escapeHTML(run.repository.name) + '</p><p class="field-help">' + escapeHTML(run.selection_reason) + "</p>" + (run.branch ? "<p><code>" + escapeHTML(run.branch) + "</code></p>" : "") + "</div>";
    if (run.base_branch) content += '<div class="drawer-section"><h2>Base branch</h2><p><code>' + escapeHTML(run.base_branch) + "</code></p></div>";
    if (run.publication_repository_url && run.publication_repository_url !== run.repository?.url) content += '<div class="drawer-section"><h2>Fix branch in your fork</h2><p><a href="' + safeURL(run.publication_repository_url) + '" target="_blank" rel="noopener noreferrer">' + escapeHTML(run.publication_repository_url.replace("https://github.com/", "")) + ' ↗</a></p><p><code>' + escapeHTML(run.publication_head_owner + ":" + run.branch) + '</code></p><p class="field-help">The pull request targets the affected repository’s recorded base branch.</p></div>';
    if (Object.keys(run.preprocessed).length) {
      content += '<div class="drawer-section"><h2>Structured bug context</h2>';
      for (const [key, label] of [["summary", "Summary"], ["expected_behavior", "Expected behavior"], ["actual_behavior", "Actual behavior"], ["error_details", "Error details"]] as const) {
        if (run.preprocessed[key]) content += '<div class="context-item"><span>' + label + "</span><p>" + escapeHTML(run.preprocessed[key]) + "</p></div>";
      }
      if (run.preprocessed.reproduction_steps?.length) content += '<div class="context-item"><span>Reproduction steps</span><ul>' + run.preprocessed.reproduction_steps.map((step) => "<li>" + escapeHTML(step) + "</li>").join("") + "</ul></div>";
      content += "</div>";
    }
    if (run.coding_brief) content += '<div class="drawer-section"><details><summary>Processed text passed to Codex</summary><pre>' + escapeHTML(run.coding_brief) + "</pre></details></div>";
    if (Object.keys(run.model_calls).length) content += '<div class="drawer-section"><h2>Completed model calls</h2>' + Object.values(run.model_calls).map(call => '<p>' + escapeHTML(call.provider) + " · " + escapeHTML(call.model) + "</p>").join("") + "</div>";
    if (run.proposal.summary) {
      content += '<div class="drawer-section"><h2>The proposed fix</h2><p>' + escapeHTML(run.proposal.summary) + '</p><div class="context-item" style="margin-top:16px"><span>Root cause</span><p>' + escapeHTML(run.proposal.root_cause) + "</p></div>";
      if (run.proposal.tests_added?.length) content += '<div class="context-item"><span>Agent-reported regression coverage</span><ul>' + run.proposal.tests_added.map((test) => "<li>" + escapeHTML(test) + "</li>").join("") + "</ul></div>";
      if (run.proposal.diff) content += '<details><summary>View example patch</summary><pre>' + escapeHTML(run.proposal.diff) + "</pre></details>";
      content += "</div>";
    }
    if (Object.keys(run.validation).length) {
      content += '<div class="drawer-section"><h2>' + "Validation" + '</h2><span class="badge ' + (run.validation.passed ? "pr_created" : "needs_attention") + '">' + (run.validation.passed ? "All checks passed" : "Checks did not pass") + "</span>";
      for (const check of run.validation.checks || []) content += '<details><summary>' + escapeHTML(check.command.join(" ")) + "</summary><pre>" + escapeHTML(check.output || "Completed successfully.") + "</pre></details>";
      if (run.validation.reason) content += '<pre>' + escapeHTML(run.validation.reason) + "</pre>";
      if (run.validation.changed_files?.length) content += '<p class="field-help">' + escapeHTML(run.validation.changed_files.join(" · ")) + "</p>";
      content += "</div>";
    }
    content += '<div class="drawer-section"><h2>Activity log</h2><ul class="log-list">' + run.events.map((event) => '<li><time>' + escapeHTML(new Date(event.created_at).toLocaleString()) + "</time>" + escapeHTML(event.message) + "</li>").join("") + "</ul></div>";
    $("#drawer-content").innerHTML = content;
  }
  function repositoryDialog(id?: string, prefillURL = "", prefillName = ""): void {
    const repo = state.repositories.find((item) => String(item.id) === String(id));
    $("#repository-error").hidden = true;
    $("#repo-id").value = repo ? String(repo.id) : "";
    $("#repository-dialog-title").textContent = repo ? "Edit repository" : "Add a repository";
    $("#repo-name").value = repo?.name || prefillName;
    $("#repo-url").value = repo?.url || prefillURL;
    $("#repo-branch").value = repo?.default_branch || "";
    $("#repo-image").value = repo?.validation_commands.length ? repo.runtime_image : "";
    $("#repo-description").value = repo?.description || "";
    $("#repo-commands").value = JSON.stringify(repo?.validation_commands || [], null, 2);
    $("#repo-active").checked = repo?.is_active ?? true;
    $("#repository-dialog").showModal();
  }
  function startNew() { showPage("overview"); $("#github-issue").focus(); $("#ticket-form").scrollIntoView({ behavior: "smooth", block: "center" }); }

  document.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const page = target.closest<HTMLElement>("[data-page]");
    if (page) showPage(page.dataset.page || "overview");
    const row = target.closest<HTMLElement>("[data-run]");
    if (row) void openDrawer(row.dataset.run!);
    const edit = target.closest<HTMLElement>("[data-edit-repo]");
    if (edit) repositoryDialog(edit.dataset.editRepo);
    const retry = target.closest<HTMLElement>("#retry-investigation");
    if (retry) {
      (retry as HTMLButtonElement).disabled = true;
      try {
        const repository = document.querySelector<HTMLSelectElement>("#retry-repository")?.value;
        await api("investigations/" + retry.dataset.id + "/retry/", { method: "POST", body: JSON.stringify(repository ? { repository } : {}) });
        toast("Investigation requeued. The worker will pick it up.");
        state.selectedUpdated = null;
        await refresh();
      } catch (error) { toast(errorMessage(error)); }
      finally { (retry as HTMLButtonElement).disabled = false; }
    }
  });
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if ((event.key === "Enter" || event.key === " ") && target.matches("tr[data-run]")) { event.preventDefault(); void openDrawer((target as HTMLElement).dataset.run!); }
    if (!$("#investigation-drawer").hidden) {
      if (event.key === "Escape") closeDrawer();
      if (event.key === "Tab") {
        const focusables = [...$("#investigation-drawer").querySelectorAll<HTMLElement>("button:not(:disabled), a[href], select, summary, [tabindex='0']")];
        const first = focusables[0], last = focusables.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    } else if (event.key === "Escape") $("#sidebar").classList.remove("open");
  });
  $("#close-drawer").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  $("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#new-investigation").addEventListener("click", startNew);
  $("#add-repository").addEventListener("click", () => repositoryDialog());
  for (const id of ["close-repository-dialog", "cancel-repository-dialog"]) $("#" + id).addEventListener("click", () => $("#repository-dialog").close());
  function syncRepositoryChoice(): void {
    const automatic = !$("#repository-input").value.trim();
    $("#find-repository").setAttribute("aria-pressed", String(automatic));
    $("#repository-help").textContent = automatic
      ? "Paste a full issue URL to infer its repository, or provide a repository for a short issues/123 endpoint."
      : "This repository will be used for the investigation. It must be registered in Repositories.";
    syncSubmissionGuidance();
  }
  $("#repository-input").addEventListener("input", syncRepositoryChoice);
  $("#github-issue").addEventListener("input", syncRepositoryChoice);
  $("#find-repository").addEventListener("click", () => {
    $("#repository-input").value = "";
    syncRepositoryChoice();
  });
  syncRepositoryChoice();
  $("#setup-repository").addEventListener("click", () => {
    const hint = repositoryHint();
    repositoryDialog(hint.match ? String(hint.match.id) : undefined, hint.url,
      hint.match?.name || (hint.url ? hint.url.split("/").at(-1) || "" : hint.value));
  });
  $("#refresh-setup").addEventListener("click", async () => {
    await refresh();
    syncSubmissionGuidance();
  });
  $("#ticket-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const blockers = liveSetupIssues();
    if (blockers.length) {
      $("#ticket-error").textContent = "Live investigation has not started. Complete the setup requirements below, then try again.";
      $("#ticket-error").hidden = false;
      syncSubmissionGuidance();
      $("#ticket-error").scrollIntoView({ behavior: "smooth", block: "nearest" });
      return;
    }
    state.submitting = true;
    $("#submit-ticket").disabled = true;
    $("#submit-ticket").textContent = "Starting investigation…";
    $("#ticket-error").hidden = true;
    try {
      const run = await api<InvestigationDetail>("investigations/", { method: "POST", body: JSON.stringify({ github_issue: $("#github-issue").value.trim(), repository: $("#repository-input").value.trim() }) });
      toast(run.status === "queued" ? "Issue queued. BugPilot will take it from here." : "This issue already has an investigation. Opening it.");
      $("#github-issue").value = "";
      await refresh();
      await openDrawer(run.id);
    } catch (error) { $("#ticket-error").textContent = errorMessage(error); $("#ticket-error").hidden = false; }
    finally { state.submitting = false; syncLiveSetup(); $("#submit-ticket").innerHTML = "Investigate & fix " + icon("arrow"); }
  });
  $("#repository-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("#repository-error").hidden = true;
    $("#save-repository").disabled = true;
    try {
      let commands: unknown;
      try { commands = JSON.parse($("#repo-commands").value.trim() || "[]"); } catch { throw new Error("Validation commands must be valid JSON argument arrays."); }
      const id = $("#repo-id").value;
      await api("repositories/" + (id ? id + "/" : ""), { method: id ? "PATCH" : "POST", body: JSON.stringify({
        name: $("#repo-name").value.trim(), url: $("#repo-url").value.trim(),
        description: $("#repo-description").value.trim(), runtime_image: $("#repo-image").value.trim(),
        validation_commands: commands, is_active: $("#repo-active").checked,
      }) });
      $("#repository-dialog").close();
      toast("Repository configuration saved.");
      await refresh();
    } catch (error) { $("#repository-error").textContent = errorMessage(error); $("#repository-error").hidden = false; }
    finally { $("#save-repository").disabled = false; }
  });
  let searchTimer: ReturnType<typeof setTimeout> | undefined;
  $("#investigation-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.listPage = 1; loadList().catch(showGlobalError); }, 300); });
  $("#status-filter").addEventListener("change", () => { state.listPage = 1; loadList().catch(showGlobalError); });
  $("#previous-page").addEventListener("click", () => { state.listPage = Math.max(1, state.listPage - 1); loadList().catch(showGlobalError); });
  $("#next-page").addEventListener("click", () => { state.listPage += 1; loadList().catch(showGlobalError); });
  window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  $("#today").textContent = new Date().toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  showPage(location.hash.slice(1) || "overview");
  refresh();
  setInterval(() => { void refresh(); }, 4000);

  $(".user-avatar").textContent = session.username.slice(0, 1).toUpperCase();
  $(".user strong").textContent = session.username;
  $(".user small").textContent = session.can_manage ? "Workspace admin" : "Developer";
  syncLiveSetup();
  $("#sign-out").addEventListener("click", async () => {
    const button = $("#sign-out");
    button.disabled = true;
    try { await api<Session>("auth/logout/", { method: "POST" }); window.location.reload(); }
    catch (error) { toast(errorMessage(error)); button.disabled = false; }
  });

}
