// The "Geographic" tab of the scan dialog (JB.scanDialog.addTab, common.js) and the small geo
// helpers both pages use (JB.geo): the client-side centre parser (the same formats the server's
// parse_center accepts), the Google Maps link, and one-line descriptions of a geo run's args and
// summary. Loaded after common.js and before the page script, so the tab exists when the dialog
// mounts. Field values persist in localStorage (a convenience; the server never sees them).
(() => {
  const { $, esc, get } = JB;
  const NUM = "-?\\d{1,3}(?:\\.\\d+)?";
  const RES = [
    new RegExp(`!3d(${NUM})!4d(${NUM})`),                                           // a Maps place link's pin
    new RegExp(`@(${NUM}),(${NUM})`),                                               // .../@lat,lng,15z
    new RegExp(`[?&](?:q|query|ll|center|destination|daddr|saddr)=(${NUM})\\s*,\\s*(${NUM})`),
    new RegExp(`^\\(?\\s*(${NUM})\\s*[, ]\\s*(${NUM})\\s*\\)?$`),                  // "lat, lng"
  ];
  function parseCenter(text) {
    let t = (text || "").trim();
    try { t = decodeURIComponent(t); } catch { /* keep as typed */ }
    for (const rx of RES) {
      const m = rx.exec(t);
      if (m) { const lat = +m[1], lon = +m[2]; if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return { lat, lon }; }
    }
    return null;
  }
  const mapsLink = (lat, lon) => `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`;
  const fmtCoord = (lat, lon) => `${Number(lat).toFixed(5)}, ${Number(lon).toFixed(5)}`;
  // " (Albuquerque, New Mexico)": the centre's place as the run resolved it, for the automatic location filter.
  const placeLabel = (cp) => { const s = [cp && cp.city, cp && cp.state].filter(Boolean).join(", "); return s ? ` (${s})` : ""; };
  // One line for a geo run's resolved args (runs.args): what was asked, without the centre.
  function describeArgs(a) {
    return [`${a.radius_km ?? 10} km`, (a.categories || []).join(", ") || "no categories",
            a.keywords ? `names ~ ${a.keywords}` : "", (a.tags || []).length ? `${a.tags.length} tag line(s)` : "",
            `finders: ${(a.finders || []).join(", ") || "none"}`,
            a.include ? `include ${a.include}` : "", a.exclude ? `exclude ${a.exclude}` : "",
            a.location ? `location ~ ${a.location}` : `location: auto${placeLabel(a.center_place)}`,
            `≤ ${a.max_places ?? 300} places`, `re-check after ${a.recheck_days ?? 30} d${a.rediscover ? " (all now)" : ""}`,
            a.relookup ? "websites re-resolved" : "",
            `postings from the last ${a.days ?? 7} days`].filter(Boolean).join(" · ");
  }
  function describeSummary(sm) {
    return `${sm.discovered ?? 0} places · ${sm.resolved ?? 0} websites found · ${sm.with_careers ?? 0} careers pages · `
      + `${sm.boards_new ?? 0} new boards · ${sm.boards_scanned ?? 0} scanned`;
  }
  JB.geo = { parseCenter, mapsLink, fmtCoord, describeArgs, describeSummary };

  // ---------------------------------------------------------------- the tab
  const KEY = "jb.geo";
  const remember = (v) => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* private mode etc. */ } };
  const recall = () => { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch { return null; } };
  let meta = null;                                   // /api/geo/categories: categories, finders, services
  let resolved = null;                               // {lat, lon, label} for the centre as typed

  JB.scanDialog.addTab("geo", {
    label: "Geographic",
    html: `<div class="geo">
      <label>Centre<input type="text" id="geoCenter" placeholder="35.0844, -106.6504 · a Google Maps link · an address" autocomplete="off"></label>
      <div class="muted" id="geoResolved">paste coordinates or a Maps link from Google Maps (right-click a spot, copy the coordinates), or an address</div>
      <div class="geo-row">
        <label>Radius <input type="number" id="geoRadius" min="1" max="200" step="0.5" value="10"> km</label>
        <label>Max places <input type="number" id="geoMax" min="1" max="5000" value="300"></label>
        <label>Re-check after <input type="number" id="geoRecheck" min="0" max="365" value="30"> days</label>
        <label class="chk"><input type="checkbox" id="geoRediscover"> re-check every place now</label>
        <label class="chk" title="Resolve every place's website again under the current rules; a changed answer resets its careers page and board"><input type="checkbox" id="geoRelookup"> re-resolve websites</label>
      </div>
      <div><span class="muted">Categories</span> <a href="#" id="geoCatsDefault" class="muted">defaults</a> · <a href="#" id="geoCatsNone" class="muted">none</a><div class="chips" id="geoCats"></div></div>
      <label>Keywords in place names<input type="text" id="geoKeywords" placeholder="optional: bank, credit union (any of)" autocomplete="off"></label>
      <details><summary class="muted">Raw OSM tag lines</summary>
        <textarea id="geoTags" rows="2" placeholder='one per line: shop=bakery   amenity=bank|post_office   ["office"="it"]'></textarea></details>
      <div><span class="muted">Finders</span><div class="chips" id="geoFinders"></div></div>
      <div class="muted" id="geoServices"></div>
      <div class="geo-row">Title filters: include <input type="text" id="geoInclude" placeholder="analyst|account" style="width:11em"> exclude <input type="text" id="geoExclude" placeholder="senior|director" style="width:11em"></div>
      <label title="A careers page is often a nationwide board: only postings whose location matches this regex are kept (postings with no location pass)">Location filter<input type="text" id="geoLocation" placeholder="auto: city and state of the centre" autocomplete="off"></label>
    </div>`,
    mount(panel) {
      const saved = recall() || {};
      const text = { geoCenter: "center", geoRadius: "radius_km", geoMax: "max_places", geoRecheck: "recheck_days",
                     geoKeywords: "keywords", geoTags: "tags", geoInclude: "include", geoExclude: "exclude", geoLocation: "location" };
      Object.entries(text).forEach(([id, k]) => { if (saved[k] != null) $("#" + id).value = saved[k]; });
      const save = () => remember(this.values(panel));
      panel.addEventListener("change", save);
      panel.addEventListener("input", (e) => { if (e.target.matches("textarea, input[type=text]")) save(); });
      $("#geoCatsDefault").addEventListener("click", (e) => { e.preventDefault(); panel.querySelectorAll('input[name="geocat"]').forEach((i) => { i.checked = i.dataset.default === "1"; }); save(); });
      $("#geoCatsNone").addEventListener("click", (e) => { e.preventDefault(); panel.querySelectorAll('input[name="geocat"]').forEach((i) => { i.checked = false; }); save(); });
      let seq = 0;
      const resolve = async () => {
        const t = $("#geoCenter").value.trim(), out = $("#geoResolved"), n = ++seq;
        resolved = null;
        if (!t) { out.textContent = "paste coordinates or a Maps link from Google Maps (right-click a spot, copy the coordinates), or an address"; return; }
        const c = parseCenter(t);
        if (c) { resolved = { ...c, label: fmtCoord(c.lat, c.lon) }; out.innerHTML = `resolved: <b>${fmtCoord(c.lat, c.lon)}</b> · <a href="${mapsLink(c.lat, c.lon)}" target="_blank" rel="noopener">open in Maps</a>`; return; }
        out.textContent = "geocoding…";
        try {
          const d = await get(`/api/geo/center?center=${encodeURIComponent(t)}`);
          if (n !== seq) return;
          resolved = d;
          out.innerHTML = `resolved: <b>${fmtCoord(d.lat, d.lon)}</b> · ${esc(d.label)} · <a href="${mapsLink(d.lat, d.lon)}" target="_blank" rel="noopener">open in Maps</a>`;
        } catch (err) { if (n === seq) out.textContent = `not resolved: ${err.message}`; }
      };
      $("#geoCenter").addEventListener("blur", resolve);
      $("#geoCenter").addEventListener("input", () => { if (parseCenter($("#geoCenter").value)) resolve(); });
      if ($("#geoCenter").value) resolve();
    },
    // Chips come from the server every time the dialog opens (a finder or service may have been
    // configured since); the checked set is what was saved, else the registry's defaults.
    async load(panel) {
      meta = await get("/api/geo/categories");
      const saved = recall() || {};
      const cats = saved.categories || meta.categories.filter((c) => c.default).map((c) => c.key);
      $("#geoCats").innerHTML = meta.categories.map((c) => `<label class="chk"><input type="checkbox" name="geocat" value="${esc(c.key)}" data-default="${c.default ? 1 : 0}" ${cats.includes(c.key) ? "checked" : ""}> ${esc(c.label)}</label>`).join("");
      const finders = saved.finders || meta.finders.filter((f) => f.configured).map((f) => f.name);
      $("#geoFinders").innerHTML = meta.finders.map((f) => `<label class="chk ${f.configured ? "" : "off"}" title="${f.configured ? "" : "not configured in this environment"}">
          <input type="checkbox" name="geofinder" value="${esc(f.name)}" ${f.configured ? "" : "disabled"} ${f.configured && finders.includes(f.name) ? "checked" : ""}> ${esc(f.display_name)}</label>`).join("");
      $("#geoServices").innerHTML = [
        meta.websearch ? "website lookup: web search on" : "website lookup: off (needs SEARXNG_URL; places without a website on the map are skipped)",
        meta.firecrawl ? "careers discovery: homepage links, common paths, Firecrawl site map" : "careers discovery: homepage links and common paths (a Firecrawl site map needs FIRECRAWL_URL)",
      ].map((s) => `<div>${esc(s)}</div>`).join("");
    },
    values(panel) {
      const checked = (name) => [...panel.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
      return { center: $("#geoCenter").value.trim(), radius_km: Number($("#geoRadius").value || 10),
               categories: checked("geocat"), keywords: $("#geoKeywords").value.trim(),
               tags: $("#geoTags").value.split("\n").map((x) => x.trim()).filter(Boolean), finders: checked("geofinder"),
               include: $("#geoInclude").value.trim(), exclude: $("#geoExclude").value.trim(), location: $("#geoLocation").value.trim(),
               max_places: Number($("#geoMax").value || 300), recheck_days: Number($("#geoRecheck").value || 0),
               rediscover: $("#geoRediscover").checked, relookup: $("#geoRelookup").checked };
    },
    // Edit and run on the Scans page: the stored request's args. center_text is what was typed
    // (an address is geocoded again on this interactive path; a Repeat uses the stored coordinates).
    prefill(panel, a) {
      $("#geoCenter").value = a.center_text || a.center || ""; $("#geoRadius").value = a.radius_km ?? 10;
      $("#geoMax").value = a.max_places ?? 300; $("#geoRecheck").value = a.recheck_days ?? 30;
      $("#geoRediscover").checked = !!a.rediscover; $("#geoRelookup").checked = !!a.relookup; $("#geoKeywords").value = a.keywords || "";
      $("#geoTags").value = Array.isArray(a.tags) ? a.tags.join("\n") : (a.tags || "");
      $("#geoInclude").value = a.include || ""; $("#geoExclude").value = a.exclude || ""; $("#geoLocation").value = a.location || "";
      const cats = a.categories || meta.categories.filter((c) => c.default).map((c) => c.key);
      panel.querySelectorAll('input[name="geocat"]').forEach((i) => { i.checked = cats.includes(i.value); });
      const finders = a.finders || meta.finders.filter((f) => f.configured).map((f) => f.name);
      panel.querySelectorAll('input[name="geofinder"]').forEach((i) => { i.checked = !i.disabled && finders.includes(i.value); });
      $("#geoCenter").dispatchEvent(new Event("blur"));
    },
    submit(panel) {
      const v = this.values(panel);
      if (!v.center) throw new Error("geographic scan: a centre is required");
      if (!v.categories.length && !v.tags.length) throw new Error("geographic scan: pick a category or type a tag line");
      if (!v.finders.length) throw new Error("geographic scan: no finder selected");
      const body = { ...v, keywords: v.keywords || undefined, include: v.include || undefined, exclude: v.exclude || undefined, location: v.location || undefined };
      const where = resolved ? resolved.label : v.center;
      return { url: "/api/geo/scan", body, label: `geographic scan ${v.radius_km} km around ${where}` };
    },
  });
})();
