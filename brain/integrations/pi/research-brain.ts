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
const DOCUMENT_ID = /^doc_[a-f0-9]{12,64}$/;
const VERSION = /^v[1-9][0-9]*$/;
const DEFAULT_RECALL_LIMIT = 5;
const MAX_RECALL_LIMIT = 8;
const MAX_EVIDENCE_PER_HIT = 4;
const MAX_TOOL_OUTPUT_CHARS = 64_000;
const MAX_EXCERPT_CHARS = 1_200;

function runResearch(args: string[], signal?: AbortSignal): Promise<string> {
	return new Promise((resolve, reject) => {
		const child = execFile(
			PYTHON,
			["-m", "research_brain", "--root", ROOT, ...args],
			{ cwd: PROJECT, timeout: 30_000, maxBuffer: 1024 * 1024 },
			(error, stdout, stderr) => {
				if (error) reject(new Error((stderr || error.message).slice(0, 4000)));
				else resolve(stdout);
			},
		);
		if (signal) signal.addEventListener("abort", () => child.kill("SIGTERM"), { once: true });
	});
}

function truncate(value: unknown, limit = MAX_EXCERPT_CHARS): unknown {
	if (typeof value !== "string" || value.length <= limit) return value;
	return `${value.slice(0, limit)}…`;
}

function compactEvidence(value: any): Record<string, unknown> {
	return {
		block_id: value.block_id || value.id,
		relation: truncate(value.relation, 400),
		block_type: value.block_type,
		document_id: value.document_id,
		document_title: truncate(value.document_title, 300),
		version_label: value.version_label,
		source_member: truncate(value.source_member, 500),
		line_start: value.line_start,
		line_end: value.line_end,
		excerpt: truncate(value.raw_text || value.normalized_text),
		raw_latex: truncate(value.raw_latex, 2_000),
	};
}

function compactStructured(value: any): Record<string, unknown> | null {
	if (!value || typeof value !== "object") return null;
	const scalarKeys = [
		"schema", "name", "problem", "mechanism", "role", "semantic_gloss", "math_move",
		"gradients_required", "training_required", "activation_access", "weight_access",
	];
	const listKeys = [
		"procedure", "inputs", "outputs", "assumptions", "failure_modes", "scientific_moves",
		"affordances",
	];
	const result: Record<string, unknown> = {};
	for (const key of scalarKeys) {
		if (value[key] !== undefined) result[key] = truncate(value[key]);
	}
	for (const key of listKeys) {
		if (Array.isArray(value[key])) result[key] = value[key].slice(0, 8).map((item: unknown) => truncate(item, 600));
	}
	if (value.exact_latex !== undefined) result.exact_latex = truncate(value.exact_latex, 2_000);
	return result;
}

function compactHit(value: any): Record<string, unknown> {
	return {
		record_type: value.record_type,
		record_id: value.record_id,
		title: truncate(value.title, 300),
		kind: value.kind,
		text: truncate(value.text),
		origin: value.origin,
		review_state: value.review_state,
		document_version_id: value.document_version_id,
		source_locator: value.source_locator,
		structured: compactStructured(value.structured),
		evidence: Array.isArray(value.evidence)
			? value.evidence.slice(0, MAX_EVIDENCE_PER_HIT).map(compactEvidence)
			: [],
	};
}

function boundedHits(stdout: string): { text: string; total: number; returned: number; truncated: boolean } {
	const parsed = JSON.parse(stdout);
	if (!Array.isArray(parsed)) throw new Error("Research recall returned a non-list payload");
	const compact = parsed.map(compactHit);
	let returned = compact.length;
	let text = JSON.stringify(compact, null, 2);
	while (text.length > MAX_TOOL_OUTPUT_CHARS && returned > 1) {
		returned -= 1;
		text = JSON.stringify(compact.slice(0, returned), null, 2);
	}
	if (text.length > MAX_TOOL_OUTPUT_CHARS) throw new Error("One compact recall hit exceeds the tool output limit");
	return { text, total: compact.length, returned, truncated: returned < compact.length };
}

function boundedObject(stdout: string): string {
	const parsed = JSON.parse(stdout);
	const compact = {
		...parsed,
		body: truncate(parsed.body, 4_000),
		normalized_text: truncate(parsed.normalized_text, 12_000),
		raw_text: truncate(parsed.raw_text, 12_000),
		raw_latex: truncate(parsed.raw_latex, 12_000),
		structured: compactStructured(parsed.structured),
		evidence: Array.isArray(parsed.evidence)
			? parsed.evidence.slice(0, MAX_EVIDENCE_PER_HIT).map(compactEvidence)
			: parsed.evidence,
	};
	const text = JSON.stringify(compact, null, 2);
	if (text.length > MAX_TOOL_OUTPUT_CHARS) throw new Error("Research object exceeds the tool output limit");
	return text;
}

function boundedPacket(stdout: string): string {
	const parsed = JSON.parse(stdout);
	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
		throw new Error("Research context returned an invalid packet");
	}
	const text = JSON.stringify(parsed, null, 2);
	if (text.length > MAX_TOOL_OUTPUT_CHARS) throw new Error("Research packet exceeds the tool output limit");
	return text;
}

