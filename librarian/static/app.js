// v6.0: the portal's own script (CSP: script-src 'self'). Progressive only: every page works
// without it. No third-party code, no inline handlers.
(function () {
  "use strict";
  // Filter forms submit themselves when a select changes (no "Show" press needed).
  document.querySelectorAll("form[data-autosubmit] select").forEach(function (s) {
    s.addEventListener("change", function () { s.form.submit(); });
  });
  // Instant filtering of what is already on the page while typing (the server search still
  // runs on Enter, for pages beyond the first).
  document.querySelectorAll("input[data-filter]").forEach(function (inp) {
    var items = document.querySelectorAll(inp.getAttribute("data-filter"));
    inp.addEventListener("input", function () {
      var q = inp.value.trim().toLowerCase();
      items.forEach(function (el) {
        el.style.display = !q || (el.getAttribute("data-text") || "").indexOf(q) !== -1 ? "" : "none";
      });
    });
  });
  // Copy buttons: <button data-copy="text">
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function (e) {
      e.preventDefault();
      if (navigator.clipboard) {
        navigator.clipboard.writeText(b.getAttribute("data-copy")).then(function () {
          var t = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = t; }, 1500);
        });
      }
    });
  });
  // Pages that wait on something (a copy being looked for, a download) refresh themselves
  // quietly: <body data-refresh="30">. Stops while the reader is typing in a field.
  var r = parseInt(document.body.getAttribute("data-refresh") || "0", 10);
  if (r > 0) {
    setInterval(function () {
      var a = document.activeElement;
      if (!a || (a.tagName !== "INPUT" && a.tagName !== "TEXTAREA" && a.tagName !== "SELECT")) { location.reload(); }
    }, r * 1000);
  }
})();
