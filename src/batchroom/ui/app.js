/* Batchroom web UI — vanilla JS, no build chain.
   Sections: api client · i18n · router · views · components · sse. */
"use strict";

/* ================= state ================= */
const S = {
  lang: localStorage.getItem("br_lang") || (navigator.language || "en").slice(0, 2),
  strings: {},
  languages: [],
  apiKey: localStorage.getItem("br_key") || "",
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
  selectedRecipe: localStorage.getItem("br_recipe") || "",
  selectedPriority: 0,
  pullProgress: {},   // model id -> pct
  es: null,
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
  localStorage.setItem("br_lang", code);
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
    let login = false;
    try {
      const body = await res.clone().json();
      login = !!(body.detail && body.detail.login_required);
    } catch { /* header-key mode */ }
    if (login || (S.me && S.me.multiuser)) showLoginView();
    else showAuthGate(true);
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
function fileUrl(path) {
  return S.apiKey ? `${path}${path.includes("?") ? "&" : "?"}api_key=${encodeURIComponent(S.apiKey)}` : path;
}

/* ================= toasts & modal ================= */
function toast(msg, kind = "") {
  const el = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 4200);
}
function confirmModal(message) {
  return new Promise((resolve) => {
    const root = $("#modal-root");
    const close = (val) => { root.replaceChildren(); resolve(val); };
    root.replaceChildren(
      h("div", { class: "modal", role: "dialog", "aria-modal": "true" },
        h("p", {}, message),
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: () => close(false) }, t("common.cancel")),
          h("button", { class: "btn sig", onclick: () => close(true) }, t("common.ok")),
        ),
      ),
    );
    root.addEventListener("click", (e) => { if (e.target === root) close(false); }, { once: true });
  });
}

/* ---------- onboarding tour ---------- */
const TOUR_KEY = "br_tour_v1";
function showTour() {
  const root = $("#modal-root");
  const close = () => root.replaceChildren();
  const step = (n, key) => h("div", { class: "tour-step" },
    h("span", { class: "tour-num" }, String(n)),
    h("p", {}, t(key)));
  root.replaceChildren(
    h("div", { class: "modal tour", role: "dialog", "aria-modal": "true" },
      h("h2", {}, t("tour.title")),
      h("p", { class: "sub" }, t("tour.intro")),
      step(1, "tour.step1"), step(2, "tour.step2"), step(3, "tour.step3"),
      h("div", { class: "row", style: "justify-content:flex-end;margin-top:16px" },
        h("button", { class: "btn sig", onclick: close }, t("tour.cta"))),
    ),
  );
  root.addEventListener("click", (e) => { if (e.target === root) close(); }, { once: true });
}
function maybeShowTour() {
  if (localStorage.getItem(TOUR_KEY)) return;
  localStorage.setItem(TOUR_KEY, "1"); // first sighting counts; reopen lives in the sidebar
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
  $("#modal-root").replaceChildren();
  const hash = location.hash.replace(/^#\/?/, "");
  const [view, arg] = hash.split("/");
  S.view = VIEWS.includes(view) && viewVisible(view) ? view : "process";
  S.viewArg = arg ? decodeURIComponent(arg) : null;
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

function renderNav() {
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
  const sel = $("#lang-select");
  sel.replaceChildren(...S.languages.map((l) =>
    h("option", { value: l.code, selected: l.code === S.lang }, l.name)));
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
    onchange: (e) => { S.selectedRecipe = e.target.value; localStorage.setItem("br_recipe", e.target.value); },
  }, ...S.recipes.map((r) =>
    h("option", { value: r.name, selected: r.name === S.selectedRecipe }, `${r.name} — ${r.description}`.slice(0, 90))));

  const fileInput = h("input", { type: "file", multiple: true, accept: "image/*,.heic,.avif", hidden: true,
    onchange: (e) => addFiles([...e.target.files]) });

  const dz = h("div", { class: "dropzone", tabindex: "0", role: "button",
    onclick: () => fileInput.click(),
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") fileInput.click(); },
    ondragover: (e) => { e.preventDefault(); dz.classList.add("drag"); },
    ondragleave: () => dz.classList.remove("drag"),
    ondrop: (e) => {
      e.preventDefault(); dz.classList.remove("drag");
      addFiles([...e.dataTransfer.files]);
    },
  },
    h("div", { class: "hint" }, t("process.drop_hint")),
    h("div", { class: "subhint" }, t("process.drop_sub")),
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
        h("div", { style: "flex:1;min-width:260px" }, h("label", {}, t("process.recipe_label")), recipeSel),
      ),
      dz, fileInput, cards,
      h("hr", { class: "sep" }),
      h("div", { style: "margin-bottom:14px" }, h("label", {}, t("process.webhook_label")), webhook),
      priorityBlock,
      h("div", { class: "row" }, startBtn, clearBtn, counter),
    ),
  );
  renderFileCards();
}

