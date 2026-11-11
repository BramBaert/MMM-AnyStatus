/*
 * MMM-AnyStatus node_helper: fetches GET <checkerUrl>/results every fetchInterval and forwards the document, or the
 * error, to the module instance that asked. It decides nothing about levels.
 *
 * One node_helper serves all instances of the module: everything is keyed by the instance's identifier.
 */
"use strict";

const NodeHelper = require("node_helper");
const { createFetcher, createPoller, timeoutFor } =require("./lib/fetcher");

module.exports = NodeHelper.create({
	start() {
		this.instances = new Map(); // identifier -> poller
	},

	stop() {
		for (const poller of this.instances.values()) poller.stop();
		this.instances.clear();
	},

	socketNotificationReceived(notification, payload) {
		if (notification !== "ANYSTATUS_START") return;
		const { identifier, checkerUrl, fetchInterval } = payload;
		const previous = this.instances.get(identifier);
		if (previous) previous.stop(); // the browser reloaded: end the old loop, drop its in-flight answer

		const fetcher = createFetcher({ checkerUrl, timeoutMs: timeoutFor(fetchInterval) });
		const poller = createPoller({
			fetcher,
			intervalMs: fetchInterval,
			onAnswer: (answer) => this.sendSocketNotification("ANYSTATUS_RESULTS", Object.assign({ identifier }, answer)),
		});
		this.instances.set(identifier, poller);
		poller.start();
	},
});
