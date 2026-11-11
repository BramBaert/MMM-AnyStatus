"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createFetcher } = require("../lib/fetcher");

function fakeClock(...times) {
	let i = 0;
	return () => times[Math.min(i++, times.length - 1)];
}

const ok = (body) => async () => ({ ok: true, status: 200, json: async () => body });

test("success returns the document", async () => {
	const doc = { spec: "1.0", results: [] };
	let seen;
	const fetcher = createFetcher({
		checkerUrl: "http://checker.example:8099/",
		fetchImpl: async (url) => {
			seen = url;
			return ok(doc)();
		},
	});
	assert.deepEqual(await fetcher.poll(), { doc });
	assert.equal(seen, "http://checker.example:8099/results");
});

test("failure reports the error with the time of the last success", async () => {
	const answers = [ok({ spec: "1.0", results: [] }), async () => ({ ok: false, status: 503 }), async () => { throw new Error("connect ECONNREFUSED"); }];
	let n = 0;
	const fetcher = createFetcher({ checkerUrl: "http://x.example", fetchImpl: (...a) => answers[n++](...a), clock: fakeClock(1000, 2000, 3000, 4000) });
	assert.ok((await fetcher.poll()).doc); // success at 1000
	const first = await fetcher.poll(); // failure, first at 2000
	assert.deepEqual(first, { error: { message: "unreachable: HTTP 503", lastSuccess: 1000, firstFailure: 2000 } });
	const second = await fetcher.poll(); // still failing: firstFailure stays
	assert.deepEqual(second.error, { message: "unreachable: connect ECONNREFUSED", lastSuccess: 1000, firstFailure: 2000 });
});

test("a failure before any success has no lastSuccess; recovery resets firstFailure", async () => {
	const answers = [async () => { throw new Error("down"); }, ok({ spec: "1.0", results: [] }), async () => { throw new Error("down"); }];
	let n = 0;
	const fetcher = createFetcher({ checkerUrl: "http://x.example", fetchImpl: (...a) => answers[n++](...a), clock: fakeClock(10, 20, 30) });
	assert.deepEqual((await fetcher.poll()).error, { message: "unreachable: down", lastSuccess: null, firstFailure: 10 });
	await fetcher.poll(); // success at 20
	assert.deepEqual((await fetcher.poll()).error, { message: "unreachable: down", lastSuccess: 20, firstFailure: 30 });
});

test("an answer that is not JSON is an error", async () => {
	const fetcher = createFetcher({
		checkerUrl: "http://x.example",
		fetchImpl: async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError("bad"); } }),
	});
	assert.equal((await fetcher.poll()).error.message, "unreachable: answer is not JSON");
});

test("a timeout is reported as such", async () => {
	const fetcher = createFetcher({
		checkerUrl: "http://x.example",
		timeoutMs: 20,
		fetchImpl: (url, { signal }) => new Promise((_, reject) => signal.addEventListener("abort", () => reject(signal.reason))),
	});
	assert.equal((await fetcher.poll()).error.message, "unreachable: timed out");
});

const { createPoller, timeoutFor } = require("../lib/fetcher");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("timeoutFor is capped at the fetch interval and at 10 s", () => {
	assert.equal(timeoutFor(5000), 5000);
	assert.equal(timeoutFor(60000), 10000);
});

test("poller: the next poll starts only after the current one finished (no overlap)", async () => {
	let running = 0;
	let maxRunning = 0;
	let polls = 0;
	const fetcher = { poll: async () => { polls += 1; running += 1; maxRunning = Math.max(maxRunning, running); await sleep(30); running -= 1; return { doc: {} }; } };
	const answers = [];
	const poller = createPoller({ fetcher, intervalMs: 5, onAnswer: (a) => answers.push(a) });
	poller.start();
	await sleep(200);
	poller.stop();
	assert.equal(maxRunning, 1);
	assert.ok(polls >= 3, `polls: ${polls}`);
	assert.equal(answers.length, polls - (running ? 1 : 0));
});

test("poller: stop ends the loop and drops an in-flight answer", async () => {
	let polls = 0;
	const answers = [];
	const poller = createPoller({ fetcher: { poll: async () => { polls += 1; await sleep(30); return {}; } }, intervalMs: 5, onAnswer: (a) => answers.push(a) });
	poller.start();
	await sleep(10);
	poller.stop();
	await sleep(120);
	assert.equal(polls, 1);
	assert.equal(answers.length, 0);
});

test("poller: start again (browser reload) runs one loop, not two", async () => {
	let polls = 0;
	const fetcher = { poll: async () => { polls += 1; await sleep(20); return {}; } };
	let answers = 0;
	const poller = createPoller({ fetcher, intervalMs: 40, onAnswer: () => { answers += 1; } });
	poller.start();
	await sleep(5);
	poller.start(); // restart while the first poll is in flight
	await sleep(300);
	poller.stop();
	const settled = polls;
	await sleep(100);
	assert.equal(polls, settled); // nothing keeps running
	// one loop: about 300 / (20 + 40) polls, plus the dropped one; two loops would roughly double it
	assert.ok(polls <= 8, `polls: ${polls}`);
	assert.ok(answers >= 3);
});
