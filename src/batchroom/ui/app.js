/* Batchroom web UI — vanilla JS, no build chain.
   Sections: api client · i18n · router · views · components · sse. */
"use strict";

/* ================= state ================= */
// Blocked site data (privacy settings, some embedded browsers) makes every
// localStorage access throw; that must not take the whole UI down.
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* not persisted */ } },
};
const S = {
  lang: store.get("br_lang") || (navigator.language || "en").slice(0, 2),
  strings: {},
  languages: [],
  apiKey: store.get("br_key") || "",
  authRequired: false,
  me: null,            // /api/auth/me: {authenticated, multiuser, user, role, license}
  view: "process",
  viewArg: null,
  jobs: [],
  recipes: [],
  recipeProblems: [],
  models: [],
  users: [],
  system: null,
  selectedFiles: [],
  selectedRecipe: store.get("br_recipe") || "",
  selectedPriority: 0,
  pullProgress: {},   // model id -> pct
  itemProgress: {},   // "jobId:idx" -> latest step message of a running item
  missingThumbs: new Set(),  // thumbnail URLs known to 404 (e.g. undecodable inputs)
  es: null,
  started: false,
};

const PRO_URL = "https://github.com/ekinarca/batchroom/blob/main/docs/PRO.md";
function hasFeature(name) {
  return !!(S.me && S.me.license && (S.me.license.features || []).includes(name));
}
function isAdmin() { return !S.me || !S.me.multiuser || S.me.role === "admin"; }

/* ================= tiny dom helpers ================= */
const $ = (sel, root) => (root || document).querySelector(sel);
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(c));
  }
  return el;
}

/* ================= i18n ================= */
function t(key, vars) {
  let s = S.strings[key] || key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(v);
  return s;
}
async function loadLang(code) {
  const data = await api(`/api/i18n/${encodeURIComponent(code)}`);
  S.strings = data;
  S.lang = code;
  store.set("br_lang", code);
  const meta = S.languages.find((l) => l.code === code);
  document.documentElement.lang = code;
  document.documentElement.dir = meta && meta.rtl ? "rtl" : "ltr";
}

/* ================= api client ================= */
async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (S.apiKey) headers["X-API-Key"] = S.apiKey;
  let res;
  try {
    res = await fetch(path, { ...opts, headers });
  } catch {
    throw new Error(t("errors.network"));
  }
  if (res.status === 401) {
    let body = null;
    try { body = await res.clone().json(); } catch { /* header-key mode */ }
    handleUnauthorized(body);
    throw new Error(t("auth.invalid"));
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : body.detail?.detail || detail;
    } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res;
}
function handleUnauthorized(body) {
  const login = !!(body && body.detail && body.detail.login_required);
  if (login || (S.me && S.me.multiuser)) showLoginView();
  else showAuthGate(true);
}
function fileUrl(path) {
  return S.apiKey ? `${path}${path.includes("?") ? "&" : "?"}api_key=${encodeURIComponent(S.apiKey)}` : path;
}
// output paths may hold any character a filename can ("#1 50% off.png", "商品.jpg")
const encPath = (p) => p.split("/").map(encodeURIComponent).join("/");

/* ================= toasts & modal ================= */
function toast(msg, kind = "") {
  const el = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 4200);
}
/* One modal at a time. A click on the backdrop or Escape closes it, unless
   it holds work that would be lost (editors, a key shown once); onClose runs
   exactly once however it closes (resolves pending confirms). */
let modalCleanup = null;
let modalDismissable = false;
function closeModal() {
  const cleanup = modalCleanup;
  modalCleanup = null;
  $("#modal-root").replaceChildren();
  if (cleanup) cleanup();
}
function openModal(dialog, { onClose = null, dismissable = true } = {}) {
  closeModal();
  const root = $("#modal-root");
  const opener = document.activeElement;
  const onBackdrop = (e) => { if (e.target === root && modalDismissable) closeModal(); };
  // aria-modal: Tab must cycle inside the dialog, never reach the page behind it
  const onTab = (e) => {
    if (e.key !== "Tab") return;
    const items = [...dialog.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")]
      .filter((el) => !el.disabled && el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    const inside = dialog.contains(document.activeElement);
    if (e.shiftKey && (!inside || document.activeElement === first)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (!inside || document.activeElement === last)) { e.preventDefault(); first.focus(); }
  };
  root.addEventListener("click", onBackdrop);
  document.addEventListener("keydown", onTab);
  modalDismissable = dismissable;
  modalCleanup = () => {
    root.removeEventListener("click", onBackdrop);
    document.removeEventListener("keydown", onTab);
    if (opener && opener.isConnected && opener.focus) opener.focus();
    if (onClose) onClose();
  };
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.tabIndex = -1;
  root.replaceChildren(dialog);
  dialog.focus();  // callers may move it on to a specific control
  return dialog;
}
function confirmModal(message) {
  return new Promise((resolve) => {
    let answer = false;
    const ok = h("button", { class: "btn sig", onclick: () => { answer = true; closeModal(); } }, t("common.ok"));
    openModal(
      h("div", { class: "modal" },
        h("p", {}, message),
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: closeModal }, t("common.cancel")),
          ok,
        ),
      ),
      { onClose: () => resolve(answer) },
    );
    ok.focus();
  });
}

/* ---------- onboarding tour ---------- */
const TOUR_KEY = "br_tour_v1";
function showTour() {
  const step = (n, key) => h("div", { class: "tour-step" },
    h("span", { class: "tour-num" }, String(n)),
    h("p", {}, t(key)));
  const cta = h("button", { class: "btn sig", onclick: closeModal }, t("tour.cta"));
  openModal(
    h("div", { class: "modal tour" },
      h("h2", {}, t("tour.title")),
      h("p", { class: "sub" }, t("tour.intro")),
      step(1, "tour.step1"), step(2, "tour.step2"), step(3, "tour.step3"),
      h("div", { class: "row", style: "justify-content:flex-end;margin-top:16px" }, cta),
    ),
  );
  cta.focus();
}
function maybeShowTour() {
  if (store.get(TOUR_KEY)) return;
  store.set(TOUR_KEY, "1"); // first sighting counts; reopen lives in the sidebar
  showTour();
}

