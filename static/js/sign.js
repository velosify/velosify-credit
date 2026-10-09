/* Signature pad for /portal/letters/<id>. A separate file because the site's
   Content-Security-Policy does not allow inline script. */
(function () {
  "use strict";
  var form = document.getElementById("sign-form");
  if (!form) return;

  var pad = document.getElementById("sign-pad");
  var ctx = pad.getContext("2d");
  var go = document.getElementById("sign-go");
  var out = document.getElementById("sign-data");
  var consent = document.getElementById("sign-consent");
  var approve = document.getElementById("sign-approve");
  var nameEl = document.getElementById("signer_name");
  var hint = document.getElementById("sign-hint");
  var drawing = false, last = null, travelled = 0;

  function size() {
    var r = window.devicePixelRatio || 1;
    pad.width = Math.round(pad.clientWidth * r);
    pad.height = Math.round(pad.clientHeight * r);
    ctx.setTransform(r, 0, 0, r, 0, 0);
    ctx.lineWidth = 2.4;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "#0d1b2f";
    ctx.fillStyle = "#0d1b2f";
    travelled = 0;
    out.value = "";
    if (hint) hint.hidden = false;
    refresh();
  }

  function point(e) {
    var b = pad.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  }

  pad.addEventListener("pointerdown", function (e) {
    drawing = true;
    last = point(e);
    pad.setPointerCapture(e.pointerId);
    ctx.beginPath();
    ctx.arc(last.x, last.y, 1.1, 0, Math.PI * 2);
    ctx.fill();
    if (hint) hint.hidden = true;
    e.preventDefault();
  });

  pad.addEventListener("pointermove", function (e) {
    if (!drawing) return;
    var p = point(e);
    ctx.beginPath();
    ctx.moveTo(last.x, last.y);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
    travelled += Math.hypot(p.x - last.x, p.y - last.y);
    last = p;
    e.preventDefault();
  });

  function end() {
    if (!drawing) return;
    drawing = false;
    out.value = travelled > 220 ? pad.toDataURL("image/png") : "";
    refresh();
  }
  pad.addEventListener("pointerup", end);
  pad.addEventListener("pointercancel", end);

  document.getElementById("sign-clear").addEventListener("click", size);

  function refresh() {
    var named = /\S+\s+\S+/.test(nameEl.value);
    go.disabled = !(consent.checked && approve.checked && named && out.value);
  }
  [consent, approve].forEach(function (el) { el.addEventListener("change", refresh); });
  nameEl.addEventListener("input", refresh);

  // Resizing clears the canvas, so only do it when nothing has been drawn.
  window.addEventListener("resize", function () { if (!drawing && !out.value) size(); });
  size();
})();
