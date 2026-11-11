/*
 * MMM-AnyStatus node_helper: fetches GET <checkerUrl>/results every fetchInterval and forwards the document, or the
 * error, to the module instance that asked. It decides nothing about levels.
 *
 * One node_helper serves all instances of the module: everything is keyed by the instance's identifier.
 */
"use strict";

const NodeHelper = require("node_helper");
const { createFetcher } = require("./lib/fetcher");

module.exports = NodeHelper.create({
	start() {
		this.instances = new Map(); // identifier -> {timer, fetcher}
	},

	stop() {
		for (const instance of this.instances.values()) clearInterval(instance.timer);
		this.instances.clear();
	},

	socketNotificationReceived(notification, payload) {
		if (notification !== "ANYSTATUS_START") return;
		const { identifier, checkerUrl, fetchInterval } = payload;
		const previous = this.instances.get(identifier);
		if (previous) clearInterval(previous.timer); // the browser reloaded

		const fetcher = createFetcher({ checkerUrl });
		const run = async () => {
			const answer = await fetcher.poll();
			this.sendSocketNotification("ANYSTATUS_RESULTS", Object.assign({ identifier }, answer));
		};
		this.instances.set(identifier, { fetcher, timer: setInterval(run, fetchInterval) });
		run();
	},
});
