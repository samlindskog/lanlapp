// The board page. Filtering is the server's job (GET /api/jobs with the controls as query
// parameters); the page keeps only sorting, selection and the open row, and re-queries when a
// control changes. Semantic search ranks the current list by the server's similarity scores.
// Shared pieces (formatters, the event stream, the run line + panel, the scan dialog) are JB in common.js.
(() => {
  const { $, esc, fmtLocal, fmtAgo, post } = JB;
  // ?run=<id> (the Scans page's "Jobs" link): only the jobs that scan stored first, with the verdict
  // and closing defaults relaxed so a fresh scan's unjudged postings are what shows.
  const runParam = new URLSearchParams(location.search).get("run");
  const state = { jobs: [], total: 0, sort: "close_date", dir: "asc", open: null, selected: new Set(), lastClicked: null, lastRows: [], bulkOpen: false, semantic: null,
                  run: runParam && /^\d+$/.test(runParam) ? Number(runParam) : null };
  const VRANK = { yes: 0, maybe: 1, none: 2, no: 3 };
  const ARANK = { applied: 0, drafted: 1, none: 4 };   // legacy values sort last
  const today = new Date().toISOString().slice(0, 10);
  const week = new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10);

  const verdictOf = (j) => j.verdict || "none";
  const actionOf = (j) => j.action || "none";
  const gradeOf = (j) => {
    if (!j.pay_plan && j.grade_low == null) return "";
    if (j.grade_low == null) return j.pay_plan;
    const pp = j.pay_plan || "";
    return j.grade_high && j.grade_high !== j.grade_low ? `${pp}-${j.grade_low}/${j.grade_high}` : `${pp}-${j.grade_low}`;
  };
  // The controls, as the API's query string. Every control only narrows; blank means no restriction.
  function query() {
    const p = new URLSearchParams();
    const verdicts = [...document.querySelectorAll('input[data-f="verdict"]:checked')].map((i) => i.value);
    if (verdicts.length && verdicts.length < 4) p.set("verdict", verdicts.join(","));
    const applied = $("#applied").value;
    if (applied === "applied") p.set("action", "applied");
    if (applied === "not") p.set("action", "drafted,none");
    if (applied === "drafted") p.set("action", "drafted");
    if (applied === "untouched") p.set("action", "none");
    if ($("#openness").value !== "any") p.set("closing", $("#openness").value);
    const src = $("#source").value;
    if (src.startsWith("board:")) p.set("board", src.slice(6));
    if (src.startsWith("source:")) p.set("source", src.slice(7));
    if (src.startsWith("origin:")) p.set("origin", src.slice(7));
    const q = $("#q").value.trim();
    if (q && !state.semantic) p.set("q", q);
    if (state.run) p.set("run", state.run);
    return p.toString();
  }
  function setRunFilter(id) {
    state.run = id; $("#runBadge").hidden = !id; $("#runId").textContent = id || "";
    history.replaceState(null, "", id ? `/?run=${id}` : "/");
  }
  const listed = () => state.semantic ? state.jobs.filter((j) => j.id in state.semantic) : state.jobs;

  function sorted(rows) {
    if (state.semantic) return rows.slice().sort((a, b) => state.semantic[b.id] - state.semantic[a.id]);
    const k = state.sort, d = state.dir === "asc" ? 1 : -1;
    const key = (j) => {
      if (k === "verdict") return VRANK[verdictOf(j)];
      if (k === "action") return ARANK[actionOf(j)] ?? 9;
      if (k === "close_date") return j.close_date || "9999-99-99";
      if (k === "grade_low") return j.grade_low ?? 99;
      if (k === "first_seen") return j.first_seen || "";
      if (k === "board") return (j.place_name || j.board_name || j.source || "").toLowerCase();
      if (k === "last_fetched") return j.last_fetched ? new Date(j.last_fetched).getTime() : 0;
      return (j.title || "").toLowerCase();
    };
    return rows.slice().sort((a, b) => {
      const x = key(a), y = key(b);
      if (x < y) return -d; if (x > y) return d;
      return (a.close_date || "9999") < (b.close_date || "9999") ? -1 : 1;
    });
  }

  const ACTIONS = ["applied", "drafted"];
  const VERDICTS = ["yes", "maybe", "no"];
  // A verdict counts as overridden when the latest one is yours and differs from Claude's latest.
  const overridden = (j) => j.verdict_author === "user" && j.claude_verdict && j.verdict !== j.claude_verdict;
  function verdictSelect(j, v, extraClass = "") {
    // Short label: a <select> sizes itself to its longest option, so a long reset label stretched the pill.
    const reset = j.claude_verdict && v !== j.claude_verdict ? `<option value="__reset" title="reset to Claude's verdict (${j.claude_verdict})">↺ ${j.claude_verdict}</option>` : "";
    return `<select class="status v-${v} ${extraClass}" data-verdict="${j.id}" title="Set verdict (saved immediately)" onclick="event.stopPropagation()">
      <option value="" ${v === "none" ? "selected" : ""}>unjudged</option>
      ${VERDICTS.map((x) => `<option value="${x}" ${v === x ? "selected" : ""}>${x}</option>`).join("")}${reset}
    </select>`;
  }
  function statusSelect(j, a, extraClass = "") {
    return `<select class="status a-${a} ${extraClass}" data-status="${j.id}" title="Set status (saved immediately)" onclick="event.stopPropagation()">
      <option value="" ${a === "none" ? "selected" : ""}>—</option>
      ${ACTIONS.map((x) => `<option value="${x}" ${a === x ? "selected" : ""}>${x}</option>`).join("")}
    </select>`;
  }
  function closeCell(j) {
    if (!j.close_date) return '<span class="closed">?</span>';
    if (j.close_date < today) return `<span class="closed">${j.close_date}</span>`;
    if (j.close_date <= week) return `<span class="soon">${j.close_date}</span>`;
    return j.close_date;
  }

  function render() {
    const rows = sorted(listed());
    state.lastRows = rows.map((j) => j.id);
    $("#count").textContent = `${rows.length} of ${state.total}`;
    $("#selAll").checked = rows.length > 0 && rows.every((j) => state.selected.has(j.id));
    renderSidebar();
    document.querySelectorAll("th[data-sort]").forEach((th) => th.classList.toggle("active", th.dataset.sort === state.sort));
    $("#rows").innerHTML = rows.map((j) => {
      const v = verdictOf(j), a = actionOf(j);
      const link = j.external_url || j.board_url;
      return `<tr class="row ${state.selected.has(j.id) ? "selected" : ""}" data-id="${j.id}">
        <td class="sel"><input type="checkbox" data-sel="${j.id}" ${state.selected.has(j.id) ? "checked" : ""}></td>
        <td>${closeCell(j)}</td>
        <td>${verdictSelect(j, v)}${overridden(j) ? `<br><small title="${esc(j.claude_reason || "")}">claude: ${esc(j.claude_verdict)}</small>` : (j.eligible_grades ? `<br><small>${esc(j.eligible_grades)}</small>` : "")}</td>
        <td>${statusSelect(j, a)}${j.action_date ? `<br><small>${j.action_date}</small>` : ""}</td>
        <td class="title">${state.semantic ? `<span class="score" title="similarity">${(state.semantic[j.id] * 100).toFixed(0)}% · </span>` : ""}${esc(j.title)}<small>${esc(j.series || "")}${j.announcement_no ? " · " + esc(j.announcement_no) : ""}</small></td>
        <td class="hide-sm">${esc(gradeOf(j))}</td>
        <td class="hide-sm" title="${esc(j.place_name ? `${j.board_name} · ${j.board_platform}` : j.board_platform || j.source)}">${esc((j.place_name || j.board_name || j.source || "").slice(0, 40))}<br><small class="muted">${esc(j.board_origin === "geo" ? "geographic" : j.board_platform || j.source)}</small></td>
        <td class="hide-sm">${esc((j.location || placeAddress(j) || "").slice(0, 60))}</td>
        <td class="hide-sm fetched" title="${esc(fmtLocal(j.last_fetched))}">${esc(fmtAgo(j.last_fetched))}</td>
        <td class="hide-sm">${link ? `<a href="${esc(link)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">open ↗</a>` : ""}</td>
      </tr>` + (state.open === j.id ? detailRow(j) : "");
    }).join("");
  }

  // A geo board's place: street, city and state as one line (the job's own location wins).
  const placeAddress = (j) => [j.place_address, [j.place_city, j.place_state].filter(Boolean).join(", ")].filter(Boolean).join(", ");
  function placeCells(j) {
    if (!j.place_name) return "";
    // The server stores only http(s) URLs; anything else is shown as text, never linked.
    const site = (u) => /^https?:\/\//i.test(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\//i, "").replace(/\/$/, "").slice(0, 50))}</a>` : `<b>${esc(u.slice(0, 50))}</b>`;
    const maps = j.place_lat != null && j.place_lon != null ? `<a href="${esc(JB.geo.mapsLink(j.place_lat, j.place_lon))}" target="_blank" rel="noopener">Google Maps</a>` : "";
    return `<div>Place: <b>${esc(j.place_name)}</b> <small>(found by a geographic scan)</small>${placeAddress(j) ? ` · ${esc(placeAddress(j))}` : ""}${maps ? ` · ${maps}` : ""}</div>
      <div>Website: ${j.place_website ? site(j.place_website) : "<b>—</b>"} · careers page: ${j.place_careers_url ? site(j.place_careers_url) : "<b>—</b>"}${j.place_portal_url ? ` · portal: ${site(j.place_portal_url)}` : ""}${j.place_phone ? ` · phone <b>${esc(j.place_phone)}</b>` : ""}</div>`;
  }
  function detailRow(j) {
    return `<tr class="detail"><td colspan="10">
      <div class="detail-grid">${placeCells(j)}
        <div>Verdict: ${verdictSelect(j, verdictOf(j), "big")} ${j.eligible_grades ? `(${esc(j.eligible_grades)})` : ""}${j.verdict_author === "user" ? ` <small>set by you</small>` : ""}</div>
        <div>${overridden(j)
          ? `<details class="reason"><summary>Claude's verdict: ${esc(j.claude_verdict)}${j.claude_grades ? ` (${esc(j.claude_grades)})` : ""} — reason minimized</summary><b>${esc(j.claude_reason || "—")}</b></details>`
          : `Reason: <b>${esc(j.claude_reason || j.verdict_reason || "—")}</b>`}</div>
        <div>Status: ${statusSelect(j, actionOf(j), "big")} ${j.action_date ? esc(j.action_date) : ""}</div>
        <div>Resume: ${(j.resume_files || []).filter((f) => f.kind === "pdf").map((f) => `<a href="/view.html?f=${encodeURIComponent("job/" + j.id + "/" + f.name)}" target="_blank" rel="noopener" title="${esc(f.label || "")} · ${esc(fmtLocal(f.uploaded_at))}">${esc((f.label ? f.label + "/" : "") + f.name)}</a>`).join(" · ") || `<b>${esc(j.resume_dir || "—")}</b>`}</div>
        <div>Note: <b>${esc(j.action_note || "—")}</b></div>
        <div>Opens / closes: <b>${esc(j.open_date || "?")} / ${esc(j.close_date || "?")}</b></div>
        <div>Salary: <b>${esc(j.salary || "—")}</b></div>
        <div>Board: <b>${esc(j.board_name || j.source)}</b> on ${esc(j.board_platform || j.source)} <small>(adapter ${esc(j.board_adapter || j.source)})</small>${j.board_source_url ? ` · <a href="${esc(j.board_source_url)}" target="_blank" rel="noopener">${esc(j.board_source_url.replace(/^https?:\/\//, "").slice(0, 60))}</a>` : ""}</div>
        <div>First seen: <b>${esc(fmtLocal(j.first_seen, true))}</b></div>
        <div>Links: ${j.external_url ? `<a href="${esc(j.external_url)}" target="_blank" rel="noopener">${/usajobs\.gov/.test(j.external_url) ? "USAJOBS" : "posting"}</a>` : ""} ${j.board_url && j.board_url !== j.external_url ? `<a href="${esc(j.board_url)}" target="_blank" rel="noopener">board</a>` : ""}</div>
        <div>Last fetched: <b>${esc(fmtLocal(j.last_fetched) || "—")}</b>
             <button data-act="refresh" data-id="${j.id}" style="margin-left:6px">Refresh posting</button></div>
      </div>
      <div id="docs-${j.id}"><em style="color:var(--muted)">loading stored text…</em></div>
    </td></tr>`;
  }

  async function loadDocs(id) {
    const el = document.getElementById(`docs-${id}`);
    if (!el) return;
    try {
      const r = await fetch(`/api/jobs/${id}`);
      const d = await r.json();
      const docs = d.documents.filter((x) => x.kind !== "board_card");
      const prim = d.primary || {};
      el.innerHTML = docs.length ? docs.map((x) => {
        const text = x.text || "";
        const isPrimary = x.id === prim.doc_id;
        // Primary document: every section the adapter found, in page order, each under its heading.
        const secs = isPrimary && prim.sections && Object.keys(prim.sections).length ? prim.sections : null;
        const preview = secs ? Object.entries(secs).map(([h, t]) => `${h.toUpperCase()}\n${t}`).join("\n\n\n") : text.slice(0, 1500);
        const truncated = secs ? false : text.length > 1500;
        return `<details ${isPrimary ? "open" : ""}><summary>${esc(x.kind)} · ${esc(fmtLocal(x.fetched_at))} · ${text.length.toLocaleString()} chars</summary>
          <pre class="doc" data-full="${esc(text)}" data-preview="${esc(preview)}">${esc(preview)}</pre>
          ${truncated || secs ? `<button class="more" data-more="1">${secs ? "Show raw page text" : "Show full text"}</button>` : ""}</details>`;
      }).join("") : '<em style="color:var(--muted)">no stored documents</em>';
    } catch (e) { el.textContent = "failed to load: " + e; }
  }

  document.addEventListener("change", (e) => { if (e.target.closest("header")) {
    if (e.target.id === "sort") { [state.sort, state.dir] = e.target.value.split(":"); render(); }
    else reload();
  }});
  let typeTimer = null;
  $("#q").addEventListener("input", () => { if (!state.semantic) { clearTimeout(typeTimer); typeTimer = setTimeout(reload, 250); } });
  async function semanticSearch(q) {
    setRun("running", `semantic search: "${q}"…`);
    try {
      const r = await fetch(`/api/search?mode=semantic&limit=200&q=${encodeURIComponent(q)}`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || r.statusText);
      state.semantic = Object.fromEntries(d.hits.map((h) => [h.id, h.score]));
      $("#semBadge").hidden = false; $("#semQuery").textContent = q;
      setRun("done", `semantic search: ${d.hits.length} ranked`);
      await reload();                               // the other filters still apply; q does not
    } catch (err) { setRun("failed", `semantic search failed: ${err.message}`); }
  }
  $("#q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { const q = $("#q").value.trim(); if (q) semanticSearch(q); }
    if (e.key === "Escape") { state.semantic = null; $("#semBadge").hidden = true; reload(); }
  });
  $("#semClear").addEventListener("click", () => { state.semantic = null; $("#semBadge").hidden = true; reload(); });
  $("#runClear").addEventListener("click", () => { setRunFilter(null); reload(); });
  $("#reset").addEventListener("click", () => {
    document.querySelectorAll('input[data-f="verdict"]').forEach((i) => i.checked = i.value === "yes" || i.value === "maybe");
    $("#applied").value = "any"; $("#openness").value = "open"; $("#source").value = ""; $("#q").value = "";
    state.semantic = null; $("#semBadge").hidden = true; setRunFilter(null);
    $("#sort").value = "close_date:asc"; state.sort = "close_date"; state.dir = "asc"; reload();
  });
  document.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    state.dir = state.sort === k && state.dir === "asc" ? "desc" : "asc"; state.sort = k;
    const opt = [...$("#sort").options].find((o) => o.value === `${k}:${state.dir}`);
    if (opt) $("#sort").value = opt.value;
    render();
  }));
  $("#rows").addEventListener("click", (e) => {
    if (e.target.closest("input[data-sel], select, a, details, button")) return;
    const tr = e.target.closest("tr.row"); if (!tr) return;
    const id = Number(tr.dataset.id);
    state.open = state.open === id ? null : id; render();
    if (state.open === id) loadDocs(id);
  });

  let reloadSeq = 0;
  async function reload() {
    const seq = ++reloadSeq;
    const r = await fetch("/api/jobs?fields=full&" + query());
    if (r.status === 401) { location.href = "/login.html?next=" + encodeURIComponent(location.pathname); return {jobs: []}; }
    const d = await r.json();
    if (seq !== reloadSeq) return d;            // a newer query answered first
    state.jobs = d.jobs; state.total = d.total;
    render();
    return d;
  }
  async function loadBoardFilter() {
    const d = await (await fetch("/api/boards")).json();
    const cur = $("#source").value;
    // Geo boards (often hundreds) are one entry, "Geographic" (origin=geo), not a line each.
    $("#source").innerHTML = '<option value="">all</option>'
      + d.boards.filter((b) => b.origin !== "geo").map((b) => `<option value="board:${esc(b.name)}">${esc(b.name)}</option>`).join("")
      + (d.boards.some((b) => b.origin === "geo") ? '<option value="origin:geo">Geographic</option>' : "")
      + '<option value="source:manual">manual</option>';
    $("#source").value = cur;
  }
  // ---------------------------------------------------------------- runs and the scan dialog (common.js)
  const { setRun, watchRun } = JB.mountRuns({ onFinished: () => reload() });
  const scanDlg = JB.scanDialog.mount({ onBoards: loadBoardFilter });
  $("#scan").addEventListener("click", () => scanDlg.open());
  $("#refreshAll").addEventListener("click", async () => {
    const ids = listed().filter((j) => j.external_url || j.board_url).map((j) => j.id);
    if (!ids.length) { setRun("failed", "refresh: nothing listed to refresh"); return; }
    setRun("running", `refresh ${ids.length} listed posting(s): starting…`);
    try { const d = await post("/api/refresh", { ids }); watchRun(d.run_id); }
    catch (e) { setRun("failed", `refresh failed: ${e.message}`); }
  });
  $("#rows").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-act=refresh]"); if (!b) return;
    e.stopPropagation(); b.disabled = true;
    const id = Number(b.dataset.id), job = state.jobs.find((j) => j.id === id);
    setRun("running", `refresh [${id}] ${job ? job.title : ""}: fetching…`);
    try {
      const d = await post(`/api/jobs/${id}/refresh`);
      setRun("done", `refresh [${id}] ${job ? job.title : ""} done: ${d.refresh.refetched} page(s), closes ${d.job.close_date || "?"}, fetched ${fmtLocal(d.job.last_fetched)}`);
      await reload(); loadDocs(id);
    } catch (err) { setRun("failed", `refresh [${id}] failed: ${err.message}`); }
    b.disabled = false;
  });
  function renderSidebar() {
    const n = state.selected.size;
    document.body.classList.toggle("select-mode", state.bulkOpen);
    $("#bulk").hidden = !state.bulkOpen;
    $("#bulkToggle").classList.toggle("on", state.bulkOpen);
    $("#bulkToggle").textContent = state.bulkOpen ? "Done selecting" : "Select…";
    $("#selCount").textContent = n;
    $("#bulkApply").disabled = n === 0; $("#bulkVerdictApply").disabled = n === 0;
    if (!$("#bulkDate").value) $("#bulkDate").value = today;
  }
  function exitSelectMode() { state.bulkOpen = false; state.selected.clear(); state.lastClicked = null; render(); }
  $("#bulkToggle").addEventListener("click", () => { if (state.bulkOpen) exitSelectMode(); else { state.bulkOpen = true; renderSidebar(); } });
  $("#bulkClose").addEventListener("click", exitSelectMode);
  $("#rows").addEventListener("click", (e) => {
    const cb = e.target.closest("input[data-sel]"); if (!cb) return;
    if (e.shiftKey) { const sel = document.getSelection(); if (sel) sel.removeAllRanges(); }
    const id = Number(cb.dataset.sel), want = cb.checked;
    let ids = [id];
    if (e.shiftKey && state.lastClicked != null) {
      const a = state.lastRows.indexOf(state.lastClicked), b = state.lastRows.indexOf(id);
      if (a >= 0 && b >= 0) ids = state.lastRows.slice(Math.min(a, b), Math.max(a, b) + 1);
    }
    ids.forEach((x) => want ? state.selected.add(x) : state.selected.delete(x));
    state.lastClicked = id;
    render();
  });
  $("#selAll").addEventListener("change", (e) => {
    state.lastRows.forEach((id) => e.target.checked ? state.selected.add(id) : state.selected.delete(id));
    render();
  });
  $("#bulkClear").addEventListener("click", () => { state.selected.clear(); state.lastClicked = null; render(); });
  $("#bulkApply").addEventListener("click", async () => {
    const ids = [...state.selected], action = $("#bulkAction").value, date = $("#bulkDate").value || today, note = $("#bulkNote").value.trim();
    let ok = 0, fail = 0;
    for (const id of ids) {
      setRun("running", `bulk status: ${action} ${ok + fail + 1}/${ids.length}…`);
      const job = state.jobs.find((j) => j.id === id);
      const body = { action, date }; if (note) body.note = note; if (job && job.resume_dir) body.resume_dir = job.resume_dir;
      try { await post(`/api/jobs/${id}/action`, body); ok++; } catch (err) { fail++; }
    }
    setRun(fail ? "failed" : "done", `bulk status: ${action} set on ${ok} job(s)${fail ? `, ${fail} failed` : ""}`);
    state.selected.clear(); state.lastClicked = null; $("#bulkNote").value = "";
    await reload();
  });
  $("#rows").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-more]"); if (!b) return;
    e.stopPropagation();
    const pre = b.previousElementSibling, full = b.dataset.more === "1";
    if (!b.dataset.label) b.dataset.label = b.textContent;
    pre.textContent = full ? pre.dataset.full : pre.dataset.preview;
    pre.classList.toggle("expanded", full);
    b.dataset.more = full ? "0" : "1";
    b.textContent = full ? "Show less" : (b.dataset.label || "Show full text");
  });
  async function setVerdict(id, value) {
    const job = state.jobs.find((j) => j.id === id);
    let body;
    if (value === "__reset") body = { verdict: job.claude_verdict, grades: job.claude_grades, reason: "reset to Claude's verdict", author: "user" };
    else body = { verdict: value, reason: "set by user", author: "user" };
    setRun("running", `verdict [${id}] ${job ? job.title : ""}: saving ${body.verdict}…`);
    try { await post(`/api/jobs/${id}/verdict`, body); setRun("done", `verdict [${id}]: ${body.verdict}`); await reload(); }
    catch (err) { setRun("failed", `verdict [${id}] failed: ${err.message}`); render(); }
  }
  $("#bulkVerdictApply").addEventListener("click", async () => {
    const ids = [...state.selected], value = $("#bulkVerdict").value;
    let ok = 0, fail = 0;
    for (const id of ids) {
      const job = state.jobs.find((j) => j.id === id);
      if (value === "__reset" && !(job && job.claude_verdict)) { fail++; continue; }
      const body = value === "__reset" ? { verdict: job.claude_verdict, grades: job.claude_grades, reason: "reset to Claude's verdict", author: "user" }
                                       : { verdict: value, reason: "set by user", author: "user" };
      setRun("running", `bulk verdict: ${ok + fail + 1}/${ids.length}…`);
      try { await post(`/api/jobs/${id}/verdict`, body); ok++; } catch (err) { fail++; }
    }
    setRun(fail ? "failed" : "done", `bulk verdict set on ${ok} job(s)${fail ? `, ${fail} skipped/failed` : ""}`);
    state.selected.clear(); state.lastClicked = null; await reload();
  });
  $("#rows").addEventListener("change", async (e) => {
    const vs = e.target.closest("select[data-verdict]");
    if (vs) { e.stopPropagation(); if (vs.value) await setVerdict(Number(vs.dataset.verdict), vs.value); else render(); return; }
    const sel = e.target.closest("select[data-status]"); if (!sel) return;
    e.stopPropagation();
    const id = Number(sel.dataset.status), action = sel.value, job = state.jobs.find((j) => j.id === id);
    if (!action) { render(); return; }
    setRun("running", `status [${id}] ${job ? job.title : ""}: saving ${action}…`);
    try {
      const body = { action, date: today };
      if (job && job.resume_dir) body.resume_dir = job.resume_dir;
      await post(`/api/jobs/${id}/action`, body);
      setRun("done", `status [${id}] ${job ? job.title : ""}: ${action} on ${today}`);
      await reload();
    } catch (err) { setRun("failed", `status [${id}] failed: ${err.message}`); render(); }
  });

  fetch("/api/auth/me").then((r) => r.json()).then((m) => { if (m.via === "session") $("#logout").hidden = false; }).catch(() => {});
  $("#logout").addEventListener("click", async (e) => { e.preventDefault(); await fetch("/api/auth/logout", { method: "POST" }); location.href = "/login.html"; });
  if (state.run) { setRunFilter(state.run); document.querySelectorAll('input[data-f="verdict"]').forEach((i) => { i.checked = false; }); $("#openness").value = "any"; }
  loadBoardFilter();
  JB.connectEvents();
  reload().then((d) => {
    if (d.active_run && d.active_run.kind !== "sync") watchRun(d.active_run.id);
  }).catch((e) => { $("#rows").innerHTML = `<tr><td colspan="10">failed to load /api/jobs: ${esc(e)}</td></tr>`; });
})();
