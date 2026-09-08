import katex from "katex";
import "katex/dist/katex.min.css";
import "./style.css";

type RecordData = Record<string, any>;
const app = document.querySelector<HTMLDivElement>("#app")!;
// Only static markup enters innerHTML; corpus text always uses textContent.
app.innerHTML = `<header><span class="mark">m.</span><div><h1>Memory Review</h1><p>RESEARCH BRAIN / HUMAN REVIEW WORKBENCH</p></div><span class="local">● Local · no model calls</span></header>
<nav class="mobile"><button data-panel="queue">Queue</button><button data-panel="card">Card</button><button data-panel="evidence">Evidence</button></nav>
<div id="notice" role="status" aria-live="polite"></div><main>
<aside id="queue" class="panel"><h2>Review queue</h2><label>Paper<select id="paper"><option value="">All papers</option></select></label><div class="filters"><label>Card type<select id="kind"><option value="">All cards</option><option value="method_card">Method</option><option value="math_card">Math</option></select></label><label>State<select id="state"></select></label></div><label>Origin<select id="origin"><option value="">All origins</option></select></label><label class="check"><input id="latest" type="checkbox" checked> Latest extraction only</label><p id="count" class="muted"></p><div id="items"></div><div class="actions"><button id="prev">Previous page</button><button id="more">Next page</button></div></aside>
<section id="card" class="panel"><p class="placeholder">Choose a card to examine its mechanism and supporting evidence.</p></section>
<aside id="evidence" class="panel"><h2>Source evidence</h2><p class="placeholder">Select an evidence reference. Exact source text and its location appear here.</p></aside></main>`;
const el = (id: string) => document.getElementById(id)!;
const value = (id: string) => (el(id) as HTMLSelectElement).value;
let token = "",
  current: RecordData | null = null,
  items: RecordData[] = [],
  offset = 0,
  total = 0;
let saving = false,
  requestSerial = 0,
  evidenceSerial = 0,
  queueSerial = 0;
