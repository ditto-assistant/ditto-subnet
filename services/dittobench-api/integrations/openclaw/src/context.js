const CONTEXTS = Symbol.for("dittobench.openclaw.run-contexts");

function contexts() {
  if (!globalThis[CONTEXTS]) globalThis[CONTEXTS] = new Map();
  return globalThis[CONTEXTS];
}

export function setRunContext(sessionKey, context) {
  contexts().set(sessionKey, context);
}

export function getRunContext(sessionKey) {
  return contexts().get(sessionKey);
}

export function clearRunContext(sessionKey) {
  contexts().delete(sessionKey);
}
