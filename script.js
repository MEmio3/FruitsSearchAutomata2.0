/* =========================================================
   Fruit Search Bot — Classic script (restored)
   Minimal changes, same vibe, tidy wiring
   ========================================================= */

/* ---------- tiny helpers ---------- */
const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function lines(textareaId) {
  const t = $(textareaId);
  if (!t) return [];
  return t.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
}
function fill(textareaId, arr) {
  const t = $(textareaId);
  if (t) t.value = (arr || []).join("\n");
}
function setStatus(msg) {
  $("#statusMessage").textContent = msg || "—";
}

/* ---------- local UI state ---------- */
const UI = {
  selectedProfiles: [],    // [{name, directory, path}]
  profilesCache: [],       // loaded list for current browser
  levelsMap: {},           // {profileName: level}
  polling: null
};

/* =========================================================
   FRUITS (left card)
   ========================================================= */
async function loadFruits() {
  try {
    const r = await fetch("/api/load");
    const j = await r.json();
    fill("#fruitList", j.fruits || []);
    updateFruitCount();
  } catch {
    alert("Couldn't load fruits.json");
  }
}
async function saveFruits() {
  const fruits = lines("#fruitList");
  try {
    const r = await fetch("/api/save", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ fruits })
    });
    const j = await r.json();
    alert(j.message || "Saved.");
    updateFruitCount();
  } catch {
    alert("Save failed.");
  }
}
function updateFruitCount() {
  const n = lines("#fruitList").length;
  $("#fruitCount").textContent = n;
}

/* =========================================================
   BROWSER / PROFILES (modal + selection)
   ========================================================= */
function onBrowserChange() {
  const label = $("#browserProfileLabel");
  const v = ($("#browserSelect").value || "edge").toUpperCase();
  label.textContent = v.charAt(0) + v.slice(1).toLowerCase();
  // clear selected when browser changes (classic behavior)
  UI.selectedProfiles = [];
  renderSelectedProfilesDisplay();
  // hint load state
  $("#profileInfo").textContent = "Looking for profiles…";
}

async function fetchProfiles(browser) {
  const [pr, lr] = await Promise.all([
    fetch(`/api/profiles/${browser}`),
    fetch("/api/levels")
  ]);
  const pj = await pr.json();
  const lj = await lr.json();
  UI.profilesCache = pj.profiles || [];
  UI.levelsMap = (lj && lj.levels) || {};
}

function openProfileModal() {
  const modal = $("#profileModal");
  const browser = $("#browserSelect").value || "edge";
  $("#modalBrowserName").textContent = browser.charAt(0).toUpperCase() + browser.slice(1);

  (async () => {
    try {
      await fetchProfiles(browser);
      renderProfileList();
      $("#modalInfo").textContent = "Pick profiles and click the badge to toggle Level (L1 = 10 PC, L2 = 32 PC + mobile eligible).";
    } catch {
      $("#modalInfo").textContent = "Couldn't load profiles.";
    }
    modal.classList.add("show");
  })();
}
function closeProfileModal(){ $("#profileModal").classList.remove("show"); }

function renderProfileList() {
  const wrap = $("#profileList");
  wrap.innerHTML = "";
  if (!UI.profilesCache.length) {
    wrap.innerHTML = `<div class="muted">No profiles found. Default will be used when starting.</div>`;
    return;
  }
  for (const p of UI.profilesCache) {
    const chosen = !!UI.selectedProfiles.find(sp => sp.name === p.name);
    const level = UI.levelsMap[p.name] || 1;

    const item = document.createElement("div");
    item.className = "profile-item";

    const left = document.createElement("div");
    left.className = "profile-left";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = chosen;
    cb.addEventListener("change", () => {
      if (cb.checked) {
        UI.selectedProfiles.push({ name: p.name, directory: p.directory || null, path: p.path || null });
      } else {
        UI.selectedProfiles = UI.selectedProfiles.filter(sp => sp.name !== p.name);
      }
    });
    const nm = document.createElement("span");
    nm.textContent = p.name;

    left.appendChild(cb);
    left.appendChild(nm);

    const right = document.createElement("div");
    const badge = document.createElement("button");
    badge.type = "button";
    badge.className = "level-badge";
    badge.dataset.level = String(level);
    badge.textContent = level === 2 ? "L2" : "L1";
    badge.title = "Click to toggle level";
    badge.onclick = async () => {
      const cur = badge.dataset.level === "2" ? 2 : 1;
      const next = cur === 2 ? 1 : 2;
      const ok = await apiSetLevel(p.name, next);
      if (ok) {
        badge.dataset.level = String(next);
        badge.textContent = next === 2 ? "L2" : "L1";
        UI.levelsMap[p.name] = next;
      }
    };

    right.appendChild(badge);

    item.appendChild(left);
    item.appendChild(right);
    wrap.appendChild(item);
  }
}

