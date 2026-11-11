"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const S = require("../lib/status");

const NOW = Date.parse("2026-11-11T14:05:00Z");
const MIN = 60 * 1000;

function result(extra = {}) {
	return { id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: 600, ...extra };
}
function doc(results, extra = {}) {
	return { spec: "1.0", results, ...extra };
}

test("parseTime accepts offsets and fractions, rejects the rest", () => {
	assert.equal(S.parseTime("2026-11-11T14:03:00Z"), Date.parse("2026-11-11T14:03:00Z"));
	assert.equal(S.parseTime("2026-11-11T15:03:00+01:00"), Date.parse("2026-11-11T14:03:00Z"));
	assert.equal(S.parseTime("2026-11-11T14:03:00.123456Z"), Date.parse("2026-11-11T14:03:00.123Z"));
	assert.ok(Number.isNaN(S.parseTime("2026-11-11T14:03:00"))); // no offset
	assert.ok(Number.isNaN(S.parseTime("2026-11-11 14:03:00Z")));
	assert.ok(Number.isNaN(S.parseTime("2026-13-11T14:03:00Z")));
	assert.ok(Number.isNaN(S.parseTime(1762869780)));
	assert.ok(Number.isNaN(S.parseTime(undefined)));
});

test("a fresh result keeps its level, label, group, summary and since", () => {
	const item = S.evaluateResult(
		result({ id: "backup.nas", label: "NAS backup", group: "backups", level: "crit", summary: "3 days", since: "2026-11-09T02:15:00Z" }),
		NOW
	);
	assert.deepEqual(item, {
		id: "backup.nas", label: "NAS backup", group: "backups", level: "crit", summary: "3 days",
		since: Date.parse("2026-11-09T02:15:00Z"), system: false,
	});
});

test("label defaults to id; value and unit fill in a missing summary", () => {
	const item = S.evaluateResult(result({ id: "ping.x", value: 1.8, unit: "ms" }), NOW);
	assert.equal(item.label, "ping.x");
	assert.equal(item.summary, "1.8 ms");
	assert.equal(S.evaluateResult(result({ value: "fine" }), NOW).summary, "fine");
});

test("age rule: too old is grey 'no data since ts', whatever the level says", () => {
	const ts = "2026-11-11T14:03:00Z";
	const fresh = S.evaluateResult(result({ ts, ttl: 120 }), Date.parse(ts) + 120 * 1000); // exactly ts+ttl: still believed
	assert.equal(fresh.level, "ok");
	const old = S.evaluateResult(result({ ts, ttl: 120, level: "ok", summary: "all fine", since: "2026-11-01T00:00:00Z" }), Date.parse(ts) + 120 * 1000 + 1);
	assert.equal(old.level, "unknown");
	assert.equal(old.summary, "no data");
	assert.equal(old.since, Date.parse(ts));
});

test("age rule: the same result turns grey as now advances (local timer)", () => {
	const raw = result({ ttl: 300 });
	assert.equal(S.evaluateResult(raw, NOW).level, "ok");
	assert.equal(S.evaluateResult(raw, NOW + 10 * MIN).level, "unknown");
});

test("age rule: from the future is grey 'clock mismatch'", () => {
	const ts = Date.parse("2026-11-11T14:03:00Z");
	assert.equal(S.evaluateResult(result({ ttl: 60 }), ts - 60 * 1000).level, "ok"); // ts == now + ttl: fine
	const item = S.evaluateResult(result({ ttl: 60, level: "crit", summary: "bad" }), ts - 60 * 1000 - 1);
	assert.equal(item.level, "unknown");
	assert.equal(item.summary, "clock mismatch");
	// within ttl in the future is tolerated
	assert.equal(S.evaluateResult(result({ ttl: 600 }), ts - 5 * MIN).level, "ok");
});

test("since later than ts is clamped to ts", () => {
	const item = S.evaluateResult(result({ since: "2026-11-11T14:04:00Z" }), NOW);
	assert.equal(item.since, Date.parse("2026-11-11T14:03:00Z"));
});

test("an unknown level is shown as unknown, not dropped", () => {
	const item = S.evaluateResult(result({ level: "panic", summary: "x" }), NOW);
	assert.equal(item.level, "unknown");
	assert.match(item.summary, /unknown level "panic"/);
	assert.equal(item.id, "a");
});

