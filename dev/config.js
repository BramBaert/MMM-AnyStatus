// MagicMirror config for development: server only, one module instance per display state, fed by the demo server.
// The scenarios are in examples/hand-written/scenarios/; the demo server serves them at /scenario/<name>/results.
const config = {
	address: "0.0.0.0",
	port: 8080,
	ipWhitelist: [], // development only: any address may open the page
	language: "en",
	timeFormat: 24,
	units: "metric",

	// Only top_* regions: MagicMirror's top regions grow downward and its bottom regions upward, and nothing stops them
	// meeting, so on a screen shorter than both together the bottom instances were drawn over the top ones. Within one
	// region the instances stack in normal flow and never overlap; a column that is too long runs off the screen.
	modules: [
		{
			module: "MMM-AnyStatus",
			position: "top_left",
			header: "all OK",
			config: { checkerUrl: "http://demo:8099/scenario/all-ok", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_left",
			header: "empty (no checks)",
			config: { checkerUrl: "http://demo:8099/scenario/empty", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_left",
			header: "unsupported spec",
			config: { checkerUrl: "http://demo:8099/scenario/unsupported", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_center",
			header: "mixed",
			config: { checkerUrl: "http://demo:8099/scenario/mixed", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_right",
			header: "many (groups)",
			config: { checkerUrl: "http://demo:8099/scenario/many", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_left",
			header: "stale and clock mismatch",
			config: { checkerUrl: "http://demo:8099/scenario/stale", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_center",
			header: "invalid results",
			config: { checkerUrl: "http://demo:8099/scenario/invalid", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_right",
			header: "rotate: a new scenario every 20 s",
			config: { checkerUrl: "http://demo:8099/scenario/rotate", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_left",
			header: "unreachable (HTTP 503)",
			config: { checkerUrl: "http://demo:8099/scenario/unreachable", fetchInterval: 5 * 1000, maxItems: 6 },
		},
	],
};

/*************** DO NOT EDIT THE LINE BELOW ***************/
if (typeof module !== "undefined") {
	module.exports = config;
}