function addFiles(files) {
  const imgs = files.filter((f) => /\.(jpe?g|png|webp|heic|heif|avif|tiff?|bmp|gif)$/i.test(f.name));
  S.selectedFiles.push(...imgs);
  renderFileCards();
}
function renderFileCards() {
  const wrap = $("#filecards");
  if (!wrap) return;
  wrap.replaceChildren(...S.selectedFiles.map((f, i) => {
    const img = h("img", { alt: "" });
    if (/\.(jpe?g|png|webp|gif|bmp)$/i.test(f.name)) {
      img.src = URL.createObjectURL(f);
      img.onload = () => URL.revokeObjectURL(img.src);
    }
    return h("div", { class: "filecard" },
      img,
      h("button", { class: "rm", title: t("common.delete"), onclick: () => { S.selectedFiles.splice(i, 1); renderFileCards(); } }, "✕"),
      h("div", { class: "name" }, f.name),
    );
  }));
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
          let msg = xhr.statusText;
          try { const b = JSON.parse(xhr.responseText); msg = typeof b.detail === "string" ? b.detail : b.detail?.detail || msg; } catch {}
          reject(new Error(msg));
        }
      };
      xhr.onerror = () => reject(new Error(t("errors.network")));
      xhr.send(form);
    });
    const job = await done;
    toast(t("process.submitted", { id: job.id }), "ok");
    S.selectedFiles = [];
    await refreshJobs();
    nav("queue");
  } catch (err) {
    toast(t("errors.upload_failed", { msg: err.message }), "error");
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

function jobCard(job) {
  const total = job.total || 1;
  const pct = (n) => `${(100 * n) / total}%`;
  const running = Math.max(0, total - job.done - job.failed - job.cancelled);
  return h("div", { class: "panel jobcard", onclick: () => nav(`queue/${job.id}`) },
    h("div", { class: "row spread" },
      h("h3", {}, job.id),
      statusChip(job.status),
    ),
    h("div", { class: "row", style: "margin-top:6px" },
      h("span", { class: "chip" }, job.recipe),
      h("span", { class: "faint mono" }, `${job.done + job.failed + job.cancelled}/${job.total}`),
      h("span", { class: "faint" }, (job.created_at || "").replace("T", " ").replace(/\+.*/, "")),
      h("span", { class: "chip" }, job.source),
    ),
    h("div", { class: "bar" },
      h("i", { class: "b-done", style: `width:${pct(job.done)}` }),
      h("i", { class: "b-failed", style: `width:${pct(job.failed)}` }),
      job.status === "running" ? h("i", { class: "b-run", style: `width:${pct(running)}` }) : null,
    ),
  );
}

async function openJobDrawer(jobId) {
  document.querySelectorAll(".drawer, .drawer-back").forEach((el) => el.remove());
  let job;
  try { job = await api(`/api/jobs/${jobId}`); }
  catch (err) { toast(err.message, "error"); nav("queue"); return; }

  const close = () => { back.remove(); drawer.remove(); if (S.view === "queue") nav("queue"); };
  const back = h("div", { class: "drawer-back", onclick: close });

  const actions = h("div", { class: "row" });
  if (job.status === "running" || job.status === "queued") {
    actions.append(h("button", { class: "btn small", onclick: async () => {
      if (await confirmModal(t("queue.cancel_confirm"))) {
        try { await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }); toast(t("common.ok"), "ok"); close(); refreshJobs(); }
        catch (err) { toast(err.message, "error"); }
      }
    } }, t("common.cancel")));
  } else {
    actions.append(
      h("a", { class: "btn small", href: fileUrl(`/api/jobs/${job.id}/archive`) }, t("queue.download_zip")),
      h("a", { class: "btn small ghost", href: fileUrl(`/api/jobs/${job.id}/outputs/manifest.json`), target: "_blank" }, t("queue.open_manifest")),
      h("button", { class: "btn small danger", onclick: async () => {
        if (await confirmModal(t("queue.delete_confirm"))) {
          try { await api(`/api/jobs/${job.id}`, { method: "DELETE" }); close(); refreshJobs(); }
          catch (err) { toast(err.message, "error"); }
        }
      } }, t("common.delete")),
    );
  }

  const rows = job.items.map((item) => {
    const result = item.result;
    const thumbIn = fileUrl(`/api/jobs/${job.id}/thumb/${item.idx}/in`);
    const thumbOut = fileUrl(`/api/jobs/${job.id}/thumb/${item.idx}/out`);
    const dur = result ? `${(result.duration_ms / 1000).toFixed(1)}s` : "";
    return h("tr", {},
      h("td", {},
        h("div", { class: "thumb-pair" },
          h("img", { src: thumbIn, alt: "", loading: "lazy", onerror: (e) => e.target.style.visibility = "hidden",
            onclick: () => item.status === "done" && openCompare(job, item) }),
          h("img", { src: thumbOut, alt: "", loading: "lazy", onerror: (e) => e.target.style.visibility = "hidden",
            onclick: () => item.status === "done" && openCompare(job, item), style: "cursor:pointer" }),
        ),
      ),
      h("td", {}, h("div", { class: "mono" }, item.source_name),
        item.error ? h("div", { class: "err-text" }, item.error) : null),
      h("td", {}, statusChip(item.status)),
      h("td", { class: "mono dim" }, dur),
      h("td", {}, ...(result?.outputs || []).slice(0, 6).map((o) =>
        h("a", { class: "chip", style: "text-decoration:none;margin-inline-end:4px",
          href: fileUrl(`/api/jobs/${job.id}/outputs/${o.path}`), download: "" }, o.preset || o.format))),
    );
  });

  const drawer = h("div", { class: "drawer", role: "dialog" },
    h("div", { class: "row spread" },
      h("h1", { class: "mono", style: "font-size:16px" }, job.id),
      h("button", { class: "btn small ghost", onclick: close }, t("common.close")),
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
  );
  document.body.append(back, drawer);
}