test("invalid results with a readable id show that id as unknown with the reason", () => {
	const cases = [
		[{ id: "a", level: "ok", ts: "2026-11-11T14:03:00Z" }, "invalid result: no ttl"],
		[{ id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: 0 }, "invalid result: bad ttl"],
		[{ id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: "60" }, "invalid result: bad ttl"],
		[{ id: "a", level: "ok", ts: "2026-11-11T14:03:00Z", ttl: -5 }, "invalid result: bad ttl"],
		[{ id: "a", level: "ok", ttl: 60 }, "invalid result: no ts"],
		[{ id: "a", level: "ok", ts: "2026-11-11T14:03:00", ttl: 60 }, "invalid result: bad ts"],
		[{ id: "a", ts: "2026-11-11T14:03:00Z", ttl: 60 }, "invalid result: no level"],
		[result({ since: "yesterday" }), "invalid result: bad since"],
		[result({ summary: 5 }), "invalid result: bad summary"],
		[result({ value: true }), "invalid result: bad value"],
	];
	for (const [raw, summary] of cases) {
		const item = S.evaluateResult(raw, NOW);
		assert.equal(item.id, "a", summary);
		assert.equal(item.level, "unknown", summary);
		assert.equal(item.summary, summary);
	}
});

test("a result without a usable id becomes one system item", () => {
	for (const raw of [null, 5, "x", [], {}, { id: "" }, { id: "-bad" }, { id: 7 }, result({ id: "has space" })]) {
		const item = S.evaluateResult(raw, NOW);
		assert.equal(item.level, "unknown");
		assert.equal(item.system, true);
		assert.match(item.summary, /^invalid result/);
	}
});

test("duplicate ids: every occurrence shows as unknown", () => {
	const items = S.evaluateDocument(doc([result({ id: "d" }), result({ id: "d", level: "crit" }), result({ id: "e" })]), NOW);
	assert.equal(items.length, 3);
	assert.deepEqual(items.map((i) => i.level), ["unknown", "unknown", "ok"]);
	assert.equal(items[0].summary, "invalid result: duplicate id");
});

test("document level: invalid, unsupported major, higher minor", () => {
	const bad = [null, [], "x", {}, { spec: 1, results: [] }, { spec: "1", results: [] }, { spec: "1.0" }, { spec: "1.0", results: {} }];
	for (const d of bad) {
		const items = S.evaluateDocument(d, NOW);
		assert.equal(items.length, 1);
		assert.equal(items[0].level, "unknown");
		assert.match(items[0].summary, /^invalid document/);
	}
	const unsupported = S.evaluateDocument({ spec: "2.0", source: "home-checker", results: [result()] }, NOW);
	assert.equal(unsupported.length, 1);
	assert.equal(unsupported[0].summary, "unsupported spec 2.0");
	assert.equal(unsupported[0].label, "home-checker");
	assert.equal(S.evaluateDocument({ spec: "0.9", results: [] }, NOW)[0].summary, "unsupported spec 0.9");

	const minor = S.evaluateDocument({ spec: "1.7", results: [result({ "x-own": 1, future: true })] }, NOW);
	assert.equal(minor[0].level, "ok"); // unknown fields ignored
});

test("an empty document gives no items", () => {
	assert.deepEqual(S.evaluateDocument(doc([]), NOW), []);
});

test("buildItems: nothing yet is a grey 'no data yet'", () => {
	for (const feed of [null, {}, { doc: null, error: null }]) {
		const items = S.buildItems(feed, NOW);
		assert.equal(items.length, 1);
		assert.equal(items[0].level, "unknown");
		assert.equal(items[0].summary, "no data yet");
	}
});

test("buildItems: unreachable keeps the last results and adds one grey item named after the source", () => {
	const last = Date.parse("2026-11-11T14:03:00Z");
	const feed = { doc: doc([result({ id: "a" })], { source: "home-checker" }), error: { message: "unreachable: HTTP 500", lastSuccess: last, firstFailure: last + MIN } };
	const items = S.buildItems(feed, NOW);
	assert.equal(items.length, 2);
	assert.equal(items[0].level, "ok");
	assert.equal(items[1].label, "home-checker unreachable");
	assert.equal(items[1].level, "unknown");
	assert.equal(items[1].since, last);
	assert.equal(items[1].summary, "unreachable: HTTP 500");
	// kept results age out by themselves
	assert.equal(S.buildItems(feed, NOW + 20 * MIN)[0].level, "unknown");
});

