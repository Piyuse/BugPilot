# BugPilot frontend

Standalone Vite + strict TypeScript application. Django supplies JSON APIs and session authentication; all application markup, styles, and browser logic live here.

## Development

Use Node.js 22.12+ (or Node 24). Start Django on port 8000 and the investigation worker separately, then:

```sh
npm ci
npm run dev
```

Open http://127.0.0.1:5173. The local demo login is `demo / bugpilot-demo` after running Django's `seed_demo` command. Select **Demo** explicitly to use fixtures. **Live** calls Gemini and Codex, then creates a GitHub PR and posts the URL as a GitHub issue comment; missing credentials are shown in the form.

Vite forwards `/api/` to Django, preserving the browser Host header for same-origin session cookies and CSRF verification. If Django uses another port, copy `.env.example` to `.env` and set `BACKEND_URL`. Restart Vite after changing it. No provider keys belong in frontend environment variables.

## Commands

- `npm run dev`: development server on port 5173.
- `npm run typecheck`: strict TypeScript verification.
- `npm run build`: verify types and generate `dist/`.
- `npm run preview`: inspect the built application on port 4173, with the same local API proxy. For local verification only.

## Source

- `src/main.ts`: application startup and sign-in.
- `src/dashboard.ts`: navigation, intake, investigation history/details, repository configuration, polling, logout.
- `src/api.ts`: typed JSON requests, CSRF headers, session-expiry handling.
- `src/types.ts`: API data contracts.
- `src/dom.ts`: typed DOM selectors and error handling.
- `src/views/`: frontend-owned HTML layouts; no server template syntax.
- `src/styles.css`: shared responsive application styling.

## Production

Run `npm ci && npm run build`. Serve `dist/` with your web server and forward `/api/` to Django on the same public origin. Preserve the Host header and forward the original HTTPS scheme through a trusted proxy. Do not expose Vite's development/preview server as production hosting.

The root README describes backend and provider setup. Backend Django templates are only used by Django's built-in admin; the application frontend builds and runs independently.

## Repository input

Paste a registered GitHub repository URL or type its name in **Affected repository**. Suggestions are provided from the registry. Click **Find repository automatically** to clear the hint and let BugPilot select an affected repository after you submit the ticket. Pasted text is preserved while the dashboard refreshes.

## GitHub issue intake

Paste a browser URL such as `https://github.com/owner/repo/issues/123` or an API endpoint such as `https://api.github.com/repos/owner/repo/issues/123`. A short `issues/123` path requires a registered repository URL or name in Affected repository. Automatic discovery requires a full issue URL so the issue can be fetched. The demo sample button supplies a complete fixture URL. Jira credentials are not needed for GitHub issue runs.

## Live setup feedback

Investigate & fix remains clickable unless a submission is in progress. If credentials, repository registration or validation commands are missing, clicking it explains the blockers without starting a model run. The form links directly to integration setup and a prefilled repository registration dialog. Provider credentials remain required and are checked again by the API. New repository quick-setup requires the actual branch and prebuilt runtime image rather than assuming a Python project.
