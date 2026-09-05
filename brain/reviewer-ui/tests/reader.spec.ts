import { test, expect } from "@playwright/test";

test("synthetic reader: evidence, JSON, failed saves, review, mobile, and empty queue", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const c: any = {
    id: "obj_demo",
    title: "Synthetic contrast method",
    kind: "method_card",
    origin: "AGENT_EXTRACTED",
    review_state: "UNREVIEWED",
    updated_at: "v1",
    history: [],
    generation: null,
    structured: {
      problem: "Compare groups [block_demo]",
      mechanism: "<img src=x onerror=alert(1)>",
      procedure: ["Collect examples", "Compute difference"],
      gradients_required: false,
      training_required: null,
      activation_access: true,
      weight_access: false,
    },
    evidence: [
      {
        block_id: "block_demo",
        relation: "supports",
        version_label: "v1",
        document_title: "Synthetic paper",
      },
    ],
  };
  let rejectSave = true,
    saves = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let data: any;
    if (url.pathname === "/api/session")
      data = {
        token: "test",
        origins: ["AGENT_EXTRACTED"],
        review_states: ["UNREVIEWED", "ACCEPTED"],
      };
    else if (url.pathname === "/api/documents") data = [];
    else if (url.pathname === "/api/cards")
      data = {
        items: c.review_state === "UNREVIEWED" ? [c] : [],
        total: c.review_state === "UNREVIEWED" ? 1 : 0,
      };
    else if (url.pathname === "/api/cards/obj_demo") data = c;
    else if (url.pathname === "/api/cards/obj_demo/review") {
      if (rejectSave) {
        await route.fulfill({
          status: 409,
          json: { detail: "Review conflict: reload before reviewing" },
        });
        return;
      }
      saves++;
      c.review_state = "ACCEPTED";
      c.updated_at = "v2";
      data = c;
    } else if (url.pathname === "/api/evidence/block_demo")
      data = {
        id: "block_demo",
        document_title: "Synthetic paper",
        version_label: "v1",
        source_member: "main.tex",
        line_start: 3,
        line_end: 4,
        char_start: 10,
        char_end: 20,
        compilation_id: "comp_demo",
        raw_sha256: "raw",
        source_sha256: "archive",
        raw_text: "Exact source",
        raw_latex: "x=y",
        context: [],
      };
    else {
      await route.abort();
      return;
    }
    await route.fulfill({ json: data });
  });
  await page.goto("/");
  await page.getByRole("button", { name: /METHOD Synthetic/ }).click();
  await expect(page.getByText("Unknown", { exact: true })).toBeVisible();
  await expect(page.locator("#card img")).toHaveCount(0);
  await page.getByRole("button", { name: "block_demo", exact: true }).click();
  await expect(page.locator("#evidence .katex")).toHaveCount(1);
  await expect(page.getByText("Exact source", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "JSON & history" }).click();
  await expect(
    page.getByRole("button", { name: "Download JSON" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Readable", exact: true }).click();
  await page.getByRole("button", { name: "Dispute", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Add a note");
  await page
    .getByRole("textbox", { name: "Review note" })
    .fill("Synthetic test note");
  await page.getByRole("button", { name: "Accept", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("conflict");
  await expect(page.getByRole("textbox")).toHaveValue("Synthetic test note");
  rejectSave = false;
  await page.getByRole("button", { name: "Accept", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Saved: ACCEPTED");
  expect(saves).toBe(1);
  await expect(page.getByRole("heading", { name: c.title })).toBeVisible();
  await expect(page.getByText("No cards match these filters.")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Card", exact: true }).click();
  await expect(page.locator("#card")).toBeVisible();
  await expect(page.locator("#queue")).toBeHidden();
  await page.keyboard.press("Tab");
  expect(await page.evaluate(() => document.activeElement?.tagName)).not.toBe(
    "BODY",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

test("math card preserves unsupported long source and missing evidence", async ({
  page,
}) => {
  const source = "\\unknowncommand{" + "x".repeat(3000) + "}";
  const c = {
    id: "obj_math",
    kind: "math_card",
    title: "Synthetic equation",
    origin: "AGENT_EXTRACTED",
    review_state: "UNREVIEWED",
    history: [],
    evidence: [],
    structured: {
      exact_latex: source,
      semantic_gloss: "Unrenderable equation",
    },
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    await route.fulfill({
      json: path.endsWith("/session")
        ? { token: "test", origins: [], review_states: ["UNREVIEWED"] }
        : path.endsWith("/documents")
          ? []
          : path.endsWith("/cards")
            ? { items: [c], total: 1 }
            : c,
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: /MATH Synthetic/ }).click();
  await expect(
    page.getByText("Rendering unavailable; exact source follows."),
  ).toBeVisible();
  await expect(
    page.getByText("No linked evidence is available."),
  ).toBeVisible();
  expect(await page.locator(".equation pre").textContent()).toBe(source);
});