test("buildItems: unreachable before any success uses firstFailure and a fallback name", () => {
	const feed = { doc: null, error: { message: "unreachable: timed out", lastSuccess: null, firstFailure: 1234 } };
	const items = S.buildItems(feed, NOW);
	assert.equal(items.length, 1);
	assert.equal(items[0].label, "checker unreachable");
	assert.equal(items[0].since, 1234);
});

function item(level, label, since = null, group = null, system = false) {
	return { id: label, label, group, level, summary: "", since, system };
}

test("arrange: all green is ok with no cards", () => {
	const view = S.arrange([item("ok", "a"), item("ok", "b")], 6);
	assert.equal(view.level, "ok");
	assert.equal(view.okCount, 2);
	assert.equal(view.total, 2);
	assert.deepEqual(view.cards, []);
	assert.equal(S.arrange([], 6).level, "ok");
});

test("arrange: only non-green, red then grey then amber, longest standing first", () => {
	const view = S.arrange(
		[item("warn", "w", 1), item("ok", "o"), item("unknown", "u", 5), item("crit", "c2", 50), item("crit", "c1", 10), item("crit", "c0", null), item("warn", "w0", 0)],
		10
	);
	assert.deepEqual(view.cards.map((c) => c.label), ["c1", "c2", "c0", "u", "w0", "w"]);
	assert.equal(view.level, "crit");
	assert.equal(view.okCount, 1);
	assert.equal(S.arrange([item("warn", "w"), item("unknown", "u")], 6).level, "unknown");
	assert.equal(S.arrange([item("warn", "w")], 6).level, "warn");
});

test("arrange: ties are ordered by label", () => {
	const view = S.arrange([item("crit", "b", 1), item("crit", "a", 1)], 6);
	assert.deepEqual(view.cards.map((c) => c.label), ["a", "b"]);
});

test("arrange: exactly maxItems stays cards; one more collapses to a count per group", () => {
	const six = Array.from({ length: 6 }, (_, i) => item("warn", `w${i}`, i, "docker"));
	assert.equal(S.arrange(six, 6).cards.length, 6);
	assert.equal(S.arrange(six, 6).groups.length, 0);

	const items = [...six, item("crit", "c", 1, "backups"), item("unknown", "u", 1, "docker"), item("warn", "n", 1)];
	const view = S.arrange(items, 6);
	assert.deepEqual(view.cards, []);
	assert.deepEqual(view.groups.map((g) => [g.group, g.level, g.count]), [["backups", "crit", 1], ["docker", "unknown", 7], ["other", "warn", 1]]);
	assert.deepEqual(view.groups[1].counts, { crit: 0, unknown: 1, warn: 6 });
	assert.equal(view.level, "crit");
});

test("arrange: system items stay cards when collapsing", () => {
	const items = [item("unknown", "~unreachable", 1, null, true), ...Array.from({ length: 4 }, (_, i) => item("warn", `w${i}`, i, "g"))];
	const view = S.arrange(items, 3);
	assert.deepEqual(view.cards.map((c) => c.label), ["~unreachable"]);
	assert.deepEqual(view.groups.map((g) => [g.group, g.count]), [["g", 4]]);
});

test("formatAge", () => {
	const at = (ms) => S.formatAge(NOW - ms, NOW);
	assert.equal(at(0), "<1 min");
	assert.equal(at(59 * 1000), "<1 min");
	assert.equal(at(60 * 1000), "1 min");
	assert.equal(at(59 * MIN), "59 min");
	assert.equal(at(60 * MIN), "1 h");
	assert.equal(at(23 * 60 * MIN + 59 * MIN), "23 h");
	assert.equal(at(24 * 60 * MIN), "1 d");
	assert.equal(at(3.5 * 24 * 60 * MIN), "3 d");
	assert.equal(S.formatAge(NOW + 5 * MIN, NOW), "<1 min"); // in the future
});

test("resultsUrl joins without doubling slashes", () => {
	assert.equal(S.resultsUrl("http://checker.example:8099"), "http://checker.example:8099/results");
	assert.equal(S.resultsUrl("http://checker.example:8099///"), "http://checker.example:8099/results");
});
