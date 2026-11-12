const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mmss = (s) => { s = Math.floor(s || 0); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); };
const size = (b) => b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1e3)) + " KB";
const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
let settingsLoaded = false, lastListKey = "";

async function api(path, opts = {}) {
  if (opts.json !== undefined) { opts.body = JSON.stringify(opts.json); opts.headers = { "Content-Type": "application/json" }; delete opts.json; }
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("Logged out"); }
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
function toast(msg, bad) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add("hidden"), 4500);
}

async function poll() {
  try { render(await api("/api/status")); } catch (e) {}
  setTimeout(poll, 1500);
}

function render(st) {
  const d = st.device, r = st.recording;
  $("#freeNote").classList.toggle("hidden", !st.free_hosting);
  const pill = $("#pill");
  pill.className = "pill " + (r ? "rec" : d.online ? "on" : "off");
  pill.textContent = r ? "● Recording" : d.online ? `ESP32 online · ${d.rssi ?? "?"} dBm` : "ESP32 offline";
  $("#devInfo").innerHTML = d.last_seen
    ? `${esc(d.id)} · mic ${d.mic ? "✔" : "✖"} · Wi-Fi ${esc(d.rssi)} dBm · last seen ${Math.round(Date.now() / 1000 - d.last_seen)}s ago` +
      (d.dropped ? ` · <span class="warn">${d.dropped} audio pieces lost (weak Wi-Fi)</span>` : "")
    : "The ESP32 hasn't connected yet.";
  $("#idle").classList.toggle("hidden", !!r);
  $("#active").classList.toggle("hidden", !r);
  $("#recBadge").classList.toggle("hidden", !r);
  if (r) {
    $("#activeTitle").textContent = "Recording: " + (r.title || "");
    $("#sTime").textContent = $("#recTime").textContent = mmss(r.elapsed);
    $("#sFrames").textContent = r.frames; $("#sAudio").textContent = mmss(r.audio);
  }
  if (!settingsLoaded) {
    $("#fps").value = st.settings.fps; $("#framesize").value = st.settings.framesize; $("#quality").value = st.settings.quality;
    settingsLoaded = true;
  }
  const key = JSON.stringify(st.recordings.map((x) => [x.id, x.status, x.step, x.files]));
  if (key !== lastListKey) { lastListKey = key; renderList(st.recordings); }
}

function renderList(list) {
  if (!list.length) { $("#list").innerHTML = '<p class="muted">Nothing yet. Start a recording above.</p>'; return; }
  $("#list").innerHTML = list.map((x) => {
    const f = (n) => `/api/rec/${x.id}/${encodeURIComponent(n)}`;
    const files = x.files || {};
    let body;
    if (x.status === "ready") {
      body = `<video controls preload="metadata" src="${f(x.video)}?inline=1"></video>
        <div class="dl">
          <a class="btn primary" href="${f(x.video)}">⬇ Video (MP4, ${size(files[x.video] || 0)})</a>
          <a class="btn" href="${f(x.pack)}">⬇ AI pack for Claude (.zip)</a>
          ${x.pdf ? `<a class="btn" href="${f(x.pdf)}">⬇ Slides PDF (${x.slides})</a>` : ""}
          ${x.transcript ? `<a class="btn" href="${f(x.transcript)}">⬇ Transcript</a>` : ""}
          <button class="btn" data-prompt="${x.id}">📋 Copy AI notes prompt</button>
        </div>${x.note ? `<p class="small warn">${esc(x.note)}</p>` : ""}`;
    } else if (x.status === "error") {
      body = `<p class="warn">${esc(x.error)}</p>`;
    } else {
      body = `<p class="muted"><span class="spinner"></span> ${esc(x.step || "Working…")}</p>`;
    }
    return `<div class="rec ${x.status}"><div class="rechead"><div><b>${esc(x.title)}</b>
        <div class="muted small">${when(x.created)}${x.duration ? " · " + mmss(x.duration) : ""}</div></div>
        <button class="del" data-del="${x.id}" title="Delete">🗑</button></div>${body}</div>`;
  }).join("");
}

$("#list").addEventListener("click", async (e) => {
  const del = e.target.dataset.del, pr = e.target.dataset.prompt;
  if (del && confirm("Delete this recording and all its files?")) {
    try { await api(`/api/rec/${del}`, { method: "DELETE" }); lastListKey = ""; } catch (err) { toast(err.message, true); }
  }
  if (pr) {
    const text = await (await fetch(`/api/rec/${pr}/ai_prompt.txt?inline=1`)).text();
    try { await navigator.clipboard.writeText(text); toast("Prompt copied. Paste it into the AI with your files."); }
    catch { $("#modalBox").innerHTML = `<h2>AI notes prompt</h2><textarea rows="16">${esc(text)}</textarea><button onclick="this.closest('.modal').classList.add('hidden')">Close</button>`;
      $("#modal").classList.remove("hidden"); }
  }
});

$("#start").onclick = async () => {
  try { await api("/api/start", { method: "POST", json: { title: $("#title").value } }); toast("Recording started"); }
  catch (e) { toast(e.message, true); }
};
$("#stop").onclick = async () => {
  $("#stop").disabled = true;
  try { await api("/api/stop", { method: "POST" }); toast("Stopped. Making your video…"); $("#title").value = ""; }
  catch (e) { toast(e.message, true); }
  $("#stop").disabled = false;
};
$("#saveSet").onclick = async () => {
  try { await api("/api/settings", { method: "POST", json: { fps: +$("#fps").value, framesize: $("#framesize").value, quality: +$("#quality").value } });
    toast("Camera settings applied"); } catch (e) { toast(e.message, true); }
};

function preview() {
  if (!document.hidden) {
    fetch("/api/preview.jpg", { cache: "no-store" }).then((r) => (r.status === 200 ? r.blob() : null)).then((b) => {
      if (!b) return;
      const u = URL.createObjectURL(b), img = $("#prev");
      img.onload = () => URL.revokeObjectURL(u); img.src = u; $("#prevEmpty").classList.add("hidden");
    }).catch(() => {}).finally(() => setTimeout(preview, 700));
  } else setTimeout(preview, 1500);
}
poll(); preview();