function selectAllProfiles(){
  UI.selectedProfiles = UI.profilesCache.map(p => ({ name: p.name, directory: p.directory || null, path: p.path || null }));
  renderProfileList();
}
function clearAllProfiles(){
  UI.selectedProfiles = [];
  renderProfileList();
}
function applyProfileSelection(){
  renderSelectedProfilesDisplay();
  closeProfileModal();
}
function renderSelectedProfilesDisplay() {
  const el = $("#selectedProfilesDisplay");
  if (!UI.selectedProfiles.length) {
    el.textContent = "No profiles selected";
    return;
  }
  const names = UI.selectedProfiles.map(p => p.name);
  el.textContent = `${UI.selectedProfiles.length} selected: ${names.join(", ")}`;
}

async function apiSetLevel(profile, level) {
  try {
    const r = await fetch("/api/levels", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ profile, level })
    });
    if (!r.ok) throw new Error();
    return true;
  } catch {
    alert(`Couldn't set level for ${profile}`);
    return false;
  }
}

/* =========================================================
   AI GENERATOR (inside Settings)
   ========================================================= */
async function aiGenerateFromPanel() {
  const prompt = $("#aiPromptInput").value.trim();
  const count  = Math.max(1, Math.min(200, parseInt($("#aiCountInput").value || "30", 10)));
  if (!prompt) { alert("Enter a topic/seed first."); return; }

  const btn = $("#aiGenerateBtn");
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = "Generating…";

  try {
    const r = await fetch("/api/ai-generate", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ prompt, count, save: true })
    });
    const j = await r.json();
    fill("#fruitList", j.fruits || []);
    updateFruitCount();
    alert(`Generated ${ (j.fruits||[]).length } queries via ${j.provider}. Saved to fruits.json.`);
  } catch {
    alert("Generation failed. Check AI provider/key in server settings.");
  } finally {
    btn.disabled = false; btn.textContent = prev;
  }
}

/* =========================================================
   RUN (start / pause / resume / stop)
   ========================================================= */
async function startAutomation() {
  const fruits = lines("#fruitList");
  if (!fruits.length) { alert("Fruit list is empty."); return; }

  const delay = parseFloat($("#delayInput").value || "3");
  const browser = $("#browserSelect").value || "edge";
  const mobileEnabled = $("#mobileToggle").checked;

  // Enable/disable buttons (classic behavior)
  setRunButtons({ started: true });

  try {
    const r = await fetch("/api/start", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        fruits,
        delay,
        browser,
        selectedProfiles: UI.selectedProfiles,
        useDefaultIfNoProfile: UI.selectedProfiles.length === 0,
        mobileEnabled
      })
    });

    if (r.status === 202) {
      const j = await r.json();
      setStatus(j.message || "Started");
      startPolling();
    } else {
      const j = await r.json().catch(()=>({}));
      alert(j.error || "Couldn't start.");
      setRunButtons({ started: false });
    }
  } catch (e) {
    alert("Start failed.");
    setRunButtons({ started: false });
  }
}

async function pauseAutomation(){ await fetch("/api/pause", {method:"POST"}); }
async function resumeAutomation(){ await fetch("/api/resume", {method:"POST"}); }
async function stopAutomation(){
  await fetch("/api/stop", {method:"POST"});
  // UI will flip back to idle once poll sees is_running=false
}

