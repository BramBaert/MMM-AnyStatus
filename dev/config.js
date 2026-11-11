// MagicMirror config for development: server only, four module instances: three fed by the demo server (two of them rotating through display states), one by the real checker.
// The scenarios are in examples/hand-written/scenarios/; the demo server serves them at /scenario/<name>/results.
const config = {
	address: "0.0.0.0",
	port: 8080,
	ipWhitelist: [], // development only: any address may open the page
	language: "en",
	timeFormat: 24,
	units: "metric",

	// Three demo instances, one per column, so that every column stays short (stacked instances overlapped on short screens).
	// Two of them rotate through scenarios (the demo server's /rotate/<name>,<name>,.../results, a new one every 20 s).
	modules: [
		{
			module: "MMM-AnyStatus",
			position: "top_left",
			header: "Healthy checker (rotating): all OK, mixed, stale",
			config: { checkerUrl: "http://demo:8099/rotate/all-ok,mixed,stale", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_center",
			header: "many problems: collapsed into groups",
			config: { checkerUrl: "http://demo:8099/scenario/many", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_right",
			header: "Broken checker (rotating): how the module copes",
			config: { checkerUrl: "http://demo:8099/rotate/invalid,empty,unsupported,unreachable", fetchInterval: 5 * 1000, maxItems: 6 },
		},
		{
			module: "MMM-AnyStatus",
			position: "top_left", // under the healthy-checker rotation: bottom_* overlaps top_* on short screens
			header: "Real checker (examples + mqtt)",
			config: { checkerUrl: "http://checker:8099", fetchInterval: 5 * 1000, maxItems: 8 },
		},
	],
};
/*************** DO NOT EDIT THE LINE BELOW ***************/
if (typeof module !== "undefined") {
	module.exports = config;
}
