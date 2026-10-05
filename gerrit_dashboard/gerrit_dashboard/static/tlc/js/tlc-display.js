/*
 * TLC display settings: theme, contrast and text size, remembered per viewer.
 *
 * In <head>, before any stylesheet paints (so a dark page never flashes
 * light), inline TLC_DISPLAY_INIT -- or load this file there; it applies the
 * saved settings at once.  Then put
 *
 *     <div class="tlc-pop" data-tlc-display></div>
 *
 * where the control should go (normally the end of the top bar) and it is
 * filled in when the document has loaded.
 *
 * Settings live in localStorage under "tlc-display" and are mirrored to a
 * cookie of the same name (path=/), so a server can render the right
 * attributes too.  "auto" means: follow the system.
 */
(function () {
  "use strict";
  var KEY = "tlc-display";
  var root = document.documentElement;

  function read() {
    var s = null;
    try { s = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
    if (!s) {
      var m = document.cookie.match(/(?:^|;\s*)tlc-display=([^;]+)/);
      if (m) { try { s = JSON.parse(decodeURIComponent(m[1])); } catch (e) {} }
    }
    return s || {};
  }

  function apply(s) {
    if (s.theme === "light" || s.theme === "dark") root.dataset.theme = s.theme;
    else delete root.dataset.theme;
    if (s.contrast === "more" || s.contrast === "standard") root.dataset.contrast = s.contrast;
    else delete root.dataset.contrast;
    if (s.size === "s" || s.size === "l" || s.size === "xl") root.dataset.textSize = s.size;
    else delete root.dataset.textSize;
  }

  function save(s) {
    var v = JSON.stringify(s);
    try { localStorage.setItem(KEY, v); } catch (e) {}
    try {
      document.cookie = KEY + "=" + encodeURIComponent(v) + "; path=/; max-age=31536000; SameSite=Lax";
    } catch (e) {}
  }

  var settings = read();
  apply(settings);

  var GROUPS = [
    { key: "theme", label: "Theme", opts: [["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]] },
    { key: "contrast", label: "Contrast", opts: [["auto", "Auto"], ["standard", "Standard"], ["more", "High"]] },
    { key: "size", label: "Text size", opts: [["s", "Small"], ["m", "Medium"], ["l", "Large"], ["xl", "Largest"]] }
  ];
  var ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/>' +
    '<path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/></svg>';

  function current(key) {
    var v = settings[key];
    if (key === "size") return v || "m";
    return v || "auto";
  }

  function build(host) {
    var id = "tlc-display-" + Math.random().toString(36).slice(2, 8);
    host.innerHTML =
      '<button type="button" class="tlc-btn tlc-btn--secondary tlc-btn--sm" aria-label="Display settings" aria-expanded="false" aria-controls="' + id + '">' +
      ICON + '<span class="tlc-display__label">Display</span></button>' +
      '<div class="tlc-pop__panel" id="' + id + '" role="group" aria-label="Display settings" hidden></div>';
    var btn = host.querySelector("button");
    var panel = host.querySelector(".tlc-pop__panel");
    GROUPS.forEach(function (g) {
      var fs = document.createElement("fieldset");
      fs.className = "tlc-display__group";
      fs.innerHTML = "<legend>" + g.label + "</legend>";
      var seg = document.createElement("div");
      seg.className = "tlc-seg";
      g.opts.forEach(function (o) {
        var b = document.createElement("button");
        b.type = "button";
        b.textContent = o[1];
        b.dataset.key = g.key;
        b.dataset.value = o[0];
        seg.appendChild(b);
      });
      fs.appendChild(seg);
      panel.appendChild(fs);
    });
    function sync() {
      panel.querySelectorAll("button[data-key]").forEach(function (b) {
        b.setAttribute("aria-pressed", String(current(b.dataset.key) === b.dataset.value));
      });
    }
    panel.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-key]");
      if (!b) return;
      var v = b.dataset.value;
      if (v === "auto" || (b.dataset.key === "size" && v === "m")) delete settings[b.dataset.key];
      else settings[b.dataset.key] = v;
      apply(settings);
      save(settings);
      sync();
      document.dispatchEvent(new CustomEvent("tlc-display-change", { detail: settings }));
    });
    function close() { panel.hidden = true; btn.setAttribute("aria-expanded", "false"); }
    btn.addEventListener("click", function () {
      var open = panel.hidden;
      panel.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      if (open) { var p = panel.querySelector('[aria-pressed="true"]'); if (p) p.focus(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !panel.hidden) { close(); btn.focus(); }
    });
    document.addEventListener("click", function (e) {
      if (!host.contains(e.target)) close();
    });
    sync();
  }

  function init() {
    document.querySelectorAll("[data-tlc-display]").forEach(build);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.TLCDisplay = { get: function () { return Object.assign({}, settings); }, apply: apply };
})();
