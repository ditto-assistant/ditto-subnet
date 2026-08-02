// Clipboard access with a legacy fallback. Button state + the #copy-status
// live-region announcement are the caller's job (CopyButton / copy-status
// store); this module only performs the copy.

/** execCommand("copy") fallback via an offscreen readonly textarea. */
function legacyCopy(value: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    try {
      if (!document.execCommand("copy")) throw new Error("Copy command failed");
      resolve();
    } catch (error) {
      reject(error);
    } finally {
      input.remove();
    }
  });
}

/** Copy text, preferring the async clipboard API and falling back to the
 * legacy path when it is absent or rejects (e.g. insecure context). */
export function copyText(text: string): Promise<void> {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).catch(() => legacyCopy(text));
  }
  return legacyCopy(text);
}
