/* OLE5 console.
 *
 * One page, four views, one event stream. The stream carries no payload -- it
 * says something moved, and whichever view is open refetches itself. That keeps
 * the server cheap and means a new view needs no change on either side.
 *
 * Editing a draft is editing the fields in place. Whatever differs from what
 * the agent proposed is sent as the final version, along with why; anything
 * untouched keeps the agent's value.
 */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const RTL = /[؀-ۿ]/;
const dir = (s) => (RTL.test(s || "") ? "rtl" : "ltr");

// Times in the DB are UTC ISO 8601 (e.g. "2024-01-15T14:23:45+00:00").
// The support team is in Riyadh; show every timestamp in their wall-clock,
// with the raw value as a tooltip for audit. Asia/Riyadh is UTC+3 year-round
// (no DST), so the conversion is a fixed offset.
// Auto-grow a textarea to fit its content. Called on input, on
// initial render (after the textarea is in the DOM so scrollHeight is
// correct), and on window resize (so width changes re-flow the height).
// A small min keeps empty textareas from collapsing to nothing.
function autoGrowTextarea(el) {
  if (!el || el.tagName.toLowerCase() !== "textarea") return;
  el.style.height = "auto";
  const min = 44;
  el.style.height = Math.max(min, el.scrollHeight) + "px";
}

// Re-grow every textarea on the page. Cheap query; safe to call often.
function autoGrowAll() {
  document.querySelectorAll("textarea").forEach(autoGrowTextarea);
}

// Debounced resize listener. Width changes re-wrap text, so heights
// need re-computing. Attached once at module scope -- not per-render.
let _resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(autoGrowAll, 100);
});

const RIYADH_TZ = "Asia/Riyadh";
const ISO_TIME_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);  // not actually a date
  return d.toLocaleString("en-GB", {
    timeZone: RIYADH_TZ,
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).replace(",", " ·");
  // "15 Jan 2024 · 17:23"
}

function isIsoTime(v) {
  return typeof v === "string" && ISO_TIME_RE.test(v);
}

// Evidence is a structured object the orchestrator emits: a list of
// searches, each with evidence chunks graded by relation; and a list of
// corrections the validator applied. Raw JSON is hard to scan in a
// table cell. This collapses it to a summary a person can read in one
// glance, with a "show raw" toggle for anyone who needs the original.
function looksLikeEvidence(v) {
  if (v == null) return false;
  let data = v;
  if (typeof v === "string") {
    if (!v.includes("\"searches\"") && !v.includes("\"corrections\"")) return false;
    try { data = JSON.parse(v); } catch { return false; }
  }
  return typeof data === "object" && data !== null &&
         (Array.isArray(data.searches) || Array.isArray(data.corrections));
}

// The agent's trace (audit action "trace", and "decision_failed"): every step
// it took, in order. Read as a numbered list, one line per step, with the raw
// JSON behind a toggle like the evidence view.
function looksLikeTrace(v) {
  let data = v;
  if (typeof v === "string") {
    if (!v.includes("\"steps\"")) return false;
    try { data = JSON.parse(v); } catch { return false; }
  }
  return typeof data === "object" && data !== null && Array.isArray(data.steps);
}

function traceLine(st) {
  const q = (x) => `\u201c${x}\u201d`;
  switch (st.step) {
    case "started":
      return `Started \u00b7 team scopes ` +
        (st.team_scopes_version ? `v${st.team_scopes_version}` : "not uploaded") +
        (st.model ? ` \u00b7 ${st.model}` : "");
    case "search_knowledge_base": {
      const n = (st.evidence || []).length;
      const direct = (st.evidence || []).filter((e) => (e.relation || "").toUpperCase() === "DIRECT").length;
      return st.error
        ? `Searched the knowledge base: ${q(st.query)} \u2192 failed: ${st.error}`
        : `Searched the knowledge base: ${q(st.query)} \u2192 ${st.verdict || "?"} ` +
          `(${n} source${n === 1 ? "" : "s"}, ${direct} direct)`;
    }
    case "search_past_tickets": {
      const t = st.tickets || [];
      return `Looked up past tickets: ${q(st.query)} \u2192 ` +
        (t.length ? t.map((x) => `${x.ticket} (${x.score})`).join(", ") : "none");
    }
    case "nudge": return `Nudged: ${st.reason}`;
    case "retry": return `Retried: ${st.reason}`;
    case "unknown_tool": return `Called a tool that does not exist: ${st.name}`;
    case "proposed": {
      const d = st.decision || {};
      return `Proposed: ${d.action || "?"} \u00b7 ${d.queue || "?"} \u00b7 ${d.next_state || "?"}`;
    }
    case "validated":
      return (st.corrections || []).length
        ? `Validator corrected: ${st.corrections.join("; ")}`
        : "Validator: no corrections";
    default:
      return `${st.step}: ${JSON.stringify(st)}`;
  }
}

function renderTrace(v) {
  const data = typeof v === "string" ? JSON.parse(v) : v;
  const box = el("div", "evidence");
  const list = el("ol", "evidence-list trace-list");
  for (const st of data.steps) list.append(el("li", null, traceLine(st)));
  box.append(list);
  const rawToggle = el("button", "link ev-raw-toggle", "Show raw JSON");
  const rawBox = el("pre", "evidence-raw");
  rawBox.textContent = JSON.stringify(data, null, 2);
  rawBox.hidden = true;
  rawToggle.addEventListener("click", () => {
    rawBox.hidden = !rawBox.hidden;
    rawToggle.textContent = rawBox.hidden ? "Show raw JSON" : "Hide raw JSON";
  });
  box.append(rawToggle, rawBox);
  return box;
}

function renderEvidence(v) {
  let data;
  if (typeof v === "string") {
    try { data = JSON.parse(v); } catch { return null; }
  } else if (typeof v === "object" && v !== null) {
    data = v;
  } else {
    return null;
  }

  const box = el("div", "evidence");

  const searches = Array.isArray(data.searches) ? data.searches : [];
  const directSources = new Set();
  const indirectSources = new Set();
  searches.forEach((s) => {
    (s.evidence || []).forEach((c) => {
      const name = c.document || c.source || c.title;
      if (!name) return;
      if ((c.relation || "").toUpperCase() === "DIRECT") directSources.add(name);
      else indirectSources.add(name);
    });
  });
  const corrections = Array.isArray(data.corrections) ? data.corrections : [];

  // One-line summary chip row.
  const summary = el("div", "evidence-summary");
  summary.append(el("span", "ev-chip",
    `${searches.length} search${searches.length === 1 ? "" : "es"}`));
  summary.append(el("span", "ev-chip",
    `${directSources.size} direct source${directSources.size === 1 ? "" : "s"}`));
  if (indirectSources.size) {
    summary.append(el("span", "ev-chip ev-dim",
      `${indirectSources.size} indirect`));
  }
  if (corrections.length) {
    summary.append(el("span", "ev-chip ev-amber",
      `${corrections.length} correction${corrections.length === 1 ? "" : "s"}`));
  }
  box.append(summary);

  // Direct sources -- the documents that actually answered the question.
  if (directSources.size) {
    box.append(el("div", "evidence-label", "Direct sources"));
    const list = el("ul", "evidence-list");
    [...directSources].forEach((name) =>
      list.append(el("li", null, name)));
    box.append(list);
  }

  // Indirect sources -- passages graded as on-the-subject but not answering.
  if (indirectSources.size) {
    box.append(el("div", "evidence-label",
      `Indirect sources (${indirectSources.size})`));
    const list = el("ul", "evidence-list ev-indirect");
    [...indirectSources].forEach((name) =>
      list.append(el("li", null, name)));
    box.append(list);
  }

  // Corrections the validator applied -- short strings, shown verbatim.
  if (corrections.length) {
    box.append(el("div", "evidence-label", "Corrections"));
    const list = el("ul", "evidence-list");
    corrections.forEach((c) => list.append(el("li", null, c)));
    box.append(list);
  }

  // Raw JSON, collapsed by default. The original value is preserved for
  // anyone who needs to audit the exact bytes the orchestrator emitted.
  const rawToggle = el("button", "link ev-raw-toggle", "Show raw JSON");
  const rawBox = el("pre", "evidence-raw");
  rawBox.textContent = JSON.stringify(data, null, 2);
  rawBox.hidden = true;
  rawToggle.addEventListener("click", () => {
    rawBox.hidden = !rawBox.hidden;
    rawToggle.textContent = rawBox.hidden ? "Show raw JSON" : "Hide raw JSON";
  });
  box.append(rawToggle, rawBox);

  return box;
}

const state = {
  reviewer: null,
  options: {},
  tab: "review",
  draftId: null,
  draft: null,
  edits: {},
  table: null,
  offset: 0,
  search: "",
  cfgKind: "queue",
  ktab: "kb",
};

// Ordered list of pending draft IDs currently shown in the list.
// Refreshed whenever loadDrafts runs; consumed by the J/K shortcuts.
let draftIds = [];
let lastPendingCount = 0;
// Drafts already seen in this browser: a new id is a new draft, even when one
// was approved in the same moment and the count did not change. Empty until
// the first load, which fills it silently.
let seenDrafts = null;

