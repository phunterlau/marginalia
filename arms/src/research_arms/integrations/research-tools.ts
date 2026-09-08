import { readFile } from "node:fs/promises";
import { createConnection } from "node:net";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

async function request(tool: string, space_id: string, args: unknown, signal?: AbortSignal): Promise<string> {
  // Credentials are supplied by the supervisor, never by model tool arguments.
  const file = process.env.ARMS_TOOL_AUTH_FILE;
  if (!file) throw new Error("Research resource unavailable");
  const auth = JSON.parse(await readFile(file, "utf8"));
  return new Promise((resolve, reject) => {
    const socket = createConnection({ path: auth.socket });
    let received = Buffer.alloc(0), settled = false;
    const finish = (error?: Error, value?: string) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", abort);
      socket.destroy();
      if (error) reject(error); else resolve(value!);
    };
    const abort = () => finish(new Error("Research request aborted"));
    if (signal?.aborted) { abort(); return; }
    signal?.addEventListener("abort", abort, { once: true });
    socket.setTimeout(25000, () => finish(new Error("Research request timed out")));
    socket.on("error", () => finish(new Error("Research resource unavailable")));
    socket.on("end", () => { if (!settled) finish(new Error("Research response interrupted")); });
    socket.on("connect", () => socket.write(JSON.stringify({ token: auth.token, tool, space_id, arguments: args }) + "\n"));
    socket.on("data", (part: Buffer) => {
      received = Buffer.concat([received, part]);
      if (received.length > 66000) { finish(new Error("Research response too large")); return; }
      const end = received.indexOf(10);
      if (end < 0) return;
      try {
        const result = JSON.parse(received.subarray(0, end).toString("utf8"));
        if (result.ok !== true) throw new Error("Unavailable");
        finish(undefined, JSON.stringify(result.data));
      } catch { finish(new Error("Research resource unavailable")); }
    });
  });
}

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    // RPC takes over stdout; use its supported notification channel instead.
    ctx.ui.notify(JSON.stringify({ type: "arms_tools_ready", tools: pi.getActiveTools() }), "info");
  });
  pi.registerTool(defineTool({
    name: "research_recall", label: "Research recall",
    description: "Recall source evidence and accepted memory in an explicitly permitted space. Always retain source context and review labels; unavailable results do not establish absence outside this scope.",
    parameters: Type.Object({ space_id: Type.String(), query: Type.String({ maxLength: 20000 }), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 8 })) }),
    async execute(_id, { space_id, query, limit }, signal) {
      return { content: [{ type: "text", text: await request("research_recall", space_id, { query, limit: limit ?? 5 }, signal) }], details: {} };
    },
  }));
  for (const name of ["research_evidence", "research_object"] as const) {
    pi.registerTool(defineTool({
      name, label: name === "research_evidence" ? "Research evidence" : "Research object",
      description: "Read one explicitly space-qualified record. Source context alone is not verified claim-level support. Never infer acceptance or visibility from an ID.",
      parameters: Type.Object({ space_id: Type.String(), id: Type.String({ maxLength: 100 }) }),
      async execute(_id, { space_id, id }, signal) {
        return { content: [{ type: "text", text: await request(name, space_id, { id }, signal) }], details: {} };
      },
    }));
  }
  pi.registerTool(defineTool({
    name: "research_discussed", label: "Discussion search",
    description: "Search recorded bot-directed exchanges in an explicitly permitted space. Attribute questions to their author and answers to the assistant; cite message links. Discussion is not accepted scientific evidence. No match does not prove a topic was never discussed, and edited or deleted messages may not yet be reflected. Never infer history outside the authorized scope.",
    parameters: Type.Object({ space_id: Type.String(), query: Type.String({ minLength: 1, maxLength: 2000 }), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 3 })) }),
    async execute(_id, { space_id, query, limit }, signal) {
      return { content: [{ type: "text", text: await request("research_discussed", space_id, { query, limit: limit ?? 3 }, signal) }], details: {} };
    },
  }));
}
