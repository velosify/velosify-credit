/* VelosifyCredit document uploader.

   Upgrades every form[data-upload] on the documents page:

     * "Take a photo" opens an in-page camera with an outline to line the
       document up in, takes as many pages as needed, and hands them to a
       review sheet. Where the browser can't open a camera stream (no HTTPS,
       permission refused, an old phone), it falls back to the phone's own
       camera through the file input's capture attribute.
     * Picking files opens the same review sheet, so every upload is
       "check what you're sending, then send".
     * Large photos are shrunk to a sensible size on the phone before they go,
       which turns a 6 MB, 30-second upload on cellular into a 600 KB one.
     * Uploads show real progress, then a confirmation, then the page reloads
       on the next step.

   Without this file the forms still work: the inputs open the camera or
   picker natively and a Send button submits them. */
(function () {
  "use strict";

  var forms = Array.prototype.slice.call(document.querySelectorAll("form[data-upload]"));
  if (!forms.length || !window.FormData || !window.XMLHttpRequest) return;

  var MAX_EDGE = 2200;        // px on the longest side; plenty to read fine print
  var JPEG_Q = 0.85;
  var SHRINK_OVER = 1.2 * 1024 * 1024;
  var canStream = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
                     window.isSecureContext !== false);

  /* ------------------------------------------------------------------ utils */
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function isImage(f) { return /^image\//.test(f.type) || /\.(jpe?g|png|webp|heic)$/i.test(f.name); }
  function lockScroll(on) { document.documentElement.classList.toggle("modal-open", on); }
  function stamp() {
    var d = new Date();
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
  }
  var ICON_X = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';
  var ICON_CAM = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13.5" r="3.6"/></svg>';
  var ICON_PLUS = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>';

  /* A canvas-drawn JPEG of an image file, or the file unchanged if it is
     already small or the browser can't decode it (HEIC outside Safari). */
  function shrink(file) {
    if (!isImage(file) || file.size < SHRINK_OVER || !window.createImageBitmap) {
      return Promise.resolve(file);
    }
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bmp) {
      var scale = Math.min(1, MAX_EDGE / Math.max(bmp.width, bmp.height));
      var c = document.createElement("canvas");
      c.width = Math.round(bmp.width * scale);
      c.height = Math.round(bmp.height * scale);
      c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
      return new Promise(function (resolve) {
        c.toBlob(function (blob) {
          if (!blob || blob.size >= file.size) return resolve(file);
          var name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
          resolve(new File([blob], name, { type: "image/jpeg" }));
        }, "image/jpeg", JPEG_Q);
      });
    }).catch(function () { return file; });
  }

  /* --------------------------------------------------------- review sheet */
  var sheet = el("div", "sheet");
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  sheet.setAttribute("aria-labelledby", "sheet-title");
  sheet.hidden = true;
  sheet.innerHTML =
    '<div class="sheet-backdrop" data-close></div>' +
    '<div class="sheet-panel">' +
      '<div class="sheet-grab" aria-hidden="true"></div>' +
      '<div class="sheet-head">' +
        '<div><div class="sheet-kicker">Ready to send</div><h2 id="sheet-title" class="sheet-title"></h2></div>' +
        '<button type="button" class="icon-btn" data-close aria-label="Cancel">' + ICON_X + "</button>" +
      "</div>" +
      '<p class="sheet-hint">Check each one is clear and every corner is in the shot.</p>' +
      '<div class="sheet-grid"></div>' +
      '<div class="sheet-error" role="alert" hidden></div>' +
      '<div class="sheet-progress" hidden><div class="sheet-bar"><span></span></div><div class="sheet-progress-label">Uploading…</div></div>' +
      '<div class="sheet-done" hidden><span class="sheet-done-ico">✓</span><strong></strong><span>Taking you to the next step…</span></div>' +
      '<div class="sheet-actions">' +
        '<button type="button" class="btn btn-primary btn-xl btn-block" data-send></button>' +
        '<button type="button" class="btn btn-soft btn-xl btn-block btn-ico" data-more></button>' +
      "</div>" +
    "</div>";
  document.body.appendChild(sheet);

  var sTitle = sheet.querySelector(".sheet-title");
  var sGrid = sheet.querySelector(".sheet-grid");
  var sErr = sheet.querySelector(".sheet-error");
  var sProg = sheet.querySelector(".sheet-progress");
  var sBar = sheet.querySelector(".sheet-bar span");
  var sProgLabel = sheet.querySelector(".sheet-progress-label");
  var sDone = sheet.querySelector(".sheet-done");
  var sSend = sheet.querySelector("[data-send]");
  var sMore = sheet.querySelector("[data-more]");
  var sActions = sheet.querySelector(".sheet-actions");
  var sHint = sheet.querySelector(".sheet-hint");

  var active = null;     // { form, files: [{file, url}] , camera: bool }
  var sending = false;

  function hasCamera(form) { return !!form.querySelector("[data-camera-btn]"); }

  function renderSheet() {
    var n = active.files.length;
    var photos = active.files.every(function (x) { return isImage(x.file); });
    sTitle.textContent = active.form.getAttribute("data-label");
    sGrid.innerHTML = "";
    active.files.forEach(function (x, i) {
      var tile = el("div", "sheet-tile");
      if (x.url) {
        var img = el("img");
        img.src = x.url;
        img.alt = "Page " + (i + 1);
        tile.appendChild(img);
      } else {
        var ext = (x.file.name.split(".").pop() || "FILE").slice(0, 4).toUpperCase();
        tile.appendChild(el("div", "sheet-file", '<span class="sheet-file-ext">' + esc(ext) + '</span><span class="sheet-file-name">' + esc(x.file.name) + "</span>"));
      }
      tile.appendChild(el("span", "sheet-tile-num", String(i + 1)));
      var rm = el("button", "sheet-tile-rm", ICON_X);
      rm.type = "button";
      rm.setAttribute("aria-label", "Remove " + (i + 1));
      rm.addEventListener("click", function () {
        if (sending) return;
        if (x.url) URL.revokeObjectURL(x.url);
        active.files.splice(i, 1);
        if (!active.files.length) return closeSheet();
        renderSheet();
      });
      tile.appendChild(rm);
      sGrid.appendChild(tile);
    });
    var noun = photos ? (n === 1 ? "photo" : "photos") : (n === 1 ? "file" : "files");
    sSend.textContent = "Upload " + n + " " + noun;
    sMore.innerHTML = hasCamera(active.form)
      ? ICON_CAM + " Add another page"
      : ICON_PLUS + " Add another file";
    sHint.hidden = !photos;
  }

  function openSheet(form, newFiles) {
    if (!active || active.form !== form) {
      discard();
      active = { form: form, files: [] };
    }
    newFiles.forEach(function (f) {
      active.files.push({ file: f, url: isImage(f) && !/heic/i.test(f.type + f.name) ? URL.createObjectURL(f) : null });
    });
    if (!active.files.length) return;
    sErr.hidden = true;
    sProg.hidden = true;
    sDone.hidden = true;
    sActions.hidden = false;
    renderSheet();
    sheet.hidden = false;
    lockScroll(true);
    requestAnimationFrame(function () { sheet.classList.add("is-open"); sSend.focus(); });
  }

  function discard() {
    if (active) active.files.forEach(function (x) { if (x.url) URL.revokeObjectURL(x.url); });
    active = null;
  }

  function closeSheet() {
    if (sending) return;
    sheet.classList.remove("is-open");
    lockScroll(false);
    setTimeout(function () { sheet.hidden = true; }, 220);
    discard();
  }

  sheet.addEventListener("click", function (e) {
    if (e.target.closest("[data-close]")) closeSheet();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (!cam.hidden) return closeCamera(false);
    if (!sheet.hidden) closeSheet();
  });

  sMore.addEventListener("click", function () {
    if (sending || !active) return;
    var form = active.form;
    if (hasCamera(form) && canStream) {
      sheet.classList.remove("is-open");
      sheet.hidden = true;
      openCamera(form);
    } else {
      var input = form.querySelector(hasCamera(form) ? "[data-camera-btn] input" : "input[type=file]");
      input.click();
    }
  });

  function showError(msg) {
    sending = false;
    sProg.hidden = true;
    sActions.hidden = false;
    sErr.textContent = msg;
    sErr.hidden = false;
  }

  sSend.addEventListener("click", function () {
    if (sending || !active || !active.files.length) return;
    sending = true;
    sErr.hidden = true;
    sActions.hidden = true;
    sProg.hidden = false;
    sBar.style.width = "4%";
    sProgLabel.textContent = "Preparing…";

    var form = active.form;
    Promise.all(active.files.map(function (x) { return shrink(x.file); })).then(function (files) {
      var fd = new FormData();
      fd.append("csrf_token", form.querySelector("[name=csrf_token]").value);
      fd.append("doc_type", form.querySelector("[name=doc_type]").value);
      files.forEach(function (f) { fd.append("files", f, f.name); });

      var xhr = new XMLHttpRequest();
      xhr.open("POST", form.action);
      xhr.setRequestHeader("X-Requested-With", "fetch");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.addEventListener("progress", function (e) {
        if (!e.lengthComputable) return;
        var pct = Math.max(4, Math.round(e.loaded / e.total * 100));
        sBar.style.width = pct + "%";
        sProgLabel.textContent = pct < 100 ? "Uploading… " + pct + "%" : "Almost done…";
      });
      xhr.addEventListener("load", function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (err) { /* not JSON */ }
        if (xhr.status === 413) {
          return showError("That's too large to send in one go. Remove a page and upload it separately.");
        }
        if (xhr.status >= 200 && xhr.status < 300 && data && data.redirect) {
          sProg.hidden = true;
          sDone.querySelector("strong").textContent = form.getAttribute("data-label") + " sent";
          sDone.hidden = false;
          var go = data.redirect;
          setTimeout(function () {
            sending = false;
            window.location.href = go;
          }, 900);
          return;
        }
        showError((data && data.error) || "That didn't go through. Check your connection and try again.");
      });
      xhr.addEventListener("error", function () {
        showError("No connection. Check your signal or Wi-Fi and try again. Your photos are still here.");
      });
      xhr.send(fd);
    });
  });

  /* ---------------------------------------------------------------- camera */
  var cam = el("div", "cam");
  cam.setAttribute("role", "dialog");
  cam.setAttribute("aria-modal", "true");
  cam.setAttribute("aria-label", "Camera");
  cam.hidden = true;
  cam.innerHTML =
    '<video class="cam-video" playsinline muted autoplay></video>' +
    '<div class="cam-guide"><div class="cam-frame"><i></i><i></i><i></i><i></i></div></div>' +
    '<div class="cam-top">' +
      '<button type="button" class="cam-icon" data-cam-close aria-label="Close camera">' + ICON_X + "</button>" +
      '<div class="cam-heading"><div class="cam-title"></div><div class="cam-step"></div></div>' +
      '<span class="cam-icon-spacer"></span>' +
    "</div>" +
    '<p class="cam-tip"></p>' +
    '<div class="cam-bottom">' +
      '<button type="button" class="cam-thumb" data-cam-done aria-label="Review photos"><img alt=""><span></span></button>' +
      '<button type="button" class="cam-shutter" aria-label="Take photo"><span></span></button>' +
      '<button type="button" class="cam-done" data-cam-done>Done</button>' +
    "</div>" +
    '<div class="cam-flash"></div>' +
    '<div class="cam-error" hidden>' +
      '<div class="cam-error-card">' +
        '<div class="cam-error-ico">' + ICON_CAM + "</div>" +
        '<h2>Let\'s use your camera app</h2>' +
        '<p class="cam-error-msg"></p>' +
        '<label class="btn btn-primary btn-xl btn-block">Open camera<input class="file-hidden" type="file" accept="image/*" capture="environment" multiple></label>' +
        '<button type="button" class="btn btn-ghost-light btn-xl btn-block" data-cam-close>Cancel</button>' +
      "</div>" +
    "</div>";
  document.body.appendChild(cam);

  var video = cam.querySelector("video");
  var cTitle = cam.querySelector(".cam-title");
  var cStep = cam.querySelector(".cam-step");
  var cTip = cam.querySelector(".cam-tip");
  var cFrame = cam.querySelector(".cam-frame");
  var cThumb = cam.querySelector(".cam-thumb");
  var cThumbImg = cThumb.querySelector("img");
  var cThumbN = cThumb.querySelector("span");
  var cDone = cam.querySelector(".cam-done");
  var cShutter = cam.querySelector(".cam-shutter");
  var cFlash = cam.querySelector(".cam-flash");
  var cErr = cam.querySelector(".cam-error");
  var cErrMsg = cam.querySelector(".cam-error-msg");
  var cErrInput = cErr.querySelector("input");

  var stream = null;
  var camForm = null;
  var shots = [];          // Files taken in this camera session

  function stopStream() {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
    video.srcObject = null;
  }

  function updateCamUi() {
    var n = shots.length + (active && active.form === camForm ? active.files.length : 0);
    cStep.textContent = n ? n + (n === 1 ? " page" : " pages") + " taken" : "Page 1";
    cThumb.hidden = !shots.length;
    cDone.hidden = !shots.length;
    if (shots.length) {
      if (cThumbImg.dataset.url) URL.revokeObjectURL(cThumbImg.dataset.url);
      var u = URL.createObjectURL(shots[shots.length - 1]);
      cThumbImg.src = u;
      cThumbImg.dataset.url = u;
      cThumbN.textContent = shots.length;
      cTip.textContent = "Got it. Take the next page or back side, or tap Done.";
    }
  }

  function camFail(msg) {
    stopStream();
    cErrMsg.textContent = msg;
    cErr.hidden = false;
  }

  function openCamera(form) {
    camForm = form;
    shots = [];
    cTitle.textContent = form.getAttribute("data-label");
    cTip.textContent = form.getAttribute("data-tip") || "Fit the document inside the frame.";
    cFrame.className = "cam-frame frame-" + (form.getAttribute("data-frame") || "page");
    cErr.hidden = true;
    cam.hidden = false;
    lockScroll(true);
    updateCamUi();

    if (!canStream) {
      return camFail("This browser can't show the camera here, so we'll open your phone's camera instead.");
    }
    navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: 2560 },
        height: { ideal: 1440 }
      }
    }).then(function (s) {
      if (cam.hidden) { s.getTracks().forEach(function (t) { t.stop(); }); return; }
      stream = s;
      video.srcObject = s;
      var p = video.play();
      if (p && p.catch) p.catch(function () { /* autoplay is muted; ignore */ });
    }).catch(function (err) {
      var denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
      camFail(denied
        ? "Camera access is turned off for this site. You can still take the photo with your phone's camera app."
        : "We couldn't start the camera on this device. You can still take the photo with your phone's camera app.");
    });
  }

  function closeCamera(keep) {
    stopStream();
    cam.hidden = true;
    var form = camForm, taken = shots;
    shots = [];
    camForm = null;
    if (keep && taken.length) {
      openSheet(form, taken);
    } else if (active && active.form === form && active.files.length) {
      // Came here from "Add another page" and took nothing: go back to the sheet.
      openSheet(form, []);
    } else {
      lockScroll(false);
    }
  }

  cShutter.addEventListener("click", function () {
    if (!stream || !video.videoWidth) return;
    var w = video.videoWidth, h = video.videoHeight;
    var scale = Math.min(1, MAX_EDGE / Math.max(w, h));
    var c = document.createElement("canvas");
    c.width = Math.round(w * scale);
    c.height = Math.round(h * scale);
    c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
    cFlash.classList.remove("go");
    void cFlash.offsetWidth;   // restart the animation
    cFlash.classList.add("go");
    if (navigator.vibrate) navigator.vibrate(15);
    c.toBlob(function (blob) {
      if (!blob) return;
      var key = camForm.getAttribute("data-doc");
      var n = shots.length + 1;
      shots.push(new File([blob], key + "-" + stamp() + "-p" + n + ".jpg", { type: "image/jpeg" }));
      updateCamUi();
    }, "image/jpeg", JPEG_Q);
  });

  cam.addEventListener("click", function (e) {
    if (e.target.closest("[data-cam-close]")) closeCamera(true);
    else if (e.target.closest("[data-cam-done]")) closeCamera(true);
  });

  cErrInput.addEventListener("change", function () {
    var files = Array.prototype.slice.call(cErrInput.files || []);
    var form = camForm;
    cErrInput.value = "";
    stopStream();
    cam.hidden = true;
    camForm = null;
    if (files.length) openSheet(form, shots.concat(files));
    else lockScroll(false);
    shots = [];
  });

  /* ----------------------------------------------------------------- wiring */
  forms.forEach(function (form) {
    form.addEventListener("submit", function (e) {
      // Only the no-script Send button submits; with script, the sheet sends.
      e.preventDefault();
    });

    form.querySelectorAll("input[type=file]").forEach(function (input) {
      input.addEventListener("change", function () {
        var files = Array.prototype.slice.call(input.files || []);
        input.value = "";
        if (files.length) openSheet(form, files);
      });
    });

    var camBtn = form.querySelector("[data-camera-btn]");
    if (camBtn && canStream) {
      camBtn.addEventListener("click", function (e) {
        // The label would open the native camera; use ours instead.
        e.preventDefault();
        openCamera(form);
      });
    }
  });

  /* Arrived from a "Take a photo" or the tab bar's camera button:
     /portal/documents?scan=photo_id. Open that step, bring it into view,
     and start the camera. */
  (function () {
    var params = new URLSearchParams(window.location.search);
    var key = params.get("scan");
    if (!key) return;
    if (window.history && history.replaceState) {
      params.delete("scan");
      var q = params.toString();
      history.replaceState(null, "", window.location.pathname + (q ? "?" + q : "") + "#doc-" + key);
    }
    var wrap = document.getElementById("doc-" + key);
    if (!wrap) return;
    var det = wrap.querySelector("details");
    if (det) det.open = true;
    wrap.scrollIntoView({ block: "start" });
    var form = wrap.querySelector("form[data-upload]");
    if (form && hasCamera(form) && canStream) openCamera(form);
  })();

  /* Following a #doc-… link from the home screen: open that step. */
  function openHash() {
    var id = window.location.hash.slice(1);
    if (!/^doc-/.test(id)) return;
    var wrap = document.getElementById(id);
    var det = wrap && wrap.querySelector("details");
    if (det) det.open = true;
  }
  window.addEventListener("hashchange", openHash);
  openHash();
})();