/* ================= router ================= */
const VIEWS = ["process", "queue", "recipes", "models", "users", "watch", "system"];
function viewVisible(view) {
  if (view !== "users") return true;
  return isAdmin() && (hasFeature("multi_user") || (S.me && S.me.multiuser));
}
function route() {
  // navigating anywhere dismisses any open drawer/modal (they must never stack)
  document.querySelectorAll(".drawer, .drawer-back").forEach((el) => el.remove());
  closeModal();
  const hash = location.hash.replace(/^#\/?/, "");
  const [view, arg] = hash.split("/");
  S.view = VIEWS.includes(view) && viewVisible(view) ? view : "process";
  try {
    S.viewArg = arg ? decodeURIComponent(arg) : null;
  } catch {  // a malformed link (#/queue/%E0) must not abort boot
    S.viewArg = null;
  }
  render();
}
function nav(to) { location.hash = `#/${to}`; }

/* ================= renderers ================= */
function render() {
  renderNav();
  const main = $("#main");
  main.replaceChildren();
  const fn = {
    process: viewProcess, queue: viewQueue, recipes: viewRecipes,
    models: viewModels, users: viewUsers, watch: viewWatch, system: viewSystem,
  }[S.view];
  fn(main);
}

function renderNavLinks() {
  const running = S.jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  $("#nav").replaceChildren(
    ...VIEWS.filter(viewVisible).map((v) =>
      h("a", { href: `#/${v}`, class: S.view === v ? "active" : "" },
        h("span", { class: "nav-dot" }),
        t(`nav.${v}`),
        v === "queue" && running ? h("span", { class: "nav-count" }, String(running)) : null,
      ),
    ),
  );
}

function renderNav() {
  renderNavLinks();
  const sel = $("#lang-select");
  sel.replaceChildren(...S.languages.map((l) =>
    h("option", { value: l.code, selected: l.code === S.lang }, l.name)));
  sel.setAttribute("aria-label", t("system.language"));
  $("#tour-link").textContent = t("tour.reopen");
  const foot = $("#user-box");
  if (foot) {
    foot.replaceChildren();
    if (S.me && S.me.authenticated && S.me.multiuser && S.me.user) {
      foot.append(
        h("div", { class: "user-line", title: S.me.user.email },
          h("span", { class: "faint", style: "font-size:11px" }, S.me.user.email)),
        h("button", { class: "btn small ghost", style: "width:100%", onclick: async () => {
          try { await api("/api/auth/logout", { method: "POST" }); } catch { /* gone anyway */ }
          location.reload();
        } }, t("auth.logout")),
      );
    }
  }
}

/* ---------- view: process ---------- */
function viewProcess(main) {
  const recipeSel = h("select", {
    id: "recipe-select",
    onchange: (e) => { S.selectedRecipe = e.target.value; store.set("br_recipe", e.target.value); },
  }, ...S.recipes.map((r) =>
    h("option", { value: r.name, selected: r.name === S.selectedRecipe }, `${r.name} — ${r.description}`.slice(0, 90))));

  const fileInput = h("input", { type: "file", multiple: true, accept: "image/*,.heic,.avif", hidden: true,
    onchange: (e) => { addFiles([...e.target.files]); e.target.value = ""; } });
  const folderInput = h("input", { type: "file", multiple: true, webkitdirectory: true, hidden: true,
    onchange: (e) => { addFiles([...e.target.files]); e.target.value = ""; } });

  const dz = h("div", { class: "dropzone", tabindex: "0", role: "button",
    onclick: () => fileInput.click(),
    onkeydown: (e) => { if (e.target === dz && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); fileInput.click(); } },
    ondragover: (e) => { e.preventDefault(); dz.classList.add("drag"); },
    ondragleave: () => dz.classList.remove("drag"),
    ondrop: async (e) => {
      e.preventDefault(); dz.classList.remove("drag");
      addFiles(await filesFromDrop(e.dataTransfer));
    },
  },
    h("div", { class: "hint" }, t("process.drop_hint")),
    h("div", { class: "subhint" }, t("process.drop_sub")),
    h("button", { type: "button", class: "btn small ghost", style: "margin-top:12px",
      onclick: (e) => { e.stopPropagation(); folderInput.click(); } }, t("process.choose_folder")),
  );

  const cards = h("div", { class: "filecards", id: "filecards" });
  const webhook = h("input", { type: "url", id: "webhook-url", placeholder: t("process.webhook_placeholder") });
  const startBtn = h("button", { class: "btn sig", onclick: submitJob }, t("process.start"));
  const clearBtn = h("button", { class: "btn ghost", onclick: () => { S.selectedFiles = []; renderFileCards(); } }, t("process.clear"));
  const counter = h("span", { class: "dim", id: "file-counter" });

  let priorityBlock = null;
  if (hasFeature("priority_queue")) {
    const prioSel = h("select", { id: "priority-select",
      onchange: (e) => { S.selectedPriority = Number(e.target.value); } },
      h("option", { value: "0", selected: S.selectedPriority === 0 }, t("process.priority.normal")),
      h("option", { value: "5", selected: S.selectedPriority === 5 }, t("process.priority.high")),
      h("option", { value: "10", selected: S.selectedPriority === 10 }, t("process.priority.urgent")),
    );
    priorityBlock = h("div", { style: "margin-bottom:14px" },
      h("label", {}, t("process.priority_label"), " ", h("span", { class: "chip sig" }, "Pro")),
      prioSel);
  }

  main.append(
    h("h1", {}, t("process.title")),
    h("p", { class: "sub" }, t("app.tagline")),
    h("div", { class: "panel" },
      h("div", { class: "row", style: "margin-bottom:14px" },
        h("div", { style: "flex:1;min-width:min(260px,100%)" }, h("label", {}, t("process.recipe_label")), recipeSel),
      ),
      dz, fileInput, folderInput, cards,
      h("hr", { class: "sep" }),
      h("div", { style: "margin-bottom:14px" }, h("label", {}, t("process.webhook_label")), webhook),
      priorityBlock,
      h("div", { class: "row" }, startBtn, clearBtn, counter),
    ),
  );
  renderFileCards();
}

const IMAGE_RE = /\.(jpe?g|png|webp|heic|heif|avif|tiff?|bmp|gif)$/i;
// hidden and NAS system folders (Synology @eaDir thumbnails...) are never inputs
const SYSTEM_SEGMENT_RE = /^[.@]|^#(recycle|snapshot)$/i;

/* Files from a drop, descending into dropped folders. Entries must be taken
   synchronously, while the drop event is still being dispatched. */
async function filesFromDrop(dt) {
  const entries = [...(dt.items || [])]
    .map((item) => (item.kind === "file" && item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (!entries.length) return [...dt.files];
  const out = [];
  const walk = async (entry) => {
    if (SYSTEM_SEGMENT_RE.test(entry.name)) return;
    if (entry.isFile) {
      try {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        file._brPath = entry.fullPath;
        out.push(file);
      } catch { /* unreadable: skip */ }
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {  // readEntries returns at most ~100 entries per call
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject)).catch(() => []);
        if (!batch.length) break;
        for (const child of batch) await walk(child);
      }
    }
  };
  for (const entry of entries) await walk(entry);
  return out;
}