// ---------------------------------------------------------------- transport

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  if (res.status === 401) {
    state.reviewer = null;
    showGate();
    throw new Error("not signed in");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

let toastTimer;
function toast(message, kind) {
  const t = $("toast");
  t.textContent = message;
  t.className = "toast" + (kind ? " " + kind : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3600);
}

// ---------------------------------------------------------------- the gate

let gateMode = "login";

function showGate(anyoneRegistered = true) {
  $("gate").hidden = false;
  $("shell").hidden = true;
  gateMode = anyoneRegistered ? "login" : "register";
  paintGate();
}

// Five screens on one card:
//   login     email + password
//   register  email + password + name          -> a code is emailed
//   verify    the code from the email          -> account created, signed in
//   forgot    email                            -> a code is emailed
//   reset     the code + a new password, twice -> password changed, signed in
const GATE = {
  login:    { sub: () => "Sign in to review drafts.", submit: "Sign in",
              toggle: "Create an account", fields: ["password"] },
  register: { sub: () => "Create an account to review drafts.", submit: "Create account",
              toggle: "I already have an account", fields: ["password", "name"] },
  verify:   { sub: () => `We emailed a 6-digit code to ${$("gate-email").value}. Enter it to create the account.`,
              submit: "Confirm", toggle: "Back", fields: ["code"], lockEmail: true },
  forgot:   { sub: () => "Enter your email and we will send you a code to reset your password.",
              submit: "Send code", toggle: "Back to sign in", fields: [] },
  reset:    { sub: () => `If ${$("gate-email").value} has an account, a 6-digit code is on its way. Enter it with a new password.`,
              submit: "Reset password", toggle: "Back to sign in", fields: ["code", "new", "new2"], lockEmail: true },
};

function paintGate() {
  const g = GATE[gateMode];
  $("gate-sub").textContent = g.sub();
  $("gate-submit").textContent = g.submit;
  $("gate-toggle").textContent = g.toggle;
  for (const f of ["password", "name", "code", "new", "new2"]) {
    const on = g.fields.includes(f);
    $(`gate-${f}-row`).hidden = !on;
    $(`gate-${f === "password" ? "password" : f}`).required = on && f !== "name";
  }
  $("gate-email").readOnly = !!g.lockEmail;
  $("gate-forgot").hidden = gateMode !== "login";
  $("gate-password").autocomplete = gateMode === "register" ? "new-password" : "current-password";
  $("gate-error").hidden = true;
  for (const f of ["code", "new", "new2"]) $(`gate-${f}`).value = "";
  if (g.fields.includes("code")) $("gate-code").focus();
}

$("gate-toggle").addEventListener("click", () => {
  gateMode = { login: "register", register: "login", verify: "register",
               forgot: "login", reset: "login" }[gateMode];
  paintGate();
});

$("gate-forgot").addEventListener("click", () => {
  gateMode = "forgot";
  paintGate();
});

$("gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("gate-email").value;
  const post = (url, fields) =>
    api(url, { method: "POST", body: new URLSearchParams({ email, ...fields }) });
  const signedIn = async (out) => { state.reviewer = out.reviewer; await start(); };
  try {
    if (gateMode === "login") {
      await signedIn(await post("/api/login", { password: $("gate-password").value }));
    } else if (gateMode === "register") {
      await post("/api/register", { password: $("gate-password").value,
                                     display_name: $("gate-name").value });
      gateMode = "verify"; paintGate();
    } else if (gateMode === "verify") {
      await signedIn(await post("/api/register/verify", { code: $("gate-code").value.trim() }));
    } else if (gateMode === "forgot") {
      await post("/api/password/forgot", {});
      gateMode = "reset"; paintGate();
    } else if (gateMode === "reset") {
      if ($("gate-new").value !== $("gate-new2").value) {
        throw new Error("The two new passwords do not match.");
      }
      await signedIn(await post("/api/password/reset", { code: $("gate-code").value.trim(),
                                                          new_password: $("gate-new").value }));
      toast("Password changed. You are signed out on every other device.", "good");
    }
  } catch (err) {
    $("gate-error").textContent = err.message;
    $("gate-error").hidden = false;
  }
});

$("sign-out").addEventListener("click", async () => {
  await post("/api/logout");
  location.reload();
});

// ---------------------------------------------------------------- tabs

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchTo(tab.dataset.tab));
});

function switchTo(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((t) => {
    if (t.dataset.tab === name) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  });
  document.querySelectorAll(".page").forEach((p) => {
    p.hidden = p.id !== "page-" + name;
  });
  refresh();
}

async function refresh() {
  // Pulse the active tab so a reviewer can see the SSE nudge landed,
  // even on a slow refetch. The class is removed in finally so an
  // error does not leave the tab stuck mid-pulse.
  const tab = document.querySelector('.tab[aria-current="page"]');
  if (tab) tab.classList.add("refreshing");
  try {
    if (state.tab === "review") await loadDrafts();
    else if (state.tab === "knowledge") await loadKnowledge();
    else if (state.tab === "dashboard") await loadDashboard();
    else if (state.tab === "tables") await loadTableIndex();
    else if (state.tab === "config") await loadConfig();
  } finally {
    if (tab) tab.classList.remove("refreshing");
  }
}

// ---------------------------------------------------------------- review

// ---------------------------------------------------------------- new drafts: badge and sound
//
// The Review badge, the tab title and the sound follow every change, on any
// page -- not only when Review is open.

function soundOn() {
  try { return localStorage.getItem("ole5-sound") !== "off"; } catch { return true; }
}

function paintSoundToggle() {
  $("sound-toggle").textContent = soundOn() ? "\u{1F514} Sound on" : "\u{1F515} Sound off";
}

$("sound-toggle").addEventListener("click", () => {
  try { localStorage.setItem("ole5-sound", soundOn() ? "off" : "on"); } catch {}
  paintSoundToggle();
  if (soundOn()) chime();
});

// static/notify.mp3 when it exists; otherwise a short two-note chime made in
// the browser. Browsers allow sound only after the page has been clicked once
// -- signing in counts.
// One chime for a burst: several drafts arriving together, or the same
// arrival reaching the page through two overlapping refreshes, sound once.
let lastChime = 0;

function chime() {
  if (!soundOn()) return;
  if (Date.now() - lastChime < 4000) return;
  lastChime = Date.now();
  const audio = new Audio("/static/notify.mp3");
  audio.play().catch(() => {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [[880, 0], [1320, 0.16]].forEach(([freq, at]) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, ctx.currentTime + at);
        gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + at + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.35);
        osc.connect(gain).connect(ctx.destination);
        osc.start(ctx.currentTime + at);
        osc.stop(ctx.currentTime + at + 0.4);
      });
    } catch {}
  });
}

function notePending(drafts) {
  const badge = $("pending-count");
  const count = drafts.length || 0;
  const ids = new Set(drafts.map((d) => d.id));
  const fresh = seenDrafts ? [...ids].filter((id) => !seenDrafts.has(id)) : [];
  if (fresh.length) {
    badge.classList.remove("fresh");
    void badge.offsetWidth;  // reflow so the animation restarts
    badge.classList.add("fresh");
    setTimeout(() => badge.classList.remove("fresh"), 1200);
    chime();
  }
  seenDrafts = new Set([...(seenDrafts || []), ...ids]);
  lastPendingCount = count;
  badge.textContent = count || "";
  document.title = (count ? `(${count}) ` : "") + "Support Agent console";
}

async function updatePending() {
  try { notePending(await api("/api/drafts")); } catch {}
}

async function loadDrafts() {
  const drafts = await api("/api/drafts");

  // Track IDs for J/K navigation, in display order (newest first).
  draftIds = drafts.map((d) => d.id);
  notePending(drafts);

  // The open draft left the list: someone reviewed it, a new message replaced
  // it, or its ticket was moved or closed in OTRS. Checked before anything
  // else, so it also clears when the list is now empty.
  let vanished = false;
  if (state.draftId && !drafts.some((d) => d.id === state.draftId)) {
    vanished = true;
    state.draftId = null;
    state.draft = null;
    state.edits = {};
    if (location.hash.startsWith("#draft-")) history.replaceState(null, "", location.pathname);
    const gone = el("div", "empty");
    gone.append(
      el("strong", null, "That draft is gone"),
      el("span", null, "It was reviewed, replaced by a newer message, or its ticket was moved or closed in OTRS.")
    );
    $("draft-detail").replaceChildren(gone);
  }

  const list = $("draft-list");
  list.replaceChildren();

  if (!drafts.length) {
    const empty = el("div", "empty");
    empty.append(
      el("strong", null, "Nothing waiting"),
      el("span", null, "Every draft has been reviewed.")
    );
    list.append(empty);
    // Keep "That draft is gone" if that just happened: it says why.
    if (!state.draftId && !vanished) {
      $("draft-detail").replaceChildren(empty.cloneNode(true));
    }
    return;
  }

  for (const d of drafts) {
    const card = el("div", "draft-card");
    card.setAttribute("aria-current", String(d.id === state.draftId));

    const top = el("div", "row");
    top.append(
      el("span", "pill " + (d.reply_kind || d.action), d.reply_kind || d.action),
      el("span", "num", d.ticket_number)
    );
    // Urgent drafts are listed first, and marked so they are seen at once.
    if (d.urgent) top.append(el("span", "pill urgent", "urgent"));
    if (d.junk) top.append(el("span", "pill junk", "junk?"));

    const title = el("div", "title", d.title || "(no subject)");
    title.dir = dir(d.title);

    // Label both fields -- "Product Support · high confidence" reads
    // fine to someone who has used the console for a week, but a new
    // reviewer does not know the second word is the confidence.
    const meta = el("div", "meta",
      `Queue: ${d.queue.replace("Ole5 New::", "")}  ·  Confidence: ${d.confidence}`);

    card.append(top, title, meta);
    card.addEventListener("click", () => openDraft(d.id));
    list.append(card);
  }

}

async function openDraft(id) {
  state.draftId = id;
  // The address names the draft, so it can be linked -- the urgent email does.
  history.replaceState(null, "", "#draft-" + id);
  state.edits = {};
  state.draft = await api("/api/drafts/" + id);
  paintDraft();
  loadDrafts();
}

// Move the selected draft by +1 (newer) or -1 (older) in the list.
// Called by the J/K and ↑/↓ shortcuts. No-op at the edges, so
// holding J does not wrap around.
function navigateDrafts(delta) {
  if (!draftIds.length) return;
  const idx = draftIds.indexOf(state.draftId);
  let nextId;
  if (idx === -1) {
    // No current selection -- pick the appropriate end.
    nextId = delta > 0 ? draftIds[0] : draftIds[draftIds.length - 1];
  } else {
    const nextIdx = idx + delta;
    if (nextIdx < 0 || nextIdx >= draftIds.length) return;
    nextId = draftIds[nextIdx];
  }
  if (nextId != null && nextId !== state.draftId) openDraft(nextId);
}

