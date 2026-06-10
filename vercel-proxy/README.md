# Dashboard state write-proxy (Vercel)

A tiny serverless function that commits the dashboard's state to
`briefing_state.json` on GitHub. It holds the GitHub token **server-side**, so
no token ever lives in the browser — the dashboard just `POST`s its state here.

```
Dashboard (GitHub Pages)  --POST state-->  Vercel function  --commit-->  briefing_state.json (GitHub)
```

Endpoint: `POST /api/sync-state` — body is the state patch JSON
(`{ completed, hidden, reactions, edits, reminders, reminderHistory }`).

## Deploy (one time, ~5 minutes)

1. Create a free account at **vercel.com** (sign in with GitHub).
2. **Add New… → Project**, and import the `tudobempm/pmdashboard` repo.
3. In the project setup, set **Root Directory** to `vercel-proxy`.
   (This deploys only the function, not the dashboard — the dashboard stays on
   GitHub Pages.)
4. Before deploying, add an **Environment Variable**:
   - **Name:** `GITHUB_TOKEN`
   - **Value:** a fine-grained Personal Access Token with **Contents: Read and
     write** on `tudobempm/pmdashboard` (github.com → Settings → Developer
     settings → Fine-grained tokens).
   - (Optional) `ALLOWED_ORIGIN` = `https://tudobempm.github.io` (this is the
     default if you omit it).
5. **Deploy.** Your endpoint will be:
   `https://<your-project>.vercel.app/api/sync-state`

## Wire the dashboard to it

Two ways:

- **Quick test (just your browser):** in the dashboard's DevTools console run
  `localStorage.setItem("llf-sync-url", "https://<your-project>.vercel.app/api/sync-state")`,
  then check off an item and look for `[github] state synced via proxy` in the
  console.
- **Permanent (everyone, every browser):** set `SYNC_PROXY_URL` in `index.html`
  to that endpoint and deploy the dashboard. After that, no token or per-browser
  setup is ever needed again.

## Security notes

- The token lives only in Vercel's encrypted env vars — never in the page.
- The function only ever writes `briefing_state.json` and is restricted to the
  dashboard origin via CORS. The file is versioned in git, so any bad write is
  recoverable.
- Rotate the `GITHUB_TOKEN` periodically and keep it fine-grained (Contents only,
  this repo only). For stronger access control later, put the function behind
  Vercel's authentication / a deployment protection layer.