const fileKey = (f) => `${f._brPath || f.webkitRelativePath || f.name}|${f.size}|${f.lastModified}`;

function addFiles(files) {
  const known = new Set(S.selectedFiles.map(fileKey));
  let skipped = 0;
  for (const f of files) {
    if ((f.webkitRelativePath || "").split("/").slice(0, -1).some((s) => SYSTEM_SEGMENT_RE.test(s))) continue;
    if (f.name.startsWith(".")) continue;
    if (!IMAGE_RE.test(f.name)) { skipped++; continue; }
    const key = fileKey(f);
    if (known.has(key)) continue;  // the same folder dropped twice
    known.add(key);
    S.selectedFiles.push(f);
  }
  if (skipped) toast(t("process.skipped_files", { n: skipped }));
  renderFileCards();
}

// a 500-photo batch must not decode 500 full-size previews at once
const PREVIEW_MAX = 60;
let previewUrls = [];
function renderFileCards() {
  const wrap = $("#filecards");
  previewUrls.forEach((u) => URL.revokeObjectURL(u));
  previewUrls = [];
  if (!wrap) return;
  const cards = S.selectedFiles.slice(0, PREVIEW_MAX).map((f, i) => {
    const img = h("img", { alt: "", loading: "lazy" });
    if (/\.(jpe?g|png|webp|gif|bmp)$/i.test(f.name)) {
      img.src = URL.createObjectURL(f);
      previewUrls.push(img.src);
    }
    return h("div", { class: "filecard" },
      img,
      h("button", { class: "rm", title: t("common.delete"), onclick: () => { S.selectedFiles.splice(i, 1); renderFileCards(); } }, "✕"),
      h("div", { class: "name", title: f.name }, f.name),
    );
  });
  const hidden = S.selectedFiles.length - PREVIEW_MAX;
  if (hidden > 0) cards.push(h("div", { class: "filecard more" }, h("span", {}, t("process.more_files", { n: hidden }))));
  wrap.replaceChildren(...cards);
  const counter = $("#file-counter");
  if (counter) counter.textContent = S.selectedFiles.length ? t("process.files_selected", { n: S.selectedFiles.length }) : "";
}

