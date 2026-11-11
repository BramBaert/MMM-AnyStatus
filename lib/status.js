/*
 * MMM-AnyStatus: the pure logic of the display (no DOM, no network).
 *
 * Loaded in the browser through the module's getScripts() (it then defines the global `AnyStatus`) and in Node by
 * require() (the tests, node_helper). Every function takes `now` (milliseconds since the epoch) as an argument, so
 * the logic is deterministic.
 *
 * It implements the consumer rules of SPEC.md: validation, the age rule, "nothing disappears silently".
 */
(function (root, factory) {
	if (typeof module === "object" && module.exports) {
		module.exports = factory();
	} else {
		root.AnyStatus = factory();
	}
})(typeof self !== "undefined" ? self : this, function () {
	"use strict";

	const SUPPORTED_MAJOR = 1;
	const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
	const TIME_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
	const LEVELS = ["ok", "warn", "crit", "unknown"];

	/** Order on screen: red, then grey, then amber. */
	const SEVERITY_ORDER = { crit: 0, unknown: 1, warn: 2, ok: 3 };

	/**
	 * Parse an RFC 3339 time with an explicit offset.
	 * @param {*} value anything
	 * @returns {number} milliseconds since the epoch, or NaN when it is not a valid time
	 */
	function parseTime(value) {
		if (typeof value !== "string" || !TIME_PATTERN.test(value)) return NaN;
		return Date.parse(value);
	}

	function isObject(value) {
		return typeof value === "object" && value !== null && !Array.isArray(value);
	}

	function describeValue(value) {
		return String(value).slice(0, 40);
	}

	/**
	 * Why a raw result is invalid, or null when it is valid. Mirrors schema/results-1.schema.json, plus the rules the
	 * schema can't express (a real calendar time).
	 */
	function invalidReason(raw) {
		if (typeof raw.level !== "string") return "no level";
		if (raw.ts === undefined) return "no ts";
		if (Number.isNaN(parseTime(raw.ts))) return "bad ts";
		if (raw.ttl === undefined) return "no ttl";
		if (typeof raw.ttl !== "number" || !Number.isFinite(raw.ttl) || raw.ttl <= 0) return "bad ttl";
		if (raw.since !== undefined && Number.isNaN(parseTime(raw.since))) return "bad since";
		for (const field of ["label", "group", "summary", "unit"]) {
			if (raw[field] !== undefined && typeof raw[field] !== "string") return `bad ${field}`;
		}
		if (raw.value !== undefined && typeof raw.value !== "number" && typeof raw.value !== "string") return "bad value";
		return null;
	}

	function makeItem(fields) {
		return Object.assign({ id: "", label: "", group: null, level: "unknown", summary: "", since: null, system: false }, fields);
	}

	/** An item that is about the display or the document, not one check. */
	function systemItem(id, label, summary, since) {
		return makeItem({ id: `~${id}`, label, level: "unknown", summary, since: since === undefined ? null : since, system: true });
	}

	function valueText(raw) {
		if (raw.value === undefined) return "";
		return raw.unit ? `${raw.value} ${raw.unit}` : String(raw.value);
	}

	/**
	 * Turn one raw result of a valid document into a display item.
	 * @param {*} raw the result as parsed from JSON
	 * @param {number} now
	 * @returns {object} item: {id, label, group, level, summary, since (ms or null), system}
	 */
	function evaluateResult(raw, now) {
		if (!isObject(raw)) {
			return systemItem("invalid-result", "Invalid result", "invalid result: not an object");
		}
		const hasId = typeof raw.id === "string" && ID_PATTERN.test(raw.id);
		if (!hasId) {
			return systemItem("invalid-result", "Invalid result", "invalid result: no usable id");
		}
		const label = typeof raw.label === "string" && raw.label !== "" ? raw.label : raw.id;
		const group = typeof raw.group === "string" && raw.group !== "" ? raw.group : null;
		const reason = invalidReason(raw);
		if (reason) {
			return makeItem({ id: raw.id, label, group, level: "unknown", summary: `invalid result: ${reason}` });
		}

		const ts = parseTime(raw.ts);
		const ttlMs = raw.ttl * 1000;
		if (now > ts + ttlMs) {
			return makeItem({ id: raw.id, label, group, level: "unknown", summary: "no data", since: ts });
		}
		if (ts > now + ttlMs) {
			return makeItem({ id: raw.id, label, group, level: "unknown", summary: "clock mismatch" });
		}

		let since = raw.since === undefined ? null : parseTime(raw.since);
		if (since !== null && since > ts) since = ts;
		const summary = typeof raw.summary === "string" && raw.summary !== "" ? raw.summary : valueText(raw);

		if (!LEVELS.includes(raw.level)) {
			return makeItem({ id: raw.id, label, group, level: "unknown", summary: `unknown level "${describeValue(raw.level)}"`, since });
		}
		return makeItem({ id: raw.id, label, group, level: raw.level, summary, since });
	}

	/**
	 * Turn a results document into display items. Nothing is dropped: whatever can't be read becomes an `unknown` item.
	 * @param {*} doc the document as parsed from JSON
	 * @param {number} now
	 * @returns {object[]} items
	 */
	function evaluateDocument(doc, now) {
		if (!isObject(doc)) {
			return [systemItem("document", "Results", "invalid document: not an object")];
		}
		const name = typeof doc.source === "string" && doc.source !== "" ? doc.source : "Results";
		const match = typeof doc.spec === "string" ? /^(\d+)\.(\d+)$/.exec(doc.spec) : null;
		if (!match) {
			return [systemItem("document", name, "invalid document: no valid spec")];
		}
		if (Number(match[1]) !== SUPPORTED_MAJOR) {
			return [systemItem("document", name, `unsupported spec ${doc.spec}`)];
		}
		if (!Array.isArray(doc.results)) {
			return [systemItem("document", name, "invalid document: no results array")];
		}

		const counts = new Map();
		for (const raw of doc.results) {
			if (isObject(raw) && typeof raw.id === "string") counts.set(raw.id, (counts.get(raw.id) || 0) + 1);
		}
		return doc.results.map((raw) => {
			if (isObject(raw) && counts.get(raw.id) > 1) {
				const label = typeof raw.label === "string" && raw.label !== "" ? raw.label : raw.id;
				return makeItem({ id: raw.id, label, group: typeof raw.group === "string" ? raw.group : null, summary: "invalid result: duplicate id" });
			}
			return evaluateResult(raw, now);
		});
	}

	/**
	 * Build the items to show from what the node_helper last reported.
	 * @param {object|null} feed {doc, error: {message, lastSuccess, firstFailure}|null, source}, null before anything arrived
	 * @param {number} now
	 * @returns {object[]} items
	 */
	function buildItems(feed, now) {
		if (!feed || (!feed.doc && !feed.error)) {
			return [systemItem("waiting", "Results", "no data yet")];
		}
		const items = feed.doc ? evaluateDocument(feed.doc, now) : [];
		if (feed.error) {
			const source = (feed.doc && typeof feed.doc.source === "string" && feed.doc.source) || feed.source || "checker";
			const since = feed.error.lastSuccess || feed.error.firstFailure || null;
			items.push(systemItem("unreachable", `${source} unreachable`, feed.error.message || "unreachable", since));
		}
		return items;
	}

	/** Compare for display: red, grey, amber; within a level, the longest standing first, then by label. */
	function compareItems(a, b) {
		const bySeverity = SEVERITY_ORDER[a.level] - SEVERITY_ORDER[b.level];
		if (bySeverity !== 0) return bySeverity;
		if (a.since !== b.since) {
			if (a.since === null) return 1;
			if (b.since === null) return -1;
			return a.since - b.since;
		}
		return a.label < b.label ? -1 : a.label > b.label ? 1 : 0;
	}

	/**
	 * Decide what to show.
	 * @param {object[]} items from buildItems()
	 * @param {number} maxItems more non-green items than this collapse to one count per group
	 * @returns {object} {total, okCount, level (worst), cards: item[], groups: [{group, level, counts, count}]}
	 */
	function arrange(items, maxItems) {
		const nonGreen = items.filter((item) => item.level !== "ok").sort(compareItems);
		const result = {
			total: items.length,
			okCount: items.length - nonGreen.length,
			level: nonGreen.length ? nonGreen[0].level : "ok",
			cards: nonGreen,
			groups: [],
		};
		if (nonGreen.length <= maxItems) return result;

		// Too many: system items (unreachable, invalid document) always stay a card; the rest become counts per group.
		const groups = new Map();
		result.cards = nonGreen.filter((item) => item.system);
		for (const item of nonGreen.filter((i) => !i.system)) {
			const key = item.group || "other";
			if (!groups.has(key)) groups.set(key, { group: key, level: item.level, counts: { crit: 0, unknown: 0, warn: 0 }, count: 0 });
			const entry = groups.get(key);
			entry.counts[item.level] += 1;
			entry.count += 1;
			if (SEVERITY_ORDER[item.level] < SEVERITY_ORDER[entry.level]) entry.level = item.level;
		}
		result.groups = Array.from(groups.values()).sort(
			(a, b) => SEVERITY_ORDER[a.level] - SEVERITY_ORDER[b.level] || (a.group < b.group ? -1 : a.group > b.group ? 1 : 0)
		);
		return result;
	}

	/**
	 * A relative age: "<1 min", "5 min", "2 h", "3 d". Rounds down; an age in the future counts as 0.
	 * @param {number} fromMs the time something started
	 * @param {number} now
	 * @returns {string}
	 */
	function formatAge(fromMs, now) {
		const seconds = Math.max(0, Math.floor((now - fromMs) / 1000));
		if (seconds < 60) return "<1 min";
		const minutes = Math.floor(seconds / 60);
		if (minutes < 60) return `${minutes} min`;
		const hours = Math.floor(minutes / 60);
		if (hours < 24) return `${hours} h`;
		return `${Math.floor(hours / 24)} d`;
	}

	/** Join the checker's base URL and the results path. */
	function resultsUrl(checkerUrl) {
		return `${String(checkerUrl).replace(/\/+$/, "")}/results`;
	}

	return { parseTime, evaluateResult, evaluateDocument, buildItems, arrange, formatAge, resultsUrl, compareItems };
});
