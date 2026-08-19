/* VelosifyCredit — small progressive enhancements.
   Everything here is optional: every form and link works with JS disabled. */
(function () {
  "use strict";

  /* ---- Mobile nav ---------------------------------------------------- */
  var toggle = document.getElementById("nav-toggle");
  var links = document.getElementById("nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", function () {
      var open = links.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  /* ---- Upload dropzones ----------------------------------------------
     Each .dropzone wraps a hidden <input type="file">. Clicking the zone
     opens the picker; dragging files onto it assigns them directly. The
     label updates so the client can see what they picked before sending. */
  document.querySelectorAll(".dropzone").forEach(function (zone) {
    var input = zone.querySelector('input[type="file"]');
    var label = zone.querySelector("[data-zone-label]");
    if (!input) return;

    var defaultText = label ? label.innerHTML : "";

    function describe(files) {
      if (!label) return;
      if (!files || !files.length) { label.innerHTML = defaultText; return; }
      if (files.length === 1) {
        label.innerHTML = '<span class="picked">' + escapeHtml(files[0].name) + "</span>";
      } else {
        label.innerHTML = '<span class="picked">' + files.length + " files selected</span>";
      }
    }

    zone.addEventListener("click", function (e) {
      if (e.target !== input) input.click();
    });
    zone.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    input.addEventListener("change", function () { describe(input.files); });

    ["dragenter", "dragover"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        zone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        zone.classList.remove("dragover");
      });
    });
    zone.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        describe(input.files);
      }
    });
  });

  /* ---- Submit guards --------------------------------------------------
     Stops a double-click from creating two orders or two uploads. */
  document.querySelectorAll("form[data-guard]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector('button[type="submit"], .btn-primary');
      if (!btn || btn.disabled) return;
      var busy = btn.getAttribute("data-busy") || "Working…";
      btn.dataset.original = btn.innerHTML;
      btn.innerHTML = busy;
      btn.disabled = true;
      // Re-enable if the browser restores the page from bfcache, otherwise
      // a back-navigation would leave a permanently dead button.
      window.addEventListener("pageshow", function () {
        btn.disabled = false;
        if (btn.dataset.original) btn.innerHTML = btn.dataset.original;
      });
    });
  });

  /* ---- Confirm destructive actions ------------------------------------ */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  /* ---- Signature mirror on the order form -----------------------------
     Shows the client the exact name they need to type to sign. */
  var first = document.getElementById("first_name");
  var last = document.getElementById("last_name");
  var mirror = document.getElementById("signature-hint");
  if (first && last && mirror) {
    function updateMirror() {
      var full = (first.value.trim() + " " + last.value.trim()).trim();
      mirror.textContent = full ? "Type “" + full + "” to sign." : "";
    }
    first.addEventListener("input", updateMirror);
    last.addEventListener("input", updateMirror);
    updateMirror();
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
})();