async function submitJob() {
  if (!S.selectedFiles.length) return toast(t("process.no_files"), "error");
  const form = new FormData();
  form.append("recipe", S.selectedRecipe || (S.recipes[0] && S.recipes[0].name) || "");
  const webhook = $("#webhook-url")?.value.trim();
  if (webhook) form.append("webhook_url", webhook);
  if (S.selectedPriority) form.append("priority", String(S.selectedPriority));
  for (const f of S.selectedFiles) form.append("files", f, f.name);
  const btns = document.querySelectorAll(".btn");
  btns.forEach((b) => (b.disabled = true));
  try {
    const xhr = new XMLHttpRequest();
    const done = new Promise((resolve, reject) => {
      xhr.open("POST", "/api/jobs");
      if (S.apiKey) xhr.setRequestHeader("X-API-Key", S.apiKey);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const pct = Math.round((e.loaded / e.total) * 100);
          const c = $("#file-counter");
          if (c) c.textContent = t("process.uploading", { pct });
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
        else {
          let body = null;
          try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
          if (xhr.status === 401) handleUnauthorized(body);
          let msg = xhr.statusText;
          if (body) msg = typeof body.detail === "string" ? body.detail : body.detail?.detail || msg;
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => reject(new Error(t("errors.network")));
      xhr.send(form);
    });
    const job = await done;
    toast(t("process.submitted", { id: job.id }), "ok");
    S.selectedFiles = [];
    renderFileCards();  // also releases the preview object URLs
    await refreshJobs();
    nav("queue");
  } catch (err) {
    toast(t("errors.upload_failed", { msg: err.message }), "error");
    renderFileCards();  // the counter still read "Uploading… 100%"
  } finally {
    btns.forEach((b) => (b.disabled = false));
  }
}

/* ---------- view: queue ---------- */
function viewQueue(main) {
  main.append(h("h1", {}, t("queue.title")));
  if (!S.jobs.length) {
    main.append(h("div", { class: "empty" }, t("queue.empty")));
    return;
  }
  const grid = h("div", { class: "grid cols-2" });
  for (const job of S.jobs) grid.append(jobCard(job));
  main.append(grid);
  if (S.viewArg) openJobDrawer(S.viewArg);
}

function statusChip(status) {
  const map = { running: "warn pulse", queued: "", done: "ok", cancelled: "", failed: "sig", pending: "" };
  return h("span", { class: `chip ${map[status] ?? ""}` }, h("span", { class: "dot" }), t(`status.${status}`) );
}

// API timestamps are UTC (ISO 8601 with offset): show them in the viewer's zone
function localTime(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return iso || "";
  try {
    return d.toLocaleString(S.lang, { dateStyle: "short", timeStyle: "medium" });
  } catch {  // a language tag the browser doesn't know
    return d.toLocaleString();
  }
}

function jobCard(job) {
  const total = job.total || 1;
  const pct = (n) => `${(100 * n) / total}%`;
  const running = Math.max(0, total - job.done - job.failed - job.cancelled);
  return h("div", { class: "panel jobcard", tabindex: "0", role: "link",
    onclick: () => nav(`queue/${job.id}`),
    onkeydown: (e) => { if (e.key === "Enter") nav(`queue/${job.id}`); } },
    h("div", { class: "row spread" },
      h("h3", {}, job.id),
      statusChip(job.status),
    ),
    h("div", { class: "row", style: "margin-top:6px" },
      h("span", { class: "chip" }, job.recipe),
      h("span", { class: "faint mono" }, `${job.done + job.failed + job.cancelled}/${job.total}`),
      h("span", { class: "faint", dir: "auto" }, localTime(job.created_at)),
      h("span", { class: "chip" }, job.source),
    ),
    h("div", { class: "bar" },
      h("i", { class: "b-done", style: `width:${pct(job.done)}` }),
      h("i", { class: "b-failed", style: `width:${pct(job.failed)}` }),
      h("i", { class: "b-cancelled", style: `width:${pct(job.cancelled)}` }),
      job.status === "running" ? h("i", { class: "b-run", style: `width:${pct(running)}` }) : null,
    ),
  );
}

/* The drawer re-renders on every live update: the open drawer keeps its
   element (so its scroll position and no re-played slide-in) and only its
   content is swapped; a response that lost the race to a newer one is dropped. */
let drawerSeq = 0;
function closeDrawer() {
  drawerSeq++;
  document.querySelectorAll(".drawer, .drawer-back").forEach((el) => el.remove());
  if (S.view === "queue" && S.viewArg) nav("queue");
}

async function openJobDrawer(jobId) {
  const seq = ++drawerSeq;
  let job;
  try { job = await api(`/api/jobs/${encodeURIComponent(jobId)}`); }
  catch (err) {
    if (seq === drawerSeq) { toast(err.message, "error"); nav("queue"); }
    return;
  }
  if (seq !== drawerSeq || S.view !== "queue" || S.viewArg !== jobId) return;
  const content = drawerContent(job);
  const current = document.querySelector(".drawer");
  if (current && current.dataset.job === job.id) {
    current.replaceChildren(...content);
    return;
  }
  document.querySelectorAll(".drawer, .drawer-back").forEach((el) => el.remove());
  const drawer = h("div", { class: "drawer", role: "dialog", "aria-modal": "true", dataset: { job: job.id } }, ...content);
  document.body.append(h("div", { class: "drawer-back", onclick: closeDrawer }), drawer);
  drawer.querySelector("button")?.focus();  // its close button
}

function drawerContent(job) {
  const actions = h("div", { class: "row" });
  if (job.status === "running" || job.status === "queued") {
    actions.append(h("button", { class: "btn small", onclick: async () => {
      if (await confirmModal(t("queue.cancel_confirm"))) {
        try { await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }); toast(t("common.ok"), "ok"); closeDrawer(); refreshJobs(); }
        catch (err) { toast(err.message, "error"); }
      }
    } }, t("common.cancel")));
  } else {
    actions.append(
      h("a", { class: "btn small", href: fileUrl(`/api/jobs/${job.id}/archive`) }, t("queue.download_zip")),
      h("a", { class: "btn small ghost", href: fileUrl(`/api/jobs/${job.id}/manifest`), target: "_blank", rel: "noopener" }, t("queue.open_manifest")),
    );
    // watch-folder sources already moved to failed/: those retry by dropping them again
    const retryable = job.source === "watch" ? 0 : job.failed + job.cancelled;
    if (retryable > 0) {
      actions.append(h("button", { class: "btn small", onclick: async () => {
        try {
          const again = await api(`/api/jobs/${job.id}/retry`, { method: "POST" });
          S.missingThumbs.clear();  // retried items may now produce them
          toast(t("queue.retry_done", { n: again.total - again.done - again.failed - again.cancelled }), "ok");
          await refreshJobs();
          if (S.view === "queue") render();
        } catch (err) { toast(err.message, "error"); }
      } }, t("queue.retry", { n: retryable })));
    }
    actions.append(h("button", { class: "btn small danger", onclick: async () => {
      if (await confirmModal(t("queue.delete_confirm"))) {
        try { await api(`/api/jobs/${job.id}`, { method: "DELETE" }); closeDrawer(); refreshJobs(); }
        catch (err) { toast(err.message, "error"); }
      }
    } }, t("common.delete")));
  }

  const rows = job.items.map((item) => {
    const result = item.result;
    const compare = () => item.status === "done" && openCompare(job, item);
    // thumbnails exist once an item started ("in") / finished ("out"): don't
    // fire a 404 per pending item on every live refresh of a big batch
    const thumb = (kind) => {
      const src = fileUrl(`/api/jobs/${job.id}/thumb/${item.idx}/${kind}`);
      const exists = kind === "out" ? item.status === "done"
        : ["running", "done", "failed"].includes(item.status);
      if (!exists || S.missingThumbs.has(src)) return h("span", { class: "thumb-empty" });
      return h("img", {
        src, alt: "", loading: "lazy",
        class: item.status === "done" ? "clickable" : "",
        onerror: (e) => { S.missingThumbs.add(src); e.target.style.visibility = "hidden"; },
        onclick: compare,
      });
    };
    const dur = result ? `${(result.duration_ms / 1000).toFixed(1)}s` : "";
    const key = `${job.id}:${item.idx}`;
    return h("tr", {},
      h("td", {}, h("div", { class: "thumb-pair" }, thumb("in"), thumb("out"))),
      h("td", {}, h("div", { class: "mono" }, item.source_name),
        item.error && item.status !== "cancelled" ? h("div", { class: "err-text" }, item.error) : null),
      h("td", {}, statusChip(item.status),
        item.status === "running"
          ? h("div", { class: "step-text", dataset: { progress: key } }, S.itemProgress[key] || "")
          : null),
      h("td", { class: "mono dim" }, dur),
      h("td", {}, ...(result?.outputs || []).slice(0, 6).map((o) =>
        h("a", { class: "chip", style: "text-decoration:none;margin-inline-end:4px",
          href: fileUrl(`/api/jobs/${job.id}/outputs/${encPath(o.path)}`), download: "" }, o.preset || o.format))),
    );
  });

  return [
    h("div", { class: "row spread" },
      h("h1", { class: "mono", style: "font-size:16px" }, job.id),
      h("button", { class: "btn small ghost", onclick: closeDrawer }, t("common.close")),
    ),
    h("div", { class: "row", style: "margin:10px 0 16px" },
      statusChip(job.status),
      h("span", { class: "chip" }, job.recipe),
      h("span", { class: "chip" }, `${t("queue.items")}: ${job.total}`),
      job.skipped ? h("span", { class: "chip warn" }, t("queue.skipped_note", { n: job.skipped })) : null,
    ),
    actions,
    h("hr", { class: "sep" }),
    h("table", { class: "items" },
      h("thead", {}, h("tr", {},
        h("th", {}, `${t("queue.before")} / ${t("queue.after")}`),
        h("th", {}, t("queue.source")),
        h("th", {}, t("queue.status")),
        h("th", {}, t("queue.duration")),
        h("th", {}, t("queue.outputs")),
      )),
      h("tbody", {}, ...rows),
    ),
  ];
}

