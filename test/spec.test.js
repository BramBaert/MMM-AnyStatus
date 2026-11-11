"use strict";

// SPEC.md, the schema, the demo file and the module's logic have to agree.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Ajv2020 = require("ajv/dist/2020");
const S = require("../lib/status");

const root = path.join(__dirname, "..");
const read = (...p) => fs.readFileSync(path.join(root, ...p), "utf8");
const validate = new Ajv2020({ strict: false, allErrors: true }).compile(JSON.parse(read("schema", "results-1.schema.json")));

// The last ```json block of SPEC.md is "A full example".
// (Outline blocks such as `"results": [ ... ]` are not JSON and are skipped.)
const blocks = [...read("SPEC.md").matchAll(/```json\n([\s\S]*?)```/g)].flatMap((m) => {
	try {
		return [JSON.parse(m[1])];
	} catch {
		return [];
	}
});
const specExample = blocks[blocks.length - 1];

test("the SPEC.md full example is valid against the schema", () => {
	assert.ok(validate(specExample), JSON.stringify(validate.errors));
	assert.equal(specExample.results.length, 3);
});

test("every JSON block in SPEC.md that is a document or a result is valid", () => {
	for (const block of blocks) {
		if (block.results && block.spec) {
			assert.ok(validate(block), JSON.stringify(validate.errors));
		}
	}
});

test("the module reads the SPEC.md example as intended (fixed now)", () => {
	const now = Date.parse("2026-11-11T14:04:00Z");
	const items = S.evaluateDocument(specExample, now);
	assert.deepEqual(items.map((i) => [i.id, i.level]), [["ping.router", "ok"], ["docker.updates", "warn"], ["backup.nas", "crit"]]);
	assert.equal(items[2].since, Date.parse("2026-11-09T02:15:00Z"));
	assert.equal(items[2].label, "NAS backup");
	assert.equal(items[1].summary, "3 images have a newer version");

	// 20 minutes later the 180 s and 900 s results are too old; the 3 day one is not
	const later = S.evaluateDocument(specExample, now + 20 * 60 * 1000);
	assert.deepEqual(later.map((i) => i.level), ["unknown", "warn", "unknown"]);
});

test("the demo results.json is valid, shows all four levels and has a stale result", () => {
	const demo = JSON.parse(read("examples", "hand-written", "results.json"));
	assert.ok(validate(demo), JSON.stringify(validate.errors));
	const now = Date.parse("2026-11-11T14:03:00Z"); // the newest ts: what --demo makes "now"
	const items = S.evaluateDocument(demo, now);
	const levels = new Set(items.map((i) => i.level));
	assert.deepEqual([...levels].sort(), ["crit", "ok", "unknown", "warn"]);
	const stale = items.find((i) => i.id === "sensor.garage");
	assert.equal(stale.level, "unknown");
	assert.equal(stale.summary, "no data");
});

// Documents the schema accepts must not be flagged invalid by the module, and the other way round.
const results = {
	minimal: { id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: 60 },
	full: { id: "a.b:c/d-e_f", level: "warn", ts: "2026-11-11T15:03:00.5+01:00", ttl: 0.5, label: "L", group: "g", summary: "", value: "x", unit: "u", since: "2026-11-11T14:00:00Z", "x-own": [1] },
	noTtl: { id: "a", level: "ok", ts: "2026-11-11T14:03:00Z" },
	zeroTtl: { id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: 0 },
	stringTtl: { id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: "60" },
	noOffset: { id: "a", level: "ok", ts: "2026-11-11T14:03:00", ttl: 60 },
	badLevel: { id: "a", level: "panic", ts: "2026-11-11T14:03:00Z", ttl: 60 }, // schema: invalid; module: shows unknown
	badId: { id: "-a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: 60 },
	badSince: { id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: 60, since: "2026-11-11" },
	emptyLabel: { id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: 60, label: "" },
};

test("schema and module agree on which results are well-formed", () => {
	const now = Date.parse("2026-11-11T14:03:30Z");
	for (const [name, raw] of Object.entries(results)) {
		const schemaOk = validate({ spec: "1.0", results: [raw] });
		const item = S.evaluateResult(raw, now);
		const flagged = item.summary.startsWith("invalid result") || item.summary.startsWith("unknown level");
		// "emptyLabel" is the one place the module is more lenient: it falls back to the id
		if (name === "emptyLabel") continue;
		assert.equal(!schemaOk, flagged, `${name}: schema ${schemaOk ? "accepts" : "rejects"}, module ${flagged ? "flags" : "accepts"} it`);
	}
});
