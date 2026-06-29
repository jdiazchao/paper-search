// PaperArena — HUD swipe deck over an always-on PDF background.
(function () {
  "use strict";

  var deck = document.getElementById("deck");
  var controls = document.getElementById("controls");
  var reasonShortcuts = document.getElementById("reason-shortcuts");
  var hint = document.getElementById("hint");
  var placeholder = document.getElementById("placeholder");
  var tabsEl = document.getElementById("tabs");
  var doneButton = document.getElementById("done-send");

  var bgPages = document.getElementById("reader-pages");
  var bgStatus = document.getElementById("reader-status");

  var round = 0, items = [], library = { liked: [], disliked: [] }, viewItems = [], state = [], idx = 0;
  var currentOriginal = "";
  var lastBgKey = null;
  var submitting = false;
  var eventsSource = null, waitingForRound = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function fetchJSON(url, opts) { return fetch(url, opts).then(function (r) { return r.json(); }); }

  function paperKey(p) {
    return String((p && (p.id || p.doi || p.url || p.pdf_url || p.title)) || "");
  }

  function arxivIdFrom(value) {
    if (!value) return null;
    var s = String(value);
    var m = s.match(/arxiv\.org\/(?:abs|pdf)\/([^?#]+?)(?:\.pdf)?(?:[?#].*)?$/i);
    if (m) return m[1];
    m = s.match(/(?:^|[^\w.])(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?\/\d{7}(?:v\d+)?)(?:$|[^\w.])/i);
    return m ? m[1] : null;
  }

  function canonicalArxivId(p) {
    return arxivIdFrom(p.id) || arxivIdFrom(p.url) || arxivIdFrom(p.pdf_url);
  }

  function pdfUrlFor(p) {
    var arxivId = canonicalArxivId(p);
    if (arxivId) return "https://arxiv.org/pdf/" + arxivId;
    if (p.pdf_url) return p.pdf_url;
    return null;
  }

  function originalUrlFor(p) {
    var arxivId = canonicalArxivId(p);
    if (arxivId) return "https://arxiv.org/abs/" + arxivId;
    return p.url || pdfUrlFor(p) || "";
  }

  function buildViewItems() {
    var savedKeys = new Set();
    var saved = (library.liked || []).map(function (p) {
      savedKeys.add(paperKey(p));
      return { paper: p, saved: true, queueIndex: -1 };
    });
    var active = items
      .map(function (p, i) { return { paper: p, saved: false, queueIndex: i }; })
      .filter(function (v) { return !savedKeys.has(paperKey(v.paper)); });
    viewItems = saved.concat(active);
  }

  function nextUndecidedViewIndex(start) {
    for (var i = Math.max(0, start); i < viewItems.length; i++) {
      var v = viewItems[i];
      if (!v.saved && state[v.queueIndex] && !state[v.queueIndex].decision) return i;
    }
    return -1;
  }

  function initialViewIndex() {
    var next = nextUndecidedViewIndex(0);
    if (next >= 0) return next;
    return viewItems.length ? 0 : 0;
  }

  function allQueueDecided() {
    return items.length > 0 && state.every(function (s) { return s.decision; });
  }

  // Browser-style tab bar: one tab per recommendation; click to view it.
  // Uses short_name when present, else the full title.
  function renderTabs() {
    tabsEl.innerHTML = viewItems.map(function (v, i) {
      var p = v.paper;
      var dec = v.saved ? "like" : ((state[v.queueIndex] || {}).decision);
      var dot = dec ? '<span class="tab-dot ' + dec + '"></span>' : "";
      var label = esc(p.short_name || p.title);
      return '<button type="button" class="tab' + (i === idx ? " active" : "") +
        (v.saved ? " saved" : "") + '" data-i="' + i +
        '" title="' + esc(p.title) + '">' + dot + '<span class="tab-label">' + label + "</span></button>";
    }).join("");
    tabsEl.querySelectorAll(".tab").forEach(function (b) {
      b.addEventListener("click", function () { goTo(parseInt(b.getAttribute("data-i"), 10)); });
    });
    var act = tabsEl.querySelector(".tab.active");
    if (act) tabsEl.scrollLeft = Math.max(0, act.offsetLeft - 20);
    updateDoneButton();
  }

  function updateDoneButton() {
    if (!doneButton) return;
    doneButton.disabled = submitting || !items.length;
    doneButton.textContent = submitting ? "Sending…" : "Done";
  }

  function goTo(i) {
    if (i < 0 || i >= viewItems.length) return;
    idx = i;
    render();
  }

  // ---- Background PDF (PDF.js -> canvas, so it renders in any webview) ----
  var PDFJS_SRC = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js";
  var PDFJS_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
  var pdfjsReady = null, renderToken = 0;

  // Zoom: pages render at a fixed high backing resolution; we scale the
  // displayed (CSS) width. fitMode sizes the page to the viewport width.
  var qualityMax = Math.max(window.devicePixelRatio || 1, 2);
  var pageW = 0;        // logical page width (CSS px), set from the first page
  var fitMode = true;   // true = fit to width; false = manual zoom
  var zoomFactor = 1;   // displayed width = pageW * zoomFactor
  var readerBody = document.getElementById("reader-body");

  function computeFit() {
    if (!pageW) return 1;
    return Math.min(Math.max((readerBody.clientWidth - 32) / pageW, 0.4), qualityMax);
  }
  function applyZoom() {
    var z = fitMode ? computeFit() : zoomFactor;
    zoomFactor = z;
    Array.prototype.forEach.call(bgPages.querySelectorAll(".pdf-page"), function (c) {
      c.style.width = (pageW * z) + "px";
    });
    var lvl = document.getElementById("zoom-level");
    if (lvl) lvl.textContent = Math.round(z * 100) + "%";
  }
  function bumpZoom(mult) {
    var base = zoomFactor;
    fitMode = false;
    zoomFactor = Math.min(Math.max(base * mult, 0.4), qualityMax);
    applyZoom();
  }

  function loadPdfJs() {
    if (pdfjsReady) return pdfjsReady;
    pdfjsReady = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = PDFJS_SRC;
      s.onload = function () {
        if (!window.pdfjsLib) return reject(new Error("pdfjsLib missing"));
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
        resolve(window.pdfjsLib);
      };
      s.onerror = function () { reject(new Error("failed to load pdf.js")); };
      document.head.appendChild(s);
    });
    return pdfjsReady;
  }

  function proxiedPdfUrl(pdf) {
    return "/api/pdf?url=" + encodeURIComponent(pdf);
  }

  function loadPdfDocument(lib, pdf) {
    // Prefer direct browser loading. arXiv and many open-access hosts send CORS
    // headers, and this avoids Python TLS certificate-store issues. If CORS or
    // the remote host blocks direct access, fall back to PaperArena's same-origin
    // proxy.
    var direct = pdf;
    var proxy = proxiedPdfUrl(pdf);
    var sources = direct === proxy ? [direct] : [direct, proxy];

    function attempt(i) {
      return lib.getDocument({ url: sources[i], withCredentials: false }).promise
        .catch(function (err) {
          if (i + 1 >= sources.length) throw err;
          bgStatus.textContent = "Loading PDF via proxy…";
          return attempt(i + 1);
        });
    }

    return attempt(0);
  }

  function showBackground(p) {
    var pdf = pdfUrlFor(p);
    var original = originalUrlFor(p);
    currentOriginal = original;

    var key = pdf || ("nopdf:" + (p.id || p.title));
    if (key === lastBgKey) return; // same paper -> don't re-render
    lastBgKey = key;

    var token = ++renderToken;
    bgPages.innerHTML = "";
    pageW = 0; // recompute fit for this document
    bgStatus.style.display = "";
    if (!pdf) { bgStatus.textContent = "No PDF available for this paper."; return; }
    bgStatus.textContent = "Loading PDF…";

    loadPdfJs()
      .then(function (lib) { return loadPdfDocument(lib, pdf); })
      .then(function (doc) {
        if (token !== renderToken) return;
        bgStatus.style.display = "none";
        var chain = Promise.resolve();
        for (var i = 1; i <= doc.numPages; i++) {
          (function (n) {
            chain = chain
              .then(function () { return token === renderToken ? doc.getPage(n) : null; })
              .then(function (page) {
                if (!page || token !== renderToken) return;
                var vp = page.getViewport({ scale: 1.4 * qualityMax });
                var canvas = document.createElement("canvas");
                canvas.className = "pdf-page";
                canvas.width = vp.width;
                canvas.height = vp.height;
                if (!pageW) pageW = vp.width / qualityMax;
                canvas.style.width = (pageW * (fitMode ? computeFit() : zoomFactor)) + "px";
                bgPages.appendChild(canvas);
                return page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise
                  .then(function () { applyZoom(); });
              });
          })(i);
        }
        return chain;
      })
      .catch(function () {
        if (token !== renderToken) return;
        bgStatus.style.display = "";
        bgStatus.innerHTML = "Couldn’t render this PDF. <a href=\"" + esc(original) +
          "\" target=\"_blank\" rel=\"noopener\">Open it in a new tab</a>.";
      });
  }

  function clearBackground(msg) {
    renderToken++;
    lastBgKey = null;
    bgPages.innerHTML = "";
    bgStatus.style.display = "";
    bgStatus.textContent = msg || "";
  }

  function copyLink(button, url) {
    var done = function () {
      var prev = button.textContent;
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = prev; }, 1400);
    };
    if (navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(url).then(done, function () { window.prompt("Copy this link:", url); });
    else window.prompt("Copy this link:", url);
  }

  // ---- Rounds + HUD deck ----
  function loadRound(data) {
    round = data.round || 0;
    items = data.items || [];
    library = data.library || { liked: [], disliked: [] };
    library.liked = library.liked || [];
    library.disliked = library.disliked || [];
    state = items.map(function () { return { decision: null, chips: new Set(), note: "" }; });
    buildViewItems();
    idx = initialViewIndex();
    submitting = false;
    render();
  }

  function normalizeReason(raw) {
    if (raw && typeof raw === "object") {
      var objectLabel = String(raw.label || raw.reason || "");
      var objectDecision = raw.decision === "like" ? "like" : "dislike";
      return {
        label: objectLabel,
        decision: objectDecision,
        emoji: String(raw.emoji || fallbackReasonEmoji(objectLabel, objectDecision)),
      };
    }
    var label = String(raw || "");
    var positive = /want more|more like|strong|useful|interesting|central|foundational|good fit|relevant|depth insight|efficiency angle/i.test(label);
    var decision = positive ? "like" : "dislike";
    return { label: label, decision: decision, emoji: fallbackReasonEmoji(label, decision) };
  }

  function fallbackReasonEmoji(label, decision) {
    if (/survey|review|overview/i.test(label)) return "📚";
    if (/theoretical|theory/i.test(label)) return "🧠";
    if (/wrong subfield|wrong field|off topic|irrelevant/i.test(label)) return "🧭";
    if (/already know|seen|duplicate/i.test(label)) return "🔁";
    if (/old|outdated/i.test(label)) return "🕰️";
    if (/applied|engineering|production/i.test(label)) return "🛠️";
    if (/methods?|algorithm|technical/i.test(label)) return "⚙️";
    if (/benchmark|evaluation|metric/i.test(label)) return "📊";
    if (/want more|more like|good fit|relevant|interesting|useful|strong/i.test(label)) return "✨";
    return decision === "like" ? "✨" : "👎";
  }

  function render() {
    renderTabs();
    if (!viewItems.length) {
      controls.hidden = true;
      hint.hidden = true;
      deck.innerHTML = '<div class="placeholder" id="placeholder">Waiting for papers…</div>';
      clearBackground("Waiting for papers…");
      updateDoneButton();
      return;
    }
    if (idx >= viewItems.length || allQueueDecided()) return renderSummary();
    var v = viewItems[idx];
    var p = v.paper;
    var saved = v.saved;
    controls.hidden = saved;
    hint.hidden = false;
    showBackground(p);

    if (saved) {
      reasonShortcuts.innerHTML = "";
    } else {
      var reasons = (p.suggested_reasons || []).map(normalizeReason).filter(function (r) { return r.label; });
      reasonShortcuts.innerHTML = reasons.map(function (r) {
        return '<button type="button" class="reason-shortcut ' + r.decision +
          '" data-reason="' + esc(r.label) + '" data-decision="' + r.decision + '">' +
          '<span class="ic" aria-hidden="true">' + esc(r.emoji) + "</span> " + esc(r.label) + "</button>";
      }).join("");
      reasonShortcuts.querySelectorAll(".reason-shortcut").forEach(function (btn) {
        btn.addEventListener("click", function () {
          commit(btn.getAttribute("data-decision"), btn.getAttribute("data-reason"));
        });
      });
    }
    var venueText = String(p.venue || "");
    var knownConference = venueText.match(
      /\b(CVPR|ICCV|ECCV|NeurIPS|ICLR|ICML|CoRL|CoLLAs|AAAI|IJCAI|ACL|EMNLP|NAACL|COLING|KDD|SIGIR|SIGGRAPH|RSS|IROS|ICRA|AISTATS|UAI|WACV|BMVC|MICCAI|CHI|ICDM)\b/i
    );
    var publicationLabel = p.conference || (knownConference ? knownConference[1] : "Preprint");
    var publicationDate = [p.month, p.year].filter(Boolean).join(" ");
    var citationText = p.citations != null
      ? p.citations.toLocaleString() + (p.citations === 1 ? " citation" : " citations")
      : "";
    var meta = (saved ? ["Saved"] : []).concat([publicationLabel, publicationDate, citationText].filter(Boolean)).map(esc).join(" · ");
    var customAbstract = [p.abstract, p.why_suggested].filter(Boolean).join(" ");
    var original = originalUrlFor(p);
    var cardActions = original
      ? '<div class="card-actions">' +
          '<button type="button" class="card-action card-copy" data-url="' + esc(original) + '">Copy link</button>' +
          '<a class="card-action card-open" href="' + esc(original) + '" target="_blank" rel="noopener">Open original &#8599;</a>' +
        "</div>"
      : "";

    var card = document.createElement("div");
    card.className = "card" + (saved ? " saved-card" : "");
    card.innerHTML =
      '<div class="stamp like">LIKE</div><div class="stamp nope">NOPE</div>' +
      '<button type="button" class="card-nav card-prev" aria-label="Previous paper"' +
        (idx === 0 ? " disabled" : "") +
        '><svg class="card-nav-symbol" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>' +
      '<div class="card-paper">' +
        '<div class="card-head">' +
          '<div class="meta">' + (meta || "") + "</div>" +
          cardActions +
        "</div>" +
        '<h2 class="title">' + esc(p.title) + "</h2>" +
        (customAbstract ? '<p class="abstract">' + esc(customAbstract) + "</p>" : "") +
      "</div>" +
      '<button type="button" class="card-nav card-next" aria-label="Next paper"' +
        (idx === viewItems.length - 1 ? " disabled" : "") +
        '><svg class="card-nav-symbol" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg></button>';
    deck.innerHTML = "";
    deck.appendChild(card);
    wireCard(card);
  }

  function wireCard(card) {
    var previous = card.querySelector(".card-prev");
    var next = card.querySelector(".card-next");
    var copy = card.querySelector(".card-copy");
    previous.addEventListener("click", function (e) { e.stopPropagation(); goTo(idx - 1); });
    next.addEventListener("click", function (e) { e.stopPropagation(); goTo(idx + 1); });
    if (copy) copy.addEventListener("click", function (e) {
      e.stopPropagation();
      copyLink(copy, copy.getAttribute("data-url") || currentOriginal);
    });
    enableDrag(card);
  }

  // Axis-locked drag: vertical scrolls the card, horizontal swipes it.
  function enableDrag(card) {
    var startX = 0, startY = 0, dx = 0, axis = null, active = false, pid = null;
    var likeStamp = card.querySelector(".stamp.like");
    var nopeStamp = card.querySelector(".stamp.nope");

    card.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".card-nav, .card-action")) return;
      active = true; axis = null; dx = 0; startX = e.clientX; startY = e.clientY; pid = e.pointerId;
    });
    card.addEventListener("pointermove", function (e) {
      if (!active) return;
      var ddx = e.clientX - startX, ddy = e.clientY - startY;
      if (axis === null) {
        if (Math.abs(ddx) < 8 && Math.abs(ddy) < 8) return;
        axis = Math.abs(ddx) > Math.abs(ddy) ? "x" : "y";
        if (axis === "x") { card.classList.add("dragging"); try { card.setPointerCapture(pid); } catch (_) {} }
        else { active = false; return; } // let the card scroll vertically
      }
      if (axis !== "x") return;
      e.preventDefault();
      dx = ddx;
      card.style.transform = "translate(" + dx + "px," + (ddy * 0.18) + "px) rotate(" + dx / 22 + "deg)";
      likeStamp.style.opacity = dx > 0 ? Math.min(dx / 110, 1) : 0;
      nopeStamp.style.opacity = dx < 0 ? Math.min(-dx / 110, 1) : 0;
    });
    function end() {
      if (axis === "x") {
        card.classList.remove("dragging");
        if (dx > 110) return commit("like");
        if (dx < -110) return commit("dislike");
        card.style.transform = "";
        likeStamp.style.opacity = nopeStamp.style.opacity = 0;
      }
      active = false; axis = null; dx = 0;
    }
    card.addEventListener("pointerup", end);
    card.addEventListener("pointercancel", end);
  }

  function commit(decision, reason) {
    var v = viewItems[idx];
    if (!v || v.saved || idx >= viewItems.length) return;
    var s = state[v.queueIndex];
    if (!s) return;
    if (reason) {
      s.chips.clear();
      s.chips.add(reason);
    }
    s.decision = decision;
    var card = deck.querySelector(".card");
    if (card) card.classList.add(decision === "like" ? "fly-right" : "fly-left");
    var next = nextUndecidedViewIndex(idx + 1);
    idx = next >= 0 ? next : viewItems.length;
    setTimeout(render, 180);
  }

  function renderSummary() {
    controls.hidden = true;
    hint.hidden = true;
    clearBackground("Round " + round + " complete.");
    var liked = state.filter(function (s) { return s.decision === "like"; }).length;
    var disliked = state.filter(function (s) { return s.decision === "dislike"; }).length;
    deck.innerHTML =
      '<div class="summary">' +
      "<h2>Round " + round + " complete</h2>" +
      '<div class="tallies">' +
      '<div class="tally like"><div class="n">' + liked + '</div><div class="l">liked</div></div>' +
      '<div class="tally dislike"><div class="n">' + disliked + '</div><div class="l">disliked</div></div>' +
      "</div>" +
      '<button class="send" id="send">Done</button>' +
      '<div style="margin-top:12px"><button id="review" style="border:none;background:none;color:var(--text-dim);cursor:pointer;font:inherit;font-size:13px">Review again</button></div>' +
      "</div>";
    document.getElementById("send").addEventListener("click", submit);
    document.getElementById("review").addEventListener("click", function () { idx = 0; render(); });
  }

  function submit() {
    if (submitting || !items.length) return;
    submitting = true;
    updateDoneButton();
    var payload = {
      round: round,
      items: items.map(function (p, i) {
        return { id: p.id, title: p.title, decision: state[i].decision, reasons: Array.from(state[i].chips), note: state[i].note };
      }).filter(function (r) { return r.decision; }),
    };
    controls.hidden = true;
    hint.hidden = true;
    deck.innerHTML = '<div class="waiting"><div class="spinner"></div>Saved. Waiting for the next round…</div>';
    fetchJSON("/api/feedback", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  }

  function shouldLoadIncomingRound(data) {
    return (data.round || 0) > round || (!viewItems.length && (((data.items || []).length) || ((data.library || {}).liked || []).length));
  }

  function handleIncomingRound(data) {
    if (shouldLoadIncomingRound(data)) loadRound(data);
  }

  function startLongPollUpdates() {
    if (waitingForRound) return;
    waitingForRound = true;

    function waitOnce() {
      fetchJSON("/api/wait-round?round=" + encodeURIComponent(round) + "&timeout=300")
        .then(function (data) {
          handleIncomingRound(data);
          setTimeout(waitOnce, 0);
        })
        .catch(function () {
          setTimeout(waitOnce, 5000);
        });
    }

    waitOnce();
  }

  function setupEvents() {
    if (eventsSource || !window.EventSource) {
      if (!window.EventSource) startLongPollUpdates();
      return;
    }
    eventsSource = new EventSource("/api/events");
    eventsSource.addEventListener("candidates", function (event) {
      try { handleIncomingRound(JSON.parse(event.data)); } catch (_) {}
    });
    eventsSource.onerror = function () { startLongPollUpdates(); };
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "ArrowLeft") goTo(idx - 1);
    else if (e.key === "ArrowRight") goTo(idx + 1);
    else if (e.key === "+" || e.key === "=") bumpZoom(1.2);
    else if (e.key === "-") bumpZoom(0.83);
    else if (e.key === "0") { fitMode = true; applyZoom(); }
  });
  document.getElementById("btn-like").addEventListener("click", function () { commit("like"); });
  document.getElementById("btn-dislike").addEventListener("click", function () { commit("dislike"); });
  doneButton.addEventListener("click", submit);
  document.getElementById("zoom-in").addEventListener("click", function () { bumpZoom(1.2); });
  document.getElementById("zoom-out").addEventListener("click", function () { bumpZoom(0.83); });
  document.getElementById("zoom-fit").addEventListener("click", function () { fitMode = true; applyZoom(); });
  window.addEventListener("resize", function () { if (fitMode) applyZoom(); });

  function boot() {
    fetchJSON("/api/candidates").then(function (data) {
      if ((data.items || []).length || (((data.library || {}).liked || []).length)) loadRound(data);
      else {
        placeholder.textContent = "Waiting for papers…";
        controls.hidden = true;
        updateDoneButton();
      }
      setupEvents();
    }).catch(function () {
      placeholder.textContent = "Waiting for the local PaperArena server…";
      setupEvents();
    });
  }
  boot();
})();