function field(key, label, value, options) {
  const wrap = el("div", "field");
  wrap.dataset.key = key;
  wrap.append(el("label", null, label));

  let input;
  if (options) {
    input = el("select");
    // The agent's value may have been disabled on the Configuration page
    // since the draft was made. Without this the browser would silently
    // show the first option instead, and the reviewer would be approving
    // something other than what they see.
    if (value && !options.includes(value)) {
      const o = el("option", null, value + " (disabled)");
      o.value = value;
      o.selected = true;
      input.append(o);
    }
    for (const option of options) {
      const o = el("option", null, option);
      o.value = option;
      if (option === value) o.selected = true;
      input.append(o);
    }
  } else if (key === "reply_body") {
    input = el("textarea");
    input.value = value || "";
    // dir="auto" lets the browser resolve direction per-paragraph as
    // the reviewer types -- an Arabic reply mixed with an English
    // signature renders both correctly. The CSS rule on .field
    // textarea adds unicode-bidi: plaintext for the same reason.
    input.dir = "auto";
  } else {
    input = el("input");
    input.type = "text";
    input.value = value == null ? "" : value;
  }

  const original = value == null ? "" : String(value);
  input.addEventListener("input", () => {
    if (input.value === original) delete state.edits[key];
    else state.edits[key] = input.value;
    wrap.classList.toggle("changed", input.value !== original);
    paintActions();
  });
  // Textareas auto-grow to fit content. The listener fires on every
  // keystroke; autoGrowTextarea is cheap (one style write).
  if (input.tagName.toLowerCase() === "textarea") {
    input.addEventListener("input", () => autoGrowTextarea(input));
  }
  input.addEventListener("change", () => input.dispatchEvent(new Event("input")));

  wrap.append(input);
  return wrap;
}

// Why the draft is urgent, and who was told. At the top of the draft: if
// someone is already on the phone about it, the reviewer should know first.
function urgentSection(d) {
  const u = d.urgent;
  if (!u) return el("span");

  const box = el("div", "urgent-box");
  box.append(el("div", "urgent-label", "URGENT"));
  if (u.by_keyword) {
    box.append(el("div", "urgent-why",
      `Matched keyword${u.keywords.length > 1 ? "s" : ""}: ${u.keywords.join(", ")}`));
  }
  if (u.by_agent) {
    const why = el("div", "urgent-why",
      `The agent judged it urgent: ${u.agent_reason || "no reason given"}`);
    why.dir = dir(u.agent_reason || "");
    box.append(why);
  }
  const emails = u.emails || [];
  const said = {
    sent: "emailed", dry_run: "not sent (dry run)",
    skipped: "not sent (no mail server)", failed: "failed",
  };
  box.append(el("div", "urgent-mail", emails.length
    ? emails.map((e) => `${e.to_address}: ${said[e.status] || e.status}`).join("  ·  ")
    : "Nobody emailed: no recipients set."));
  return box;
}

// A warning when the ticket resembles the junk the team throws away. Shown
// above everything else, because it changes how the rest should be read. It
// is only a flag: the agent decided as usual, and the reviewer decides now.
function junkSection(d) {
  const j = d.junk;
  if (!j || !j.flagged) return el("span");        // nothing to say

  const box = el("div", "junk-warn");
  const head = el("div", "junk-head");
  head.append(el("span", "junk-label", "LOOKS LIKE JUNK"));
  if (j.score) head.append(el("span", "junk-score", `closest match ${j.score.toFixed(2)}`));
  box.append(head);
  box.append(el("div", "junk-reason", j.reason || ""));

  for (const m of (j.matches || []).slice(0, 3)) {
    const row = el("div", "junk-match");
    row.append(el("span", "junk-number", m.ticket_number));
    const t = el("span", "junk-text", (m.text || "").slice(0, 120));
    t.dir = dir(m.text || "");
    row.append(t);
    box.append(row);
  }
  return box;
}

// Similar past tickets: a compact row of ticket numbers, closest first. Each
// opens the whole ticket in the viewer -- its route, what happened, and the
// cleaned conversation. The draft stays about the decision; the history is a
// click away.
function similarSection(d) {
  const box = el("div", "similar");
  const list = d.similar || [];
  box.append(el("div", "section-label",
    list.length ? `Similar past tickets (${list.length})` : "Similar past tickets"));

  if (!list.length) {
    const why = {
      none: "No close matches.",
      failed: "The search failed: the embedding service didn't answer.",
      no_index: "The history index wasn't built yet.",
    }[d.similar_status] || (d.similar_index ? "No close matches." : "The history index isn't built yet.");
    box.append(el("div", "empty-meta", why));
    return box;
  }

  // Ticket number and similarity, closest first. Nothing else: the route,
  // the journey and the conversation open in the viewer on a click.
  const rows = el("div", "similar-list");
  for (const s of [...list].sort((a, b) => (b.score || 0) - (a.score || 0))) {
    const b = el("button", "similar-line");
    b.append(el("span", "similar-number", s.ticket_number),
             el("span", "similar-score", (s.score || 0).toFixed(2)));
    b.title = "Open this ticket";
    b.addEventListener("click", () => openPastTicket(s.ticket_number));
    rows.append(b);
  }
  box.append(rows);
  return box;
}

// ---------------------------------------------------------------- the ticket viewer
//
// A closed ticket, whole, in the inspector overlay the Tables page uses: its
// details, the route it took and why it moved, what happened (the journey),
// and the conversation as cleaned messages.

const KIND_LABEL = {
  customer: "Customer", reply: "Reply to customer", note: "Internal note",
  queue: "Arrived in queue",
};

async function openPastTicket(number) {
  let t;
  try {
    t = await api("/api/past-tickets/" + encodeURIComponent(number));
  } catch (err) {
    toast(err.message, "bad");
    return;
  }
  $("inspector-title").textContent = `ticket ${t.ticket_number}`;
  const body = $("inspector-body");
  body.replaceChildren();
  const view = el("div", "ticket-view");

  const title = el("h3", "tv-title", t.title || "(no subject)");
  title.dir = dir(t.title || "");
  view.append(title);

  const facts = el("div", "tv-facts");
  if (t.junk) facts.append(el("span", "pill junk", "junk"));
  for (const v of [t.queue, t.type, (t.subtype || "").replace(/^OLE5::/, "")]) {
    if (v) facts.append(el("span", "chip", v.replace(/^Ole5 New::/, "")));
  }
  const dates = [t.otrs_created_at && "opened " + fmtTime(t.otrs_created_at),
                 t.otrs_closed_at && "closed " + fmtTime(t.otrs_closed_at)].filter(Boolean);
  if (dates.length) facts.append(el("span", "tv-dates", dates.join(" · ")));
  view.append(facts);

  // The route, and why each move happened where the messages said.
  if ((t.path || []).length > 1) {
    view.append(el("div", "section-label", "Route"));
    view.append(el("div", "tv-route", t.path.map((q) => q.split("::").pop()).join("  →  ")));
    const moves = (t.moves || []).filter((m) => m.why && m.why !== "not stated");
    for (const m of moves) {
      const line = el("div", "tv-move");
      line.append(el("span", "tv-move-arrow",
        `${m.from.split("::").pop()} → ${m.to.split("::").pop()}`),
        document.createTextNode(m.why));
      view.append(line);
    }
    const silent = (t.moves || []).length - moves.length;
    if (silent > 0) view.append(el("div", "empty-meta",
      `${silent} move${silent > 1 ? "s" : ""} without a stated reason.`));
  }

  // What happened, from the journey extraction.
  if (t.journey) {
    view.append(el("div", "section-label", "What happened"));
    view.append(el("p", "tv-summary", t.journey));
    if ((t.steps || []).length) {
      const ol = el("ol", "tv-steps");
      for (const st of t.steps) {
        const li = el("li");
        if (st.queue) li.append(el("span", "chip dim", st.queue.split("::").pop()));
        li.append(document.createTextNode(" " + st.what));
        ol.append(li);
      }
      view.append(ol);
    }
  }

  // The conversation, cleaned: signatures, disclaimers and quoted history gone.
  const entries = t.entries || [];
  view.append(el("div", "section-label",
    entries.length ? `The conversation (${entries.filter((e) => e.kind !== "queue").length} messages)`
                   : "The message"));
  if (!entries.length) {
    const msg = el("div", "tv-msg");
    const text = el("div", "tv-text", t.search_text || "(no text)");
    text.dir = dir(t.search_text || "");
    msg.append(text);
    view.append(msg);
  }
  for (const e of entries) {
    if (e.kind === "queue") {
      view.append(el("div", "tv-arrival", `→ ${e.queue}` + (e.at ? `  ·  ${e.at}` : "")));
      continue;
    }
    const msg = el("div", "tv-msg tv-" + e.kind);
    const head = el("div", "tv-msg-head");
    head.append(el("span", "tv-kind", KIND_LABEL[e.kind] || e.kind));
    if (e.from) head.append(el("span", "tv-from", e.from));
    if (e.at) head.append(el("span", "tv-at", e.at));
    msg.append(head);
    const text = el("div", "tv-text", e.text || "");
    text.dir = dir(e.text || "");
    msg.append(text);
    view.append(msg);
  }

  body.append(view);
  $("inspector").hidden = false;
}

// Why the junk check flagged a draft: the reason in numbers, the sender's
// history, and the nearest junk tickets -- each opens in the viewer.
function openJunk(j) {
  $("inspector-title").textContent = "why this looks like junk";
  const body = $("inspector-body");
  body.replaceChildren();
  const view = el("div", "ticket-view");
  view.append(el("p", "tv-summary", j.reason || "Flagged by the junk check."));

  const facts = el("div", "tv-facts");
  facts.append(el("span", "chip", `closest junk match ${(j.score || 0).toFixed(2)}`));
  facts.append(el("span", "chip dim",
    `sender's domain: ${j.sender_junk} junk, ${j.sender_real} real`));
  view.append(facts);
  view.append(el("p", "empty-meta",
    "A flag only. Nothing is closed because of it."));

  view.append(el("div", "section-label", "Nearest junk tickets"));
  for (const m of j.matches || []) {
    const b = el("button", "junk-match-row");
    b.append(el("span", "similar-number", m.ticket_number),
             el("span", "tv-at", (m.score || 0).toFixed(2)));
    const t = el("span", "junk-text", m.text || "");
    t.dir = dir(m.text || "");
    b.append(t);
    b.addEventListener("click", () => openPastTicket(m.ticket_number));
    view.append(b);
  }
  body.append(view);
  $("inspector").hidden = false;
}

