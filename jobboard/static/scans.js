// The Scans page: every background run, newest first, from GET /api/runs (filters are the
// server's; `before` pages back by id), kept current by the event stream (a run event for a row
// we know updates it in place; one for a run we do not know means a new run: refetch the first
// page). The run line, run panel and scan dialog are JB's (common.js), shared with the board.
(() => {
  const { $, esc, fmtLocal, fmtAgo, fmtElapsed, parseJSON, post, get, headline } = JB;
  const PAGE = 50;
  const state = { runs: [], logs: new Set(), more: false };   // logs: run ids whose log row is open
  const byId = (id) => state.runs.find((r) => String(r.id) === String(id));
  const persisted = (r) => /^\d+$/.test(String(r.id));

  const query = (before) => {
    const p = new URLSearchParams({ limit: PAGE });
    if ($("#kind").value) p.set("kind", $("#kind").value);
    if ($("#status").value) p.set("status", $("#status").value);
    if (before) p.set("before", before);
    return p.toString();
  };
  async function reload() {
    const d = await get("/api/runs?" + query());
    state.runs = d.runs; state.more = d.runs.filter(persisted).length >= PAGE;
    render();
  }
  async function loadMore() {
    const last = state.runs.filter(persisted).map((r) => r.id).sort((a, b) => a - b)[0];
    if (!last) return;
    const d = await get("/api/runs?" + query(last));
    state.runs.push(...d.runs); state.more = d.runs.length >= PAGE;
    render();
  }

  const kindBadge = (k) => `<span class="badge k-${["scan", "refresh", "geo"].includes(k) ? k : "other"}">${esc(k)}</span>`;
  // Parameters: what the run was asked to do. Scans name their boards; a geo run its centre
  // (a Maps link) and search; anything else lists its args.
  function params(r) {
    const a = r.args || {};
    if (r.kind === "scan") {
      const names = a.names || (a.boards || []).map((b) => `board ${b}`);
      return `${esc(names.join(", ") || (a.source ? `every ${a.source} board` : "every enabled board"))}
        <small>${a.all ? "whole board" : `last ${a.days ?? 7} days`}${a.refetch ? " · re-fetching stored postings" : ""}${a.source ? ` · source ${esc(a.source)}` : ""}</small>`;
    }
    if (r.kind === "geo") {
      const typed = a.center_text || a.center || "";        // center_text: as typed (an address, a Maps link); center: the coordinates
      const where = a.lat != null ? `<a href="${esc(JB.geo.mapsLink(a.lat, a.lon))}" target="_blank" rel="noopener" title="${esc(typed)}">${esc(a.label || JB.geo.fmtCoord(a.lat, a.lon))}</a>` : esc(typed || "?");
      return `${where} <small>${esc(JB.geo.describeArgs(a))}</small>`;
    }
    if (r.kind === "refresh") return `${(a.ids || []).length} posting(s)`;
    return Object.entries(a).map(([k, v]) => `<small>${esc(k)}: ${esc(Array.isArray(v) ? `${v.length} item(s)` : typeof v === "object" && v ? JSON.stringify(v) : v)}</small>`).join("") || "—";
  }
  // Results: the summary once finished, the live progress headline while running.
  function results(r) {
    const sm = r.summary || {};
    if (r.status === "running") return `<span class="muted">${esc(headline(r))}${r.cancel_requested ? " · cancelling" : ""}</span>`;
    if (r.kind === "scan" && (sm.cards != null || sm.boards)) {
      const failed = (sm.failed || []).length;
      return `${sm.cards ?? 0} cards · <b>${sm.new ?? 0} new</b> · ${sm.known ?? 0} known${failed ? ` · <span style="color:var(--no)">${failed} failed</span>` : ""}
        ${sm.need_judgment && sm.need_judgment.length ? `<small>${sm.need_judgment.length} need judgment</small>` : ""}`;
    }
    if (r.kind === "geo" && sm.discovered != null) {
      const failed = (sm.failed || []).length;
      return `${esc(JB.geo.describeSummary(sm))} · <b>${sm.new ?? 0} new jobs</b>${failed ? ` · <span style="color:var(--no)">${failed} failed</span>` : ""}
        ${sm.location ? `<small>location filter ${esc(sm.location)}</small>` : ""}
        ${sm.search_unavailable ? `<small style="color:var(--maybe)">${sm.search_unavailable} lookups throttled, retried next run</small>` : ""}
        ${sm.facilities ? `<small class="muted">${sm.facilities} buildings and stations skipped</small>` : ""}
        ${sm.need_judgment && sm.need_judgment.length ? `<small>${sm.need_judgment.length} need judgment</small>` : ""}`;
    }
    if (r.kind === "refresh" && sm.refreshed != null) return `${sm.refreshed} refreshed${sm.failed ? ` · <span style="color:var(--no)">${sm.failed} failed</span>` : ""}`;
    const scalars = Object.entries(sm).filter(([, v]) => v == null || typeof v !== "object");
    return scalars.length ? scalars.map(([k, v]) => `<small>${esc(k)}: ${esc(v)}</small>`).join("") : "—";
  }
  function actions(r) {
    const running = r.status === "running", req = r.request || {};
    return [
      running ? `<button class="x" data-act="cancel" data-id="${esc(r.id)}" ${r.cancel_requested ? "disabled" : ""}>Cancel</button>` : "",
      !running && persisted(r) && req.op ? `<button data-act="repeat" data-id="${r.id}" title="Run again with the same request">Repeat</button>` : "",
      ["scan", "geo_scan"].includes(req.op) ? `<button data-act="edit" data-id="${esc(r.id)}" title="Open the scan dialog pre-filled from this run">Edit and run</button>` : "",
      ["scan", "geo"].includes(r.kind) && persisted(r) ? `<a class="btn" href="/?run=${r.id}" title="The jobs this scan stored first">Jobs</a>` : "",
      `<button data-act="log" data-id="${esc(r.id)}" class="${state.logs.has(String(r.id)) ? "on" : ""}">Log</button>`,
    ].join("");
  }
  function row(r) {
    const running = r.status === "running";
    return `<tr data-id="${esc(r.id)}">
      <td class="id">${kindBadge(r.kind)} ${esc(r.id)}</td>
      <td><span class="st ${esc(r.status)}">${esc(r.status)}</span></td>
      <td class="when" title="${esc(fmtLocal(r.started_at))}">${esc(fmtAgo(r.started_at))}<small style="display:block">${esc(fmtLocal(r.started_at))}</small></td>
      <td class="dur hide-sm" data-started="${esc(r.started_at)}" data-running="${running ? 1 : 0}">${esc(fmtElapsed(r.started_at, r.finished_at))}</td>
      <td class="params">${params(r)}</td>
      <td class="results">${results(r)}</td>
      <td class="actions">${actions(r)}</td>
    </tr>` + (state.logs.has(String(r.id)) ? `<tr class="logrow" data-log="${esc(r.id)}"><td colspan="7"><pre>loading…</pre></td></tr>` : "");
  }
  function render() {
    $("#count").textContent = `${state.runs.length} run(s)`;
    $("#rows").innerHTML = state.runs.map(row).join("") || `<tr><td colspan="7" class="muted">no runs match</td></tr>`;
    $("#more").hidden = !state.more;
    state.logs.forEach(loadLog);
  }
  function renderOne(r) {
    const tr = $(`#rows tr[data-id="${r.id}"]`); if (!tr) return;
    tr.outerHTML = row(r).replace(/<tr class="logrow"[\s\S]*$/, "");   // the log row stays as it is
  }
  async function loadLog(id) {
    const pre = $(`tr[data-log="${id}"] pre`); if (!pre) return;
    try { const d = await get(`/api/runs/${encodeURIComponent(id)}`); pre.textContent = d.log || "(empty log)"; }
    catch (e) { pre.textContent = `failed to load: ${e.message}`; }
  }
  // Running rows count up every second without a re-render.
  setInterval(() => document.querySelectorAll('td.dur[data-running="1"]').forEach((td) => { td.textContent = fmtElapsed(td.dataset.started); }), 1000);

  // ---------------------------------------------------------------- live
  const runs = JB.mountRuns({ onFinished: () => {} });
  const scanDlg = JB.scanDialog.mount();
  let refetch = null;
  JB.onEvent("run", (ev) => {
    const r = byId(ev.run);
    if (!r) { clearTimeout(refetch); refetch = setTimeout(reload, 300); return; }   // a run we have not listed yet
    const was = r.status;
    r.status = ev.status; r.cancel_requested = ev.cancel_requested; r.progress = ev.progress || r.progress;
    if (ev.summary) r.summary = parseJSON(ev.summary, {});
    if (was === "running" && r.status !== "running") r.finished_at = ev.t;
    renderOne(r);
    if (state.logs.has(String(r.id)) && ev.line != null) {
      const pre = $(`tr[data-log="${r.id}"] pre`);
      if (pre && pre.textContent !== "loading…") pre.textContent = pre.textContent === "(empty log)" ? ev.line : pre.textContent + "\n" + ev.line;
    }
  });
  JB.onEvent("reopen", reload);

  $("#rows").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const id = b.dataset.id, r = byId(id), act = b.dataset.act;
    if (act === "log") {
      if (state.logs.has(id)) state.logs.delete(id); else state.logs.add(id);
      render(); return;
    }
    // A scan asked for by source or "every enabled board" stored no ids: the boards it resolved to
    // (args.boards) are the selection to edit; an explicit request keeps its own.
    if (act === "edit") {
      const req = r.request || {};
      if (req.op === "geo_scan") scanDlg.open(req.args || {}, "geo");
      else scanDlg.open({ boards: (r.args || {}).boards, ...(req.args || {}) });
      return;
    }
    b.disabled = true;
    try {
      if (act === "cancel") { await post(`/api/runs/${encodeURIComponent(id)}/cancel`); }
      if (act === "repeat") {
        runs.setRun("running", `repeating run ${id}: starting…`);
        const d = await post(`/api/runs/${id}/repeat`);
        runs.watchRun(d.run_id);
        if (d.dropped && d.dropped.length) runs.setRun("warn", `run ${d.run_id} started without deleted board(s) ${d.dropped.join(", ")}`);
      }
    } catch (err) { runs.setRun("failed", `${act} run ${id} failed: ${err.message}`); b.disabled = false; }
  });
  $("#kind").addEventListener("change", reload);
  $("#status").addEventListener("change", reload);
  $("#more").addEventListener("click", loadMore);
  $("#scan").addEventListener("click", () => scanDlg.open());

  JB.connectEvents();
  reload().then(() => {
    const live = state.runs.find((r) => r.status === "running");
    if (live) runs.watchRun(live.id);
  }).catch((e) => { $("#rows").innerHTML = `<tr><td colspan="7">failed to load /api/runs: ${esc(e.message || e)}</td></tr>`; });
})();
