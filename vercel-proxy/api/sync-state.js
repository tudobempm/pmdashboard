// Vercel serverless function — GitHub state write-proxy for the pmdashboard.
//
// Holds the GitHub token server-side so no token ever lives in the browser.
// The dashboard POSTs a state patch; this function merges it into
// briefing_state.json on GitHub and commits it.
//
// Required env var (set in Vercel → Project → Settings → Environment Variables):
//   GITHUB_TOKEN   fine-grained PAT with "Contents: Read and write" on the repo
// Optional env var:
//   ALLOWED_ORIGIN the dashboard origin allowed to call this function
//                  (defaults to https://tudobempm.github.io)

const REPO = "tudobempm/pmdashboard";
const BRANCH = "main";
const FILE = "briefing_state.json";
const DEFAULT_ORIGIN = "https://tudobempm.github.io";
const REQUIRED_KEYS = ["completed", "hidden", "reactions", "edits", "reminders", "reminderHistory"];

export default async function handler(req, res) {
  const allowedOrigin = process.env.ALLOWED_ORIGIN || DEFAULT_ORIGIN;
  res.setHeader("Access-Control-Allow-Origin", allowedOrigin);
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  res.setHeader("Vary", "Origin");

  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });

  // Only accept calls from the dashboard origin.
  const origin = req.headers.origin || "";
  if (origin !== allowedOrigin) return res.status(403).json({ error: "forbidden origin" });

  const token = process.env.GITHUB_TOKEN;
  if (!token) return res.status(500).json({ error: "server not configured: missing GITHUB_TOKEN" });

  // Vercel auto-parses JSON bodies; tolerate a raw string just in case.
  let patch = req.body;
  if (typeof patch === "string") {
    try { patch = JSON.parse(patch); } catch { return res.status(400).json({ error: "invalid JSON body" }); }
  }
  if (!patch || typeof patch !== "object" || Array.isArray(patch)) {
    return res.status(400).json({ error: "body must be a JSON object" });
  }

  const gh = (path, init = {}) => fetch(`https://api.github.com${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "pmdashboard-sync",
      ...(init.headers || {}),
    },
  });

  try {
    // Read current file, merge, commit. Retry once on a sha conflict
    // (two writes racing) by re-fetching the latest sha.
    for (let attempt = 0; attempt < 2; attempt++) {
      const getRes = await gh(`/repos/${REPO}/contents/${FILE}?ref=${BRANCH}`);
      if (!getRes.ok) return res.status(502).json({ error: `GitHub GET failed: ${getRes.status}` });
      const file = await getRes.json();

      let current = {};
      try { current = JSON.parse(Buffer.from(file.content, "base64").toString("utf8")); } catch { current = {}; }

      const updated = { ...current, ...patch, lastModified: Date.now() };
      // Guarantee the keys the publish workflow and dashboard expect.
      for (const k of REQUIRED_KEYS) {
        if (!(k in updated)) {
          updated[k] = (k === "reminders" || k === "reminderHistory" || k === "hidden") ? [] : {};
        }
      }

      const body = JSON.stringify(updated, null, 2) + "\n";
      const putRes = await gh(`/repos/${REPO}/contents/${FILE}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: "chore: sync dashboard state",
          content: Buffer.from(body, "utf8").toString("base64"),
          sha: file.sha,
          branch: BRANCH,
        }),
      });

      if (putRes.ok) return res.status(200).json({ ok: true, keys: Object.keys(patch) });
      if ((putRes.status === 409 || putRes.status === 422) && attempt === 0) continue; // stale sha, retry
      const detail = await putRes.text().catch(() => "");
      return res.status(502).json({ error: `GitHub PUT failed: ${putRes.status}`, detail: detail.slice(0, 200) });
    }
    return res.status(409).json({ error: "sha conflict after retry" });
  } catch (e) {
    return res.status(500).json({ error: String((e && e.message) || e) });
  }
}
