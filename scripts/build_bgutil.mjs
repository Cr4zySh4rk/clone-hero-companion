// Bundles bgutil-ytdlp-pot-provider's server (server/src/main.ts) into ONE self-contained ES module that runs with
// plain `deno run` (no node_modules). Called by build_bgutil.sh from inside a checkout of the upstream server dir.
//   node build_bgutil.mjs <server-dir> <out-file>
// canvas (a native addon that jsdom only loads optionally) is left out; jsdom's default stylesheet, which it reads
// from disk at load time, is inlined.
import { build } from "esbuild";
import { readFileSync } from "node:fs";
import path from "node:path";

const [serverDir, outFile] = process.argv.slice(2);
const inlineStylesheet = {
  name: "inline-jsdom-stylesheet",
  setup(b) {
    b.onLoad({ filter: /helpers[\\/]computed-style\.js$/ }, (args) => {
      let src = readFileSync(args.path, "utf8");
      const css = readFileSync(path.join(path.dirname(args.path), "../../../browser/default-stylesheet.css"), "utf8");
      const re = /fs\.readFileSync\(\s*path\.resolve\(__dirname, "\.\.\/\.\.\/\.\.\/browser\/default-stylesheet\.css"\),\s*\{ encoding: "utf-8" \}\s*\)/;
      if (!re.test(src)) throw new Error("jsdom layout changed: default-stylesheet read not found");
      return { contents: src.replace(re, () => JSON.stringify(css)), loader: "js", resolveDir: path.dirname(args.path) };
    });
  },
};
// css-tree's ESM entry loads its JSON data through a runtime createRequire(); its CommonJS build has it inlined.
// Synchronous XHR (never used by the PO token code) would spawn a worker file; point it at nothing.
const noSyncXhrWorker = {
  name: "no-sync-xhr-worker",
  setup(b) {
    b.onLoad({ filter: /xhr[\\/]XMLHttpRequest-impl\.js$/ }, (args) => {
      const needle = 'require.resolve("./xhr-sync-worker.js")';
      const src = readFileSync(args.path, "utf8");
      if (!src.includes(needle)) throw new Error("jsdom layout changed: xhr-sync-worker resolve not found");
      return { contents: src.replace(needle, '"unavailable"'), loader: "js", resolveDir: path.dirname(args.path) };
    });
  },
};
const cssTreeCjs = {
  name: "css-tree-cjs",
  setup(b) {
    b.onResolve({ filter: /^css-tree$/ }, () => ({ path: path.resolve(serverDir, "node_modules/css-tree/cjs/index.cjs") }));
  },
};
await build({
  entryPoints: [path.join(serverDir, "src/main.ts")],
  bundle: true, platform: "node", format: "esm", target: "es2022", minify: true, legalComments: "none",
  external: ["canvas"], plugins: [inlineStylesheet, noSyncXhrWorker, cssTreeCjs], outfile: outFile, logLevel: "warning",
  banner: { js: 'import{createRequire as __cr}from"node:module";const require=__cr(import.meta.url);' },
});
