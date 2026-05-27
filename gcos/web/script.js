// GCOS dashboard — SSE-driven, push-based (M5).
// Falls back to polling if EventSource isn't available or the stream errors.

const TERMINAL = new Set(["DONE", "TIMEOUT", "ERROR", "ZOMBIE"]);
const POLL_FALLBACK_MS = 1500;

function el(id) { return document.getElementById(id); }

async function api(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status} ${detail}`);
  }
  return r.json();
}

function renderStatus(s) {
  el("stat-scheduler").textContent  = s.scheduler;
  el("stat-workers").textContent    = s.workers;
  el("stat-busy").textContent       = s.busy;
  el("stat-queue").textContent      = s.queue_len;
  el("stat-total").textContent      = s.total_agents;
  el("stat-quota").textContent      = s.quota.remaining;
  el("stat-quota-total").textContent = s.quota.total;

  const b = s.batcher;
  const bEl = el("stat-batcher");
  if (b && bEl) {
    bEl.textContent =
      `${b.in_flight}/${s.workers ?? '?'} inflight, ` +
      `peak ${b.peak_in_flight}, ` +
      `${b.total_calls} calls, ` +
      `avg wait ${b.avg_wait_ms}ms`;
  } else if (bEl) {
    bEl.textContent = "—";
  }
}

function renderAgents(agents) {
  const body = el("agents-body");
  body.innerHTML = "";
  for (const a of agents) {
    const out = a.error ? `[err] ${a.error}` : (a.result || "");
    const truncatedOut = out.length > 300 ? out.slice(0, 300) + "…" : out;
    const killDisabled = TERMINAL.has(a.state);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${a.pid}</td>
      <td>${escapeHTML(a.name)}</td>
      <td><span class="state state-${a.state}">${a.state}</span></td>
      <td>${a.prio}</td>
      <td>${a.parent ?? ""}</td>
      <td>${a.quota}</td>
      <td>${a.calls}</td>
      <td>${a.tokens}</td>
      <td>${a.wall.toFixed(2)}s</td>
      <td class="result">${escapeHTML(truncatedOut)}</td>
      <td>
        <button class="kill-btn" data-pid="${a.pid}" ${killDisabled ? "disabled" : ""}>
          kill
        </button>
      </td>
    `;
    body.appendChild(tr);
  }
}

function escapeHTML(s) {
  if (s == null) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function applySnapshot(snap) {
  renderStatus(snap.status);
  renderAgents(snap.agents || []);
}

async function fallbackPoll() {
  try {
    const [status, agents] = await Promise.all([
      api("/kernel/status"),
      api("/agents"),
    ]);
    applySnapshot({ status, agents: agents.agents });
  } catch (e) {
    console.error("poll failed", e);
  }
}

let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(fallbackPoll, POLL_FALLBACK_MS);
  fallbackPoll();
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function startSSE() {
  if (!("EventSource" in window)) { startPolling(); return; }
  const es = new EventSource("/api/events");
  let alive = false;
  es.addEventListener("snapshot", (ev) => {
    alive = true;
    stopPolling();
    try {
      applySnapshot(JSON.parse(ev.data));
    } catch (e) {
      console.error("bad SSE payload", e);
    }
  });
  es.addEventListener("error", () => {
    if (!alive) {
      console.warn("SSE failed, falling back to polling");
      es.close();
      startPolling();
    }
  });
}

// Form handlers (unchanged from M2)
document.getElementById("spawn-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    prompt:   el("prompt").value.trim(),
    name:     el("name").value.trim() || "anon",
    priority: parseInt(el("priority").value, 10),
    quota:    parseInt(el("quota").value, 10),
  };
  if (!payload.prompt) return;
  try {
    await api("/spawn", { method: "POST", body: JSON.stringify(payload) });
    el("prompt").value = "";
  } catch (err) {
    alert("spawn failed: " + err.message);
  }
});

document.getElementById("agents-body").addEventListener("click", async (e) => {
  const btn = e.target.closest(".kill-btn");
  if (!btn || btn.disabled) return;
  try { await api(`/agents/${btn.dataset.pid}`, { method: "DELETE" }); }
  catch (err) { alert("kill failed: " + err.message); }
});

document.getElementById("topup-btn").addEventListener("click", async () => {
  try { await api("/kernel/quota/topup?amount=25", { method: "POST" }); }
  catch (err) { alert("topup failed: " + err.message); }
});

startSSE();
