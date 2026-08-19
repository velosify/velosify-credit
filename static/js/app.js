/* VelosifyCredit small progressive enhancements.
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

  /* ---- Hero score panel ------------------------------------------------
     Counts the score up, fills the bar, and resolves each disputed account
     in turn, then loops. Runs only while the panel is on screen, and not at
     all when the visitor has asked for reduced motion; in that case the
     markup's resolved state is already correct and is simply left alone. */
  (function heroScore() {
    var demo = document.getElementById("score-demo");
    if (!demo) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    var numEl = demo.querySelector("[data-score-now]");
    var deltaEl = demo.querySelector("[data-score-delta]");
    var fillEl = demo.querySelector(".score-fill");
    var items = Array.prototype.slice.call(demo.querySelectorAll(".score-item"));
    if (!numEl || !fillEl || !items.length) return;

    var from = parseInt(numEl.getAttribute("data-score-from"), 10);
    var to = parseInt(numEl.getAttribute("data-score-to"), 10);
    var good = parseInt(numEl.getAttribute("data-score-good"), 10);
    if (isNaN(good)) good = null;
    if (isNaN(from) || isNaN(to)) return;

    var FLOOR = 300, CEIL = 850;      // the FICO range the bar represents
    var COUNT_MS = 2200;              // score count-up
    var LEAD_MS = 400;                // pause before anything moves
    var STEP_MS = 620;                // gap between rows resolving
    var HOLD_MS = 3200;               // dwell on the finished state before looping

    var timers = [];
    var runId = 0;                    // invalidates in-flight rAF loops on reset

    demo.classList.add("is-animated");

    function pct(v) { return ((v - FLOOR) / (CEIL - FLOOR)) * 100; }
    function easeOut(t) { return 1 - Math.pow(1 - t, 3); }
    function clearTimers() { timers.forEach(clearTimeout); timers = []; }

    function reset() {
      clearTimers();
      runId++;
      numEl.textContent = from;
      numEl.classList.toggle("is-good", good !== null && from >= good);
      deltaEl.textContent = "";
      items.forEach(function (it) { it.classList.remove("is-done"); });
      fillEl.style.transition = "none";
      fillEl.style.width = pct(from) + "%";
      void fillEl.offsetWidth;        // flush, so the width change below animates
      fillEl.style.transition = "";
    }

    function run() {
      reset();
      var id = runId;

      timers.push(setTimeout(function () {
        fillEl.style.width = pct(to) + "%";
        var t0 = performance.now();
        (function tick(now) {
          if (id !== runId) return;   // a reset happened; abandon this loop
          var p = Math.min(1, (now - t0) / COUNT_MS);
          var v = Math.round(from + (to - from) * easeOut(p));
          numEl.textContent = v;
          numEl.classList.toggle("is-good", good !== null && v >= good);
          deltaEl.textContent = v > from ? "\u25B2 " + (v - from) + " pts" : "";
          if (p < 1) requestAnimationFrame(tick);
        })(performance.now());
      }, LEAD_MS));

      items.forEach(function (it, i) {
        timers.push(setTimeout(function () {
          it.classList.add("is-done");
        }, LEAD_MS + 500 + i * STEP_MS));
      });

      timers.push(setTimeout(run, LEAD_MS + 500 + items.length * STEP_MS + HOLD_MS));
    }

    if (!("IntersectionObserver" in window)) { run(); return; }
    new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) run();
        else { clearTimers(); runId++; }
      });
    }, { threshold: 0.35 }).observe(demo);
  })();

  /* ---- Scroll reveal ----------------------------------------------------
     Staggers [data-reveal] elements in as their group scrolls into view.
     The hiding class is applied by script, so nothing is ever stuck
     invisible if JS fails or is switched off. */
  (function reveal() {
    var nodes = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
    if (!nodes.length) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!("IntersectionObserver" in window)) return;

    nodes.forEach(function (n) { n.classList.add("reveal-init"); });

    var io = new IntersectionObserver(function (entries) {
      // Stagger by position within the batch that just became visible, so a
      // row of cards cascades rather than all landing at once.
      var shown = 0;
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var el = e.target;
        setTimeout(function () { el.classList.add("is-visible"); }, shown * 110);
        shown++;
        io.unobserve(el);
      });
    }, { threshold: 0.2, rootMargin: "0px 0px -40px 0px" });

    nodes.forEach(function (n) { io.observe(n); });
  })();

  /* ---- Portal panel replay ----------------------------------------------
     Walks the case tracker forward and drops the timeline entries in oldest
     first, then loops. Same contract as the score panel: the markup ships
     finished, script rewinds it, reduced motion leaves it finished. */
  (function portalDemo() {
    var demo = document.getElementById("portal-demo");
    if (!demo) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    var steps = Array.prototype.slice.call(demo.querySelectorAll(".tracker-step"));
    // Newest sits at the top of the timeline, so replay from the bottom up.
    var events = Array.prototype.slice.call(demo.querySelectorAll(".timeline li")).reverse();
    if (!steps.length || !events.length) return;

    var STOP_AT = steps.filter(function (s) { return s.classList.contains("current"); }).length
      ? steps.indexOf(demo.querySelector(".tracker-step.current"))
      : steps.length - 1;

    var LEAD_MS = 500, STEP_MS = 700, EVENT_MS = 620, HOLD_MS = 3400;
    var timers = [];
    function clearTimers() { timers.forEach(clearTimeout); timers = []; }

    demo.classList.add("is-animated");

    function paint(upto) {
      steps.forEach(function (s, i) {
        s.classList.toggle("done", i < upto);
        s.classList.toggle("current", i === upto);
      });
    }

    function reset() {
      clearTimers();
      paint(-1);
      events.forEach(function (e) { e.classList.remove("is-in"); });
    }

    function run() {
      reset();
      for (var i = 0; i <= STOP_AT; i++) {
        (function (idx) {
          timers.push(setTimeout(function () { paint(idx); }, LEAD_MS + idx * STEP_MS));
        })(i);
      }
      var afterTracker = LEAD_MS + (STOP_AT + 1) * STEP_MS;
      events.forEach(function (el, i) {
        timers.push(setTimeout(function () { el.classList.add("is-in"); },
                               afterTracker + i * EVENT_MS));
      });
      timers.push(setTimeout(run, afterTracker + events.length * EVENT_MS + HOLD_MS));
    }

    if (!("IntersectionObserver" in window)) { run(); return; }
    new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) run(); else clearTimers();
      });
    }, { threshold: 0.3 }).observe(demo);
  })();

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
})();
