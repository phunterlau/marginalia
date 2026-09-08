// Trusted local supervisor helper, never exposed as a Pi tool or Discord path API.
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";

try {
  const input = readFileSync(0, "utf8");
  if (Buffer.byteLength(input) > 16000) throw new Error("Request too large");
  const args = JSON.parse(input);
  const { SessionManager } = await import(pathToFileURL(args.sdk_module).href);
  const manager = SessionManager.open(args.source, args.destination);
  if (manager.getSessionId() !== args.session_id) throw new Error("Session mismatch");
  const target = manager.getEntry(args.entry_id);
  if (target?.type !== "message" || target.message?.role !== "assistant" || target.message.stopReason !== "stop") {
    throw new Error("A completed assistant entry is required");
  }
  const branch = manager.getBranch(args.entry_id);
  if (branch.at(-1)?.id !== args.entry_id) throw new Error("Branch mismatch");
  const path = manager.createBranchedSession(args.entry_id);
  if (!path) throw new Error("Fork was not persisted");
  process.stdout.write(JSON.stringify({ session_id: manager.getSessionId(), path,
    entry_id: args.entry_id, retained_entries: branch.length }) + "\n");
} catch {
  // Do not leak session contents, paths or provider credentials on failure.
  process.stderr.write("Session fork unavailable\n");
  process.exitCode = 1;
}