function openCompare(job, item) {
  // the original drives the frame's aspect; both layers letterbox inside it
  const before = h("img", {
    src: fileUrl(`/api/jobs/${job.id}/thumb/${item.idx}/in`), alt: t("queue.before"),
    onload: (e) => {
      const im = e.currentTarget;
      if (im.naturalWidth && im.naturalHeight) {
        box.style.setProperty("--compare-ar", `${im.naturalWidth} / ${im.naturalHeight}`);
      }
    },
  });
  const after = h("img", { src: fileUrl(`/api/jobs/${job.id}/thumb/${item.idx}/out`), alt: t("queue.after") });
  const afterWrap = h("div", { class: "after-wrap" }, after);
  const divider = h("div", { class: "divider" });
  const box = h("div", { class: "compare" }, before, afterWrap, divider,
    h("span", { class: "tag before" }, t("queue.before")),
    h("span", { class: "tag after" }, t("queue.after")));
  let pos = 0.5;  // divider position, measured from the left edge
  const apply = () => {
    const rtl = document.documentElement.dir === "rtl";
    const leftPct = pos * 100;
    // "after" sits on the inline-end side: right of the divider in LTR, left of it in RTL
    afterWrap.style.clipPath = rtl
      ? `inset(0 ${100 - leftPct}% 0 0)`
      : `inset(0 0 0 ${leftPct}%)`;
    divider.style.insetInlineStart = `${rtl ? 100 - leftPct : leftPct}%`;
  };
  const move = (clientX) => {
    const rect = box.getBoundingClientRect();
    pos = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    apply();
  };
  box.addEventListener("pointerdown", (e) => { box.setPointerCapture(e.pointerId); move(e.clientX); });
  box.addEventListener("pointermove", (e) => { if (e.buttons) move(e.clientX); });
  apply();
  openModal(h("div", { class: "modal", style: "max-width:520px" },
    box,
    h("div", { class: "row", style: "margin-top:14px;justify-content:flex-end" },
      h("button", { class: "btn", onclick: closeModal }, t("common.close")))));
}

/* ---------- view: recipes ---------- */
function viewRecipes(main) {
  main.append(
    h("h1", {}, t("recipes.title")),
    h("p", { class: "sub" }, t("recipes.intro")),
  );
  if (S.recipeProblems.length) {
    main.append(h("div", { class: "problems" },
      h("strong", {}, t("recipes.problems")),
      ...S.recipeProblems.map((p) => h("div", {}, p))));
  }
  const grid = h("div", { class: "grid cols-2", style: "margin-top:14px" });
  for (const r of S.recipes) {
    const steps = [];
    r.steps.forEach((s, i) => {
      if (i) steps.push(h("span", { class: "arrow" }, "→"));
      steps.push(h("span", { class: "chip" }, s));
    });
    grid.append(h("div", { class: "panel recipecard" },
      h("div", { class: "row spread" },
        h("h3", {}, r.name),
        h("span", { class: `chip ${r.builtin ? "" : "sig"}` }, r.builtin ? t("recipes.builtin") : t("recipes.user")),
      ),
      h("p", { class: "dim", style: "margin:6px 0 0;font-size:12.5px" }, r.description),
      h("div", { class: "steps" }, steps),
      h("div", { class: "row" },
        r.models.length ? h("span", { class: "faint", style: "font-size:12px" }, `${t("recipes.models")}: ${r.models.join(", ")}`) : null,
        h("button", { class: "btn small right", onclick: () => openRecipeEditor(r.name) },
          t(isAdmin() ? "recipes.edit" : "recipes.view")),
        // recipes are shared by every user: only admins change them
        isAdmin() && !r.builtin ? h("button", { class: "btn small danger", onclick: async () => {
          if (await confirmModal(t("recipes.delete_confirm"))) {
            try { await api(`/api/recipes/${encodeURIComponent(r.name)}`, { method: "DELETE" }); }
            catch (err) { toast(err.message, "error"); }
            refreshRecipes().then(render);
          }
        } }, t("common.delete")) : null,
      ),
    ));
  }
  main.append(grid);
  if (isAdmin()) {
    main.append(h("div", { class: "row", style: "margin-top:16px" },
      h("button", { class: "btn sig", onclick: () => openRecipeEditor(null) }, t("recipes.new"))));
  }
}

async function openRecipeEditor(name) {
  let source = `name: my-recipe\ndescription: What it does\nsteps:\n  - op: remove-background\n    model: auto\n  - op: preset-pack\n    presets:\n      - { name: web, size: 1600x1600, format: jpg, quality: 90 }\noutput:\n  structure: per-preset-folder\n`;
  let builtin = false;
  if (name) {
    try {
      const r = await api(`/api/recipes/${encodeURIComponent(name)}`);
      source = r.source; builtin = r.builtin;
    } catch (err) { toast(err.message, "error"); return; }
  }
  const editable = isAdmin();
  const ta = h("textarea", { rows: 18, spellcheck: "false", dir: "ltr", readonly: !editable }, source);
  const status = h("div", { class: "dim", style: "min-height:1.4em;font-size:12.5px" });
  const validate = async () => {
    try {
      const res = await api("/api/recipes/validate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: ta.value }),
      });
      status.textContent = res.valid ? `✓ ${t("recipes.valid")}` : `✗ ${res.error}`;
      status.style.color = res.valid ? "var(--ok)" : "var(--sig-ink)";
      return res.valid;
    } catch (err) { status.textContent = err.message; return false; }
  };
  const close = closeModal;
  openModal(h("div", { class: "modal", style: "max-width:720px" },
    h("h1", { style: "font-size:16px;margin-bottom:10px" }, t("recipes.editor_title")),
    builtin && editable ? h("p", { class: "dim", style: "font-size:12.5px" }, t("recipes.builtin_note")) : null,
    ta,
    h("div", { class: "row", style: "margin-top:10px" },
      status,
      h("button", { class: "btn small right", onclick: validate }, t("recipes.validate")),
      !editable ? null : h("button", { class: "btn small sig", onclick: async () => {
        if (!(await validate())) return;
        try {
          await api("/api/recipes", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: ta.value }) });
          toast(t("common.ok"), "ok"); close(); refreshRecipes().then(render);
        } catch (err) { toast(err.message, "error"); }
      } }, t("recipes.save")),
      h("button", { class: "btn small ghost", onclick: close }, t("common.close")),
    ),
  ), { dismissable: false });  // a stray click must not throw away unsaved YAML
}

