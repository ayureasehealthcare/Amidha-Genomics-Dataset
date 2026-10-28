const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

let rows = [];          // bulk/scrape results
let currentJob = null;  // polling job id

// ---------- helpers ----------
const fmtDur = (s) => {
  if (!s && s !== 0) return "—";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
};
const fmtNum = (n) => (n == null ? "—" : Intl.NumberFormat("en", { notation: "compact" }).format(n));
const fmtDate = (d) => (d && d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : d || "");
const hostName = (u) => { try { return new URL(u).hostname.replace(/^www\.|^m\./, ""); } catch { return ""; } };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function parseUrls(text) {
  const found = text.match(/https?:\/\/[^\s,;"'<>]+|(?:www\.)[^\s,;"'<>]+/gi) || [];
  return [...new Set(found.map((u) => u.replace(/[).\]]+$/, "")))];
}

function setStatus(msg, type = "loading") {
  const el = $("#status");
  if (!msg) { el.hidden = true; return; }
  el.hidden = false;
  el.className = "status" + (type === "error" ? " error" : "");
  el.innerHTML = (type === "loading" ? '<span class="spinner"></span>' : "") + `<span>${esc(msg)}</span>`;
}

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function downloadUrl(url, format) {
  return `/api/download?url=${encodeURIComponent(url)}&format=${encodeURIComponent(format)}`;
}

function triggerDownload(href) {
  const a = document.createElement("a");
  a.href = href; a.download = ""; document.body.appendChild(a); a.click(); a.remove();
}

function saveFile(name, content, type) {
  const blob = new Blob([content], { type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ---------- tabs / theme ----------
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + t.dataset.tab));
}));

const savedTheme = (() => { try { return localStorage.getItem("vf-theme"); } catch { return null; } })();
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
$("#themeBtn").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("vf-theme", next); } catch {}
});
$("#year").textContent = new Date().getFullYear();

// ---------- single ----------
$("#pasteBtn").addEventListener("click", async () => {
  try { $("#singleUrl").value = (await navigator.clipboard.readText()).trim(); } catch { $("#singleUrl").focus(); }
});

$("#singleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#singleUrl").value.trim();
  const box = $("#singleResult");
  box.hidden = true;
  setStatus("Fetching video info…");
  try {
    const info = await api("/api/info", { url });
    setStatus(null);
    renderSingle(info);
  } catch (err) {
    setStatus(err.message, "error");
  }
});

function renderSingle(info) {
  const box = $("#singleResult");
  box.innerHTML = `
    <div><img src="${esc(info.thumbnail || "")}" alt="" referrerpolicy="no-referrer"></div>
    <div>
      <h2>${esc(info.title)}</h2>
      <div class="stats">
        ${info.uploader ? `<span>👤 ${esc(info.uploader)}</span>` : ""}
        <span>🎞 ${esc(info.platform || hostName(info.url))}</span>
        <span>⏱ ${fmtDur(info.duration)}</span>
        ${info.view_count != null ? `<span>👁 ${fmtNum(info.view_count)}</span>` : ""}
        ${info.like_count != null ? `<span>❤ ${fmtNum(info.like_count)}</span>` : ""}
        ${info.upload_date ? `<span>📅 ${fmtDate(info.upload_date)}</span>` : ""}
      </div>
      <div class="fmt-list">
        ${info.formats.map((f) => `
          <div class="fmt">
            <span><b>${esc(f.label)}</b><span class="tag ${f.kind}">${esc(f.ext.toUpperCase())}</span>
              ${f.size ? `<span class="muted small"> ~${esc(f.size)}</span>` : ""}</span>
            <a class="primary btn sm" href="${downloadUrl(info.url, f.id)}" download>⬇</a>
          </div>`).join("")}
      </div>
      <p class="muted small">Large files are prepared on the server first — the download starts when ready.</p>
    </div>`;
  box.hidden = false;
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- bulk ----------
const bulkTa = $("#bulkUrls");
bulkTa.addEventListener("input", () => { $("#bulkCount").textContent = `${parseUrls(bulkTa.value).length} links detected`; });
$("#bulkFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  bulkTa.value = (bulkTa.value ? bulkTa.value + "\n" : "") + parseUrls(await file.text()).join("\n");
  bulkTa.dispatchEvent(new Event("input"));
  e.target.value = "";
});

