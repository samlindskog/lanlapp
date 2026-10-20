// Shared by the board (index.html + app.js) and the Scans page (scans.html + scans.js): small
// formatters, the event-stream client, the run line + run panel, and the scan dialog. Everything
// hangs off window.JB; a page mounts what it uses:
//   const runs = JB.mountRuns({ onFinished })      run line into #runline, run panel onto <body>
//   const dlg  = JB.scanDialog.mount({ onBoards })  the dialog onto <body>; dlg.open(prefill)
//   JB.connectEvents(); JB.onEvent("run", fn)       the one EventSource, fanned out
window.JB = (() => {
  const $ = (s) => document.querySelector(s);
  // ISO timestamp -> browser-local "YYYY-MM-DD HH:MM". Naive strings (no zone) are treated by
  // Date() as local time, which matches rows written by the CLI on this machine; zone-aware
  // strings from the server convert correctly.
  const fmtLocal = (iso, dateOnly = false) => {
    if (!iso) return "";
    const d = new Date(iso); if (isNaN(d)) return iso;
    const p = (n) => String(n).padStart(2, "0");
    const date = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    return dateOnly ? date : `${date} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  // Relative age for lists: "3h ago", "2d ago"; the exact time goes in the tooltip.
  const fmtAgo = (iso) => {
    if (!iso) return "—";
    const ms = Date.now() - new Date(iso).getTime(); if (isNaN(ms)) return "";
    const m = Math.round(ms / 6e4), h = Math.round(ms / 36e5), d = Math.round(ms / 864e5);
    return m < 1 ? "just now" : m < 60 ? `${m}m ago` : h < 48 ? `${h}h ago` : `${d}d ago`;
  };
  const fmtElapsed = (a, b) => {
    const s = Math.max(0, Math.round((new Date(b || Date.now()) - new Date(a)) / 1000)); if (isNaN(s)) return "";
    const p = (n) => String(n).padStart(2, "0");
    return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m${p(s % 60)}s` : `${Math.floor(s / 3600)}h${p(Math.floor(s % 3600 / 60))}m`;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // JSON columns (args, request, summary) arrive as objects from /api/runs and as strings from /api/runs/<id>.
  const parseJSON = (v, dflt) => { if (v == null) return dflt; if (typeof v !== "string") return v; try { return JSON.parse(v); } catch { return dflt; } };

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }
  // GET JSON; an expired session sends the page to the login form (and comes back here).
  async function get(url) {
    const r = await fetch(url);
    if (r.status === 401) { location.href = "/login.html?next=" + encodeURIComponent(location.pathname + location.search); return new Promise(() => {}); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }

  // ---------------------------------------------------------------- the event stream
  // The server streams events (/api/events/stream): its own log lines and every update of a
  // background run (a log line, a progress change, a status change). One EventSource per page;
  // listeners by kind ("run", "log"), plus "reopen" after a reconnect (Last-Event-ID resumes,
  // but a follower may want to resync).
  const listeners = { run: [], log: [], reopen: [] };
  const onEvent = (kind, fn) => { (listeners[kind] = listeners[kind] || []).push(fn); };
  let es = null;
  function connectEvents() {
    if (es) return es;
    es = new EventSource("/api/events/stream");
    es.onmessage = (e) => {
      let ev; try { ev = JSON.parse(e.data); } catch { return; }
      (listeners[ev.kind] || []).forEach((fn) => fn(ev));
    };
    let first = true;
    es.onopen = () => { if (!first) listeners.reopen.forEach((fn) => fn()); first = false; };
    return es;
  }

  // ---------------------------------------------------------------- runs: line + panel
  // How far along a run is, 0..1, or null when it cannot be known (listing a board, no totals yet).
  function fraction(r) {
    const p = r.progress || {};
    if (r.status !== "running") return r.status === "done" ? 1 : null;
    if (r.kind === "scan" && p.boards_total) {
      const finished = (p.boards_done || 0) + (p.boards_failed || 0);
      const within = p.phase === "listing" ? 0 : p.cards ? Math.min(1, (p.job_index || 0) / p.cards) : p.phase === "embedding" ? 1 : 0;
      return Math.min(1, (finished + (finished < p.boards_total ? within : 0)) / p.boards_total);
    }
    if (r.kind === "refresh" && p.total) return Math.min(1, (p.job_index || 0) / p.total);
    return null;
  }
  // "scan · Northrop Grumman 3/7 · 12/40": what the one-liner puts in front of the latest log line.
  function headline(r) {
    const p = r.progress || {};
    if (r.kind === "scan") {
      const where = p.board ? `${p.board}${p.boards_total ? ` ${p.board_index}/${p.boards_total}` : ""}` : "";
      const jobs = p.phase === "listing" ? "listing" : p.phase === "embedding" ? "embedding" : p.cards ? `${p.job_index || 0}/${p.cards}` : "";
      return ["scan", where, jobs].filter(Boolean).join(" · ");
    }
    if (r.kind === "refresh") return `refresh ${p.job_index || 0}/${p.total || "?"}`;
    return r.kind;
  }
  // One line describing a run's parameters (the panel's args line, the Scans page's column).
  function describeArgs(r, a) {
    a = a || parseJSON(r.args, {}) || {};
    if (r.kind === "scan") return `${(a.names || a.boards || []).length} board(s): ${(a.names || []).join(", ")} · ${a.all ? "whole board" : `last ${a.days} days`}${a.refetch ? " · re-fetching stored postings" : ""}`;
    if (r.kind === "refresh") return `${(a.ids || []).length} posting(s)`;
    return Object.entries(a).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.length + " item(s)" : typeof v === "object" && v ? JSON.stringify(v) : v}`).join(" · ");
  }
  const RUN_LINE = `<span id="runBar" class="bar" hidden><i></i></span>
    <span id="run" class="runstat" title="Click for the run's details and log"></span>
    <button id="runCancel" class="x" title="Cancel this run" hidden>✕</button>`;
  const RUN_PANEL = `<aside id="runPanel" hidden>
    <div class="rp-head">
      <select id="rpPick" title="Recent runs"></select>
      <span id="rpStatus" class="rp-status"></span>
      <span id="rpElapsed" class="muted"></span>
      <span style="flex:1"></span>
      <button id="rpCancel" hidden>Cancel run</button>
      <button id="rpClose" title="Close">✕</button>
    </div>
    <div id="rpArgs" class="muted"></div>
    <div id="rpStats" class="rp-grid"></div>
    <div id="rpBar" class="bar wide" hidden><i></i></div>
    <pre id="rpLog" class="rp-log"></pre>
  </aside>`;

  // The one-liner in the header shows the latest of whatever is happening; while a run is active
  // it owns the line (headline + progress bar + cancel), and clicking it opens the run panel: the
  // board and posting in flight, the counters, and the full log as it streams in. Local UI
  // actions (verdicts, bulk status, single refresh) still report through setRun.
  //   runState.run  - the run the stream follows (the active one, or the last one seen)
  //   runState.view - a run picked in the panel's dropdown to read instead; null = same as run
  // onFinished(run) is called when the followed run leaves 'running' (the board reloads its list).
  let runs = null;
  function mountRuns({ onFinished } = {}) {
    if (runs) return runs;
    const line = $("#runline"); if (line && !line.children.length) line.innerHTML = RUN_LINE;
    if (!$("#runPanel")) document.body.insertAdjacentHTML("beforeend", RUN_PANEL);
    const runState = { run: null, view: null, open: false, follow: true, pending: null, queue: [], recent: [] };
    let clearTimer = null;
    const FADES = new Set(["done", "info", "warn"]);
    const setRun = (status, text) => {
      const el = $("#run"); clearTimeout(clearTimer);
      el.className = "runstat " + status; el.textContent = text;
      if (FADES.has(status)) clearTimer = setTimeout(() => {
        el.classList.add("fade");
        clearTimer = setTimeout(() => { if (isRunning()) renderRunLine(); else { el.textContent = ""; el.className = "runstat"; } }, 600);
      }, 2500);
    };
    const isRunning = () => !!(runState.run && runState.run.status === "running");
    function renderRunLine() {
      const r = runState.run, bar = $("#runBar"), x = $("#runCancel");
      if (!r) { bar.hidden = true; x.hidden = true; return; }
      const running = r.status === "running", f = fraction(r);
      x.hidden = !running; x.disabled = !!r.cancel_requested; x.title = r.cancel_requested ? "cancelling…" : "Cancel this run";
      bar.hidden = !running; bar.classList.toggle("indeterminate", running && f == null);
      bar.firstElementChild.style.width = f == null ? "0%" : `${Math.round(f * 100)}%`;
      const last = (r.lines.length ? r.lines[r.lines.length - 1] : "").trim();
      if (running) setRun("running", `${r.cancel_requested ? "cancelling · " : ""}${headline(r)}: ${last}`);
      else setRun(r.status, `${r.kind} ${r.status}: ${last}`);
    }
    function renderPanel() {
      const panel = $("#runPanel"); panel.hidden = !runState.open; if (!runState.open) return;
      const r = runState.view || runState.run, pick = $("#rpPick");
      const opts = runState.recent.slice();
      if (r && !opts.some((x) => String(x.id) === String(r.id))) opts.unshift(r);
      pick.innerHTML = opts.map((x) => `<option value="${esc(x.id)}" ${r && String(x.id) === String(r.id) ? "selected" : ""}>${esc(x.kind)} ${esc(x.id)} · ${esc(fmtLocal(x.started_at))} · ${esc(x.status)}</option>`).join("")
        || '<option value="">no runs yet</option>';
      const log = $("#rpLog");
      if (!r) { $("#rpStatus").textContent = ""; $("#rpElapsed").textContent = ""; $("#rpArgs").textContent = "nothing has run yet"; $("#rpStats").innerHTML = ""; $("#rpBar").hidden = true; $("#rpCancel").hidden = true; log.textContent = ""; return; }
      const p = r.progress || {}, sm = parseJSON(r.summary, {}) || {};
      const running = r.status === "running";
      $("#rpStatus").textContent = r.status + (running && r.cancel_requested ? " · cancelling" : "");
      $("#rpStatus").className = "rp-status " + r.status;
      $("#rpElapsed").textContent = fmtElapsed(r.started_at, r.finished_at) + (running ? " elapsed" : "");
      $("#rpCancel").hidden = !running; $("#rpCancel").disabled = !!r.cancel_requested;
      $("#rpArgs").textContent = describeArgs(r);
      const cell = (label, v, cls = "") => v == null || v === "" ? "" : `<div class="${cls}">${label} <b>${esc(v)}</b></div>`;
      let cells;
      if (r.kind === "scan") {
        const total = p.boards_total ?? (sm.boards ? sm.boards.length + (sm.failed || []).length : null);
        const finished = (p.boards_done || 0) + (p.boards_failed || 0);
        const queuedCards = running && p.cards != null && p.phase !== "listing" ? Math.max(0, p.cards - (p.job_index || 0)) : null;
        const boardsLeft = running && total != null ? Math.max(0, total - finished - (p.board ? 1 : 0)) : null;
        cells = cell("Board", p.board ? `${p.board_index}/${total} · ${p.board}${p.platform ? ` (${p.platform})` : ""}` : total != null ? `${finished || (sm.boards || []).length}/${total} done` : null, "span")
          + cell("Posting", p.job_title ? `${p.job_index}/${p.cards} · ${p.job_title}` : p.phase === "listing" ? "listing the board's cards…" : p.phase === "embedding" ? "embedding new text…" : null, "span")
          + cell("Cards listed", p.cards_total ?? sm.cards)
          + cell("Pages fetched", p.fetched)
          + cell("New", p.new ?? sm.new)
          + cell("Already known", p.known ?? sm.known)
          + cell("Re-fetched", p.refetched)
          + cell("Verdicts carried", p.carried ?? sm.carried)
          + cell("Embedded", p.embedded ?? sm.embedded)
          + cell("No page (skipped)", p.skipped)
          + cell("Queued", queuedCards != null ? `${queuedCards} card(s)${boardsLeft ? ` + ${boardsLeft} board(s)` : ""}` : null)
          + cell("Boards failed", p.boards_failed ?? (sm.failed ? sm.failed.length : null))
          + cell("Need judgment", sm.need_judgment ? sm.need_judgment.length : null);
      } else {
        cells = cell("Posting", p.job_title ? `${p.job_index}/${p.total} · ${p.job_title}` : p.phase === "embedding" ? "embedding changed text…" : null, "span")
          + cell("Refreshed", p.refreshed ?? sm.refreshed) + cell("Failed", p.failed ?? sm.failed) + cell("Embedded", p.embedded ?? sm.embedded);
      }
      $("#rpStats").innerHTML = cells;
      const f = fraction(r), bar = $("#rpBar");
      bar.hidden = !running; bar.classList.toggle("indeterminate", running && f == null);
      bar.firstElementChild.style.width = f == null ? "0%" : `${Math.round(f * 100)}%`;
      // The log: append only what is new, so reading while it grows keeps the scroll position.
      const shown = log.dataset.run === String(r.id) ? Number(log.dataset.n || 0) : 0;
      if (shown === 0 || shown > r.lines.length) { log.textContent = r.lines.join("\n"); runState.follow = true; }
      else if (r.lines.length > shown) log.append(document.createTextNode("\n" + r.lines.slice(shown).join("\n")));
      log.dataset.run = String(r.id); log.dataset.n = String(r.lines.length);
      if (runState.follow) log.scrollTop = log.scrollHeight;
    }
    $("#rpLog").addEventListener("scroll", () => { const l = $("#rpLog"); runState.follow = l.scrollHeight - l.scrollTop - l.clientHeight < 30; });
    setInterval(() => { if (runState.open && isRunning() && !runState.view) $("#rpElapsed").textContent = fmtElapsed(runState.run.started_at) + " elapsed"; }, 1000);

    async function fetchRun(id) {
      const r = await fetch(`/api/runs/${encodeURIComponent(id)}`);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
      const d = await r.json();
      d.lines = d.log ? d.log.split("\n") : [];
      d.n = d.line_count ?? d.lines.length;          // entries the server has logged (one may span lines)
      return d;
    }
    // Follow a run: its log so far from the API, every later line and progress change from the stream.
    function watchRun(id) {
      const p = runState.pending = fetchRun(id).then((d) => {
        if (runState.pending !== p) return;
        runState.pending = null;
        if (runState.view && String(runState.view.id) === String(d.id)) runState.view = null;
        runState.run = d;
        const q = runState.queue; runState.queue = [];
        q.forEach(applyEvent);
        renderRunLine(); renderPanel();
      }).catch((e) => { if (runState.pending === p) runState.pending = null; setRun("failed", `run ${id}: ${e.message}`); });
      return p;
    }
    function applyEvent(ev) {
      const r = runState.run;
      if (runState.pending) { runState.queue.push(ev); return; }
      if (!r || String(r.id) !== String(ev.run)) {       // a run we do not know (started here, via MCP, or before the page loaded)
        runState.queue = [ev]; watchRun(ev.run); return;
      }
      const wasRunning = r.status === "running";
      if (ev.line != null) {
        if (ev.line_no === r.n + 1) { r.lines.push(...String(ev.line).split("\n")); r.n++; }
        else if (ev.line_no > r.n + 1) { runState.queue = [ev]; watchRun(r.id); return; }   // missed some: resync
      }
      if (ev.progress) r.progress = ev.progress;
      r.status = ev.status; r.cancel_requested = ev.cancel_requested;
      if (ev.summary) r.summary = ev.summary;
      if (wasRunning && r.status !== "running") { r.finished_at = ev.t; loadRecent(); if (onFinished) onFinished(r); }
      renderRunLine(); renderPanel();
    }
    function onLogEvent(ev) {
      if (ev.run != null || isRunning()) return;       // run lines come through the run itself; a run owns the line
      setRun(ev.level === "error" ? "error" : ev.level === "warn" ? "warn" : "info", String(ev.msg).split("\n")[0]);
    }
    onEvent("run", applyEvent); onEvent("log", onLogEvent);
    onEvent("reopen", () => { if (isRunning()) watchRun(runState.run.id); });
    async function cancelRun() {
      const r = runState.run; if (!r || r.status !== "running") return;
      $("#runCancel").disabled = true; $("#rpCancel").disabled = true;
      try { await post(`/api/runs/${encodeURIComponent(r.id)}/cancel`); }
      catch (e) { setRun("failed", `cancel failed: ${e.message}`); $("#runCancel").disabled = false; $("#rpCancel").disabled = false; }
    }
    async function loadRecent() {
      try { runState.recent = (await (await fetch("/api/runs?limit=15")).json()).runs || []; } catch { /* keep what we have */ }
      renderPanel();
    }
    async function openPanel() {
      runState.open = true; renderPanel();
      await loadRecent();
      if (!runState.run && !runState.view && runState.recent.length) {   // nothing live: show the last run
        try { runState.view = await fetchRun(runState.recent[0].id); } catch { /* panel shows the list only */ }
        renderPanel();
      }
    }
    $("#run").addEventListener("click", openPanel);
    $("#rpClose").addEventListener("click", () => { runState.open = false; runState.view = null; renderPanel(); });
    $("#rpPick").addEventListener("change", async (e) => {
      const id = e.target.value; if (!id) return;
      if (runState.run && String(runState.run.id) === id) { runState.view = null; renderPanel(); return; }
      try { runState.view = await fetchRun(id); } catch (err) { setRun("failed", `run ${id}: ${err.message}`); }
      renderPanel();
    });
    $("#runCancel").addEventListener("click", cancelRun);
    $("#rpCancel").addEventListener("click", cancelRun);
    runs = { state: runState, setRun, watchRun, isRunning, openPanel, cancelRun };
    return runs;
  }

  // ---------------------------------------------------------------- the scan dialog
  // One dialog for both pages: a tab strip, one panel per tab, the shared options (days, whole
  // board, re-fetch) under the panels, Start. Each tab is
  //   { label, html, mount(panel), load(panel), prefill(panel, args), submit(panel, form) -> {url, body, label} }
  //   html: the panel's markup; mount: wire it once; load: (re)fill it each time the dialog opens;
  //   prefill: apply a stored request's args (Edit and run on the Scans page); submit: the request to
  //   POST (throw an Error to refuse; the message goes to the run line). The reply must carry run_id.
  // A "Geographic" tab (geo_scan) registers with JB.scanDialog.addTab("geo", {...}) before mount();
  // open(args, "geo") then selects it. Nothing else here knows about tabs by name.
  const scanDialog = (() => {
    const tabs = {};
    let dlg = null, opts = {}, active = "boards";
    const addTab = (name, tab) => { tabs[name] = tab; };

    let boards = [];
    addTab("boards", {
      label: "Boards",
      html: `<div id="boardList" class="boardlist"></div>
        <div class="muted" style="margin:4px 0 8px"><a href="#" id="boardsAll">all</a> · <a href="#" id="boardsNone">none</a></div>
        <details id="addBoards"><summary>Add boards by URL</summary>
          <textarea id="boardUrls" rows="3" placeholder="one board URL per line (Workday, Oracle HCM, iCIMS, UKG, NEOGOV, Greenhouse, Eightfold, Yello ...)" style="width:100%"></textarea>
          <div class="muted">Optional title filters for the new boards: include <input type="text" id="boardInclude" placeholder="analyst|account|actuar" style="width:11em"> exclude <input type="text" id="boardExclude" placeholder="senior|director|manager" style="width:11em"></div>
          <button type="button" id="boardAdd">Add</button> <span id="boardAddStatus" class="muted"></span>
        </details>`,
      mount(panel) {
        const all = (on) => panel.querySelectorAll('input[name="board"]').forEach((i) => { i.checked = on; });
        $("#boardsAll").addEventListener("click", (e) => { e.preventDefault(); all(true); });
        $("#boardsNone").addEventListener("click", (e) => { e.preventDefault(); all(false); });
        $("#boardAdd").addEventListener("click", async () => {
          const urls = $("#boardUrls").value.split("\n").map((x) => x.trim()).filter(Boolean);
          if (!urls.length) return;
          const inc = $("#boardInclude").value.trim(), exc = $("#boardExclude").value.trim();
          $("#boardAddStatus").textContent = `adding ${urls.length}…`;
          try {
            const d = await post("/api/boards", { urls: urls.map((url) => ({ url, include: inc || undefined, exclude: exc || undefined })) });
            const bad = d.results.filter((r) => r.error);
            $("#boardAddStatus").textContent = `${d.added} added` + (bad.length ? `; ${bad.length} failed: ` + bad.map((r) => `${r.url} (${r.error})`).join("; ") : "");
            if (d.added) $("#boardUrls").value = bad.map((r) => r.url).join("\n");
            await this.load(panel);
          } catch (e) { $("#boardAddStatus").textContent = `failed: ${e.message}`; }
        });
      },
      async load(panel) {
        boards = (await get("/api/boards")).boards;
        $("#boardList").innerHTML = boards.length ? boards.map((b) => {
          const ls = b.last_scan;
          const when = ls ? `${fmtAgo(ls.started_at)}${ls.status === "failed" ? " · failed" : ls.new != null ? ` · ${ls.new} new` : ""}` : "never scanned";
          return `<label class="chk board"><input type="checkbox" name="board" value="${b.id}">
            <b>${esc(b.name)}</b> <small class="muted">${esc(b.adapter)} · ${b.jobs} jobs · ${esc(when)}</small>
            <a href="${esc(b.url)}" target="_blank" rel="noopener" title="${esc(b.url)}">↗</a></label>`;
        }).join("") : `<div class="muted">No boards yet. Add one by URL below.</div>`;
        if (opts.onBoards) opts.onBoards(boards);          // the board page refreshes its Board filter
      },
      prefill(panel, a) {
        const want = new Set((a.boards || []).map(Number));
        panel.querySelectorAll('input[name="board"]').forEach((i) => { i.checked = want.has(Number(i.value)); });
      },
      submit(panel) {
        const ids = [...panel.querySelectorAll('input[name="board"]:checked')].map((i) => Number(i.value));
        if (!ids.length) throw new Error("scan: no board selected");
        return { url: "/api/scan", body: { boards: ids }, label: `scan ${ids.length} board(s)` };
      },
    });

    const html = () => `<dialog id="scanDlg">
      <form method="dialog" id="scanForm">
        <h3>Scan job boards</h3>
        <div class="tabs" id="scanTabs">${Object.entries(tabs).map(([n, t]) => `<button type="button" data-tab="${n}">${esc(t.label)}</button>`).join("")}</div>
        ${Object.entries(tabs).map(([n, t]) => `<div class="tabpanel" data-panel="${n}" hidden>${t.html}</div>`).join("")}
        <label>Posted within the last <input type="number" name="days" min="1" max="365" value="7" style="width:5em"> days</label>
        <label class="chk"><input type="checkbox" name="all"> whole board (ignore days)</label>
        <label class="chk"><input type="checkbox" name="refetch"> re-fetch postings already stored</label>
        <div class="dlg-actions"><button value="cancel" type="button" id="scanCancel">Cancel</button><button value="start" id="scanStart">Start scan</button></div>
      </form>
    </dialog>`;
    const panelOf = (name) => dlg.querySelector(`[data-panel="${name}"]`);
    const loaded = new Set();
    async function select(name) {
      active = name;
      dlg.querySelectorAll("#scanTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
      dlg.querySelectorAll(".tabpanel").forEach((p) => { p.hidden = p.dataset.panel !== name; });
      if (!loaded.has(name) && tabs[name].load) { loaded.add(name); await tabs[name].load(panelOf(name)); }
    }
    function mount(o) {
      if (dlg) return api;
      opts = o || {};
      document.body.insertAdjacentHTML("beforeend", html());
      dlg = $("#scanDlg");
      Object.entries(tabs).forEach(([n, t]) => { if (t.mount) t.mount(panelOf(n)); });
      dlg.querySelectorAll("#scanTabs button").forEach((b) => b.addEventListener("click", () => select(b.dataset.tab)));
      $("#scanCancel").addEventListener("click", () => dlg.close());
      $("#scanForm").addEventListener("submit", async (e) => {
        e.preventDefault();
        const f = new FormData(e.target), tab = tabs[active];
        let req;
        try { req = tab.submit(panelOf(active), f); } catch (err) { if (runs) runs.setRun("failed", err.message); return; }
        req.body = { ...req.body, days: Number(f.get("days") || 7), all: !!f.get("all"), refetch: !!f.get("refetch") };
        dlg.close();
        if (runs) runs.setRun("running", `${req.label}: starting…`);
        try { const d = await post(req.url, req.body); if (runs) runs.watchRun(d.run_id); }
        catch (err) { if (runs) runs.setRun("failed", `${req.label} failed: ${err.message}`); }
      });
      return api;
    }
    // open(args, tab): every open reloads the panels' data (boards and their last scan); args is a
    // stored request's args to pre-fill (boards, days, all, refetch), absent for a fresh dialog.
    async function open(args, tab = "boards") {
      if (!dlg) mount();
      const f = $("#scanForm");
      loaded.clear(); await select(tab);
      if (args) {                                    // a fresh open keeps whatever was last typed, as before
        f.elements.days.value = args.days != null ? args.days : 7;
        f.elements.all.checked = !!args.all; f.elements.refetch.checked = !!args.refetch;
        if (tabs[tab].prefill) tabs[tab].prefill(panelOf(tab), args);
      }
      dlg.showModal();
    }
    const api = { addTab, mount, open, boards: () => boards };
    return api;
  })();

  return { $, esc, fmtLocal, fmtAgo, fmtElapsed, parseJSON, post, get, connectEvents, onEvent,
           fraction, headline, describeArgs, mountRuns, scanDialog, get runs() { return runs; } };
})();
