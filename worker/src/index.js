/**
 * Cloudflare Worker: public API for the "request a workshop map" feature.
 * See ~/.claude/plans/federated-hugging-puddle.md for the full design.
 *
 * Thin orchestrator over Workers KV — never touches Steam, DepotDownloader,
 * R2, or git. The local daemon (scripts/map_request_daemon.py) polls
 * /next-job and reports back via /job-result; it never receives inbound
 * connections.
 */

const JOB_TTL_SECONDS = 7 * 24 * 60 * 60;       // 7 days
const JOB_DONE_TTL_SECONDS = 90 * 24 * 60 * 60; // 90 days once successful
const RATE_LIMIT_BUCKET_TTL = 3600;             // 1 hour
const MAPS_JSON_URL =
  "https://raw.githubusercontent.com/xplodingeggo/RLWorkshopCollection/main/maps.json";

function cors(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
  };
}

function json(data, env, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...cors(env) },
  });
}

function parseWorkshopId(input) {
  if (!input) return null;
  const trimmed = String(input).trim();
  const m = trimmed.match(/[?&]id=(\d+)/);
  if (m) return m[1];
  if (/^\d+$/.test(trimmed)) return trimmed;
  return null;
}

async function verifyTurnstile(token, ip, env) {
  if (!token) return false;
  const body = new FormData();
  body.append("secret", env.TURNSTILE_SECRET);
  body.append("response", token);
  if (ip) body.append("remoteip", ip);
  const r = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
    method: "POST",
    body,
  });
  const outcome = await r.json();
  return outcome.success === true;
}

async function hashIp(ip) {
  const data = new TextEncoder().encode(ip);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

function requireDaemonAuth(request, env) {
  const auth = request.headers.get("Authorization") || "";
  return auth === `Bearer ${env.DAEMON_SECRET}`;
}

async function isAlreadyInCatalog(wid) {
  // Catches maps added before this feature existed (never tracked in KV)
  // or added manually since — stops a wasted download before it's even
  // queued, on top of the daemon's own belt-and-braces maps.json check.
  try {
    const r = await fetch(MAPS_JSON_URL, { cf: { cacheTtl: 60 } });
    if (!r.ok) return null; // fetch failed — don't block submission over it
    const maps = await r.json();
    return maps.find((m) => (m.steamUrl || "").includes(`id=${wid}`)) || false;
  } catch {
    return null; // network hiccup — fail open, daemon still catches real dupes
  }
}

async function handleSubmit(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid_json" }, env, 400);
  }

  const wid = parseWorkshopId(body.input);
  if (!wid) {
    return json({ error: "invalid_workshop_id" }, env, 400);
  }

  // Check the live catalog before spending a Turnstile round-trip or
  // touching the queue at all — covers maps added before this feature
  // existed, or manually since, that never got a KV `submitted:` entry.
  const catalogHit = await isAlreadyInCatalog(wid);
  if (catalogHit) {
    return json({ status: "duplicate", entry: catalogHit }, env);
  }

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";

  const turnstileOk = await verifyTurnstile(body.turnstileToken, ip, env);
  if (!turnstileOk) {
    return json({ error: "turnstile_failed" }, env, 403);
  }

  // Coarse hour-bucketed per-IP rate limit: read first (free), only write
  // if under threshold, so already-blocked repeat abuse costs no writes.
  const ipHash = await hashIp(ip);
  const hourBucket = new Date().toISOString().slice(0, 13).replace(/[-T:]/g, "");
  const rlKey = `rl:${ipHash}:${hourBucket}`;
  const limit = parseInt(env.RATE_LIMIT_PER_HOUR || "5", 10);
  const current = parseInt((await env.MAPREQUESTS.get(rlKey)) || "0", 10);
  if (current >= limit) {
    return json({ error: "rate_limited" }, env, 429);
  }
  await env.MAPREQUESTS.put(rlKey, String(current + 1), { expirationTtl: RATE_LIMIT_BUCKET_TTL });

  // De-dupe: if this workshop id already has a live/duplicate job, hand
  // back the existing job instead of creating a new one.
  const existingJobId = await env.MAPREQUESTS.get(`submitted:${wid}`);
  if (existingJobId) {
    const existingJobRaw = await env.MAPREQUESTS.get(`job:${existingJobId}`);
    if (existingJobRaw) {
      const existingJob = JSON.parse(existingJobRaw);
      if (["queued", "downloading", "uploading", "done"].includes(existingJob.status)) {
        return json({ jobId: existingJobId, status: existingJob.status }, env);
      }
    }
  }

  const jobId = crypto.randomUUID();
  const now = Date.now();
  const job = { wid, status: "queued", createdAt: now, updatedAt: now, ip_hash: ipHash };
  await env.MAPREQUESTS.put(`job:${jobId}`, JSON.stringify(job), { expirationTtl: JOB_TTL_SECONDS });
  await env.MAPREQUESTS.put(`submitted:${wid}`, jobId, { expirationTtl: JOB_TTL_SECONDS });

  const indexRaw = await env.MAPREQUESTS.get("queue:index");
  const index = indexRaw ? JSON.parse(indexRaw) : [];
  index.push(jobId);
  await env.MAPREQUESTS.put("queue:index", JSON.stringify(index));

  return json({ jobId, status: "queued" }, env);
}

