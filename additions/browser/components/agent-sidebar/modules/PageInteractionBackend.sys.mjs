const ACTOR = "AgentInteraction";
let registered = false;
function ensureActor() {
  if (registered) return;
  try {
    ChromeUtils.registerWindowActor(ACTOR, { child: { esModuleURI: "resource:///modules/agentsidebar/AgentInteractionChild.sys.mjs" }, allFrames: true });
  } catch (error) {
    if (!/already.*regist/i.test(String(error.message))) throw error;
  }
  registered = true;
}

export class PageInteractionBackend {
  constructor(query, browser) {
    this.query = query;
    this.browser = browser;
    this.refs = new Map();
    this.sequence = 0;
    this.epoch = Services.uuid.generateUUID().toString().replace(/[^a-z0-9]/gi, "").slice(0, 12);
  }

  tree(ctx) {
    const top = this.browser(ctx).browsingContext;
    if (!top?.currentWindowGlobal) throw new Error("page_not_ready");
    const frames = [];
    const queue = [top];
    while (queue.length && frames.length < 128) {
      const frame = queue.shift();
      frames.push(frame);
      queue.push(...frame.children);
    }
    return { top, frames, truncated: queue.length > 0 };
  }

  frameRecord(frame, top) {
    const global = frame.currentWindowGlobal;
    return { frameId: String(frame.id), parentId: frame === top ? null : String(frame.parent.id),
      documentId: global ? String(global.innerWindowId) : null,
      url: String(global?.documentURI?.spec || "").slice(0, 2000), available: !!global && !global.documentPrincipal?.isSystemPrincipal };
  }

  frames(_args, ctx) {
    const { top, frames, truncated } = this.tree(ctx);
    return { ok: true, topFrameId: String(top.id), frames: frames.map(frame => this.frameRecord(frame, top)), truncated };
  }

  async scan(action, args = {}, ctx) {
    ensureActor();
    const { top, frames, truncated: treeTruncated } = this.tree(ctx);
    const selected = args.frameId === "all" ? frames.slice(0, 32) : [args.frameId && args.frameId !== "top" ? frames.find(frame => String(frame.id) === String(args.frameId)) : top];
    if (!selected[0]) throw new Error("frame_not_found: refresh page_frames");
    const limit = Math.max(1, Math.min(120, Math.floor(Number(args.limit) || 40)));
    const result = { ok: true, topFrameId: String(top.id), coordinateSpace: "frame-viewport-css", elements: [], frames: [], truncated: args.frameId === "all" && (treeTruncated || frames.length > 32) };
    const topDocument = String(top.currentWindowGlobal.innerWindowId);
    const deadline = Date.now() + 30000;
    for (const frame of selected) {
      if (ctx?.signal?.aborted) throw new Error("page_query_cancelled");
      if (Date.now() >= deadline) { result.truncated = true; result.partial = true; break; }
      if (result.elements.length >= limit) { result.truncated = true; break; }
      const metadata = this.frameRecord(frame, top);
      result.frames.push(metadata);
      if (!metadata.available) { metadata.error = "frame_unavailable"; continue; }
      const global = frame.currentWindowGlobal;
      try {
        const data = await this.query(global.getActor(ACTOR), "interact", { ...args, action, limit: limit - result.elements.length }, ctx, Math.max(1, Math.min(5000, deadline - Date.now())));
        if (frame.currentWindowGlobal !== global || String(top.currentWindowGlobal?.innerWindowId) !== topDocument) throw new Error("stale_document: refresh snapshot");
        if (!data?.ok) { metadata.error = data?.error || "frame_query_failed"; continue; }
        metadata.title = String(data.title || "").slice(0, 200);
        metadata.truncated = data.truncated;
        result.truncated ||= data.truncated;
        for (const { id, ...element } of data.elements) {
          const ref = this.epoch + ":" + (++this.sequence).toString(36);
          this.refs.set(ref, { topId: String(top.id), topDocument, frameId: metadata.frameId, documentId: metadata.documentId, id });
          if (this.refs.size > 1000) this.refs.delete(this.refs.keys().next().value);
          result.elements.push({ ref, frameId: metadata.frameId, ...element });
        }
      } catch (error) {
        if (ctx?.signal?.aborted) throw error;
        metadata.error = String(error.message || error);
      }
    }
    result.count = result.elements.length;
    result.partial ||= result.frames.some(frame => frame.error);
    return result;
  }

  async act(action, args, ctx) {
    ensureActor();
    const { top, frames } = this.tree(ctx);
    let frame;
    const request = { ...args, action };
    if (args.ref) {
      if (["selector", "role", "name", "label", "scope", "exact", "x", "y"].some(key => args[key] !== undefined) || (action !== "type" && args.text !== undefined)) throw new Error("ambiguous_locator: use ref alone");
      const ref = this.refs.get(args.ref);
      if (!ref || ref.topId !== String(top.id) || ref.topDocument !== String(top.currentWindowGlobal.innerWindowId)) throw new Error("stale_ref: reference belongs to another tab or document");
      frame = frames.find(frame => String(frame.id) === ref.frameId);
      if (!frame || String(frame.currentWindowGlobal?.innerWindowId) !== ref.documentId) throw new Error("stale_ref: frame navigated or detached");
      if (args.frameId && String(args.frameId) !== ref.frameId) throw new Error("ref_frame_mismatch");
      request.id = ref.id;
    } else {
      frame = args.frameId && args.frameId !== "top" ? frames.find(frame => String(frame.id) === String(args.frameId)) : top;
      if (!frame) throw new Error("frame_not_found: choose one frameId, not all");
    }
    if (!frame.currentWindowGlobal) throw new Error("frame_unavailable");
    if (frame.currentWindowGlobal.documentPrincipal?.isSystemPrincipal) throw new Error("unsupported_document");
    const result = await this.query(frame.currentWindowGlobal.getActor(ACTOR), "interact", request, ctx);
    if (!result?.ok) throw new Error(result?.error || "page_action_failed");
    return { ...result, frameId: String(frame.id) };
  }
}