$("#bulkForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const urls = parseUrls(bulkTa.value);
  if (!urls.length) return setStatus("No valid links found.", "error");
  if (urls.length > 200) return setStatus("Max 200 links at once.", "error");
  setStatus(`Fetching info for ${urls.length} link(s)…`);
  try {
    const { results } = await api("/api/bulk-info", { urls });
    setStatus(null);
    showRows(results.map((r) => ({ ...r, status: r.ok ? "ready" : "error" })), "Bulk results");
  } catch (err) {
    setStatus(err.message, "error");
  }
});

// ---------- scrape ----------
$("#scrapeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#scrapeUrl").value.trim();
  const limit = +$("#scrapeLimit").value;
  setStatus("Scraping playlist / channel… this can take a moment.");
  try {
    const data = await api("/api/scrape", { url, limit });
    setStatus(null);
    if (!data.items.length) return setStatus("No videos found at that URL.", "error");
    showRows(data.items.map((r) => ({ ...r, ok: true, status: "ready" })), data.title || "Scraped videos", data.uploader);
  } catch (err) {
    setStatus(err.message, "error");
  }
});

// ---------- results table ----------
function showRows(list, title, subtitle) {
  rows = list;
  $("#bulkTitle").textContent = title;
  const ok = rows.filter((r) => r.ok).length;
  const totalDur = rows.reduce((a, r) => a + (r.duration || 0), 0);
  $("#bulkMeta").textContent = [subtitle, `${rows.length} items`, ok !== rows.length ? `${rows.length - ok} failed` : "", totalDur ? `total ${fmtDur(totalDur)}` : ""]
    .filter(Boolean).join(" · ");
  $("#jobBar").hidden = true;
  $("#selectAll").checked = true;
  $("#filterInput").value = "";
  renderRows();
  $("#bulkResult").hidden = false;
  $("#bulkResult").scrollIntoView({ behavior: "smooth", block: "start" });
}

function statusPill(r) {
  const map = { ready: ["Ready", ""], queued: ["Queued", "run"], downloading: [`${r.progress ?? 0}%`, "run"], processing: ["Processing", "run"], done: ["Done", "ok"], error: ["Failed", "err"] };
  const [txt, cls] = map[r.status] || [r.status, ""];
  return `<span class="pill ${cls}" title="${esc(r.error || "")}">${esc(txt)}</span>`;
}

function renderRows() {
  const body = $("#bulkBody");
  body.innerHTML = "";
  const tpl = $("#rowTpl");
  rows.forEach((r, i) => {
    const tr = tpl.content.firstElementChild.cloneNode(true);
    tr.dataset.idx = i;
    const sel = $(".sel", tr);
    sel.checked = r.selected !== false && r.ok;
    sel.disabled = !r.ok;
    sel.addEventListener("change", () => { r.selected = sel.checked; });
    const img = $("img", tr);
    if (r.thumbnail) { img.src = r.thumbnail; img.referrerPolicy = "no-referrer"; } else img.style.visibility = "hidden";
    const a = $(".t", tr);
    a.href = r.url; a.textContent = r.title || r.url;
    $(".u", tr).textContent = r.ok ? (r.uploader || hostName(r.url)) : (r.error || "");
    $(".plat", tr).textContent = r.platform || hostName(r.url);
    $(".dur", tr).textContent = fmtDur(r.duration);
    $(".views", tr).textContent = fmtNum(r.view_count);
    $(".st", tr).innerHTML = statusPill(r);
    const dl = $(".dl", tr);
    dl.disabled = !r.ok;
    dl.addEventListener("click", () => {
      if (r.jobFile) return triggerDownload(r.jobFile);
      triggerDownload(downloadUrl(r.url, $("#bulkFormat").value));
    });
    body.appendChild(tr);
  });
  applyFilter();
}

