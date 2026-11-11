/*
 * MMM-AnyStatus: a calm status display. "All OK", or only what needs attention.
 * The checker decides the levels; this module applies one rule of its own, the age rule (see lib/status.js, SPEC.md).
 */
Module.register("MMM-AnyStatus", {
	defaults: {
		checkerUrl: null, // required: base URL of the checker, e.g. "http://checker.example:8099"
		fetchInterval: 60 * 1000, // ms between fetches of <checkerUrl>/results
		maxItems: 6, // more non-green items than this collapse to one count per group
	},

	// How often the display is redrawn from what it has, so ages and the age rule stay current even when no
	// fetch succeeds.
	redrawInterval: 15 * 1000,

	getStyles() {
		return ["MMM-AnyStatus.css"];
	},

	getScripts() {
		return [this.file("lib/status.js")];
	},

	start() {
		this.feed = null; // what the node_helper last reported: {doc, lastValid, error}
		this.startedAt = Date.now();
		this.lastFeedAt = null; // when ANYSTATUS_RESULTS last arrived
		this.cards = new Map(); // key -> element
		this.wrapper = null;
		this.fetchInterval = Math.max(5000, Number(this.config.fetchInterval) || this.defaults.fetchInterval);

		if (this.config.checkerUrl) {
			this.sendSocketNotification("ANYSTATUS_START", {
				identifier: this.identifier,
				checkerUrl: this.config.checkerUrl,
				fetchInterval: this.fetchInterval,
			});
		}
		setInterval(() => this.render(), this.redrawInterval);
	},

	socketNotificationReceived(notification, payload) {
		if (notification !== "ANYSTATUS_RESULTS" || payload.identifier !== this.identifier) return;
		// The last valid results are kept whatever happens: they turn grey by themselves when their ttl runs out.
		this.lastFeedAt = Date.now();
		this.feed = AnyStatus.acceptAnswer(this.feed, payload);
		this.render();
	},

	getDom() {
		if (!this.wrapper) {
			this.wrapper = document.createElement("div");
			this.wrapper.className = "anystatus";
			this.ok = this.element("div", "as-ok");
			this.list = this.element("div", "as-list");
			this.foot = this.element("div", "as-foot");
			this.wrapper.append(this.ok, this.list, this.foot);
			this.render();
		}
		return this.wrapper;
	},

	element(tag, className, text) {
		const el = document.createElement(tag);
		el.className = className;
		if (text !== undefined) el.textContent = text;
		return el;
	},

	items(now) {
		if (!this.config.checkerUrl) {
			return [{ id: "~config", label: "MMM-AnyStatus", group: null, level: "unknown", summary: "checkerUrl is not configured", since: null, system: true }];
		}
		return AnyStatus.buildItems(this.feed, now, { lastFeedAt: this.lastFeedAt === null ? this.startedAt : this.lastFeedAt, fetchInterval: this.fetchInterval });
	},

	// Updates the DOM in place (not through updateDom) so that CSS transitions run.
	render() {
		if (!this.wrapper) return;
		const now = Date.now();
		const view = AnyStatus.arrange(this.items(now), this.config.maxItems);

		// Card keys must be unique: an id used more than once (SPEC: every one shows) gets "#2", "#3"...
		const seenIds = new Map();
		const keyFor = (id) => {
			const n = (seenIds.get(id) || 0) + 1;
			seenIds.set(id, n);
			return n === 1 ? id : `${id}#${n}`;
		};
		const wanted = view.cards.map((item) => ({
			key: keyFor(item.id),
			level: item.level,
			label: item.label,
			summary: item.summary,
			meta: item.since === null ? "" : `since ${AnyStatus.formatAge(item.since, now)}`,
		}));
		const names = { crit: "red", unknown: "grey", warn: "amber" };
		for (const group of view.groups) {
			// The names, each in its level's colour, replace the "2 red, 1 grey" counts; the counts stay as a tooltip.
			const parts = ["crit", "unknown", "warn"].filter((l) => group.counts[l]).map((l) => `${group.counts[l]} ${names[l]}`);
			wanted.push({
				key: `group:${group.group}`,
				level: group.level,
				label: group.group,
				summary: group.members.map((m) => m.label).join(", ") + (group.more ? ` +${group.more} more` : ""),
				names: group.members,
				more: group.more,
				title: parts.join(", "),
				meta: String(group.count),
			});
		}
		this.reconcile(wanted);

		this.wrapper.classList.toggle("as-is-ok", view.level === "ok");
		this.ok.textContent = "";
		this.ok.append(this.element("div", "as-ok-title", "All OK"), this.element("div", "as-ok-sub", view.total === 0 ? "no checks reported" : `${view.okCount} checks`));
		this.foot.textContent = view.level !== "ok" && view.okCount > 0 ? `${view.okCount} ok` : "";
	},

	reconcile(wanted) {
		const seen = new Set();
		for (const want of wanted) {
			seen.add(want.key);
			let el = this.cards.get(want.key);
			if (!el) {
				el = this.element("div", "as-card as-enter");
				el.append(this.element("div", "as-label"), this.element("div", "as-summary"), this.element("div", "as-meta"));
				this.cards.set(want.key, el);
				this.list.append(el);
				requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("as-enter")));
			}
			el.dataset.level = want.level;
			el.children[0].textContent = want.label;
			if (want.names) {
				const summary = el.children[1];
				summary.textContent = "";
				want.names.forEach((m, i) => {
					if (i > 0) summary.append(", ");
					const span = this.element("span", "as-name", m.label);
					span.dataset.level = m.level;
					summary.append(span);
				});
				if (want.more) summary.append(this.element("span", "as-more", ` +${want.more} more`));
				el.title = want.title;
			} else {
				el.children[1].textContent = want.summary;
				el.removeAttribute("title");
			}
			el.children[2].textContent = want.meta;
		}
		for (const [key, el] of this.cards) {
			if (seen.has(key)) continue;
			this.cards.delete(key);
			el.classList.add("as-leave");
			setTimeout(() => el.remove(), 700);
		}
		// Put the cards in the wanted order, skipping those that are leaving.
		let ref = this.list.firstChild;
		for (const want of wanted) {
			const el = this.cards.get(want.key);
			while (ref && ref.classList.contains("as-leave")) ref = ref.nextSibling;
			if (ref === el) ref = ref.nextSibling;
			else this.list.insertBefore(el, ref);
		}
	},
});
