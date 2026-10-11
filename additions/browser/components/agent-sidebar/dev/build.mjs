import { build, context } from "esbuild";
import { readFile } from "node:fs/promises";

const license = await readFile(new URL("../node_modules/dom-accessibility-api/LICENSE.md", import.meta.url), "utf8");
const jobs = [
  { entryPoints: ["content/index.jsx"], bundle: true, minify: true, charset: "utf8", format: "iife", outfile: "content/agent-sidebar.bundle.js" },
  { entryPoints: ["dev/accessibility.mjs"], bundle: true, minify: true, format: "esm", platform: "browser", outfile: "modules/PageAccessibility.sys.mjs", banner: { js: "/*! dom-accessibility-api\n" + license + "\n*/" } },
];
for (const options of jobs) {
  if (process.argv.includes("--watch")) await (await context(options)).watch();
  else await build(options);
}