function paintDraft() {
  const d = state.draft;
  const o = state.options;
  const panel = $("draft-detail");
  panel.replaceChildren();

  // head -- three chips on the left, the ticket title on the far
  // right. Title is the actual d.title from the draft, not a hardcoded
  // label -- so the reviewer can see which ticket this is at a glance.
  const head = el("div", "detail-head");
  head.append(el("span", "n", "#" + d.id));

  const decision = el("span", "chip " + (d.reply_kind || d.action));
  decision.append(
    el("span", "chip-key", "decision:"),
    document.createTextNode(" " + (d.reply_kind || d.action))
  );
  const confidence = el("span", "chip " + d.confidence);
  confidence.append(
    el("span", "chip-key", "confidence:"),
    document.createTextNode(" " + d.confidence)
  );
  // The ticket title. margin-left: auto (in CSS .detail-head h2) pushes
  // it to the far right; dir is set per-content so Arabic titles render
  // RTL.
  const title = el("h2", null, d.title || "(no subject)");
  title.dir = dir(d.title);

  head.append(decision, confidence);
  // Junk is a flag on the decision, not a section of the page: a chip beside
  // the others, and the evidence one click away.
  // Shown when the junk check flagged it, or when the agent itself chose the
  // Junk queue: either way the reviewer should see it at a glance.
  const agentJunk = /junk/i.test(d.queue || "");
  if ((d.junk && d.junk.flagged) || agentJunk) {
    const junk = el("button", "chip junk junk-chip");
    const flagged = d.junk && d.junk.flagged;
    junk.append(el("span", "chip-key", "junk?"),
                document.createTextNode(flagged ? " " + (d.junk.score || 0).toFixed(2) : " agent"));
    junk.title = flagged ? "Why this looks like junk" : "The agent chose the Junk queue";
    if (d.junk) junk.addEventListener("click", () => openJunk(d.junk));
    head.append(junk);
  }
  head.append(title);
  panel.append(head);

  const grid = el("div", "detail-grid");

  // left: what the customer sent
  const left = el("div");
  left.append(urgentSection(d));
  left.append(el("div", "section-label", "From the customer"));

  const who = el("dl", "kv");
  const add = (k, v) => {
    if (!v) return;
    who.append(el("dt", null, k), el("dd", null, v));
  };
  add("Email", d.requester_email);
  add("Customer", d.customer_org || d.customer_id_otrs);
  add("Ticket", d.ticket_number);
  add("Arrived", fmtTime(d.otrs_created_at));
  left.append(who);

  const request = (d.articles || []).find((a) => a.party === "request");
  if (request) {
    left.append(el("div", "section-label", "The message"));
    const body = el("div", "message", request.body_clean || request.body_raw || "");
    body.dir = dir(request.body_clean || request.body_raw);
    left.append(body);
  }

  const others = (d.articles || []).filter((a) => a.party !== "request");
  if (others.length) {
    left.append(el("div", "section-label",
      `Also on the ticket (${others.length})`));
    for (const a of others) {
      const box = el("div", "message",
        `[${a.article_no}] ${a.sender_address || ""}\n\n` +
        (a.body_clean || a.body_raw || "").slice(0, 600));
      box.dir = dir(a.body_clean);
      left.append(box);
    }
  }

  left.append(similarSection(d));

  // right: what the agent proposes
  // The section that was here ("What the agent decided" / a summary
  // block) is gone. The summary was the agent's paraphrase of the
  // customer's message; the actual message is shown on the left, so
  // the paraphrase was redundant. The "print delivery statement"
  // label moved up into the detail-head as a far-right chip.
  const right = el("div");

  if (d.missing && d.missing.length) {
    right.append(el("div", "section-label", "Still unknown"));
    const chips = el("div", "chips");
    d.missing.forEach((m) => chips.append(el("span", "chip", m)));
    right.append(chips);
  }

  const fields = el("div", "fields");
  fields.append(field("action", "Action", d.action, o.action));
  if (d.action === "answer") {
    fields.append(field("reply_kind", "Reply kind", d.reply_kind, o.reply_kind));
    fields.append(field("reply_body", "SUGGESTED REPLY", d.reply_body));
  }
  fields.append(field("service", "Service", d.service, o.service));
  fields.append(field("queue", "Queue", d.queue, o.queue));
  fields.append(field("next_state", "Next state", d.next_state, o.next_state));
  fields.append(field("type", "Type", d.type, o.type));
  fields.append(field("subtype", "SubType", d.subtype, o.subtype));
  fields.append(field("priority", "Priority", d.priority, o.priority));
  fields.append(field("sla", "SLA", d.sla, o.sla));
  fields.append(field("conclusions", "Conclusions", d.conclusions));
  right.append(el("div", "section-label", "Classification"), fields);

  // The previous "Why" wrapper is gone. The three things it bundled --
  // reasoning, the sources the agent relied on, and the queue reason --
  // each get their own labelled section so a reviewer scanning down the
  // right column finds them as peers rather than nested children.

  right.append(el("div", "section-label", "Reasoning"));
  const reasoningBody = el("div", "message", d.reasoning || "");
  reasoningBody.dir = dir(d.reasoning);
  right.append(reasoningBody);

  // Based on -- always shown, even when the answer is "nothing".
  // Empty state is more useful than hiding the section.
  right.append(el("div", "section-label", "Based on"));
  const searches = ((d.evidence || {}).searches || []);
  const sources = new Set();
  searches.forEach((s) =>
    (s.evidence || []).forEach((c) => {
      if ((c.relation || "").toUpperCase() === "DIRECT" && c.document)
        sources.add(c.document);
    })
  );
  if (sources.size) {
    const chips = el("div", "chips");
    [...sources].forEach((name) => chips.append(el("span", "chip", name)));
    right.append(chips);
  } else {
    right.append(el("div", "empty-meta",
      "Nothing in the knowledge base answers this directly."));
  }

  // Queue reason -- the free-text justification the agent gave for the
  // queue it picked. Already in the API response (d.* includes it), just
  // not surfaced before.
  right.append(el("div", "section-label", "Queue reason"));
  if (d.queue_reason) {
    const qrBody = el("div", "message", d.queue_reason);
    qrBody.dir = dir(d.queue_reason);
    right.append(qrBody);
  } else {
    right.append(el("div", "empty-meta",
      "No queue reason was recorded for this draft."));
  }

  // Note from the agent -- shown only when present, same as before.
  if (d.note) {
    right.append(el("div", "section-label", "Note from the agent"));
    const noteBody = el("div", "message", d.note);
    noteBody.dir = dir(d.note);
    right.append(noteBody);
  }

  // Corrections the validator applied, shown only when present.
  const corrections = (d.evidence || {}).corrections || [];
  if (corrections.length) {
    right.append(el("div", "section-label", "Corrected"));
    const chips = el("div", "chips");
    corrections.forEach((c) => chips.append(el("span", "chip", c)));
    right.append(chips);
  }

  grid.append(left, right);
  panel.append(grid);

  // actions
  const actions = el("div", "detail-actions");
  // The why-justification is now a textarea, so typing wraps to a new
  // line when it reaches the right edge. Auto-grows so it never scrolls
  // internally either.
  const whyInput = el("textarea", "why");
  whyInput.id = "why";
  whyInput.dir = "auto";
  whyInput.rows = 1;
  whyInput.placeholder = "Why did you change it? (required when you do)";
  whyInput.addEventListener("input", () => autoGrowTextarea(whyInput));

  const actionsRow = el("div", "actions-row");
  actionsRow.append(
    Object.assign(el("button", "btn approve", "Approve"), { id: "do-approve" }),
    Object.assign(el("button", "btn reject", "Reject"), { id: "do-reject" })
  );
  actions.append(whyInput, actionsRow);
  panel.append(actions);

  // Now that everything is in the DOM, fit every textarea to its content.
  // This handles the agent's existing reply (loaded into the textarea in
  // field()) and any pre-existing justification.
  panel.querySelectorAll("textarea").forEach(autoGrowTextarea);

  $("do-approve").addEventListener("click", approveDraft);
  $("do-reject").addEventListener("click", rejectDraft);
  paintActions();
}

function paintActions() {
  const changed = Object.keys(state.edits).length;
  const button = $("do-approve");
  if (button) button.textContent = changed ? `Approve with ${changed} change${changed > 1 ? "s" : ""}` : "Approve";
}

// After a decision: open the draft that now sits where this one was, or show
// the empty state when none are left. Without this the decided draft stayed on
// the right until something else was clicked.
async function afterDecision(decidedId) {
  const at = draftIds.indexOf(decidedId);
  state.draftId = null;
  state.draft = null;
  state.edits = {};
  history.replaceState(null, "", location.pathname);
  await loadDrafts();
  if (draftIds.length) {
    const next = draftIds[Math.min(Math.max(at, 0), draftIds.length - 1)];
    await openDraft(next);
  } else {
    const empty = el("div", "empty");
    empty.append(el("strong", null, "Nothing waiting"),
                 el("span", null, "Every draft has been reviewed."));
    $("draft-detail").replaceChildren(empty);
  }
}

async function approveDraft() {
  const justification = $("why").value.trim();
  if (Object.keys(state.edits).length && !justification) {
    toast("Say why you changed it", "bad");
    $("why").focus();
    return;
  }
  try {
    const out = await post(`/api/drafts/${state.draftId}/approve`, {
      final: state.edits,
      justification: justification || null,
    });
    toast(out.changed_fields.length
      ? `Approved with changes to ${out.changed_fields.join(", ")}`
      : "Approved", "good");
    await afterDecision(state.draftId);
  } catch (err) {
    toast(err.message, "bad");
  }
}

async function rejectDraft() {
  const justification = $("why").value.trim();
  if (!justification) {
    toast("A rejection needs a reason", "bad");
    $("why").focus();
    return;
  }
  try {
    await post(`/api/drafts/${state.draftId}/reject`, { justification });
    toast("Rejected", "good");
    await afterDecision(state.draftId);
  } catch (err) {
    toast(err.message, "bad");
  }
}

// ---------------------------------------------------------------- knowledge

async function loadDocuments() {
  const docs = await api("/api/documents");
  const box = $("docs");
  box.replaceChildren();

  if (!docs.length) {
    const empty = el("div", "empty");
    empty.append(
      el("strong", null, "Nothing indexed"),
      el("span", null, "Upload what the agent should answer from.")
    );
    box.append(empty);
    return;
  }

  for (const d of docs) {
    const row = el("div", "doc");
    const name = el("div");
    name.append(
      el("div", null, d.filename),
      el("div", "sub",
        [d.chunk_count ? d.chunk_count + " chunks" : null,
         d.size_bytes ? Math.round(d.size_bytes / 1024) + " KB" : null,
         d.uploaded_by].filter(Boolean).join(" · "))
    );

    const remove = el("button", "link", "Remove");
    remove.addEventListener("click", async () => {
      if (!confirm(`Remove ${d.filename} from the knowledge base?`)) return;
      try {
        await api("/api/documents/" + d.id, { method: "DELETE" });
        toast(`${d.filename} removed`, "good");
      } catch (err) {
        toast(err.message, "bad");
      }
      loadDocuments();
    });

    row.append(
      name,
      el("span", "sub", fmtTime(d.created_at)),
      el("span", "status " + d.status, d.status),
      remove
    );
    if (d.error) row.append(el("div", "err", d.error));
    box.append(row);
  }
}