/* ---------- view: models ---------- */
function viewModels(main) {
  main.append(
    h("h1", {}, t("models.title")),
    h("p", { class: "sub" }, t("models.intro")),
  );
  const grid = h("div", { class: "grid cols-3" });
  for (const m of S.models) {
    const pct = S.pullProgress[m.id];
    const size = m.size_bytes ? `${Math.round(m.size_bytes / 1048576)} MB` : "?";
    const stateChip = m.downloaded
      ? h("span", { class: "chip ok" }, t("models.downloaded"))
      : (pct != null || m.downloading)
        ? h("span", { class: "chip warn pulse" }, h("span", { class: "dot" }), t("models.downloading", { pct: pct ?? 0 }))
        : h("span", { class: "chip" }, t("models.not_downloaded"));
    const actions = h("div", { class: "row" });
    if (!m.downloaded && pct == null && !m.downloading) {
      actions.append(h("button", { class: "btn small sig", onclick: () => pullModel(m) }, t("models.download")));
      if (!m.pinned) actions.append(h("span", { class: "chip warn" }, t("models.unpinned")));
    }
    if (m.downloaded) {
      actions.append(
        h("button", { class: "btn small", onclick: async () => {
          try { const r = await api(`/api/models/${m.id}/verify`); toast(`${m.id}: ${r.detail}`, r.ok ? "ok" : "error"); }
          catch (err) { toast(err.message, "error"); }
        } }, t("models.verify")),
        isAdmin() ? h("button", { class: "btn small danger", onclick: async () => {
          if (await confirmModal(t("models.delete_confirm"))) {
            try { await api(`/api/models/${m.id}`, { method: "DELETE" }); }
            catch (err) { toast(err.message, "error"); }
            refreshModels().then(render);
          }
        } }, t("common.delete")) : null,
      );
    }
    grid.append(h("div", { class: "panel modelcard" },
      h("div", { class: "row spread" },
        h("h3", { class: "mono" }, m.id),
        m.default ? h("span", { class: "chip sig" }, t("models.default_badge")) : null,
      ),
      h("div", { class: "faint", style: "font-size:12px;margin-bottom:6px" }, t(`models.task.${m.task}`)),
      h("p", { class: "desc" }, m.description),
      h("div", { class: "meta" },
        h("a", { class: "chip", href: m.license_url, target: "_blank", rel: "noopener", style: "text-decoration:none" }, `${t("models.license")}: ${m.license}`),
        h("span", { class: "chip" }, `${t("models.size")}: ${size}`),
      ),
      pct != null ? h("div", { class: "bar" }, h("i", { class: "b-run", style: `width:${pct}%` })) : null,
      h("div", { class: "row spread" }, stateChip, actions),
    ));
  }
  main.append(grid);
}

async function pullModel(m) {
  try {
    S.pullProgress[m.id] = 0;
    render();
    const r = await api(`/api/models/${m.id}/pull`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_unpinned: !m.pinned }),
    });
    if (r.status === "already-downloaded") {  // no progress events will follow
      delete S.pullProgress[m.id];
      await refreshModels();
      if (S.view === "models") render();
    }
  } catch (err) {
    delete S.pullProgress[m.id];
    toast(err.message, "error");
    render();
  }
}

/* ---------- view: users (Pro, admin) ---------- */
async function refreshUsers() {
  try { const d = await api("/api/users"); S.users = d.users; S.usersSeats = d.seats; }
  catch { S.users = []; }
}

function viewUsers(main) {
  main.append(h("h1", {}, t("users.title")), h("p", { class: "sub" }, t("users.intro")));
  const licensed = hasFeature("multi_user");
  if (!licensed) {
    main.append(h("div", { class: "problems" }, t("users.locked_note"), " ",
      h("a", { href: PRO_URL, target: "_blank", rel: "noopener" }, t("license.get_pro"))));
  }
  const wrap = h("div");
  main.append(wrap);

  const draw = () => {
    wrap.replaceChildren(
      h("div", { class: "panel" },
        h("div", { class: "row spread", style: "margin-bottom:10px" },
          h("strong", {}, `${S.users.length} / ${S.usersSeats ?? "-"} ${t("license.seats")}`),
          licensed ? h("button", { class: "btn small sig", onclick: () => userEditor(null, draw) }, t("users.new_title")) : null,
        ),
        h("table", { class: "items" },
          h("thead", {}, h("tr", {},
            h("th", {}, t("users.email")), h("th", {}, t("users.name")),
            h("th", {}, t("users.role")), h("th", {}, t("queue.status")), h("th", {}, t("queue.actions")))),
          h("tbody", {}, ...S.users.map((u) => h("tr", {},
            h("td", { class: "mono" }, u.email),
            h("td", {}, u.name || "—"),
            h("td", {}, h("span", { class: `chip ${u.role === "admin" ? "sig" : ""}` }, t(`users.role.${u.role}`))),
            h("td", {}, u.disabled ? h("span", { class: "chip warn" }, t("users.disabled")) : h("span", { class: "chip ok" }, t("users.active"))),
            h("td", {}, licensed ? h("div", { class: "row" },
              h("button", { class: "btn small", onclick: async () => {
                try {
                  await api(`/api/users/${u.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ role: u.role === "admin" ? "member" : "admin" }) });
                  await refreshUsers(); draw();
                } catch (err) { toast(err.message, "error"); }
              } }, u.role === "admin" ? t("users.role.member") : t("users.role.admin")),
              h("button", { class: "btn small", onclick: async () => {
                try {
                  await api(`/api/users/${u.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ disabled: !u.disabled }) });
                  await refreshUsers(); draw();
                } catch (err) { toast(err.message, "error"); }
              } }, u.disabled ? t("users.enable") : t("users.disable")),
              h("button", { class: "btn small", onclick: async () => {
                try {
                  const r = await api(`/api/users/${u.id}/api-key`, { method: "POST" });
                  openModal(h("div", { class: "modal" },
                    h("p", {}, t("users.api_key_note")),
                    h("pre", { class: "yaml", style: "user-select:all" }, r.api_key),
                    h("div", { class: "row" }, h("button", { class: "btn sig", onclick: closeModal }, t("common.ok")))),
                  { dismissable: false });  // shown once: only an explicit OK may close it
                  await refreshUsers();
                } catch (err) { toast(err.message, "error"); }
              } }, t("users.api_key")),
              h("button", { class: "btn small danger", onclick: async () => {
                if (await confirmModal(t("users.delete_confirm"))) {
                  try { await api(`/api/users/${u.id}`, { method: "DELETE" }); await refreshUsers(); draw(); }
                  catch (err) { toast(err.message, "error"); }
                }
              } }, t("common.delete")),
            ) : null),
          ))),
        ),
        S.users.length === 0 ? h("p", { class: "dim", style: "margin:12px 0 0" },
          t("users.bootstrap_hint"), " ", h("code", { class: "inline" }, "batchroom users create-admin --email you@example.com")) : null,
      ),
    );
  };
  refreshUsers().then(draw);
}

