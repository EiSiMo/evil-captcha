// evilCAPTCHA: turns each snippet (<div class="evil-captcha">) into the framed widget.
// See https://evil-captcha.org/docs. The snippet works without this script too.
//
// The frame fills in the snippet's field evil-captcha-response with the pass token, and
// the snippet's div dispatches "evil-captcha:ready" once framed and "evil-captcha:pass"
// once solved (detail.token). While the popup is open, the frame covers the whole page.
(() => {
  const origin = new URL(document.currentScript.src).origin;
  const WIDTH = 304;
  const HEIGHT = 78;

  function mount(box) {
    if (box.dataset.evilCaptcha) return;
    box.dataset.evilCaptcha = "mounted";
    let field = box.querySelector('input[name="evil-captcha-response"]');
    if (!field) {
      field = document.createElement("input");
      field.name = "evil-captcha-response";
    }
    field.type = "hidden";
    field.value = "";
    const frame = document.createElement("iframe");
    frame.src = `${origin}/widget`;
    frame.title = "evilCAPTCHA";
    frame.name = "evil-captcha";  // tells the widget it is framed by this script
    const small = { position: "", inset: "", width: `${WIDTH}px`, height: `${HEIGHT}px`, zIndex: "" };
    const cover = { position: "fixed", inset: "0", width: "100%", height: "100%", zIndex: "2147483647" };
    Object.assign(frame.style, { display: "block", margin: "0", padding: "0", border: "0", background: "transparent", colorScheme: "normal" }, small);
    // Covering the page from the top layer escapes transformed or stacked ancestors
    // (a login modal, say); the frame stays where it is, so it does not reload.
    const topLayer = (on) => {
      if (!frame.showPopover) return;
      if (on) {
        frame.popover = "manual";
        frame.showPopover();
      } else if (frame.popover) {
        frame.hidePopover();
        frame.removeAttribute("popover");
      }
    };
    // The box keeps its size, so the page does not shift while the frame covers it.
    Object.assign(box.style, { width: `${WIDTH}px`, height: `${HEIGHT}px` });
    box.replaceChildren(frame, field);

    let overlay = false;
    const place = () => {
      const { left, top } = box.getBoundingClientRect();
      frame.contentWindow.postMessage({ source: "evil-captcha", type: "place", overlay, left, top }, origin);
    };
    addEventListener("message", (event) => {
      const message = event.data;
      if (event.origin !== origin || event.source !== frame.contentWindow || message?.source !== "evil-captcha") return;
      if (message.type === "layout") {
        overlay = Boolean(message.overlay);
        frame.style.opacity = "0";  // hidden until the widget has moved its box into place
        Object.assign(frame.style, overlay ? cover : small);
        topLayer(overlay);
        place();
      } else if (message.type === "placed") {
        frame.style.opacity = "";
      } else if (message.type === "pass") {
        field.value = String(message.token);
        box.dispatchEvent(new CustomEvent("evil-captcha:pass", { bubbles: true, detail: { token: field.value } }));
      }
    });
    const follow = () => overlay && place();
    addEventListener("scroll", follow, { passive: true });
    addEventListener("resize", follow);
    box.dispatchEvent(new CustomEvent("evil-captcha:ready", { bubbles: true }));
  }

  const start = () => document.querySelectorAll(".evil-captcha").forEach(mount);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