function openCompare(job, item) {
  const root = $("#modal-root");
  const closeAll = () => root.replaceChildren();
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
  let pos = 0.5;
  const apply = () => {
    const rtl = document.documentElement.dir === "rtl";
    const clipPct = pos * 100;
    afterWrap.style.clipPath = rtl
      ? `inset(0 ${clipPct}% 0 0)`
      : `inset(0 0 0 ${clipPct}%)`;
    divider.style.insetInlineStart = `${rtl ? 100 - clipPct : clipPct}%`;
  };
  const move = (clientX) => {
    const rect = box.getBoundingClientRect();
    pos = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    if (document.documentElement.dir === "rtl") { /* pos measured ltr; clip handles rtl */ }
    apply();
  };
  box.addEventListener("pointerdown", (e) => { box.setPointerCapture(e.pointerId); move(e.clientX); });
  box.addEventListener("pointermove", (e) => { if (e.buttons) move(e.clientX); });
  apply();
  root.replaceChildren(h("div", { class: "modal", style: "max-width:520px" },
    box,
    h("div", { class: "row", style: "margin-top:14px;justify-content:flex-end" },
      h("button", { class: "btn", onclick: closeAll }, t("common.close")))));
  root.addEventListener("click", (e) => { if (e.target === root) closeAll(); }, { once: true });
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
        h("button", { class: "btn small right", onclick: () => openRecipeEditor(r.name) }, t("recipes.edit")),
      ),
    ));
  }
  main.append(grid,
    h("div", { class: "row", style: "margin-top:16px" },
      h("button", { class: "btn sig", onclick: () => openRecipeEditor(null) }, t("recipes.new"))));
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
  const ta = h("textarea", { rows: 18, spellcheck: "false" }, source);
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
  const root = $("#modal-root");
  const close = () => root.replaceChildren();
  root.replaceChildren(h("div", { class: "modal", style: "max-width:720px" },
    h("h1", { style: "font-size:16px;margin-bottom:10px" }, t("recipes.editor_title")),
    builtin ? h("p", { class: "dim", style: "font-size:12.5px" }, t("recipes.builtin_note")) : null,
    ta,
    h("div", { class: "row", style: "margin-top:10px" },
      status,
      h("button", { class: "btn small right", onclick: validate }, t("recipes.validate")),
      h("button", { class: "btn small sig", onclick: async () => {
        if (!(await validate())) return;
        try {
          await api("/api/recipes", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: ta.value }) });
          toast(t("common.ok"), "ok"); close(); refreshRecipes().then(render);
        } catch (err) { toast(err.message, "error"); }
      } }, t("recipes.save")),
      h("button", { class: "btn small ghost", onclick: close }, t("common.close")),
    ),
  ));
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
        h("button", { class: "btn small danger", onclick: async () => {
          if (await confirmModal(t("models.delete_confirm"))) {
            await api(`/api/models/${m.id}`, { method: "DELETE" }); refreshModels().then(render);
          }
        } }, t("common.delete")),
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
    await api(`/api/models/${m.id}/pull`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_unpinned: !m.pinned }),
    });
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
                  const root = $("#modal-root");
                  root.replaceChildren(h("div", { class: "modal" },
                    h("p", {}, t("users.api_key_note")),
                    h("pre", { class: "yaml", style: "user-select:all" }, r.api_key),
                    h("div", { class: "row" }, h("button", { class: "btn sig", onclick: () => root.replaceChildren() }, t("common.ok")))));
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
  const root = $("#modal-root");
  const close = () => root.replaceChildren();
  root.replaceChildren(h("div", { class: "modal" },
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
  ));
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
    h("div", { class: "row", style: "margin-top:16px" },
      h("button", { class: "btn", onclick: async () => {
        await api("/api/watch/reload", { method: "POST" }); await refreshSystem(); render(); toast(t("common.ok"), "ok");
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
          h("dt", {}, t("system.queue_depth")), h("dd", {}, `${sys.queue.pending} pending · ${sys.queue.running} running`),
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
    const input = h("textarea", { rows: 3, placeholder: t("license.install_placeholder"), spellcheck: "false" });
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
            await api("/api/license", { method: "DELETE" });
            location.reload();
          }
        } }, t("license.remove")) : null,
      ));
  }
  return panel;
}