$("pick").addEventListener("click", () => $("file").click());

// ---------------------------------------------------------------- knowledge: the three parts
//
// The documents (searched through the index), the team scopes (one document,
// read whole by the agent), and the urgent keywords. One page, three panels.

async function loadKnowledge() {
  for (const b of document.querySelectorAll("#ktabs [data-ktab]")) {
    b.setAttribute("aria-current", String(b.dataset.ktab === state.ktab));
  }
  for (const k of ["kb", "scopes", "keywords"]) $("kpanel-" + k).hidden = k !== state.ktab;
  if (state.ktab === "kb") await loadDocuments();
  else if (state.ktab === "scopes") await loadScopes();
  else { await loadKeywords(); await loadRecipients(); }
}

for (const b of document.querySelectorAll("#ktabs [data-ktab]")) {
  b.addEventListener("click", () => { state.ktab = b.dataset.ktab; loadKnowledge(); });
}

// Someone is typing a keyword: a refresh would throw it away.
function knowledgeBusy() {
  return ["kw-input", "rc-email", "rc-name"].some((id) => {
    const input = $(id);
    return !!input && (document.activeElement === input || input.value.trim() !== "");
  });
}

async function loadScopes() {
  const { scopes } = await api("/api/team-scopes");
  const meta = $("scopes-meta");
  const doc = $("scopes-doc");
  $("scopes-download").hidden = !scopes;
  if (!scopes) {
    meta.textContent = "";
    doc.replaceChildren(el("div", "empty-meta",
      "No team scopes uploaded yet."));
    return;
  }
  meta.textContent = `${scopes.filename || "team_scopes.md"} · uploaded by ` +
    `${scopes.uploaded_by || "unknown"} · ${fmtTime(scopes.created_at)} · ` +
    `version ${scopes.versions}`;
  // Shown as written: the Markdown is the document the agent reads, so the
  // page shows exactly that, not a rendering of it.
  const pre = el("pre", "scopes-text", scopes.content);
  pre.dir = "auto";
  doc.replaceChildren(pre);
}

$("scopes-upload").addEventListener("click", () => $("scopes-file").click());
$("scopes-file").addEventListener("change", async () => {
  const file = $("scopes-file").files[0];
  $("scopes-file").value = "";
  if (!file) return;
  if (!confirm(`Replace the team scopes with ${file.name}?\n\nThe agent uses it from the ` +
               `next ticket on. The current version is kept.`)) return;
  const body = new FormData();
  body.append("file", file);
  try {
    await api("/api/team-scopes", { method: "POST", body });
    toast("Team scopes updated", "good");
  } catch (err) {
    toast(err.message, "bad");
  }
  loadScopes();
});

async function loadRecipients() {
  const list = $("rc-list");
  const people = await api("/api/urgent-recipients");
  list.replaceChildren();
  if (!people.length) {
    list.append(el("div", "empty-meta",
      "No recipients yet."));
    return;
  }
  for (const p of people) {
    const row = el("div", "cfg-row kw-row");
    // Name and address on separate lines: an Arabic name beside a Latin
    // address on one line reorders itself unpredictably.
    const who = el("div", "cfg-value rc-who");
    if (p.name) {
      const n = el("div", "rc-name", p.name);
      n.dir = "auto";
      who.append(n);
    }
    who.append(el("div", "rc-email", p.email));
    row.append(who,
               el("div", "kw-meta", `added by ${p.added_by || "unknown"} · ${fmtTime(p.created_at)}`));
    const remove = el("button", "link", "Remove");
    remove.addEventListener("click", async () => {
      if (!confirm(`Stop emailing ${p.email} about urgent tickets?`)) return;
      try {
        await api("/api/urgent-recipients/" + p.id, { method: "DELETE" });
        toast(`${p.email} removed`, "good");
      } catch (err) { toast(err.message, "bad"); }
      loadRecipients();
    });
    row.append(remove);
    list.append(row);
  }
}

async function addRecipient() {
  const email = $("rc-email").value.trim();
  const name = $("rc-name").value.trim();
  if (!email) { $("rc-email").focus(); return; }
  try {
    await post("/api/urgent-recipients", { email, name });
    toast(`${email} added`, "good");
    $("rc-email").value = ""; $("rc-name").value = "";
    document.activeElement.blur();
  } catch (err) { toast(err.message, "bad"); return; }
  loadRecipients();
}
$("rc-add").addEventListener("click", addRecipient);
for (const id of ["rc-email", "rc-name"]) {
  $(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addRecipient(); }
  });
}

async function loadKeywords() {
  const list = $("kw-list");
  const words = await api("/api/urgent-keywords");
  list.replaceChildren();
  if (!words.length) {
    list.append(el("div", "empty-meta", "No urgent keywords yet."));
    return;
  }
  for (const w of words) {
    const row = el("div", "cfg-row kw-row");
    const word = el("div", "cfg-value", w.keyword);
    word.dir = "auto";
    const meta = el("div", "kw-meta",
      `added by ${w.added_by || "unknown"} · ${fmtTime(w.created_at)}`);
    const remove = el("button", "link", "Remove");
    remove.addEventListener("click", async () => {
      if (!confirm(`Remove "${w.keyword}"? Tickets containing it will no longer be marked urgent.`)) return;
      try {
        await api("/api/urgent-keywords/" + w.id, { method: "DELETE" });
        toast(`${w.keyword} removed`, "good");
      } catch (err) {
        toast(err.message, "bad");
      }
      loadKeywords();
    });
    row.append(word, meta, remove);
    list.append(row);
  }
}

async function addKeyword() {
  const input = $("kw-input");
  const keyword = input.value.trim();
  if (!keyword) { input.focus(); return; }
  try {
    await post("/api/urgent-keywords", { keyword });
    toast(`${keyword} added`, "good");
    input.value = "";
    input.blur();
  } catch (err) {
    toast(err.message, "bad");
    return;
  }
  loadKeywords();
}
$("kw-add").addEventListener("click", addKeyword);
$("kw-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); addKeyword(); }
});
$("file").addEventListener("change", (e) => upload(e.target.files));

const drop = $("drop");
["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("over");
  })
);
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("over");
  })
);
drop.addEventListener("drop", (e) => upload(e.dataTransfer.files));

async function upload(files) {
  for (const file of files) {
    const body = new FormData();
    body.append("file", file);
    try {
      const out = await api("/api/documents", { method: "POST", body });
      toast(out.duplicate ? `${file.name} is already indexed`
                          : `${file.name} is being indexed`, "good");
    } catch (err) {
      toast(`${file.name}: ${err.message}`, "bad");
    }
  }
  loadDocuments();
}

// ---------------------------------------------------------------- dashboard

async function loadDashboard() {
  const d = await api("/api/dashboard");
  const box = $("dash");
  box.replaceChildren();

  // Pipeline traffic -- the through-line of the whole system. Counts at
  // each stage, with arrows that pulse when a stage has work waiting.
  box.append(renderPipeline(d));

  // KPI hero row -- five cards including the tickets-arrived total.
  box.append(renderKPIs(d));

  // Time-series chart: polled tickets per day for the last 14 days.
  // One line, no overlays -- answers "is today's intake unusual?"
  // without the noise of the old two-line chart.
  box.append(renderTimeseries(d));

  // Composition -- two donut charts side by side.
  box.append(renderComposition(d));

  // Distributions -- top corrected fields and by-queue, side by side.
  box.append(renderDistributions(d));

  // The bottom outbox status row is gone -- the Outbox stage left the
  // pipeline (replaced by Rejected), so showing outbox numbers down
  // here would be orphan data. The background send task still runs
  // after every approval.
}

// ------------------------------------------------------------- pipeline traffic

function renderPipeline(d) {
  const wrap = el("div", "pipeline");

  const drafts = d.drafts.total ?? 0;
  const pending = d.drafts.pending ?? 0;
  const approved = d.drafts.approved ?? 0;

  const stages = [
    { n: drafts, l: "Drafts" },
    { n: pending, l: "Pending review", hot: pending > 0 },
    { n: approved, l: "Approved" },
    { n: d.drafts.rejected ?? 0, l: "Rejected" },
    // Edited -- drafts a reviewer changed before approving. Sits
    // alongside Rejected as the second 'reviewer got involved' outcome.
    { n: d.reviews.edited ?? 0, l: "Edited" },
  ];

  stages.forEach((s, i) => {
    const stage = el("div", "pipe-stage" + (s.hot ? " hot" : ""));
    stage.append(el("div", "n", String(s.n)), el("div", "l", s.l));
    wrap.append(stage);
    // Nothing between stages -- the user wanted it that way. The flex
    // layout's gap: 0 plus each stage's own padding gives enough air.
  });

  return wrap;
}

// ------------------------------------------------------------- KPI hero row

function renderKPIs(d) {
  const wrap = el("div", "kpi-row");

  // 1. Clean-approval rate -- the headline metric. Bigger card, with
  //    the hint showing the denominator so the percentage has context.
  const clean = d.reviews.clean_rate;
  const reviewed = d.reviews.reviewed ?? 0;
  const cleanCard = el("div", "kpi kpi-hero");
  cleanCard.append(
    el("div", "n", clean == null ? "—" : clean + "%"),
    el("div", "l", "Approved unchanged"),
    el("div", "h", `out of ${reviewed} drafts`)
  );
  wrap.append(cleanCard);

  // 2. Pending review count -- the work waiting right now.
  wrap.append(kpiCard(String(d.drafts.pending ?? 0), "Waiting for review"));

  // 3. Average wait for review.
  const waitMin = d.waiting_minutes;
  wrap.append(kpiCard(
    waitMin == null ? "—" : waitMin + "m",
    "Average wait for review"
  ));

  // 4. Average time to draft.
  const draftMs = d.latency.avg_ms;
  wrap.append(kpiCard(
    draftMs == null ? "—" : Math.round(draftMs / 1000) + "s",
    "Average time to draft"
  ));

  // 5. Tickets arrived -- the total count of tickets ever ingested.
  //    Replaces the time-series chart; one number, no chart noise.
  wrap.append(kpiCard(String(d.tickets_total ?? 0), "Tickets arrived"));

  return wrap;
}

