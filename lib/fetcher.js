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

/**
 * Poll one checker without overlap: the next poll is scheduled only after the current one has finished.
 * start() begins a new loop and stops the previous one; an answer that arrives after stop() is dropped.
 * @param {object} options
 * @param {{poll: Function}} options.fetcher
 * @param {number} options.intervalMs wait this long between the end of one poll and the start of the next
 * @param {Function} options.onAnswer called with each answer of the current loop
 * @returns {{start: function(): void, stop: function(): void}}
 */
function createPoller({ fetcher, intervalMs, onAnswer }) {
	let generation = 0;
	let timer = null;

	function stop() {
		generation += 1;
		if (timer !== null) clearTimeout(timer);
		timer = null;
	}

	function start() {
		stop();
		const mine = generation;
		const run = async () => {
			timer = null;
			const answer = await fetcher.poll();
			if (mine !== generation) return;
			onAnswer(answer);
			timer = setTimeout(run, intervalMs);
		};
		run();
	}

	return { start, stop };
}

/** One request may take at most 10 s, and never longer than the fetch interval. */
function timeoutFor(fetchInterval) {
	return Math.min(10000, fetchInterval);
}

module.exports = { createFetcher, createPoller, timeoutFor };
