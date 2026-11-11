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