async function handleStatus(jobId, env) {
  const raw = await env.MAPREQUESTS.get(`job:${jobId}`);
  if (!raw) return json({ error: "not_found" }, env, 404);
  return json(JSON.parse(raw), env);
}

async function handleNextJob(request, env) {
  if (!requireDaemonAuth(request, env)) return json({ error: "unauthorized" }, env, 401);

  await env.MAPREQUESTS.put("daemon:last_seen", String(Date.now()));

  const indexRaw = await env.MAPREQUESTS.get("queue:index");
  let index = indexRaw ? JSON.parse(indexRaw) : [];

  while (index.length > 0) {
    const jobId = index.shift();
    const jobRaw = await env.MAPREQUESTS.get(`job:${jobId}`);
    if (!jobRaw) continue; // job expired/missing, skip (self-healing)
    const job = JSON.parse(jobRaw);
    if (job.status !== "queued") continue; // stale index entry, skip

    await env.MAPREQUESTS.put("queue:index", JSON.stringify(index));
    job.status = "downloading";
    job.updatedAt = Date.now();
    await env.MAPREQUESTS.put(`job:${jobId}`, JSON.stringify(job), { expirationTtl: JOB_TTL_SECONDS });

    return json({ jobId, wid: job.wid }, env);
  }

  await env.MAPREQUESTS.put("queue:index", JSON.stringify(index));

  // Reconciliation fallback: queue:index writes aren't atomic across
  // concurrent /submit calls, so occasionally list() for any queued job
  // the index might have dropped.
  const list = await env.MAPREQUESTS.list({ prefix: "job:" });
  for (const key of list.keys) {
    const jobRaw = await env.MAPREQUESTS.get(key.name);
    if (!jobRaw) continue;
    const job = JSON.parse(jobRaw);
    if (job.status === "queued") {
      const jobId = key.name.slice("job:".length);
      job.status = "downloading";
      job.updatedAt = Date.now();
      await env.MAPREQUESTS.put(key.name, JSON.stringify(job), { expirationTtl: JOB_TTL_SECONDS });
      return json({ jobId, wid: job.wid }, env);
    }
  }

  return new Response(null, { status: 204, headers: cors(env) });
}

async function handleJobs(env) {
  // Public, persistent activity list — reflects Worker/KV state, not
  // anything held in a visitor's browser, so it survives tab close/refresh
  // and shows the same thing to every visitor.
  const list = await env.MAPREQUESTS.list({ prefix: "job:" });
  const jobs = [];
  for (const key of list.keys) {
    const raw = await env.MAPREQUESTS.get(key.name);
    if (!raw) continue;
    const job = JSON.parse(raw);
    jobs.push({
      jobId: key.name.slice("job:".length),
      wid: job.wid,
      title: job.title || null,
      status: job.status,
      message: job.message || null,
      createdAt: job.createdAt,
      updatedAt: job.updatedAt,
    });
  }
  jobs.sort((a, b) => b.updatedAt - a.updatedAt);

  const active = jobs.filter((j) => ["queued", "downloading", "uploading"].includes(j.status));
  const recent = jobs
    .filter((j) => ["done", "failed", "not_rl", "not_found"].includes(j.status))
    .slice(0, 8);

  return json({ active, recent }, env);
}

async function handleJobResult(request, env) {
  if (!requireDaemonAuth(request, env)) return json({ error: "unauthorized" }, env, 401);

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid_json" }, env, 400);
  }

  const { jobId, status, message, entry, title } = body;
  if (!jobId || !status) return json({ error: "missing_fields" }, env, 400);

  const raw = await env.MAPREQUESTS.get(`job:${jobId}`);
  if (!raw) return json({ error: "job_not_found" }, env, 404);
  const job = JSON.parse(raw);

  job.status = status;
  job.updatedAt = Date.now();
  if (message) job.message = message;
  if (entry) job.entry = entry;
  if (title) job.title = title; // once known, kept even if a later update omits it

  const ttl = status === "done" ? JOB_DONE_TTL_SECONDS : JOB_TTL_SECONDS;
  await env.MAPREQUESTS.put(`job:${jobId}`, JSON.stringify(job), { expirationTtl: ttl });

  if (status === "done" && job.wid) {
    await env.MAPREQUESTS.put(`submitted:${job.wid}`, jobId, { expirationTtl: ttl });
  }

  return json({ ok: true }, env);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors(env) });
    }

    if (request.method === "POST" && url.pathname === "/submit") {
      return handleSubmit(request, env);
    }
    if (request.method === "GET" && url.pathname.startsWith("/status/")) {
      return handleStatus(url.pathname.slice("/status/".length), env);
    }
    if (request.method === "GET" && url.pathname === "/jobs") {
      return handleJobs(env);
    }
    if (request.method === "GET" && url.pathname === "/next-job") {
      return handleNextJob(request, env);
    }
    if (request.method === "POST" && url.pathname === "/job-result") {
      return handleJobResult(request, env);
    }

    return json({ error: "not_found" }, env, 404);
  },
};
