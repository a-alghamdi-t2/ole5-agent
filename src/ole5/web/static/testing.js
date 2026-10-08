/* Testing page: send a ticket to the intake queue from the console.
 *
 * Kept apart from app.js so it can be removed by deleting this file,
 * testing.css and testing.py, and the lines marked "testing page".
 * Uses app.js's helpers ($, el, api, post, toast, fmtTime, dir, state).
 */

(() => {
  const tab = $("testing-tab");
  const form = $("test-form");
  const title = $("test-title");
  const body = $("test-body");
  const send = $("test-send");
  const result = $("test-result");
  const list = $("test-list");

  // The tab shows only when the server says the page may be used: signed in,
  // TESTING_PAGE on, and not pointed at production OTRS.
  async function check() {
    try {
      const res = await fetch("/api/testing", { credentials: "same-origin" });
      if (!res.ok) return;
      const s = await res.json();
      // Shown whenever TESTING_PAGE is on; when sending is not allowed (for
      // example, pointed at production) the page says why and the form is off.
      tab.hidden = !s.on;
      $("test-where").textContent = s.enabled
        ? `Send a ticket to the ${s.queue} queue on ${s.otrs} as if a customer emailed it. ` +
          "The agent drafts it within a minute or two, and it appears on Review."
        : `Sending is off: ${s.reason}`;
      for (const n of [title, body, send]) n.disabled = !s.enabled;
    } catch (e) {}
  }

  // The console shows itself after sign-in without a reload, so wait for it.
  const shell = $("shell");
  new MutationObserver(() => { if (!shell.hidden) check(); })
    .observe(shell, { attributes: true, attributeFilter: ["hidden"] });
  if (!shell.hidden) check();

  function where(r) {
    if (!r.draft_id) return { text: "Waiting for the agent", cls: "wait" };
    if (r.draft_status === "pending") return { text: `On Review · draft ${r.draft_id}`, cls: "ready", draft: r.draft_id };
    return { text: `${r.draft_status} · draft ${r.draft_id}`, cls: "done" };
  }

  async function load() {
    let rows;
    try { rows = await api("/api/testing/tickets"); } catch (e) { return; }
    list.replaceChildren();
    if (!rows.length) {
      list.append(el("div", "test-empty", "Nothing sent yet."));
      return;
    }
    for (const r of rows) {
      const row = el("div", "test-row");
      row.append(el("span", "test-num", r.number));
      const t = el("span", "test-title", r.title);
      t.dir = dir(r.title);
      row.append(t);
      row.append(el("span", "test-meta", `${r.by} · ${fmtTime(r.created_at)}`));
      const w = where(r);
      const status = el(w.draft ? "button" : "span", "test-status " + w.cls, w.text);
      if (w.draft) {
        status.title = "Open this draft";
        status.addEventListener("click", () => { location.hash = "#draft-" + w.draft; });
      }
      row.append(status);
      list.append(row);
    }
  }

  tab.addEventListener("click", load);
  // While the page is open, follow each ticket until it is drafted.
  setInterval(() => { if (state.tab === "testing") load(); }, 10000);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    send.disabled = true;
    result.textContent = "Sending…";
    try {
      const r = await post("/api/testing/tickets", { title: title.value, body: body.value });
      result.textContent = `Sent as ticket ${r.number}.`;
      toast(`Ticket ${r.number} sent to ${r.queue}`);
      title.value = "";
      body.value = "";
      load();
    } catch (err) {
      result.textContent = "";
      toast(err.message, "bad");
    } finally {
      send.disabled = false;
    }
  });
})();