function kpiCard(n, label) {
  const c = el("div", "kpi");
  c.append(el("div", "n", n), el("div", "l", label));
  return c;
}

// ------------------------------------------------------------- donut chart

// SVG donut. Slices are objects: { value, color, label }. total is the
// sum the slices are a fraction of (usually sum of values, but passed
// separately so empty slices can still resolve to a fraction).
const SVG_NS = "http://www.w3.org/2000/svg";

// Colour per ticket type. 12 types in the canonical list, each with a
// warm-toned colour that fits the cream-and-dark palette. Reused
// accent colours where they make sense (sage for Default, terracotta
// for Complaints, etc.); the rest are muted warm hues that read as a
// family. Unknown types fall back to a neutral warm gray.
const TYPE_COLORS = {
  "Complaints":               "#b07065",  // terracotta (matches --rose)
  "Default":                  "#9eb088",  // sage (matches --seal)
  "External / Vendor Support": "#9a8a8e",  // mauve (matches --violet)
  "Feedback / Suggestion":   "#c9a878",  // tan (matches --amber)
  "Incident / Urgent":        "#a86548",  // rust
  "Inquiry":                  "#d4c8a8",  // light cream
  "Internal Tickets":         "#8a96a8",  // dusty blue
  "Issue / Problem":          "#6a8068",  // dark sage
  "Junk":                     "#6a6862",  // warm gray (matches --slate)
  "New requirement":          "#b8a070",  // sand
  "Request":                  "#8a9070",  // olive
  "Service Failure":          "#c89070",  // apricot
};
const TYPE_COLOR_FALLBACK = "#7a786e";
function typeColor(name) {
  return TYPE_COLORS[name] || TYPE_COLOR_FALLBACK;
}

function donut(slices, total, centerText, centerLabel) {
  const r = 38;
  const C = 2 * Math.PI * r;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.classList.add("donut");

  // Background track -- shows the full ring behind any slices.
  const track = document.createElementNS(SVG_NS, "circle");
  track.setAttribute("cx", 50);
  track.setAttribute("cy", 50);
  track.setAttribute("r", r);
  track.setAttribute("fill", "none");
  track.setAttribute("stroke", "var(--rule)");
  track.setAttribute("stroke-width", 9);
  svg.append(track);

  // Each slice is its own circle with stroke-dasharray. Rotated -90
  // around the centre so the first slice starts at the top.
  let cumulative = 0;
  for (const slice of slices) {
    if (slice.value <= 0) continue;
    const sliceLength = (slice.value / total) * C;
    // Tiny gap between slices so they read as separate even when the
    // colours are close.
    const gap = slices.length > 1 ? 1.2 : 0;
    const visible = Math.max(0, sliceLength - gap);

    const path = document.createElementNS(SVG_NS, "circle");
    path.setAttribute("cx", 50);
    path.setAttribute("cy", 50);
    path.setAttribute("r", r);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", slice.color);
    path.setAttribute("stroke-width", 9);
    path.setAttribute("stroke-dasharray", `${visible} ${C - visible}`);
    path.setAttribute("stroke-dashoffset", `-${cumulative}`);
    path.setAttribute("transform", "rotate(-90 50 50)");
    path.setAttribute("stroke-linecap", "round");
    svg.append(path);

    cumulative += sliceLength;
  }

  // Centre text -- the total above, the unit label below.
  if (centerText) {
    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", 50);
    text.setAttribute("y", 49);
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("dominant-baseline", "middle");
    text.setAttribute("font-size", 15);
    text.setAttribute("font-weight", 600);
    text.setAttribute("fill", "var(--text)");
    text.textContent = centerText;
    svg.append(text);
  }
  if (centerLabel) {
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", 50);
    label.setAttribute("y", 62);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("font-size", 6.5);
    label.setAttribute("letter-spacing", 0.5);
    label.setAttribute("fill", "var(--faint)");
    label.textContent = centerLabel;
    svg.append(label);
  }

  return svg;
}

function renderLegend(slices, total) {
  const legend = el("div", "legend");
  for (const s of slices) {
    const row = el("div", "legend-row");
    const swatch = el("span", "swatch");
    swatch.style.background = s.color;
    row.append(swatch);
    row.append(el("span", "legend-label", s.label));
    const pct = total > 0 ? Math.round((100 * s.value) / total) + "%" : "—";
    row.append(el("span", "legend-value", `${s.value}  \u00b7  ${pct}`));
    legend.append(row);
  }
  return legend;
}

// ------------------------------------------------------------- time series
// Single-line SVG chart of polled tickets per day for the last 14
// days. The backend returns it as d.ticket_history = [{day, n}, ...]
// with one row per day even on days nothing was polled (so the line
// drops to zero rather than skipping).

function renderTimeseries(d) {
  const data = d.ticket_history || [];
  const card = el("div", "timeseries-card");
  card.append(el("div", "l", "Polled tickets \u00b7 last 14 days"));

  if (!data.length) {
    card.append(el("div", "empty-meta", "No tickets polled yet."));
    return card;
  }

  // Whole-number steps: 0, 1, 2 when the busiest day had 2; for big numbers a
  // round step (1, 2 or 5 times a power of ten) giving at most 5 lines, so a
  // busy day reads 0, 50, 100, 150 rather than odd or repeated values.
  const peak = Math.max(0, ...data.map((p) => p.n));
  let step, max;
  if (peak <= 4) {
    step = 1;
    max = Math.max(1, peak);
  } else {
    const raw = peak / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    step = [1, 2, 5, 10].map((m) => m * mag).find((v) => v >= raw);
    max = Math.ceil(peak / step) * step;
  }
  const ticks = Math.round(max / step);
  const fmt = (v) => v >= 10000 ? `${Math.round(v / 1000)}k`
                   : v >= 1000 ? `${(v / 1000).toFixed(1).replace(/\.0$/, "")}k`
                   : String(v);
  const W = 700, H = 160;
  const padX = 36, padTop = 14, padBottom = 24;
  const innerW = W - padX * 2;
  const innerH = H - padTop - padBottom;

  const x = (i) => data.length > 1
    ? padX + (i / (data.length - 1)) * innerW
    : padX + innerW / 2;
  const y = (v) => padTop + innerH - (v / max) * innerH;

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.classList.add("timeseries");

  // Grid: one dashed line per step, labelled with whole numbers.
  for (let i = 0; i <= ticks; i++) {
    const gy = padTop + (i / ticks) * innerH;
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", padX);
    line.setAttribute("y1", gy);
    line.setAttribute("x2", W - padX);
    line.setAttribute("y2", gy);
    line.setAttribute("stroke", "var(--rule)");
    line.setAttribute("stroke-width", 1);
    line.setAttribute("stroke-dasharray", "2 4");
    svg.append(line);

    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("x", padX - 6);
    label.setAttribute("y", gy + 3);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("font-size", 10);
    label.setAttribute("fill", "var(--faint)");
    label.textContent = fmt(max - i * step);
    svg.append(label);
  }

  // X-axis labels: every 3rd day, plus the last one.
  data.forEach((p, i) => {
    if (i % 3 !== 0 && i !== data.length - 1) return;
    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("x", x(i));
    label.setAttribute("y", H - 4);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("font-size", 10);
    label.setAttribute("fill", "var(--faint)");
    label.textContent = p.day.slice(5);  // MM-DD
    svg.append(label);
  });

  // The line.
  const points = data.map((p, i) => `${x(i)},${y(p.n)}`).join(" ");
  const line = document.createElementNS(svgNS, "polyline");
  line.setAttribute("points", points);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "var(--seal)");
  line.setAttribute("stroke-width", 2);
  line.setAttribute("stroke-linejoin", "round");
  line.setAttribute("stroke-linecap", "round");
  svg.append(line);

  // Dots on non-zero days.
  data.forEach((p, i) => {
    if (p.n === 0) return;
    const dot = document.createElementNS(svgNS, "circle");
    dot.setAttribute("cx", x(i));
    dot.setAttribute("cy", y(p.n));
    dot.setAttribute("r", 2.5);
    dot.setAttribute("fill", "var(--seal)");
    svg.append(dot);
  });

  card.append(svg);
  return card;
}

// ------------------------------------------------------------- composition

function renderComposition(d) {
  const wrap = el("div", "composition");

  // Donut 1: agent decisions (answer / route / ask_more). The
  // previous "Draft outcomes" donut was redundant with the pipeline
  // (which already shows approved/rejected); this surface what the
  // agent *did*, which is not visible elsewhere.
  const answers = (d.drafts.answers ?? 0) - (d.drafts.ask_more ?? 0);
  const routes = d.drafts.routes ?? 0;
  const askMore = d.drafts.ask_more ?? 0;
  const decisionTotal = answers + routes + askMore;
  const decisionSlices = [];
  if (answers)  decisionSlices.push(
    { value: answers, color: "var(--seal)",   label: "Answer" });
  if (routes)   decisionSlices.push(
    { value: routes,  color: "var(--violet)", label: "Route" });
  if (askMore)  decisionSlices.push(
    { value: askMore, color: "var(--amber)",  label: "Ask more" });

  wrap.append(compositionCard(
    "Agent decisions",
    decisionSlices,
    decisionTotal,
    String(decisionTotal),
    "decisions"
  ));

  // Donut 2: ticket types. Each type gets its own colour from the
  // TYPE_COLORS palette. Replaces the old "Review decisions" donut;
  // the review outcomes are already in the pipeline.
  const typeRows = d.by_type || [];
  const typeTotal = typeRows.reduce((s, r) => s + (r.n || 0), 0);
  const typeSlices = typeRows.map((r) => ({
    value: r.n,
    color: typeColor(r.type),
    label: r.type,
  }));

  wrap.append(compositionCard(
    "Ticket types",
    typeSlices,
    typeTotal,
    String(typeTotal),
    "drafts"
  ));

  return wrap;
}

function compositionCard(title, slices, total, centerText, centerLabel) {
  const card = el("div", "comp-card");
  card.append(el("div", "l", title));

  const inner = el("div", "comp-inner");
  if (total > 0) {
    inner.append(donut(slices, total, centerText, centerLabel));
  } else {
    const empty = el("div", "donut-empty");
    empty.append(el("div", "n", "0"), el("div", "l", centerLabel || "none"));
    inner.append(empty);
  }
  inner.append(renderLegend(slices, total));
  card.append(inner);
  return card;
}

