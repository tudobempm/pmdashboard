const REPO = process.env.GITHUB_REPO || "tudobempm/pmdashboard";
const BRANCH = process.env.GITHUB_BRANCH || "main";
const REQUESTS_PATH = process.env.PLANNER_REQUESTS_PATH || "planner_task_requests.json";
const SUPABASE_URL = process.env.SUPABASE_URL || "https://niqzkombzncxxihhulqq.supabase.co";
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJuaXF6a29tYnpuY3h4aWhodWxxcSIsInJlZiI6Im5pcXprb21iem5jeHhpaGh1bHFxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM2OTI5NTgsImV4cCI6MjA4OTI2ODk1OH0.9DMbwxPi4yBkXG034sakwh6tnxt-AUcKkt_MER71qcg";

function json(res, status, body) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
}

async function verifyUser(req) {
  const auth = req.headers.authorization || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token) return null;
  const response = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
    headers: { apikey: SUPABASE_ANON_KEY, Authorization: `Bearer ${token}` },
  });
  if (!response.ok) return null;
  return response.json();
}

async function github(path, options = {}) {
  const token = process.env.GITHUB_TOKEN;
  if (!token) throw new Error("Missing GITHUB_TOKEN server environment variable.");
  const response = await fetch(`https://api.github.com/repos/${REPO}/${path}`, {
    ...options,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`GitHub ${response.status}: ${text.slice(0, 240)}`);
  }
  return response.json();
}

async function readQueue() {
  const file = await github(`contents/${REQUESTS_PATH}?ref=${BRANCH}`);
  const jsonText = Buffer.from(file.content.replace(/\n/g, ""), "base64").toString("utf8");
  return { file, data: JSON.parse(jsonText) };
}

async function writeQueue(file, data) {
  const content = Buffer.from(JSON.stringify(data, null, 2) + "\n", "utf8").toString("base64");
  return github(`contents/${REQUESTS_PATH}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: "chore: queue Planner task request",
      content,
      sha: file.sha,
      branch: BRANCH,
    }),
  });
}

module.exports = async function handler(req, res) {
  if (req.method !== "POST" && req.method !== "GET") {
    res.setHeader("Allow", "GET, POST");
    return json(res, 405, { error: "Method not allowed" });
  }
  try {
    if (req.method === "GET") {
      const { data } = await readQueue();
      return json(res, 200, data);
    }
    const user = await verifyUser(req);
    if (!user) return json(res, 401, { error: "Unauthorized" });
    const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : (req.body || {});
    const action = body.action;
    const payload = body.payload || {};
    if (!["create", "delete", "complete"].includes(action)) return json(res, 400, { error: "Unsupported action" });
    if (action === "create" && !payload.title) return json(res, 400, { error: "Missing task title" });
    if (action === "delete" && !payload.task_id) return json(res, 400, { error: "Missing task_id" });
    if (action === "complete" && !payload.task_id) return json(res, 400, { error: "Missing task_id" });
    const { file, data } = await readQueue();
    const request = {
      id: `ptr-${Date.now()}`,
      action,
      payload,
      status: "queued",
      requested_by: { id: user.id, email: user.email },
      requested_at: new Date().toISOString(),
    };
    const updated = {
      schema_version: data.schema_version || 1,
      updated_at: new Date().toISOString(),
      requests: [...(data.requests || []), request],
    };
    await writeQueue(file, updated);
    return json(res, 200, { ok: true, request });
  } catch (error) {
    return json(res, 500, { error: error.message || "Planner request failed" });
  }
};
