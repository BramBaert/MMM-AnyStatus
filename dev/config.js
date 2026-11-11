// MagicMirror config for development: server only, the module pointed at the demo results server.
const config = {
	address: "0.0.0.0",
	port: 8080,
	ipWhitelist: [], // development only: any address may open the page
	language: "en",
	timeFormat: 24,
	units: "metric",

	modules: [
		{
			module: "MMM-AnyStatus",
			position: "middle_center",
			config: {
				checkerUrl: "http://demo:8099",
				fetchInterval: 10 * 1000,
				maxItems: 6,
			},
		},
	],
};

/*************** DO NOT EDIT THE LINE BELOW ***************/
if (typeof module !== "undefined") {
	module.exports = config;
}