/* ================= data refresh ================= */
async function refreshJobs() {
  try { S.jobs = (await api("/api/jobs?limit=100")).jobs; } catch { /* surfaced elsewhere */ }
  if (S.view === "queue") { renderNav(); }
}
async function refreshRecipes() {
  try {
    const d = await api("/api/recipes");
    S.recipes = d.recipes; S.recipeProblems = d.problems;
    if (!S.selectedRecipe && S.recipes.length) S.selectedRecipe = S.recipes[0].name;
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
    if (ev.type === "job.updated" || ev.type === "item.finished" || ev.type === "job.deleted") {
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
  es.onerror = () => { /* EventSource auto-reconnects */ };
}

/* ================= auth gate (env API key) ================= */
function showAuthGate(show) {
  $("#auth-gate").hidden = !show;
  $("#app").hidden = show;
  if (show) {
    $("#auth-prompt").textContent = t("auth.prompt") || "This server requires an API key.";
    $("#auth-submit").textContent = t("auth.submit") || "Unlock";
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
    localStorage.setItem("br_key", S.apiKey);
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
  $("#brand-version").textContent = "";
  await Promise.all([refreshRecipes(), refreshJobs(), refreshModels(), refreshSystem()]);
  if (S.system) $("#brand-version").textContent = `v${S.system.version}`;
  connectEvents();
  setInterval(() => { refreshSystem(); if (S.view === "system") render(); }, 30000);
  route();
  maybeShowTour();
}

boot();