function applyFilter() {
  const q = $("#filterInput").value.toLowerCase();
  $$("#bulkBody tr").forEach((tr) => {
    const r = rows[tr.dataset.idx];
    tr.classList.toggle("hidden", !!q && !(r.title || r.url).toLowerCase().includes(q));
  });
}
$("#filterInput").addEventListener("input", applyFilter);

$("#selectAll").addEventListener("change", (e) => {
  $$("#bulkBody tr:not(.hidden)").forEach((tr) => {
    const r = rows[tr.dataset.idx];
    if (!r.ok) return;
    r.selected = e.target.checked;
    $(".sel", tr).checked = e.target.checked;
  });
});

const selectedRows = () => rows.filter((r) => r.ok && r.selected !== false);

// ---------- exports ----------
const exportFields = ["title", "url", "platform", "uploader", "duration", "view_count", "like_count", "comment_count", "upload_date", "thumbnail", "description"];
$("#exportCsv").addEventListener("click", () => {
  const list = selectedRows().length ? selectedRows() : rows;
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const csv = [exportFields.join(","), ...list.map((r) => exportFields.map((f) => q(r[f])).join(","))].join("\n");
  saveFile("videofetch-export.csv", "﻿" + csv, "text/csv");
});
$("#exportJson").addEventListener("click", () => {
  const list = (selectedRows().length ? selectedRows() : rows).map((r) => Object.fromEntries([...exportFields, "formats"].map((f) => [f, r[f]])));
  saveFile("videofetch-export.json", JSON.stringify(list, null, 2), "application/json");
});
$("#copyLinks").addEventListener("click", async (e) => {
  const list = selectedRows().length ? selectedRows() : rows;
  await navigator.clipboard.writeText(list.map((r) => r.url).join("\n"));
  e.target.textContent = "Copied!";
  setTimeout(() => (e.target.textContent = "Copy links"), 1500);
});

// ---------- bulk download job ----------
$("#downloadSelected").addEventListener("click", async () => {
  const list = selectedRows();
  if (!list.length) return alert("Select at least one video.");
  const format = $("#bulkFormat").value;
  const btn = $("#downloadSelected");
  btn.disabled = true;
  try {
    const job = await api("/api/jobs", {
      items: list.map((r) => ({ url: r.url, format, title: r.title })),
      zip_name: ($("#bulkTitle").textContent || "videofetch").slice(0, 60) + ".zip",
    });
    currentJob = job.id;
    list.forEach((r, i) => { r.jobIdx = i; r.status = "queued"; r.progress = 0; r.jobFile = null; });
    $("#jobBar").hidden = false;
    $("#zipLink").hidden = true;
    pollJob(job.id, list);
  } catch (err) {
    alert(err.message);
    btn.disabled = false;
  }
});

async function pollJob(id, list) {
  if (currentJob !== id) return;
  let job;
  try { job = await api(`/api/jobs/${id}`); } catch { return setTimeout(() => pollJob(id, list), 2000); }
  job.items.forEach((it, i) => {
    const r = list[i];
    r.status = it.status; r.progress = it.progress; r.error = it.error;
    if (it.status === "done") r.jobFile = `/api/jobs/${id}/file/${i}`;
  });
  const pct = job.items.reduce((a, it) => a + (it.status === "done" || it.status === "error" ? 100 : it.progress || 0), 0) / job.total;
  $("#jobProgress").style.width = pct.toFixed(1) + "%";
  $("#jobText").textContent = `${job.completed}/${job.total} processed · ${job.succeeded} succeeded`;
  $$("#bulkBody tr").forEach((tr) => { $(".st", tr).innerHTML = statusPill(rows[tr.dataset.idx]); });

  if (job.status === "finished") {
    $("#downloadSelected").disabled = false;
    if (job.zip_ready) {
      const z = $("#zipLink");
      z.href = `/api/jobs/${id}/zip`; z.hidden = false;
      triggerDownload(z.href);
      $("#jobText").textContent += " · ZIP ready";
    } else {
      $("#jobText").textContent += " · nothing to zip";
    }
    return;
  }
  setTimeout(() => pollJob(id, list), 1200);
}