const notes = new Map<string, string>();
function say(message: string) {
  el("notice").textContent = message;
}
function node(tag: string, text = "", cls = "") {
  const n = document.createElement(tag);
  n.textContent = text;
  n.className = cls;
  return n;
}
function button(text: string, action: () => void) {
  const b = node("button", text) as HTMLButtonElement;
  b.onclick = action;
  return b;
}
async function api(path: string, options?: RequestInit) {
  const response = await fetch("/api/" + path, {
    signal: AbortSignal.timeout(15000),
    ...options,
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string" ? data.detail : "Request failed",
    );
  return data;
}
function guarded(action: () => Promise<void>) {
  action().catch((e) => say(e.message));
}
function panel(name: string) {
  document.body.dataset.panel = name;
}
document
  .querySelectorAll<HTMLButtonElement>("[data-panel]")
  .forEach((b) => (b.onclick = () => panel(b.dataset.panel!)));
function option(select: string, name: string, id = name) {
  const o = document.createElement("option");
  o.value = id;
  o.textContent = name;
  el(select).append(o);
}
function preserveNote() {
  if (current && el("note"))
    notes.set(current.id, (el("note") as HTMLTextAreaElement).value);
}
async function queue() {
  const serial = ++queueSerial;
  const query = new URLSearchParams({
    state: value("state"),
    latest: String((el("latest") as HTMLInputElement).checked),
    offset: String(offset),
    limit: "25",
  });
  for (const [key, id] of [
    ["document", "paper"],
    ["kind", "kind"],
    ["origin", "origin"],
  ])
    if (value(id)) query.set(key, value(id));
  const data = await api("cards?" + query);
  if (serial !== queueSerial) return;
  items = data.items;
  total = data.total;
  if (offset >= total && offset > 0) {
    offset = Math.max(0, Math.floor((total - 1) / 25) * 25);
    return queue();
  }
  el("count").textContent =
    `${total} cards · ${total ? offset + 1 : 0}–${Math.min(offset + 25, total)}`;
  el("items").replaceChildren();
  for (const item of items) {
    const b = button("", () => {
      if (!saving) guarded(() => select(item.id));
    });
    b.className = "queue-card";
    b.dataset.id = item.id;
    b.classList.toggle("selected", current?.id === item.id);
    b.append(
      node("span", item.kind === "math_card" ? "MATH" : "METHOD", "eyebrow"),
      node("strong", item.title || item.id),
      node("small", item.review_state),
    );
    el("items").append(b);
  }
  if (!items.length)
    el("items").append(
      node("p", "No cards match these filters.", "placeholder"),
    );
  (el("prev") as HTMLButtonElement).disabled = offset === 0;
  (el("more") as HTMLButtonElement).disabled = offset + 25 >= total;
}
for (const id of ["paper", "kind", "state", "origin", "latest"])
  el(id).onchange = () => {
    offset = 0;
    guarded(queue);
  };
el("prev").onclick = () => {
  offset = Math.max(0, offset - 25);
  guarded(queue);
};
el("more").onclick = () => {
  offset += 25;
  guarded(queue);
};
function textWithRefs(text: string) {
  const p = node("p");
  for (const part of text.split(/(block_[a-zA-Z0-9_-]+)/g)) {
    if (
      /^block_/.test(part) &&
      current?.evidence.some((e: RecordData) => e.block_id === part)
    )
      p.append(button(part, () => guarded(() => showEvidence(part))));
    else p.append(document.createTextNode(part));
  }
  return p;
}
function equation(parent: HTMLElement, source: string) {
  const raw = node("pre", source, "source");
  const rendered = node("div", "", "equation");
  try {
    katex.render(source, rendered, {
      displayMode: true,
      throwOnError: true,
      trust: false,
      maxExpand: 1000,
      maxSize: 20,
    });
  } catch {
    rendered.append(
      node("p", "Rendering unavailable; exact source follows.", "muted"),
      node("pre", source, "source"),
    );
  }
  raw.hidden = true;
  parent.append(
    button("Rendered / raw LaTeX", () => {
      raw.hidden = !raw.hidden;
      rendered.hidden = !rendered.hidden;
    }),
    rendered,
    raw,
  );
}
function renderCard() {
  const c = current!;
  const s = c.structured || {};
  const parent = el("card");
  parent.replaceChildren();
  parent.append(
    node("span", c.kind.replace("_", " ").toUpperCase(), "eyebrow"),
    node("h2", c.title || s.name || c.id),
  );
  parent.append(
    node("p", `${c.origin} · ${c.review_state}`, "badge"),
    node("small", c.id, "muted"),
  );
  const papers = [
    ...new Set(
      c.evidence.map(
        (e: RecordData) =>
          `${e.document_title || e.document_id} · ${e.version_label || "revision unavailable"}`,
      ),
    ),
  ];
  parent.append(node("p", papers.join("\n"), "muted"));
  if (s.citation_mode === "source_context_only")
    parent.append(node("p", "Source context only: attached by Brain from the extraction input. These are not generated citations or verified claim-level support.", "badge"));
  if (c.generation)
    parent.append(
      node(
        "p",
        `Extraction: ${c.generation.model || "Model unavailable"} · ${c.generation.prompt_version} · ${c.generation.schema_version}`,
        "muted",
      ),
    );
  const readable = node("div"),
    inspector = node("div");
  inspector.hidden = true;
  const tabs = node("div", "", "actions");
  tabs.append(
    button("Readable", () => {
      readable.hidden = false;
      inspector.hidden = true;
    }),
    button("JSON & history", () => {
      readable.hidden = true;
      inspector.hidden = false;
    }),
  );
  parent.append(tabs);
  const json = JSON.stringify(c, null, 2);
  inspector.append(
    button("Copy JSON", () =>
      guarded(async () => {
        await navigator.clipboard.writeText(json);
        say("JSON copied.");
      }),
    ),
    button("Download JSON", () => {
      const url = URL.createObjectURL(
        new Blob([json], { type: "application/json" }),
      );
      const a = document.createElement("a");
      a.href = url;
      a.download = c.id + ".json";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }),
    node("pre", json, "source"),
  );
  const history = node("details");
  history.append(node("summary", "Review history"));
  const reviews = c.history.filter(
    (e: RecordData) => e.event_type === "object_reviewed",
  );
  if (!reviews.length)
    history.append(node("p", "No review decisions yet.", "muted"));
  for (const event of reviews) {
    const p = JSON.parse(event.payload_json);
    history.append(
      node(
        "p",
        `${event.created_at} · ${event.actor}\n${p.from} → ${p.to}\n${p.note || "No note"}`,
      ),
    );
  }
  parent.append(history);
  if (s.exact_latex) equation(readable, s.exact_latex);
  if (c.kind === "method_card") {
    const grid = node("div", "", "constraints");
    for (const [key, label] of [
      ["gradients_required", "Gradients"],
      ["training_required", "Training"],
      ["activation_access", "Activation access"],
      ["weight_access", "Weight access"],
    ]) {
      const box = node("div");
      box.append(
        node("small", label),
        node(
          "strong",
          s[key] === true
            ? "Required"
            : s[key] === false
              ? "Not required"
              : "Unknown",
        ),
      );
      grid.append(box);
    }
    readable.append(grid);
  }
  const fields =
    c.kind === "method_card"
      ? [
          "problem",
          "mechanism",
          "procedure",
          "inputs",
          "outputs",
          "assumptions",
          "failure_modes",
          "scientific_moves",
        ]
      : [
          "semantic_gloss",
          "symbols",
          "role",
          "assumptions",
          "affordances",
          "failure_modes",
          "math_move",
        ];
  for (const key of fields) {
    readable.append(node("h3", key.replaceAll("_", " ")));
    const v = s[key];
    if (Array.isArray(v)) {
      const list = node(key === "procedure" ? "ol" : "ul");
      for (const x of v) {
        const li = node("li");
        li.append(textWithRefs(String(x)));
        list.append(li);
      }
      readable.append(list);
    } else
      readable.append(
        textWithRefs(
          v == null
            ? "Not specified"
            : typeof v === "object"
              ? Object.entries(v)
                  .map(([k, v]) => `${k}: ${v}`)
                  .join("\n")
              : String(v),
        ),
      );
  }
  readable.append(
    node("h3", "Card-level evidence"),
    node(
      "p",
      "These links support the card as a whole unless a field explicitly cites a block.",
      "muted",
    ),
  );
  for (const e of c.evidence)
    readable.append(
      button(`${e.relation} · ${e.block_id}`, () =>
        guarded(() => showEvidence(e.block_id)),
      ),
    );
  if (!c.evidence.length)
    readable.append(node("p", "No linked evidence is available.", "warning"));
  parent.append(readable, inspector);
  const review = node("div", "", "review");
  review.append(
    node("h3", "Your review"),
    node(
      "p",
      "Accept only if this card faithfully represents its cited source and qualifications. Acceptance enables reliable recall; it does not certify universal correctness.",
      "muted",
    ),
  );
  const label = node("label", "Review note");
  const note = document.createElement("textarea");
  note.id = "note";
  note.rows = 3;
  note.maxLength = 4000;
  note.value = notes.get(c.id) || "";
  note.oninput = () => notes.set(c.id, note.value);
  label.append(note);
  review.append(label);
  const actions = node("div", "", "actions");
  for (const [title, decision] of [
    ["Accept", "ACCEPTED"],
    ["Dispute", "DISPUTED"],
    ["Reject", "REJECTED"],
  ]) {
    const b = button(title, () => guarded(() => save(decision)));
    b.dataset.review = "true";
    actions.append(b);
  }
  actions.append(
    button("Skip / Next", () => {
      if (!saving) guarded(next);
    }),
  );
  review.append(actions);
  parent.append(review);
}
async function select(id: string) {
  preserveNote();
  const serial = ++requestSerial;
  const result = await api("cards/" + id);
  if (serial !== requestSerial) return;
  current = result;
  renderCard();
  panel("card");
  say("");
  ++evidenceSerial;
  el("evidence").replaceChildren(
    node("h2", "Source evidence"),
    node("p", "Select an evidence reference for this card.", "placeholder"),
  );
  document
    .querySelectorAll<HTMLElement>(".queue-card")
    .forEach((b) => b.classList.toggle("selected", b.dataset.id === id));
}
async function next() {
  const index = items.findIndex((r) => r.id === current?.id);
  if (index + 1 < items.length) await select(items[index + 1].id);
  else if (offset + 25 < total) {
    offset += 25;
    await queue();
    if (items[0]) await select(items[0].id);
  } else say("End of this queue. Change filters to inspect other cards.");
}
async function save(decision: string) {
  if (saving || !current) return;
  preserveNote();
  const c = current;
  const note = notes.get(c.id) || "";
  if (decision !== "ACCEPTED" && !note.trim()) {
    say("Add a note explaining the dispute or rejection.");
    return;
  }
  saving = true;
  document
    .querySelectorAll<HTMLButtonElement>("[data-review]")
    .forEach((b) => (b.disabled = true));
  try {
    const updated = await api("cards/" + c.id + "/review", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Review-Token": token },
      body: JSON.stringify({ decision, note, expected_version: c.updated_at }),
    });
    current = updated;
    notes.delete(c.id);
    renderCard();
    await queue();
    say(`Saved: ${decision}. Decision appended to review history.`);
  } finally {
    saving = false;
    document
      .querySelectorAll<HTMLButtonElement>("[data-review]")
      .forEach((b) => (b.disabled = false));
  }
}
async function showEvidence(id: string) {
  const serial = ++evidenceSerial;
  const e = await api("evidence/" + id);
  if (serial !== evidenceSerial) return;
  const p = el("evidence");
  p.replaceChildren(
    node("h2", "Source evidence"),
    node("strong", e.document_title || e.document_id),
    node(
      "p",
      `${e.version_label || "Revision unavailable"} · ${e.source_member || "PDF source"} · lines ${e.line_start ?? "—"}–${e.line_end ?? "—"}${e.page != null ? " · page " + e.page : ""}`,
    ),
  );
  p.append(
    node(
      "p",
      `Block: ${e.id}\nCompilation: ${e.compilation_id}\nCharacters: ${e.char_start ?? "—"}–${e.char_end ?? "—"}\nSource SHA-256: ${e.source_sha256}\nRaw SHA-256: ${e.raw_sha256 ?? "—"}`,
      "locator",
    ),
  );
  p.append(node("h3", "Exact block"));
  if (e.raw_latex) equation(p, e.raw_latex);
  p.append(
    node("pre", e.raw_text, "source exact"),
    node("h3", "Surrounding context"),
  );
  if (!e.context.length)
    p.append(node("p", "No neighboring blocks in this section.", "muted"));
  for (const n of e.context) {
    p.append(
      node(
        "small",
        `${n.ordinal < e.ordinal ? "Before" : "After"} · ${n.source_member || "PDF"} · ${n.id}`,
        "muted",
      ),
      node("pre", n.raw_text, "source"),
    );
  }
  panel("evidence");
}
window.addEventListener("beforeunload", (e) => {
  if ([...notes.values()].some((n) => n.trim())) {
    e.preventDefault();
    e.returnValue = "";
  }
});
guarded(async () => {
  const session = await api("session");
  token = session.token;
  for (const state of session.review_states) option("state", state);
  option("state", "All states", "");
  (el("state") as HTMLSelectElement).value = "UNREVIEWED";
  for (const origin of session.origins) option("origin", origin);
  for (const d of await api("documents"))
    option("paper", d.title || d.id, d.id);
  panel("queue");
  await queue();
});
