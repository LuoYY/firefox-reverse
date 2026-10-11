import { computeAccessibleName, getRole, isInaccessible } from "./PageAccessibility.sys.mjs";

const TARGETS = "a[href],button,input,textarea,select,summary,[role],[tabindex],[contenteditable=true]";
const clean = value => String(value || "").replace(/\s+/g, " ").trim();
const bounded = (value, fallback, max) => Math.max(1, Math.min(max, Math.floor(Number(value) || fallback)));

export class PageInteractionCore {
  constructor(win) {
    this.win = win;
    this.elements = new Map();
    this.ids = new WeakMap();
    this.sequence = 0;
    this.styleOptions = { getComputedStyle: win.getComputedStyle.bind(win) };
  }

  visible(element) {
    return element.isConnected && element.ownerDocument === this.win.document &&
      !!element.getClientRects().length && !isInaccessible(element, this.styleOptions);
  }

  id(element) {
    let id = this.ids.get(element);
    if (!id || !this.elements.has(id)) {
      id = String(++this.sequence);
      this.ids.set(element, id);
      this.elements.set(id, new WeakRef(element));
      if (this.elements.size > 1000) this.elements.delete(this.elements.keys().next().value);
    }
    return id;
  }

  describe(element) {
    const rect = element.getBoundingClientRect();
    return { id: this.id(element), tag: element.localName, role: getRole(element),
      name: clean(computeAccessibleName(element)).slice(0, 160),
      disabled: element.matches(":disabled") || element.getAttribute("aria-disabled") === "true",
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) } };
  }

  scan(args = {}) {
    for (const key of ["selector", "scope", "role", "name", "label", "text"]) {
      if (args[key] !== undefined && (typeof args[key] !== "string" || args[key].length > 2000)) throw new Error("invalid_locator: strings must be at most 2000 characters");
    }
    const limit = bounded(args.limit, 40, 120);
    const maxNodes = bounded(args.maxNodes, 4000, 10000);
    const document = this.win.document;
    const scope = args.scope ? document.querySelector(args.scope) : document;
    if (!scope) throw new Error("scope_not_found: choose an existing CSS scope in this frame");
    const selector = args.selector || (args.text ? "*" : TARGETS);
    const matches = [];
    const stack = [scope];
    let visited = 0;
    let truncated = false;
    const compare = (value, query) => args.exact === false ? clean(value).includes(clean(query)) : clean(value) === clean(query);
    while (stack.length) {
      const root = stack.pop();
      const walker = document.createTreeWalker(root, 1);
      for (let element = root.nodeType === 1 ? root : walker.nextNode(); element; element = walker.nextNode()) {
        if (++visited > maxNodes) { truncated = true; break; }
        if (element.shadowRoot) stack.push(element.shadowRoot);
        if (!element.matches(selector) || !this.visible(element)) continue;
        if (args.role && getRole(element) !== args.role) continue;
        if (args.name !== undefined && !compare(computeAccessibleName(element), args.name)) continue;
        if (args.label !== undefined) {
          const label = [...(element.labels || [])].map(item => item.textContent).join(" ") || element.getAttribute("aria-label") || "";
          if (!compare(label, args.label)) continue;
        }
        if (args.text !== undefined && !compare(element.textContent, args.text)) continue;
        if (matches.length === limit) { truncated = true; break; }
        matches.push(this.describe(element));
      }
      if (truncated) break;
    }
    return { ok: true, url: document.URL, title: document.title, elements: matches, visited: Math.min(visited, maxNodes), truncated };
  }

  resolve(args) {
    if (args.id) {
      const element = this.elements.get(String(args.id))?.deref();
      if (!element || !element.isConnected || element.ownerDocument !== this.win.document) throw new Error("stale_ref: element detached; refresh snapshot");
      return element;
    }
    if (!["selector", "role", "name", "label", "text"].some(key => args[key] !== undefined)) throw new Error("locator_required");
    const found = this.scan({ ...args, limit: 2 });
    if (found.elements.length > 1) throw new Error("ambiguous_target: refine locator or use a snapshot ref");
    if (found.truncated) throw new Error("incomplete_search: narrow scope before acting");
    if (!found.elements.length) throw new Error("target_not_found");
    return this.elements.get(found.elements[0].id).deref();
  }

  act(action, args) {
    const win = this.win;
    if (action === "click" && (args.x !== undefined || args.y !== undefined)) {
      if (!Number.isFinite(args.x) || !Number.isFinite(args.y) || args.x < 0 || args.y < 0 || args.x >= win.innerWidth || args.y >= win.innerHeight) throw new Error("invalid_coordinates: use viewport CSS pixels in the selected frame");
      if (["id", "selector", "role", "name", "label", "text", "scope", "exact"].some(key => args[key] !== undefined)) throw new Error("ambiguous_locator: coordinates cannot be combined with an element locator");
      const target = win.document.elementFromPoint(args.x, args.y);
      if (!target) throw new Error("target_not_found");
      if (target.matches("iframe,frame")) throw new Error("frame_target: choose that frameId before coordinate clicking");
      if (typeof win.synthesizeMouseEvent !== "function") throw new Error("coordinate_input_unavailable");
      const options = { toWindow: true };
      win.synthesizeMouseEvent("mousemove", args.x, args.y, { button: 0, clickCount: 0 }, options);
      win.synthesizeMouseEvent("mousedown", args.x, args.y, { button: 0, clickCount: 1 }, options);
      win.synthesizeMouseEvent("mouseup", args.x, args.y, { button: 0, clickCount: 1 }, options);
      return { ok: true, clicked: true, coordinateSpace: "frame-viewport-css", x: args.x, y: args.y };
    }
    if (action === "scroll" && !["id", "selector", "role", "name", "label", "text"].some(key => args[key] !== undefined)) {
      if (args.toBottom) win.scrollTo(0, win.document.documentElement.scrollHeight);
      else if (args.toTop) win.scrollTo(0, 0);
      else win.scrollBy(Number(args.dx) || 0, args.dy === undefined ? Math.round(win.innerHeight * 0.8) : Number(args.dy) || 0);
      return { ok: true, y: win.scrollY };
    }
    const locator = { ...args };
    if (action === "type") delete locator.text;
    const element = this.resolve(locator);
    if (!this.visible(element)) throw new Error("target_hidden: refresh snapshot");
    if (element.matches(":disabled") || element.getAttribute("aria-disabled") === "true") throw new Error("target_disabled");
    element.scrollIntoView({ block: "center", inline: "center" });
    if (action === "click") {
      element.click();
      return { ok: true, clicked: true, tag: element.localName };
    }
    if (action === "scroll") return { ok: true, scrolled: true };
    if (action !== "type") throw new Error("unsupported_action");
    const input = element.localName === "input";
    if (!(input || element.localName === "textarea") || element.readOnly || (input && ["file", "button", "submit", "reset", "checkbox", "radio", "hidden"].includes(element.type))) throw new Error("target_not_editable");
    if (typeof args.text !== "string") throw new Error("text_required");
    element.focus();
    const prototype = input ? win.HTMLInputElement.prototype : win.HTMLTextAreaElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, args.text);
    element.dispatchEvent(new win.Event("input", { bubbles: true }));
    element.dispatchEvent(new win.Event("change", { bubbles: true }));
    if (args.submit) {
      element.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Enter", keyCode: 13, which: 13, bubbles: true }));
      element.dispatchEvent(new win.KeyboardEvent("keyup", { key: "Enter", keyCode: 13, which: 13, bubbles: true }));
    }
    return { ok: true, filled: true, length: args.text.length };
  }
}