function userEditor(_user, done) {
  const email = h("input", { type: "text", placeholder: "user@example.com" });
  const name = h("input", { type: "text" });
  const password = h("input", { type: "password", autocomplete: "new-password" });
  const role = h("select", {},
    h("option", { value: "member" }, t("users.role.member")),
    h("option", { value: "admin" }, t("users.role.admin")));
  const close = closeModal;
  openModal(h("div", { class: "modal" },
    h("h1", { style: "font-size:16px;margin:0 0 14px" }, t("users.new_title")),
    h("div", { style: "display:grid;gap:10px" },
      h("div", {}, h("label", {}, t("users.email")), email),
      h("div", {}, h("label", {}, t("users.name")), name),
      h("div", {}, h("label", {}, t("users.password")), password),
      h("div", {}, h("label", {}, t("users.role")), role)),
    h("div", { class: "row", style: "margin-top:16px" },
      h("button", { class: "btn", onclick: close }, t("common.cancel")),
      h("button", { class: "btn sig", onclick: async () => {
        try {
          await api("/api/users", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email: email.value.trim(), name: name.value.trim(),
              password: password.value, role: role.value }) });
          close(); await refreshUsers(); done();
        } catch (err) { toast(err.message, "error"); }
      } }, t("users.create"))),
  ), { dismissable: false });
}

/* ---------- view: watch ---------- */
function viewWatch(main) {
  main.append(h("h1", {}, t("watch.title")), h("p", { class: "sub" }, t("watch.intro")));
  const w = S.system?.watch;
  if (!w) { main.append(h("div", { class: "empty" }, t("common.loading"))); return; }
  if (!w.enabled) main.append(h("div", { class: "problems" }, t("watch.disabled")));
  if (w.problems?.length) {
    main.append(h("div", { class: "problems", style: "margin-top:10px" },
      h("strong", {}, t("watch.problems")), ...w.problems.map((p) => h("div", {}, p))));
  }
  if (!w.folders?.length) {
    main.append(h("div", { class: "empty", style: "margin-top:14px" }, t("watch.no_folders")));
  } else {
    const grid = h("div", { class: "grid cols-2", style: "margin-top:14px" });
    for (const f of w.folders) {
      grid.append(h("div", { class: "panel" },
        h("div", { class: "row spread" },
          h("h3", { class: "mono", style: "margin:0;font-size:13.5px" }, f.folder),
          f.exists ? h("span", { class: "chip ok" }, "✓") : h("span", { class: "chip warn" }, t("watch.missing")),
        ),
        h("dl", { class: "kv", style: "margin:10px 0 0" },
          h("dt", {}, t("watch.recipe")), h("dd", {}, f.recipe),
          h("dt", {}, t("watch.output")), h("dd", {}, f.output),
        ),
      ));
    }
    main.append(grid);
  }
  main.append(
    !isAdmin() ? "" : h("div", { class: "row", style: "margin-top:16px" },
      h("button", { class: "btn", onclick: async () => {
        try {
          await api("/api/watch/reload", { method: "POST" }); await refreshSystem(); render(); toast(t("common.ok"), "ok");
        } catch (err) { toast(err.message, "error"); }
      } }, t("watch.reload"))),
    h("div", { class: "panel", style: "margin-top:16px" },
      h("strong", {}, t("watch.howto_title")),
      h("p", { class: "dim", style: "margin:8px 0 0" }, t("watch.howto"))),
  );
}

/* ---------- view: system ---------- */
function viewSystem(main) {
  main.append(h("h1", {}, t("system.title")));
  const sys = S.system;
  if (!sys) { main.append(h("div", { class: "empty" }, t("common.loading"))); return; }
  const up = sys.uptime_seconds;
  const uptime = up >= 3600 ? t("time.hours", { n: Math.floor(up / 3600) })
    : up >= 60 ? t("time.minutes", { n: Math.floor(up / 60) }) : t("time.seconds", { n: up });
  main.append(
    h("div", { class: "grid cols-2" },
      h("div", { class: "panel" },
        h("dl", { class: "kv" },
          h("dt", {}, t("system.version")), h("dd", {}, `${sys.app} ${sys.version}`),
          h("dt", {}, t("system.workers")), h("dd", {}, String(sys.workers)),
          h("dt", {}, t("system.uptime")), h("dd", {}, uptime),
          h("dt", {}, t("system.queue_depth")), h("dd", {}, `${sys.queue.pending} ${t("status.pending")} · ${sys.queue.running} ${t("status.running")}`),
          h("dt", {}, t("system.auth")), h("dd", {}, sys.auth_required ? t("system.auth_on") : t("system.auth_off")),
          h("dt", {}, t("system.watch_state")), h("dd", {}, sys.watch?.enabled ? t("system.on") : t("system.off")),
        ),
        h("hr", { class: "sep" }),
        h("a", { class: "btn small", href: "/api/docs", target: "_blank" }, t("system.api_docs")),
      ),
      h("div", { class: "panel" },
        h("strong", {}, t("system.formats")),
        h("div", { class: "row", style: "margin:10px 0 16px" },
          ...Object.entries(sys.formats).map(([fmt, ok]) =>
            h("span", { class: `chip ${ok ? "ok" : ""}` }, `${fmt} ${ok ? "✓" : "✗"}`))),
        h("strong", {}, t("system.ops")),
        h("div", { class: "row", style: "margin-top:10px" },
          ...sys.ops.map((op) => h("span", { class: "chip", title: op.summary }, op.name))),
      ),
    ),
    licensePanel(sys.license),
  );
}

function licensePanel(lic) {
  const panel = h("div", { class: "panel", style: "margin-top:14px" });
  const tierChip = lic.active
    ? h("span", { class: "chip sig" }, lic.tier.toUpperCase())
    : h("span", { class: "chip" }, t("license.community"));
  panel.append(h("div", { class: "row spread" },
    h("strong", {}, t("license.title")), tierChip));

  if (lic.valid) {
    panel.append(h("dl", { class: "kv", style: "margin-top:12px" },
      h("dt", {}, t("license.licensee")), h("dd", {}, `${lic.licensee} <${lic.email}>`),
      h("dt", {}, t("license.expires")), h("dd", {}, lic.expires || t("license.never")),
      h("dt", {}, t("license.seats")), h("dd", {}, String(lic.seats)),
      h("dt", {}, t("license.features")), h("dd", {}, (lic.features || []).join(", ") || "—"),
    ));
    if (lic.expired) {
      panel.append(h("div", { class: "problems", style: "margin-top:10px" }, t("license.expired_note")));
    }
  } else {
    panel.append(h("p", { class: "dim", style: "margin:10px 0 0" },
      t("license.community_note"), " ",
      h("a", { href: PRO_URL, target: "_blank", rel: "noopener" }, t("license.get_pro"))));
  }

  if (isAdmin()) {
    const input = h("textarea", { rows: 3, placeholder: t("license.install_placeholder"), spellcheck: "false", dir: "auto" });
    panel.append(h("hr", { class: "sep" }),
      h("label", {}, t("license.install_label")), input,
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "btn small sig", onclick: async () => {
          try {
            await api("/api/license", { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ key: input.value.trim() }) });
            toast(t("license.installed"), "ok");
            location.reload();
          } catch (err) { toast(err.message, "error"); }
        } }, t("license.install")),
        lic.valid ? h("button", { class: "btn small danger", onclick: async () => {
          if (await confirmModal(t("license.remove_confirm"))) {
            try { await api("/api/license", { method: "DELETE" }); location.reload(); }
            catch (err) { toast(err.message, "error"); }
          }
        } }, t("license.remove")) : null,
      ));
  }
  return panel;
}

