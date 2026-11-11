/*
 * MMM-AnyStatus: fetch the results from a checker and keep track of when that last worked.
 * Node only (global fetch, Node 18+). The helper decides nothing about levels: it forwards the document, or the error.
 */
"use strict";

const { resultsUrl } = require("./status");

/**
 * Create a fetcher for one checker.
 * @param {object} options
 * @param {string} options.checkerUrl base URL of the checker
 * @param {number} [options.timeoutMs] give up on one request after this long
 * @param {Function} [options.fetchImpl] for tests
 * @param {Function} [options.clock] for tests; returns ms since the epoch
 * @returns {{poll: function(): Promise<object>}} poll() resolves to {doc} or {error: {message, lastSuccess, firstFailure}}
 */
function createFetcher({ checkerUrl, timeoutMs = 10000, fetchImpl = globalThis.fetch, clock = Date.now }) {
	const url = resultsUrl(checkerUrl);
	let lastSuccess = null;
	let firstFailure = null;

	async function poll() {
		try {
			const response = await fetchImpl(url, { signal: AbortSignal.timeout(timeoutMs), headers: { Accept: "application/json" } });
			if (!response.ok) throw new Error(`HTTP ${response.status}`);
			let doc;
			try {
				doc = await response.json();
			} catch {
				throw new Error("answer is not JSON");
			}
			lastSuccess = clock();
			firstFailure = null;
			return { doc };
		} catch (err) {
			if (firstFailure === null) firstFailure = clock();
			const reason = err && err.name === "TimeoutError" ? "timed out" : (err && err.message) || "failed";
			return { error: { message: `unreachable: ${reason}`, lastSuccess, firstFailure } };
		}
	}

	return { poll };
}

module.exports = { createFetcher };
