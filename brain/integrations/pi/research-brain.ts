import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROJECT = path.resolve(HERE, "../..");
const PYTHON = process.env.RESEARCH_BRAIN_PYTHON || path.join(PROJECT, ".venv/bin/python");
const ROOT = process.env.RESEARCH_BRAIN_ROOT || path.join(PROJECT, "data");
const ID = /^(?:block|obj)_[a-f0-9]{12,64}$/;

function runResearch(args: string[], signal?: AbortSignal): Promise<string> {
	return new Promise((resolve, reject) => {
		const child = execFile(
			PYTHON,
			["-m", "research_brain", "--root", ROOT, ...args],
			{ cwd: PROJECT, timeout: 30_000, maxBuffer: 256 * 1024 },
			(error, stdout, stderr) => {
				if (error) reject(new Error((stderr || error.message).slice(0, 4000)));
				else resolve(stdout.slice(0, 200_000));
			},
		);
		if (signal) signal.addEventListener("abort", () => child.kill("SIGTERM"), { once: true });
	});
}

const recallTool = defineTool({
	name: "research_recall",
	label: "Research recall",
	description: "Read reliable source evidence and accepted research cards from the local Research Brain.",
	parameters: Type.Object({
		query: Type.String({ minLength: 1, maxLength: 2000 }),
		kind: Type.Optional(Type.String({ maxLength: 80 })),
		limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
		semantic_live: Type.Optional(Type.Boolean()),
	}),
	async execute(_id, params, signal) {
		const args = ["recall", params.query, "--limit", String(params.limit || 8)];
		if (params.kind) args.push("--kind", params.kind);
		if (params.semantic_live) args.push("--semantic-live");
		const text = await runResearch(args, signal);
		return { content: [{ type: "text", text }], details: { lane: "reliable" } };
	},
});

const evidenceTool = defineTool({
	name: "research_evidence",
	label: "Research evidence",
	description: "Read one immutable evidence block and its exact source locator.",
	parameters: Type.Object({ block_id: Type.String() }),
	async execute(_id, params, signal) {
		if (!ID.test(params.block_id) || !params.block_id.startsWith("block_")) throw new Error("Invalid block id");
		const text = await runResearch(["evidence", params.block_id], signal);
		return { content: [{ type: "text", text }], details: { block_id: params.block_id } };
	},
});

const objectTool = defineTool({
	name: "research_object",
	label: "Research object",
	description: "Read one research card, including review state and evidence bundle.",
	parameters: Type.Object({ object_id: Type.String() }),
	async execute(_id, params, signal) {
		if (!ID.test(params.object_id) || !params.object_id.startsWith("obj_")) throw new Error("Invalid object id");
		const text = await runResearch(["object", "evidence", params.object_id], signal);
		return { content: [{ type: "text", text }], details: { object_id: params.object_id } };
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(recallTool);
	pi.registerTool(evidenceTool);
	pi.registerTool(objectTool);
}