const originValue = Type.Union([
	Type.Literal("SOURCE_EXPLICIT"), Type.Literal("SOURCE_IMPLIED"),
	Type.Literal("AGENT_EXTRACTED"), Type.Literal("AGENT_INTERPRETED"),
	Type.Literal("AGENT_PROPOSED"), Type.Literal("USER_STATED"),
	Type.Literal("USER_ACCEPTED"), Type.Literal("EXPERIMENT_OBSERVED"),
	Type.Literal("EXTERNALLY_VERIFIED"),
]);
const reviewStateValue = Type.Union([
	Type.Literal("UNREVIEWED"), Type.Literal("ACCEPTED"), Type.Literal("REJECTED"),
	Type.Literal("DISPUTED"), Type.Literal("SUPERSEDED"), Type.Literal("DEPRECATED"),
	Type.Literal("INVALIDATED"),
]);
const retrievalFilters = Type.Object({
	gradients_required: Type.Optional(Type.Boolean()),
	training_required: Type.Optional(Type.Boolean()),
	activation_access: Type.Optional(Type.Boolean()),
	weight_access: Type.Optional(Type.Boolean()),
	representation_kind: Type.Optional(Type.String({ maxLength: 80 })),
	origins: Type.Optional(Type.Array(originValue, { maxItems: 8 })),
	review_states: Type.Optional(Type.Array(reviewStateValue, { maxItems: 8 })),
	document_id: Type.Optional(Type.String({ pattern: DOCUMENT_ID.source })),
	version_label: Type.Optional(Type.String({ pattern: VERSION.source })),
}, { additionalProperties: false });

const recallTool = defineTool({
	name: "research_recall",
	label: "Research recall",
	description: "Read reliable source evidence and accepted research cards from the local Research Brain.",
	parameters: Type.Object({
		query: Type.String({ minLength: 1, maxLength: 2000 }),
		kind: Type.Optional(Type.String({ maxLength: 80 })),
		limit: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_RECALL_LIMIT })),
		filters: Type.Optional(retrievalFilters),
	}, { additionalProperties: false }),
	async execute(_id, params, signal) {
		const args = ["recall", params.query, "--limit", String(params.limit || DEFAULT_RECALL_LIMIT)];
		if (params.kind) args.push("--kind", params.kind);
		if (params.filters) args.push("--filters", JSON.stringify(params.filters));
		const result = boundedHits(await runResearch(args, signal));
		return {
			content: [{ type: "text", text: result.text }],
			details: {
				lane: "reliable", filters_applied: params.filters || {}, total: result.total,
				returned: result.returned, truncated: result.truncated,
				max_output_chars: MAX_TOOL_OUTPUT_CHARS,
			},
		};
	},
});

const contextTool = defineTool({
	name: "research_context",
	label: "Research context",
	description: "Compile a compact frontier-aware ResearchPacket for recall, analysis, critique, brainstorm, or decision work.",
	parameters: Type.Union([
		Type.Object({
			question: Type.String({ minLength: 1, maxLength: 2000 }),
			thread_id: Type.Optional(Type.String({ pattern: /^obj_[a-f0-9]{12,64}$/.source })),
			mode: Type.Union([
				Type.Literal("recall"), Type.Literal("analysis"), Type.Literal("critique"),
				Type.Literal("decision"),
			]),
			filters: Type.Optional(retrievalFilters),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_RECALL_LIMIT })),
		}, { additionalProperties: false }),
		Type.Object({
			question: Type.String({ minLength: 1, maxLength: 2000 }),
			thread_id: Type.Optional(Type.String({ pattern: /^obj_[a-f0-9]{12,64}$/.source })),
			mode: Type.Literal("brainstorm"),
			filters: Type.Optional(retrievalFilters),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_RECALL_LIMIT })),
			blind_first: Type.String({ minLength: 1, maxLength: 20_000 }),
		}, { additionalProperties: false }),
	]),
	async execute(_id, params, signal) {
		const args = ["context", params.question, "--mode", params.mode, "--limit", String(params.limit || DEFAULT_RECALL_LIMIT)];
		if (params.thread_id) args.push("--thread", params.thread_id);
		if (params.filters) args.push("--filters", JSON.stringify(params.filters));
		if ("blind_first" in params) args.push("--blind-first", params.blind_first);
		const text = boundedPacket(await runResearch(args, signal));
		return {
			content: [{ type: "text", text }],
			details: { mode: params.mode, thread_id: params.thread_id, max_output_chars: MAX_TOOL_OUTPUT_CHARS },
		};
	},
});

const evidenceTool = defineTool({
	name: "research_evidence",
	label: "Research evidence",
	description: "Read one immutable evidence block and its exact source locator.",
	parameters: Type.Object({ block_id: Type.String() }),
	async execute(_id, params, signal) {
		if (!ID.test(params.block_id) || !params.block_id.startsWith("block_")) throw new Error("Invalid block id");
		const text = boundedObject(await runResearch(["evidence", params.block_id], signal));
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
		const text = boundedObject(await runResearch(["object", "evidence", params.object_id], signal));
		return { content: [{ type: "text", text }], details: { object_id: params.object_id } };
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(contextTool);
	pi.registerTool(recallTool);
	pi.registerTool(evidenceTool);
	pi.registerTool(objectTool);
}