// ------------------------------------------------------------- distributions

function renderDistributions(d) {
  const wrap = el("div", "distributions");

  // Top corrected fields -- what reviewers edit most often.
  wrap.append(distCard(
    "What reviewers change most",
    d.corrected_fields,
    (f) => f.field,
    (f) => f.n
  ));

  // By queue -- where drafts get routed.
  wrap.append(distCard(
    "Where drafts are routed",
    d.by_queue,
    (q) => (q.queue || "").replace("Ole5 New::", ""),
    (q) => q.n
  ));

  return wrap;
}

function distCard(title, items, labelFn, valueFn) {
  const card = el("div", "dist-card");
  card.append(el("div", "l", title));

  if (!items.length) {
    card.append(el("div", "empty-meta", "No data yet."));
    return card;
  }

  const bars = el("div", "dist-bars");
  const max = Math.max(...items.map(valueFn));
  for (const item of items) {
    const v = valueFn(item);
    const bar = el("div", "dist-bar");
    const track = el("div", "track");
    const fill = el("div", "fill");
    fill.style.width = Math.round((100 * v) / max) + "%";
    track.append(fill);
    bar.append(
      el("span", "label", labelFn(item)),
      track,
      el("span", "v", String(v))
    );
    bars.append(bar);
  }
  card.append(bars);
  return card;
}

// ------------------------------------------------------------- outbox status

function renderOutboxStatus(d) {
  const wrap = el("div", "outbox-status");
  const pending = d.outbox.pending ?? 0;
  const sent = d.outbox.sent ?? 0;
  const failed = d.outbox.failed ?? 0;

  wrap.append(el("span", "outbox-label", "Outbox"));

  const pendingPill = el("span",
    "outbox-pill" + (pending ? " hot" : " idle"),
    `${pending} pending`);
  wrap.append(pendingPill);

  wrap.append(el("span", "outbox-sep", "\u00b7"));
  wrap.append(el("span", "outbox-text", `${sent} written to OTRS`));

  if (failed) {
    wrap.append(el("span", "outbox-sep", "\u00b7"));
    wrap.append(el("span", "outbox-fail", `${failed} failed`));
  }

  if (!pending && !failed) {
    wrap.append(el("span", "outbox-ok", "All caught up"));
  }

  return wrap;
}

// ---------------------------------------------------------------- tables

async function loadTableIndex() {
  const tables = await api("/api/tables");
  const list = $("table-list");
  list.replaceChildren();

  for (const t of tables) {
    const b = el("button");
    b.append(el("span", null, t.name), el("span", "c", t.rows));
    b.setAttribute("aria-current", String(t.name === state.table));
    b.addEventListener("click", () => {
      state.table = t.name;
      state.offset = 0;
      state.search = "";
      $("tbl-search").value = "";
      loadTableIndex();
      loadTable();
    });
    list.append(b);
  }
  if (state.table) loadTable();
}

async function loadTable() {
  const params = new URLSearchParams({ limit: 50, offset: state.offset });
  if (state.search) params.set("search", state.search);
  const data = await api(`/api/tables/${state.table}?${params}`);

  const scroll = $("table-scroll");
  scroll.replaceChildren();

  if (!data.rows.length) {
    const empty = el("div", "empty");
    empty.append(el("strong", null, "Nothing here"),
                 el("span", null, state.search ? "No row matches that." : "The table is empty."));
    scroll.append(empty);
  } else {
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    data.columns.forEach((c) => hr.append(el("th", null, c)));
    thead.append(hr);

    const tbody = el("tbody");
    for (const row of data.rows) {
      const tr = el("tr");
      for (const c of data.columns) {
        const v = row[c];
        const td = el("td", v == null ? "null" : null,
          v == null ? "null" : typeof v === "object" ? JSON.stringify(v) : String(v));
        tr.append(td);
      }
      tr.addEventListener("click", () => inspect(state.table, row.id));
      tbody.append(tr);
    }
    table.append(thead, tbody);
    scroll.append(table);
  }

  const from = data.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + 50, data.total);
  $("tbl-range").textContent = `${from}–${to} of ${data.total}`;
  $("tbl-prev").disabled = state.offset === 0;
  $("tbl-next").disabled = to >= data.total;
}

$("tbl-prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - 50);
  loadTable();
});
$("tbl-next").addEventListener("click", () => {
  state.offset += 50;
  loadTable();
});

let searchTimer;
$("tbl-search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value.trim();
    state.offset = 0;
    loadTable();
  }, 250);
});

async function inspect(table, id) {
  if (id == null) return;
  const row = await api(`/api/tables/${table}/${id}`);
  $("inspector-title").textContent = `${table} · ${id}`;
  const body = $("inspector-body");
  const dl = el("dl", "kv");

  // audit_log rows carry resolved reviewer fields from the backend JOIN.
  // Pull them out of the generic row dictionary so they don't render as
  // their own dt/dd entries; instead they enrich the `actor` cell below.
  const audit = (table === "audit_log")
    ? {
        name: row.reviewer_name || null,
        email: row.reviewer_email || null,
        outcome: row.review_outcome || null,
      }
    : null;
  if (audit) {
    delete row.reviewer_name;
    delete row.reviewer_email;
    delete row.review_outcome;
    delete row.review_id;
    delete row.review_at;
  }

  for (const [k, v] of Object.entries(row)) {
    const dt = el("dt", null, k);
    const dd = el("dd");

    // Actor cell on audit_log: show the resolved name when the backend
    // found one (a human reviewer), otherwise fall back to the raw value
    // (e.g. "agent", "system" -- no review row, no name to resolve).
    if (k === "actor" && audit && audit.name) {
      dd.append(el("span", "actor-name", audit.name));
      if (audit.email) {
        const email = el("span", "actor-email", audit.email);
        email.dir = "ltr";  // emails are always LTR even in RTL contexts
        dd.append(el("span", "actor-sep", " · "), email);
      }
      if (audit.outcome) {
        dd.append(el("span", "actor-outcome",
          ` · ${audit.outcome}`));
      }
      dd.title = `raw actor: ${v}`;
      dl.append(dt, dd);
      continue;
    }

    if (v == null) {
      // Null is its own visual state, not a missing value. The faint italic
      // says "we looked, there is nothing here" without pretending it is
      // the string "null".
      dd.textContent = "null";
      dd.className = "null";
    } else if (looksLikeTrace(v)) {
      dd.append(renderTrace(v));
    } else if (looksLikeEvidence(v)) {
      // Evidence is the worst case for raw JSON -- nested searches, each
      // with an evidence array, plus corrections. Rendered as a structured
      // summary instead, with a "show raw" toggle.
      const rendered = renderEvidence(v);
      if (rendered) {
        dd.append(rendered);
      } else {
        dd.textContent = JSON.stringify(
          typeof v === "string" ? JSON.parse(v) : v, null, 2);
        dd.className = "json";
      }
    } else if (isIsoTime(v)) {
      // Every timestamp the DB returns is UTC ISO 8601. Show Riyadh
      // wall-clock, keep the raw value in a tooltip for audit.
      dd.textContent = fmtTime(v);
      dd.title = v;
    } else if (typeof v === "string" && (v.trim().startsWith("{") || v.trim().startsWith("["))) {
      // Other JSON strings: pretty-print them so a person can scan.
      try {
        const parsed = JSON.parse(v);
        dd.textContent = JSON.stringify(parsed, null, 2);
        dd.className = "json";
      } catch {
        dd.textContent = v;
        dd.dir = dir(v);
      }
    } else if (typeof v === "object") {
      dd.textContent = JSON.stringify(v, null, 2);
      dd.className = "json";
    } else {
      dd.textContent = String(v);
      dd.dir = dir(v);
    }

    dl.append(dt, dd);
  }

  body.replaceChildren(dl);
  $("inspector").hidden = false;
}

$("inspector-close").addEventListener("click", () => ($("inspector").hidden = true));
$("inspector").addEventListener("click", (e) => {
  if (e.target === $("inspector")) $("inspector").hidden = true;
});

// Shortcuts overlay -- same shell as the inspector.
$("shortcuts-open").addEventListener("click", () => {
  $("shortcuts").hidden = false;
});
$("shortcuts-close").addEventListener("click", () => {
  $("shortcuts").hidden = true;
});
$("shortcuts").addEventListener("click", (e) => {
  if (e.target === $("shortcuts")) $("shortcuts").hidden = true;
});

// Keyboard shortcuts. The rule: ignore keystrokes when the user is in
// a form field (typing a justification, editing the reply, searching
// a table) -- those have their own semantics. Escape closes whatever
// panel is open. Everything else only fires in the Review tab.
document.addEventListener("keydown", (e) => {
  // Escape closes any open panel, in priority order.
  if (e.key === "Escape") {
    const inspector = $("inspector");
    if (inspector && !inspector.hidden) { inspector.hidden = true; return; }
    const shortcuts = $("shortcuts");
    if (shortcuts && !shortcuts.hidden) { shortcuts.hidden = true; return; }
    return;
  }

  // ? toggles the shortcuts overlay from anywhere.
  if (e.key === "?") {
    e.preventDefault();
    const shortcuts = $("shortcuts");
    if (shortcuts) shortcuts.hidden = !shortcuts.hidden;
    return;
  }

  // Skip the rest if the user is in a form field.
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" ||
      e.target.isContentEditable) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  // / focuses the table search when the Tables tab is open.
  if (e.key === "/" && state.tab === "tables") {
    e.preventDefault();
    $("tbl-search").focus();
    return;
  }

  // Review-tab-only shortcuts.
  if (state.tab !== "review") return;

  if (e.key === "j" || e.key === "ArrowDown") {
    e.preventDefault();
    navigateDrafts(1);
  } else if (e.key === "k" || e.key === "ArrowUp") {
    e.preventDefault();
    navigateDrafts(-1);
  } else if (e.key === "a" || e.key === "A") {
    if (state.draftId) { e.preventDefault(); approveDraft(); }
  } else if (e.key === "r" || e.key === "R") {
    if (state.draftId) { e.preventDefault(); rejectDraft(); }
  } else if (e.key === "e" || e.key === "E") {
    if (state.draftId) {
      e.preventDefault();
      const why = $("why");
      if (why) why.focus();
    }
  }
});