/* ================= data refresh ================= */
async function refreshJobs() {
  try { S.jobs = (await api("/api/jobs?limit=100")).jobs; } catch { /* surfaced elsewhere */ }
  renderNavLinks();  // the running-jobs badge is visible from every view
}
async function refreshRecipes() {
  try {
    const d = await api("/api/recipes");
    S.recipes = d.recipes; S.recipeProblems = d.problems;
    // a remembered recipe may have been deleted or renamed: what the picker
    // shows must be exactly what gets submitted
    if (!S.recipes.some((r) => r.name === S.selectedRecipe)) {
      S.selectedRecipe = S.recipes.length ? S.recipes[0].name : "";
    }
  } catch { /* ignore */ }
}
async function refreshModels() {
  try { S.models = (await api("/api/models")).models; } catch { /* ignore */ }
}
async function refreshSystem() {
  try { S.system = await api("/api/system"); } catch { /* ignore */ }
}

/* ================= SSE ================= */
function connectEvents() {
  if (S.es) S.es.close();
  const es = new EventSource(fileUrl("/api/events"));
  S.es = es;
  let refreshTimer = null;
  const debouncedRefresh = () => {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      await refreshJobs();
      if (S.view === "queue") render();
    }, 250);
  };
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.type === "item.progress") {
      // current step / first-use model download, shown under the running item
      const key = `${ev.job_id}:${ev.idx}`;
      S.itemProgress[key] = ev.message;
      const el = document.querySelector(`.drawer [data-progress="${CSS.escape(key)}"]`);
      if (el) el.textContent = ev.message;
    } else if (ev.type === "job.updated" || ev.type === "item.finished" || ev.type === "job.deleted") {
      if (ev.type === "item.finished") delete S.itemProgress[`${ev.job_id}:${ev.idx}`];
      debouncedRefresh();
    } else if (ev.type === "model.progress") {
      S.pullProgress[ev.model] = ev.total ? Math.round((ev.done / ev.total) * 100) : 0;
      if (S.view === "models") render();
    } else if (ev.type === "model.ready") {
      delete S.pullProgress[ev.model];
      toast(t("models.ready", { id: ev.model }), "ok");
      refreshModels().then(() => S.view === "models" && render());
    } else if (ev.type === "model.error") {
      delete S.pullProgress[ev.model];
      toast(t("models.error", { msg: ev.error }), "error");
      refreshModels().then(() => S.view === "models" && render());
    }
  };
  let opened = false;
  es.onopen = () => {
    if (opened) debouncedRefresh();  // events sent while we were disconnected are lost
    opened = true;
  };
  es.onerror = () => { /* EventSource auto-reconnects */ };
}

/* ================= auth gate (env API key) ================= */
function showAuthGate(show) {
  $("#auth-gate").hidden = !show;
  $("#app").hidden = show;
  if (show) {
    $("#auth-prompt").textContent = t("auth.prompt") || "This server requires an API key.";
    $("#auth-submit").textContent = t("auth.submit") || "Unlock";
    $("#auth-key").placeholder = t("auth.placeholder");
    $("#auth-key").focus();
  }
}

/* ================= login (multi-user Pro) ================= */
function showLoginView() {
  const gate = $("#auth-gate");
  gate.hidden = false;
  $("#app").hidden = true;
  const email = h("input", { type: "text", placeholder: t("auth.email"), autocomplete: "username", autofocus: true });
  const password = h("input", { type: "password", placeholder: t("auth.password"), autocomplete: "current-password" });
  const error = h("p", { class: "auth-error", hidden: true });
  const form = h("form", { class: "panel auth-panel", onsubmit: async (e) => {
    e.preventDefault();
    error.hidden = true;
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.value.trim(), password: password.value }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : t("auth.bad_credentials"));
      }
      location.reload();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  } },
    h("span", { class: "brand-mark big", "aria-hidden": "true" }),
    h("p", {}, t("auth.login_title")),
    email, password,
    h("button", { type: "submit", class: "btn sig" }, t("auth.sign_in")),
    error,
  );
  gate.replaceChildren(form);
  email.focus();
}

/* ================= boot ================= */
async function boot() {
  $("#auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    S.apiKey = $("#auth-key").value.trim();
    store.set("br_key", S.apiKey);
    $("#auth-error").hidden = true;
    try {
      await api("/api/system");
      showAuthGate(false);
      await start();
    } catch {
      $("#auth-error").textContent = t("auth.invalid") || "Invalid API key";
      $("#auth-error").hidden = false;
    }
  });
  $("#lang-select").addEventListener("change", async (e) => {
    await loadLang(e.target.value);
    render();
  });
  $("#tour-link").addEventListener("click", showTour);
  window.addEventListener("hashchange", route);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if ($("#modal-root").childElementCount) { if (modalDismissable) closeModal(); }
    else if (document.querySelector(".drawer")) closeDrawer();
  });

  try {
    const langs = await api("/api/i18n");
    S.languages = langs.languages;
  } catch (err) {
    document.body.insertAdjacentText("afterbegin", err.message);
    return;
  }
  if (!S.languages.some((l) => l.code === S.lang)) S.lang = "en";
  await loadLang(S.lang);

  // auth probe: /auth/me is open and tells us which gate (if any) to show
  try {
    S.me = await api("/api/auth/me");
  } catch { S.me = null; }
  if (S.me && S.me.multiuser && !S.me.authenticated) {
    showLoginView();
    return;
  }
  try {
    await api("/api/system");
  } catch {
    showAuthGate(true);
    return;
  }
  await start();
}

async function start() {
  $("#app").hidden = false;
  await Promise.all([refreshRecipes(), refreshJobs(), refreshModels(), refreshSystem()]);
  if (S.system) $("#brand-version").textContent = `v${S.system.version}`;
  connectEvents();  // (re)connect: after re-unlocking, the stream needs the new key
  route();
  if (S.started) return;  // unlocking again after a 401 must not double the timers
  S.started = true;
  setInterval(async () => {
    await refreshSystem();
    // a re-render would wipe a license key being pasted into the system view
    const typing = $("#main textarea");
    if (S.view === "system" && !(typing && (typing.value || typing === document.activeElement))) render();
  }, 30000);
  maybeShowTour();
}

boot();
