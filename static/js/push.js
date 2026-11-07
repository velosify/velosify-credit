/* Phone notifications and sharing for signed-in pages.

   Registers the service worker (which also makes the portal installable to a
   home screen), then wires:
     [data-push-card]  shown when this browser can take notifications and
                       they are not on yet
     [data-push-ios]   shown on an iPhone that hasn't added the portal to its
                       home screen, which Safari requires before push works
     [data-push-on]    turns notifications on for this device
     [data-push-off]   turns them off for this device
     [data-push-state] says which it is
     [data-share]      native share sheet, falling back to copying the link
   Nothing here is needed for the portal to work. */
(function () {
  "use strict";

  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.getAttribute("content") : "";

  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function show(sel, on) { $all(sel).forEach(function (el) { el.hidden = !on; }); }
  function text(sel, t) { $all(sel).forEach(function (el) { el.textContent = t; }); }

  /* ---- Share ----------------------------------------------------------- */
  $all("[data-share]").forEach(function (btn) {
    if (!navigator.share) { btn.hidden = true; return; }
    btn.addEventListener("click", function () {
      navigator.share({
        title: "Velosify Credit",
        text: btn.getAttribute("data-share-text") || "",
        url: btn.getAttribute("data-share")
      }).catch(function () { /* closed the sheet */ });
    });
  });

  /* ---- Push ------------------------------------------------------------ */
  var ua = navigator.userAgent || "";
  var isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  var supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

  if (!("serviceWorker" in navigator)) {
    text("[data-push-state]", "Not available in this browser");
    return;
  }

  if (isIOS && !standalone) {
    // Safari only allows notifications for a portal opened from the home screen.
    show("[data-push-ios]", true);
    text("[data-push-state]", "Add to your home screen first");
  }

  function b64ToBytes(b64) {
    var pad = "=".repeat((4 - (b64.length % 4)) % 4);
    var raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Accept": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function (reg) {
    if (!supported || (isIOS && !standalone)) {
      if (!supported && !(isIOS && !standalone)) text("[data-push-state]", "Not available in this browser");
      return;
    }

    function render(sub) {
      var denied = Notification.permission === "denied";
      show("[data-push-card]", !sub && !denied);
      show("[data-push-on]", !sub && !denied);
      show("[data-push-off]", !!sub);
      text("[data-push-state]", sub ? "On for this device"
        : denied ? "Blocked in your browser settings" : "Off");
    }

    reg.pushManager.getSubscription().then(render);

    $all("[data-push-on]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        btn.disabled = true;
        var label = btn.textContent;
        btn.textContent = "Turning on…";
        Notification.requestPermission().then(function (perm) {
          if (perm !== "granted") throw new Error("denied");
          return fetch("/push/key", { credentials: "same-origin" }).then(function (r) { return r.json(); });
        }).then(function (k) {
          if (!k.key) throw new Error("no key");
          return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(k.key) });
        }).then(function (sub) {
          return post("/push/subscribe", sub.toJSON()).then(function () { return sub; });
        }).then(function (sub) {
          render(sub);
        }).catch(function () {
          render(null);
          text("[data-push-copy]", "Notifications weren't turned on. You can allow them in your browser settings and try again.");
        }).then(function () {
          btn.disabled = false;
          btn.textContent = label;
        });
      });
    });

    $all("[data-push-off]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        reg.pushManager.getSubscription().then(function (sub) {
          if (!sub) return render(null);
          var ep = sub.endpoint;
          return sub.unsubscribe().then(function () { return post("/push/unsubscribe", { endpoint: ep }); })
            .then(function () { render(null); });
        });
      });
    });
  }).catch(function () {
    text("[data-push-state]", "Not available in this browser");
  });
})();
