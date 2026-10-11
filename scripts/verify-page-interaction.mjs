#!/usr/bin/env node
// Synthetic fixtures only. Every browser process/profile is owned by this run.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import http from "node:http";
import net from "node:net";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

const options = process.argv.slice(2);
const option = key => options[options.indexOf(key) + 1];
assert(options.includes("--firefox") && options.includes("--mcp-root") && options.includes("--xul-sha256"));
const app = path.resolve(option("--firefox"));
const executable = app.endsWith(".app") ? path.join(app, "Contents/MacOS/firefox") : app;
const xul = path.join(path.dirname(executable), process.platform === "darwin" ? "XUL" : process.platform === "win32" ? "xul.dll" : "libxul.so");
const hash = createHash("sha256").update(await fs.readFile(xul)).digest("hex");
assert.equal(hash, option("--xul-sha256"));
const { MarionetteWire } = await import(pathToFileURL(path.resolve(option("--mcp-root"), "dist/bridge/marionetteWire.js")));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "frx-page-test-"));
const listen = server => new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
const portServer = net.createServer();
await listen(portServer);
const port = portServer.address().port;
await new Promise(resolve => portServer.close(resolve));
const server = http.createServer((request, response) => {
  response.setHeader("Content-Type", "text/html; charset=utf-8");
  const origin = `http://127.0.0.1:${server.address().port}`;
  const cross = origin.replace("127.0.0.1", "localhost");
  const fixture = `<style>button,input{margin:8px;padding:12px}iframe{width:600px;height:250px}body{margin:0}</style>
    <button id="go" onclick="this.dataset.clicked='yes';this.dataset.trusted=String(event.isTrusted)" aria-label="Continue">Go</button>
    <label for="account">Account</label><input id="account"><input type="password" aria-label="Secret" value="never-in-snapshot">
    <button>Duplicate</button><button>Duplicate</button><button hidden>Hidden</button>
    <div id="shadow"></div><script>document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML='<button>Shadow</button>';</script>`;
  response.end(`<!doctype html><title>FRX Page Fixture</title>${fixture}${request.url === "/" ? `<iframe src="${cross}/child"></iframe>` : request.url === "/child" ? `<iframe src="${origin}/nested"></iframe>` : ""}`);
});
await listen(server);
await fs.writeFile(path.join(directory, "user.js"), 'user_pref("browser.shell.checkDefaultBrowser",false);\nuser_pref("browser.startup.homepage","about:blank");\nuser_pref("browser.aboutwelcome.enabled",false);\n');
const env = { ...process.env };
for (const key of Object.keys(env)) if (/^(MOZ_FRX_|FRX_ENV_|XRE_PROFILE_|MOZ_DISABLE_CONTENT_SANDBOX)/.test(key)) delete env[key];
Object.assign(env, { MOZ_MARIONETTE: "1", MOZ_MARIONETTE_PREF_STATE_ACROSS_RESTARTS: JSON.stringify({ "marionette.port": port }) });
const proc = spawn(executable, ["-new-instance", "-no-remote", "-profile", directory, "-marionette", "-remote-allow-system-access", ...(process.platform === "win32" ? ["-no-deelevate", "-wait-for-browser"] : []), "about:blank"], { env, stdio: ["ignore", "pipe", "pipe"] });
let output = "";
let launchError;
proc.on("error", error => { launchError = error; });
proc.stdout.on("data", chunk => { output = (output + chunk).slice(-4000); });
proc.stderr.on("data", chunk => { output = (output + chunk).slice(-4000); });
let wire;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const checks = [];
const check = (label, value) => { assert(value, label); checks.push(label); };
try {
  const deadline = Date.now() + 90000;
  while (true) {
    if (launchError || proc.exitCode !== null) throw new Error(`launch failed: ${launchError || output}`);
    wire = new MarionetteWire();
    try { await wire.connect("127.0.0.1", port, 10000); break; }
    catch (error) { await wire.close(); if (Date.now() > deadline) throw error; await delay(300); }
  }
  const identity = await wire.execute('return {profile:Services.dirsvc.get("ProfD",Ci.nsIFile).path,buildID:Services.appinfo.appBuildID};');
  check("owned profile", await fs.realpath(identity.profile) === await fs.realpath(directory));
  await wire.command("Marionette:SetContext", { value: "content" });
  await wire.command("WebDriver:Navigate", { url: `http://127.0.0.1:${server.address().port}/` });
  await wire.command("Marionette:SetContext", { value: "chrome" });
  await wire.execute('const win=Services.wm.getMostRecentWindow("navigator:browser");win.__frxPageTest=new (ChromeUtils.importESModule("resource:///modules/agentsidebar/PageBackend.sys.mjs").PageBackend)();');
  const call = (method, args = {}) => wire.executeAsync(`const done=arguments[arguments.length-1];const win=Services.wm.getMostRecentWindow("navigator:browser");Promise.resolve().then(()=>win.__frxPageTest[${JSON.stringify(method)}](${JSON.stringify(args)},{win})).then(done,error=>done({ok:false,error:String(error)}));`);
  let snapshot;
  for (let i = 0; i < 60; i++) { snapshot = await call("snapshot", { frameId: "all" }); if (snapshot.frames?.length === 3 && !snapshot.partial) break; await delay(200); }
  assert(snapshot.frames?.length === 3 && !snapshot.partial, `three nested frames: ${JSON.stringify(snapshot)}`);
  checks.push("three nested frames");
  check("password redaction", !JSON.stringify(snapshot).includes("never-in-snapshot"));
  check("open shadow roots", snapshot.elements.filter(e => e.name === "Shadow").length === 3);
  const frame = snapshot.frames[2].frameId;
  const found = await call("find", { frameId: frame, role: "button", name: "Continue" });
  check("semantic nested lookup", found.count === 1);
  check("nested reference click", (await call("click", { ref: found.elements[0].ref })).ok);
  check("ambiguous target rejected", /ambiguous_target/.test((await call("click", { frameId: frame, name: "Duplicate" })).error));
  check("semantic type treats text as value", (await call("type", { frameId: frame, label: "Account", text: "filled-value" })).ok);
  await wire.command("Marionette:SetContext", { value: "content" });
  await wire.command("WebDriver:SwitchToFrame", { id: 0 });
  await wire.command("WebDriver:SwitchToFrame", { id: 0 });
  check("nested click changes DOM", await wire.execute("return document.querySelector('#go').dataset.clicked === 'yes';"));
  check("nested input changes DOM", await wire.execute("return document.querySelector('#account').value === 'filled-value';"));
  await wire.command("WebDriver:SwitchToFrame", { id: null });
  await wire.command("Marionette:SetContext", { value: "chrome" });
  const topButton = (await call("find", { role: "button", name: "Continue" })).elements[0];
  const coordinates = await call("click", { x: topButton.rect.x + 5, y: topButton.rect.y + 5 });
  assert(coordinates.ok, `coordinate click: ${JSON.stringify(coordinates)}; target=${JSON.stringify(topButton)}`);
  checks.push("coordinate click");
  check("coordinate click dispatched trusted event", (await call("eval", { expression: "document.querySelector('#go').dataset.trusted" })).value === "true");
  check("legacy selector click", (await call("click", { selector: "#go" })).ok);
  check("legacy elements", (await call("elements", { limit: 2 })).count === 2);
  check("legacy eval", (await call("eval", { expression: "document.querySelector('#go').dataset.clicked" })).value === "yes");
  check("bounds", (await call("snapshot", { frameId: "all", limit: 1 })).truncated);
  const image = await call("screenshot");
  check("screenshot dimensions", image.width > 100 && image.height > 100 && image.coordinateSpace === "top-viewport-css");
  if (options.includes("--screenshot")) await fs.writeFile(option("--screenshot"), Buffer.from(image._media[0].dataUrl.split(",")[1], "base64"));
  await wire.command("Marionette:SetContext", { value: "content" });
  await wire.command("WebDriver:Navigate", { url: "about:blank" });
  await wire.command("Marionette:SetContext", { value: "chrome" });
  check("navigation invalidates ref", /stale_ref/.test((await call("click", { ref: topButton.ref })).error));
  const result = { buildID: identity.buildID, xulSHA256: hash, checks, limitations: ["Synthetic fixtures, not third-party website acceptance", "Closed shadow roots unsupported"] };
  if (options.includes("--output")) await fs.writeFile(option("--output"), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  console.error(output);
  throw error;
} finally {
  if (wire) {
    await wire.command("Marionette:SetContext", { value: "chrome" }).catch(() => {});
    await wire.execute('Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit);').catch(() => {});
    await wire.close();
  }
  for (let i = 0; i < 50 && proc.exitCode === null && proc.signalCode === null; i++) await delay(100);
  if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGTERM");
  for (let i = 0; i < 30 && proc.exitCode === null && proc.signalCode === null; i++) await delay(100);
  if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  await fs.rm(directory, { recursive: true, force: true });
}
