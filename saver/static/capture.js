// Run in every page the server captures (--browser-script), before it loads.
// Lists (SINGLEFILE_REMOVED_ELEMENTS, the cookie extension's stylesheet) only
// know the common consent tools; plenty of sites build their own banner. This
// removes any fixed or sticky box that talks about cookies or consent and
// offers a choice (accept, reject, settings...): the shape every banner has,
// and one a sticky header or a "back to top" button never has.
(() => {
  const TOPIC = /\b(cookies?|consent|gdpr|tracking technologies|einwillig\w*|zustimm\w*|datenschutzeinstellungen)\b/i;
  const CHOICE = /\b(accept|agree|allow|reject|decline|deny|refuse|got it|ok(ay)?|understood|dismiss|close|manage|settings|preferences|customi[sz]e|options|akzeptieren|zustimmen|ablehnen|einverstanden|verstanden|einstellungen)\b/i;
  const CONTROLS = "button, a, [role=button], input[type=button], input[type=submit]";

  function isBanner(el) {
    const text = (el.innerText || "").trim();
    // A banner is a short notice; a fixed element holding the article is not one.
    if (!text || text.length > 2000 || !TOPIC.test(text)) return false;
    return [...el.querySelectorAll(CONTROLS)].some((c) =>
      CHOICE.test(c.innerText || c.value || c.getAttribute("aria-label") || ""));
  }

  // Cheap enough to run on every burst of DOM changes while a heavy page
  // builds: only text that mentions the topic is looked at, and only its
  // ancestors are styled. (Styling every element each time slowed pages
  // enough that SingleFile stopped loading their lazy content early.)
  function sweep() {
    if (!document.body) return;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => (TOPIC.test(node.data) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
    });
    const texts = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) texts.push(node);
    for (const node of texts) {
      if (!node.isConnected) continue;  // its banner already went
      // The innermost fixed/sticky box around the text that looks like a
      // banner goes: when a wrapper holds the banner, the wrapper stays.
      for (let el = node.parentElement; el && el !== document.body; el = el.parentElement) {
        const position = getComputedStyle(el).position;
        if ((position === "fixed" || position === "sticky") && isBanner(el)) {
          el.remove();
          break;
        }
      }
    }
  }

  let queued = false;
  const later = () => {
    if (queued) return;
    queued = true;
    setTimeout(() => { queued = false; sweep(); }, 500);
  };
  // Banners often arrive late (a consent script, an entrance animation).
  new MutationObserver(later).observe(document, { childList: true, subtree: true });
  addEventListener("DOMContentLoaded", sweep);
  addEventListener("load", sweep);
})();
