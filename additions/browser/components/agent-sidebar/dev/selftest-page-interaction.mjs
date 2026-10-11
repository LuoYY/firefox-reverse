import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { JSDOM } from "jsdom";
import { PageInteractionCore } from "../modules/PageInteractionCore.sys.mjs";
import { PageInteractionBackend } from "../modules/PageInteractionBackend.sys.mjs";

const dom = new JSDOM(`<!doctype html><title>Fixture</title>
  <button id="go" aria-labelledby="caption">X</button><span id="caption">Continue</span>
  <label for="input">Account</label><input id="input"><input type="password" value="private-secret" aria-label="Password">
  <button hidden>Invisible</button><button disabled>Disabled</button>
  <button>Duplicate</button><button>Duplicate</button><div id="shadow"></div>`, { pretendToBeVisual: true, url: "https://fixture.invalid/" });
const { window: win } = dom;
win.HTMLElement.prototype.getClientRects = function() { return this.hidden ? [] : [{ x: 1, y: 2, width: 40, height: 20 }]; };
win.HTMLElement.prototype.scrollIntoView = function() {};
const core = new PageInteractionCore(win);
const scan = core.scan();
assert.equal(scan.elements.length, 6);
assert.ok(!JSON.stringify(scan).includes("private-secret"));
assert.equal(core.scan({ role: "button", name: "Continue" }).elements.length, 1);
assert.equal(core.scan({ label: "Account" }).elements.length, 1);
assert.equal(core.scan({ name: "Invisible" }).elements.length, 0);
assert.throws(() => core.act("click", { role: "button", name: "Duplicate" }), /ambiguous_target/);
assert.throws(() => core.act("click", { name: "Disabled" }), /target_disabled/);
assert.throws(() => core.scan({ selector: "[" }), /not a valid selector/);
assert.throws(() => core.scan({ name: "x".repeat(2001) }), /invalid_locator/);
assert.equal(core.scan({ limit: 1 }).truncated, true);
assert.equal(core.scan({ maxNodes: 1 }).truncated, true);
assert.throws(() => core.act("click", { name: "Continue", maxNodes: 1 }), /incomplete_search/);
let clicked = 0;
win.document.querySelector("#go").addEventListener("click", () => clicked++);
const ref = core.scan({ name: "Continue" }).elements[0].id;
core.act("click", { id: ref });
assert.equal(clicked, 1);
core.act("type", { label: "Account", text: "value-not-a-locator" });
assert.equal(win.document.querySelector("#input").value, "value-not-a-locator");
const keys = [];
for (const type of ["keydown", "keyup"]) win.document.querySelector("#input").addEventListener(type, e => keys.push([e.type, e.key, e.keyCode, e.which]));
core.act("type", { label: "Account", text: "submitted", submit: true });
assert.deepEqual(keys, [["keydown", "Enter", 13, 13], ["keyup", "Enter", 13, 13]]);
win.document.querySelector("#go").remove();
assert.throws(() => core.act("click", { id: ref }), /stale_ref/);
const shadow = win.document.querySelector("#shadow").attachShadow({ mode: "open" });
shadow.innerHTML = "<button>Shadow action</button>";
assert.equal(core.scan({ name: "Shadow action" }).elements.length, 1);
assert.throws(() => core.act("click", { x: -1, y: 1 }), /invalid_coordinates/);
assert.throws(() => core.act("click", { x: 1, y: 1, id: ref }), /ambiguous_locator/);
const events = [];
win.document.elementFromPoint = () => win.document.querySelector("#input");
win.synthesizeMouseEvent = (...args) => events.push(args);
core.act("click", { x: 10, y: 20 });
assert.deepEqual(events.map(e => e[0]), ["mousemove", "mousedown", "mouseup"]);
win.document.elementFromPoint = () => win.document.createElement("iframe");
assert.throws(() => core.act("click", { x: 1, y: 1 }), /frame_target/);

globalThis.Services = { uuid: { generateUUID: randomUUID } };
globalThis.ChromeUtils = { registerWindowActor() {} };
const makeFrame = (id, parent = null) => ({ id, parent, children: [], currentWindowGlobal: {
  innerWindowId: id + 100, documentURI: { spec: `https://frame${id}.invalid` }, documentPrincipal: { isSystemPrincipal: false }, getActor() { return { frame: id }; },
} });
const top = makeFrame(1);
const child = makeFrame(2, top);
const nested = makeFrame(3, child);
top.children.push(child);
child.children.push(nested);
const calls = [];
const backend = new PageInteractionBackend(async (actor, _name, data) => {
  calls.push({ actor, data });
  return data.action === "snapshot" || data.action === "find" ? { ok: true, title: "frame", elements: [{ id: "1", name: "Button", role: "button" }], truncated: false } : { ok: true };
}, () => ({ browsingContext: top }));
assert.deepEqual(backend.frames({}).frames.map(f => f.parentId), [null, "1", "2"]);
const snapshot = await backend.scan("snapshot", { frameId: "all" });
assert.equal(snapshot.count, 3);
assert.equal(snapshot.partial, false);
assert.equal((await backend.scan("snapshot", { frameId: "all", limit: 1 })).truncated, true);
await backend.act("click", { ref: snapshot.elements[2].ref });
assert.equal(calls.at(-1).actor.frame, 3);
await backend.act("type", { ref: snapshot.elements[2].ref, text: "secret" });
assert.equal(calls.at(-1).data.text, "secret");
await assert.rejects(() => backend.act("click", { ref: snapshot.elements[2].ref, name: "other" }), /ambiguous_locator/);
await assert.rejects(() => backend.act("click", { ref: snapshot.elements[2].ref, frameId: "2" }), /ref_frame_mismatch/);
await assert.rejects(() => backend.act("click", { frameId: "all", name: "Button" }), /frame_not_found/);
nested.currentWindowGlobal.innerWindowId++;
await assert.rejects(() => backend.act("click", { ref: snapshot.elements[2].ref }), /stale_ref/);
top.currentWindowGlobal.innerWindowId++;
await assert.rejects(() => backend.act("click", { ref: snapshot.elements[0].ref }), /stale_ref/);
const unknownBackend = new PageInteractionBackend(backend.query, backend.browser);
await assert.rejects(() => unknownBackend.act("click", { ref: snapshot.elements[0].ref }), /stale_ref/);
assert.notEqual(backend.epoch, unknownBackend.epoch);
child.currentWindowGlobal.documentPrincipal.isSystemPrincipal = true;
assert.equal((await backend.scan("snapshot", { frameId: "all" })).partial, true);
await assert.rejects(() => backend.act("click", { frameId: "2", selector: "button" }), /unsupported_document/);
const controller = new AbortController();
controller.abort();
await assert.rejects(() => backend.scan("snapshot", {}, { signal: controller.signal }), /cancelled/);
dom.window.close();
console.log("page interaction: semantic, bounds, shadow, coordinates, frame routing and stale references passed");