function setRunButtons({ started }) {
  $("#startBtn").disabled  = started;
  $("#pauseBtn").disabled  = !started;
  $("#resumeBtn").disabled = !started;
  $("#stopBtn").disabled   = !started;
}

/* =========================================================
   STATUS / POLLING
   ========================================================= */
function startPolling(){
  if (UI.polling) return;
  UI.polling = setInterval(updateStatus, 1000);
}
function stopPolling(){
  if (UI.polling) { clearInterval(UI.polling); UI.polling = null; }
}

async function updateStatus() {
  try {
    const r = await fetch("/api/status");
    const j = await r.json();

    // progress + labels
    const pct = Math.round(j.progress || 0);
    $("#progressBar").style.width = `${pct}%`;
    $("#progressText").textContent = `${pct}%`;

    const running = !!j.is_running;
    $("#statusIndicator").style.background = running ? "#22c55e" : "#7b86a6";
    $("#statusMessage").textContent = j.status || (running ? "Running…" : "Idle");
    $("#currentSearch").textContent = j.current_search || "";

    // when done, unlock buttons
    if (!running) {
      setRunButtons({ started: false });
      stopPolling();
    }

    // inject per-profile boards (desktop + mobile) under the status panel
    renderProfileBoards(j.profile_progress || {}, j.mobile_progress || {});
  } catch {
    // ignore one miss
  }
}

function renderProfileBoards(desktop, mobile) {
  let host = document.getElementById("profileBoards");
  if (!host) {
    host = document.createElement("div");
    host.id = "profileBoards";
    const statusPanel = document.querySelector(".status-panel");
    statusPanel.appendChild(document.createElement("div")).style.marginTop = "8px";
    statusPanel.appendChild(host);
  }
  host.innerHTML = "";

  const section = (title, map, colorA, colorB) => {
    const entries = Object.entries(map);
    if (!entries.length) return;
    const h = document.createElement("div");
    h.className = "card-title";
    h.style.marginTop = "10px";
    h.textContent = title;
    host.appendChild(h);

    for (const [name, obj] of entries) {
      const row = document.createElement("div");
      row.className = "status-row";
      const label = document.createElement("div");
      label.textContent = `${name}: ${obj.done||0}/${obj.total||0}`;
      label.style.minWidth = "200px";
      const bar = document.createElement("div");
      bar.className = "progress";
      bar.style.height = "8px";
      const span = document.createElement("div");
      span.className = "bar";
      span.style.height = "100%";
      span.style.width = obj.total ? `${Math.min(100,(obj.done/obj.total)*100)}%` : "0%";
      span.style.background = `linear-gradient(90deg, ${colorA}, ${colorB})`;
      bar.appendChild(span);
      row.appendChild(label);
      row.appendChild(bar);
      host.appendChild(row);
    }
  };

  section("Desktop", desktop, "#9aa7ff", "#5b7cff");
  section("Mobile",  mobile,  "#10d97a", "#31e481");
}

/* =========================================================
   POINTS (stub — server disabled in this build)
   ========================================================= */
async function refreshPointsClick() {
  try {
    const r = await fetch("/api/rewards/refresh", { method:"POST" });
    const j = await r.json();
    if (j.error) alert(j.error);
  } catch {
    alert("Points refresh not available in this build.");
  }
}

/* =========================================================
   HEALTH + BOOT
   ========================================================= */
async function loadHealth() {
  try {
    const r = await fetch("/api/health");
    const j = await r.json();
    // reflect server-side mobile count as placeholder (UI field is informational)
    const m = $("#mobileCountInput");
    if (m) m.placeholder = `server: ${j.mobile_search_count ?? "—"}`;
  } catch {}
}

function bind() {
  // basic actions already wired by HTML attributes (classic),
  // but we keep a few live bindings:
  $("#fruitList")?.addEventListener("input", updateFruitCount);
  $("#browserSelect")?.addEventListener("change", onBrowserChange);
}

document.addEventListener("DOMContentLoaded", async () => {
  bind();
  onBrowserChange();
  await Promise.all([loadFruits(), loadHealth()]);
  // don’t auto-load profiles to keep it snappy; user opens the modal to fetch
  setStatus("Ready");
  // start polling only when started
});
