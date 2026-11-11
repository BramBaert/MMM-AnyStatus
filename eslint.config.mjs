import js from "@eslint/js";
import globals from "globals";

export default [
	{ ignores: ["node_modules/"] },
	js.configs.recommended,
	{
		languageOptions: { ecmaVersion: 2023, sourceType: "commonjs", globals: { ...globals.node } },
	},
	{
		files: ["eslint.config.mjs"],
		languageOptions: { sourceType: "module" },
	},
	{
		// The pure logic runs in the browser and in Node.
		files: ["lib/status.js"],
		languageOptions: { globals: { ...globals.node, ...globals.browser } },
	},
	{
		files: ["MMM-AnyStatus.js"],
		languageOptions: { sourceType: "script", globals: { ...globals.browser, Module: "readonly", AnyStatus: "readonly" } },
	},
	{
		files: ["dev/config.js"],
		languageOptions: { globals: { ...globals.node } },
	},
];
