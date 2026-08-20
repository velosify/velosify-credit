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

  /* ---- Scroll reveal ----------------------------------------------------
     Staggers [data-reveal] elements in as their group scrolls into view.
     The hiding class is applied by script, so nothing is ever stuck
     invisible if JS fails or is switched off.

     data-reveal carries the direction ("", "left", "right", "scale"); the
     CSS reads it. data-reveal-step overrides the stagger for a group that
     should land faster or slower than the default cascade. */
  (function reveal() {
    var nodes = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
    if (!nodes.length) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!("IntersectionObserver" in window)) return;

    nodes.forEach(function (n) { n.classList.add("reveal-init"); });

    var pending = nodes.length;

    function show(el) {
      if (el.classList.contains("is-visible")) return;
      el.classList.add("is-visible");
      io.unobserve(el);
      if (!--pending) {
        window.removeEventListener("scroll", sweep);
        window.removeEventListener("resize", sweep);
      }
    }

    var io = new IntersectionObserver(function (entries) {
      // Stagger by position within the batch that just became visible, so a
      // row of cards cascades rather than all landing at once.
      var shown = 0;
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var el = e.target;
        var step = parseInt(el.getAttribute("data-reveal-step"), 10);
        if (!isFinite(step)) step = 90;
        var at = shown * step;
        shown++;
        io.unobserve(el);
        if (!at) { show(el); return; }
        setTimeout(function () { show(el); }, at);
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -50px 0px" });

    nodes.forEach(function (n) { io.observe(n); });

    /* A jump straight down the page, by dragging the scrollbar or following
       an anchor, never intersects the elements it skips over. Without this
       they would sit at opacity 0 for good, which is far worse than not
       animating at all. Only elements now entirely above the viewport are
       rescued here: anything still on screen is the observer's to stagger,
       so the cascade is left alone. */
    var queued = false;
    function sweep() {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        for (var i = 0; i < nodes.length; i++) {
          var el = nodes[i];
          if (el.classList.contains("is-visible")) continue;
          if (el.getBoundingClientRect().bottom <= 0) show(el);
        }
      });
    }
    window.addEventListener("scroll", sweep, { passive: true });
    window.addEventListener("resize", sweep, { passive: true });
  })();

  /* ---- Hero video (phones only) -----------------------------------------
     The element ships with no src at all, so a desktop visitor never spends
     the bytes. It is decorative: if anything here fails, the hero keeps the
     photograph it already has behind it. */
  (function heroVideo() {
    var video = document.querySelector("[data-hero-video]");
    if (!video) return;
    if (!window.matchMedia) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!window.matchMedia("(max-width: 720px)").matches) return;
    // Respect a metered connection or an explicit data-saver preference.
    var conn = navigator.connection;
    if (conn && (conn.saveData || /(^|-)2g$/.test(conn.effectiveType || ""))) return;

    // VP9 where it is available (smaller file), H.264 everywhere else, which
    // is what iOS actually decodes.
    var src = "";
    if (video.canPlayType('video/webm; codecs="vp9"')) src = video.dataset.srcWebm;
    if (!src && video.canPlayType("video/mp4")) src = video.dataset.srcMp4;
    if (!src) return;

    video.muted = true;              // as a property too, for iOS autoplay
    video.src = src;

    function play() {
      var attempt = video.play();
      if (attempt && attempt.catch) attempt.catch(function () { /* poster stands in */ });
    }
    play();
    // Some browsers refuse before any interaction. One retry on the first
    // touch or scroll costs nothing and rescues those.
    ["touchstart", "scroll"].forEach(function (evt) {
      window.addEventListener(evt, function once() {
        window.removeEventListener(evt, once);
        if (video.paused) play();
      }, { passive: true });
    });
  })();

  /* ---- How-it-works flow -------------------------------------------------
     Fills the track and lights each marker as the fill reaches it, so the
     four steps read as one connected process. Runs once, on first view.
     The markup ships finished; this only rewinds it when it is safe to. */
  (function howFlow() {
    var flow = document.getElementById("how-flow");
    if (!flow) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!("IntersectionObserver" in window)) return;

    var steps = Array.prototype.slice.call(flow.querySelectorAll(".flow-step"));
    if (!steps.length) return;

    var FILL_MS = 2000;           // must match the CSS transition on .flow-fill
    var LEAD_MS = 220;

    // Rewind only now that we know the script is running.
    flow.classList.add("is-armed");
    steps.forEach(function (s) { s.classList.remove("is-on"); });

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        io.unobserve(flow);
        setTimeout(function () { flow.classList.add("is-running"); }, LEAD_MS);
        // Light each marker as the fill passes it. The first sits at the
        // track's origin, so it lights immediately.
        steps.forEach(function (step, i) {
          var at = LEAD_MS + (i / (steps.length - 1)) * FILL_MS;
          setTimeout(function () { step.classList.add("is-on"); }, at);
        });
      });
    }, { threshold: 0.25 });
    io.observe(flow);
  })();

  /* ---- Three-bureau gauges ------------------------------------------------
     Each dial spans 300 to 850 across a semicircle. One rAF loop drives all
     three, staggered, through: climb, hold, fade out, fade back in at the
     start, repeat. It only runs while the section is on screen.

     The markup ships finished, so this rewinds nothing until it has proved
     it can animate: reduce-motion or no rAF leaves three complete dials. */
  (function bureauGauges() {
    var gauges = Array.prototype.slice.call(document.querySelectorAll("[data-gauge]"));
    if (!gauges.length) return;
    if (!window.requestAnimationFrame) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    var MIN = 300, MAX = 850;          // the range the arc represents
    var CX = 100, CY = 102, R = 80;    // dial geometry, matches the SVG path
    var LEN = Math.PI * R;             // arc length, for stroke-dashoffset

    var RUN_MS = 2600;                 // the climb
    var HOLD_MS = 1900;                // the pause at the top
    var OUT_MS = 340;                  // fade the finished dial away
    var GAP_MS = 120;                  // beat at zero opacity, mid-reset
    var IN_MS = 340;                   // fade back in at the starting score
    var CYCLE = RUN_MS + HOLD_MS + OUT_MS + GAP_MS + IN_MS;

    var dials = gauges.map(function (el) {
      return {
        el: el,
        from: parseInt(el.getAttribute("data-from"), 10),
        to: parseInt(el.getAttribute("data-to"), 10),
        delay: parseInt(el.getAttribute("data-delay"), 10) || 0,
        arc: el.querySelector(".gauge-arc"),
        halo: el.querySelector(".gauge-halo"),
        bubble: el.querySelector(".gauge-bubble"),
        score: el.querySelector("[data-gauge-score]"),
        band: ""
      };
    }).filter(function (d) {
      return d.arc && d.bubble && d.score && isFinite(d.from) && isFinite(d.to);
    });
    if (!dials.length) return;

    function bandFor(score) {
      if (score < 620) return "is-poor";
      if (score < 740) return "is-fair";
      return "is-good";
    }

    // Ease-out so the climb starts briskly and settles, the way a needle does.
    function ease(p) { return 1 - Math.pow(1 - p, 3); }

    function paint(d, raw, fade) {
      // Band off the number the visitor can actually read, not the raw value.
      // Rounding first stops a frame at 619.6 from showing "620" in red.
      var score = Math.round(raw);
      var t = (score - MIN) / (MAX - MIN);
      if (t < 0) t = 0; else if (t > 1) t = 1;

      var a = (180 - 180 * t) * Math.PI / 180;
      d.arc.setAttribute("stroke-dashoffset", (LEN * (1 - t)).toFixed(2));
      var x = (CX + R * Math.cos(a)).toFixed(2);
      var y = (CY - R * Math.sin(a)).toFixed(2);
      d.bubble.setAttribute("cx", x);
      d.bubble.setAttribute("cy", y);
      if (d.halo) { d.halo.setAttribute("cx", x); d.halo.setAttribute("cy", y); }

      d.score.textContent = score;

      var band = bandFor(score);
      if (band !== d.band) {
        d.el.classList.remove("is-poor", "is-fair", "is-good");
        d.el.classList.add(band);
        d.band = band;
      }
      d.el.style.setProperty("--gauge-fade", fade);
    }

    // Where a dial sits at a given point in its own cycle.
    function frame(d, ms) {
      var p = ((ms % CYCLE) + CYCLE) % CYCLE;
      if (p < RUN_MS) return paint(d, d.from + (d.to - d.from) * ease(p / RUN_MS), 1);
      p -= RUN_MS;
      if (p < HOLD_MS) return paint(d, d.to, 1);
      p -= HOLD_MS;
      if (p < OUT_MS) return paint(d, d.to, 1 - p / OUT_MS);
      p -= OUT_MS;
      if (p < GAP_MS) return paint(d, d.from, 0);
      p -= GAP_MS;
      return paint(d, d.from, p / IN_MS);
    }

    var start = 0, running = false, raf = 0;

    function tick(now) {
      if (!running) return;
      if (!start) start = now;
      var ms = now - start;
      for (var i = 0; i < dials.length; i++) frame(dials[i], ms - dials[i].delay);
      raf = window.requestAnimationFrame(tick);
    }

    function play() {
      if (running) return;
      running = true;
      start = 0;
      raf = window.requestAnimationFrame(tick);
    }
    function pause() {
      running = false;
      if (raf) window.cancelAnimationFrame(raf);
      raf = 0;
    }

    // Rewind now that we know we can drive it.
    dials.forEach(function (d) { paint(d, d.from, 1); });

    // Play only when the section is both on screen and in a foreground tab.
    var onScreen = true;

    function sync() {
      if (onScreen && !document.hidden) play();
      else pause();
    }

    var section = dials[0].el.parentNode;
    if ("IntersectionObserver" in window) {
      onScreen = false;
      new IntersectionObserver(function (entries) {
        entries.forEach(function (e) { onScreen = e.isIntersecting; });
        sync();
      }, { threshold: 0.15 }).observe(section);
    }

    // A backgrounded tab throttles rAF, which would strand the dials mid-climb
    // and then jump when it resumes. Stop, and restart the cycle on return.
    document.addEventListener("visibilitychange", sync);
    sync();
  })();

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
})();