// ---------------------------------------------------------------- configuration

// One kind at a time: seven lists side by side would be a wall. The kind
// switcher shows how many values each list has, so nothing is hidden.

const KIND_HINTS = {
  service: "The product the ticket is about. Not written to OTRS.",
  queue: "Where the ticket goes when it leaves the Support queue.",
  next_state: "The state the ticket is left in.",
  type: "What kind of ticket this is.",
  subtype: "The OLE5 SubType tree.",
  priority: "How much impact the ticket describes.",
  sla: "The service level the ticket falls under.",
};

// True while someone is typing on the page. A refresh would rebuild the
// inputs and throw their text away.
function configBusy() {
  const page = $("page-config");
  if (!page) return false;
  if (page.contains(document.activeElement) &&
      document.activeElement.tagName !== "BUTTON") return true;
  return [...page.querySelectorAll(".cfg-add input")].some((i) => i.value.trim());
}

async function loadConfig() {
  const data = await api("/api/config/options");
  const kinds = data.kinds;
  if (!kinds.some((k) => k.kind === state.cfgKind)) state.cfgKind = kinds[0].kind;

  // the switcher
  const nav = $("cfg-kinds");
  nav.replaceChildren();
  for (const k of kinds) {
    const count = data.options.filter((o) => o.kind === k.kind && o.active).length;
    const b = el("button", "cfg-kind");
    b.append(el("span", null, k.label), el("span", "c", String(count)));
    b.setAttribute("aria-current", String(k.kind === state.cfgKind));
    b.addEventListener("click", () => {
      state.cfgKind = k.kind;
      loadConfig();
    });
    nav.append(b);
  }

  const kind = state.cfgKind;
  const label = kinds.find((k) => k.kind === kind).label;
  const rows = data.options.filter((o) => o.kind === kind);

  const body = $("cfg-body");
  body.replaceChildren();
  body.append(el("p", "cfg-hint", KIND_HINTS[kind] || ""));
  body.append(guidanceBox(kind, (data.guidance || {})[kind] || ""));

  const missing = rows.filter((o) => !(o.description || "").trim()).length;
  if (missing) {
    body.append(el("div", "cfg-missing",
      `${missing} value${missing > 1 ? "s have" : " has"} no description yet. ` +
      "Add one: the agent only knows when to choose a value from its description."));
  }
  body.append(el("div", "section-label", "Values"));

  const list = el("div", "cfg-list");
  for (const o of rows) list.append(optionRow(o));
  body.append(list);

  body.append(addRow(kind, label));
}

// How to choose this field: the guidance about the field itself, above its
// values in the agent's prompt. Required; saved when the box loses focus.
function guidanceBox(kind, text) {
  const box = el("div", "cfg-guidance");
  box.append(el("div", "section-label", "How to choose"));
  const area = el("textarea", "cfg-guidance-text");
  area.dir = "auto";
  area.rows = Math.min(10, Math.max(3, Math.ceil(text.length / 110) + text.split("\n").length));
  area.value = text;
  area.placeholder = "How should the agent choose this field? (required)";
  const original = text;
  area.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { area.value = original; area.blur(); }
  });
  area.addEventListener("blur", async () => {
    if (area.value.trim() === original.trim()) return;
    if (!area.value.trim()) {
      toast("How to choose is required", "bad");
      area.value = original;
      return;
    }
    try {
      await api("/api/config/guidance/" + kind, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ guidance: area.value }),
      });
      toast("Saved. The agent uses it from the next ticket.", "good");
    } catch (err) {
      toast(err.message, "bad");
    }
    loadConfig();
  });
  box.append(area);
  return box;
}

function optionRow(o) {
  const described = !!(o.description || "").trim();
  const row = el("div", "cfg-row" + (o.active ? "" : " off") + (described ? "" : " missing"));

  const value = el("div", "cfg-value", o.value);
  value.dir = "ltr";

  // The description is what the agent reads to learn when to use a value it
  // has never seen. Saved when the field loses focus, or on Enter.
  // A box that wraps and fits its text: descriptions carry real content now
  // (the SLA definitions, the team boundaries) and must be readable whole.
  const desc = el("textarea", "cfg-desc");
  desc.dir = "auto";
  desc.rows = 1;
  desc.value = o.description || "";
  desc.placeholder = "When should the agent choose this? (required)";
  const fit = () => { desc.style.height = "auto"; desc.style.height = desc.scrollHeight + "px"; };
  desc.addEventListener("input", fit);
  requestAnimationFrame(fit);
  const original = desc.value;
  const save = async () => {
    if (desc.value.trim() === original.trim()) return;
    if (!desc.value.trim()) {
      toast("A description is required", "bad");
      desc.value = original;
      return;
    }
    try {
      await api("/api/config/options/" + o.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: desc.value }),
      });
      toast("Description saved", "good");
    } catch (err) {
      toast(err.message, "bad");
    }
    loadConfig();
  };
  desc.addEventListener("keydown", (e) => {
    // Enter saves, as before; a description is one paragraph.
    if (e.key === "Enter") { e.preventDefault(); desc.blur(); }
    if (e.key === "Escape") { desc.value = original; fit(); desc.blur(); }
  });
  desc.addEventListener("blur", save);

  const status = el("span", "status " + (o.active ? "indexed" : "off"),
    o.active ? "active" : "disabled");

  let control;
  if (o.locked) {
    control = el("span", "cfg-lock", "required");
    control.title = "The agent's rules refer to this value by name, so it cannot be disabled.";
  } else {
    control = el("button", "link", o.active ? "Disable" : "Enable");
    control.addEventListener("click", async () => {
      if (o.active && !confirm(
        `Disable "${o.value}"?\n\nThe agent will stop choosing it and it ` +
        `will disappear from the dropdowns. Drafts that already use it keep it.`
      )) return;
      try {
        await api("/api/config/options/" + o.id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ active: !o.active }),
        });
        toast(`${o.value} ${o.active ? "disabled" : "enabled"}`, "good");
      } catch (err) {
        toast(err.message, "bad");
      }
      loadConfig();
    });
  }

  row.append(value, desc, status, control);
  return row;
}

function addRow(kind, label) {
  const box = el("div", "cfg-add");
  box.append(el("div", "section-label", `New ${label}`));

  const value = el("input");
  value.type = "text";
  value.dir = "ltr";
  value.placeholder = `Exactly as it appears in OTRS`;

  const desc = el("input");
  desc.type = "text";
  desc.dir = "auto";
  desc.placeholder = "When should the agent choose this? (required)";

  const add = el("button", "btn approve", "Add");
  const submit = async () => {
    const v = value.value.trim();
    if (!v) { value.focus(); return; }
    if (!desc.value.trim()) {
      toast("A description is required", "bad");
      desc.focus();
      return;
    }
    if (!confirm(
      `Add "${v}" as a ${label}?\n\n` +
      `It must exist in OTRS spelled exactly like this. If it does not, ` +
      `approving a draft that uses it will fail when the ticket is written ` +
      `to OTRS.\n\nThe agent can choose it from the next ticket on.`
    )) return;
    try {
      await post("/api/config/options", { kind, value: v, description: desc.value });
      toast(`${v} added`, "good");
      value.value = "";
      desc.value = "";
      document.activeElement.blur();
    } catch (err) {
      toast(err.message, "bad");
      return;
    }
    loadConfig();
  };
  add.addEventListener("click", submit);
  [value, desc].forEach((i) => i.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
  }));

  const inputs = el("div", "cfg-add-row");
  inputs.append(value, desc, add);
  box.append(inputs);
  return box;
}

// ---------------------------------------------------------------- the stream

function listen() {
  const source = new EventSource("/api/stream");
  source.onmessage = async () => {
    // Someone may have changed the Configuration page. The lists are small;
    // refetching them on every nudge is cheaper than tracking what moved.
    // A draft already painted keeps its dropdowns until it is repainted.
    try { state.options = await api("/api/options"); } catch {}
    // The badge, title and sound follow every change, on any page.
    if (state.tab !== "review") updatePending();

    // A nudge, not a payload. Refetch whatever is open -- except a page
    // someone is typing into, which must not lose what they have typed.
    if (state.tab === "config" && configBusy()) return;
    if (state.tab === "knowledge" && knowledgeBusy()) return;
    if (state.tab === "review" && Object.keys(state.edits).length) {
      loadDrafts();
      return;
    }
    refresh();
  };
  source.onerror = () => {
    source.close();
    setTimeout(listen, 5000);
  };
}

// ---------------------------------------------------------------- start

// ---------------------------------------------------------------- theme
//
// Dark by default; light on request, remembered per browser. index.html sets
// it before the first paint; this keeps the button in step and switches it.

function paintThemeToggle() {
  const light = document.documentElement.dataset.theme === "light";
  // The button names what it switches to.
  $("theme-toggle").textContent = light ? "☾ Dark" : "☀ Light";
}

$("theme-toggle").addEventListener("click", () => {
  const light = document.documentElement.dataset.theme !== "light";
  if (light) document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  try { localStorage.setItem("ole5-theme", light ? "light" : "dark"); } catch (e) {}
  paintThemeToggle();
});
paintThemeToggle();
paintSoundToggle();

async function start() {
  $("gate").hidden = true;
  $("shell").hidden = false;
  $("who").textContent = state.reviewer.display_name || state.reviewer.email;

  state.options = await api("/api/options");
  const health = await api("/api/health");
  $("dry-run").hidden = !health.dry_run;

  refresh();
  listen();

  // Opened from a link to one draft (the urgent email has one), or pointed
  // at one while already open.
  openLinkedDraft();
  window.addEventListener("hashchange", openLinkedDraft);
}

function openLinkedDraft() {
  const linked = location.hash.match(/^#draft-(\d+)$/);
  if (!linked || Number(linked[1]) === state.draftId) return;
  if (state.tab !== "review") {
    const tab = document.querySelector('.tab[data-tab="review"]');
    if (tab) tab.click();
  }
  openDraft(Number(linked[1])).catch(() => toast("That draft is no longer waiting", "bad"));
}

(async () => {
  const me = await fetch("/api/me", { credentials: "same-origin" }).then((r) => r.json());
  if (me.reviewer) {
    state.reviewer = me.reviewer;
    start();
  } else {
    showGate(me.anyone_registered);
  }
})();