// AVADO Private Assistant setup page. Plain JS, no dependencies; served under a strict CSP (script-src 'self').
// It reads /avado/api/status and, for the owner (signed in to the assistant as its admin), sends the button presses.
(function () {
  "use strict";

  var params = new URLSearchParams(location.search);
  var theme = params.get("theme");
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme", theme);

  var state = null;
  var timer = null;
  var busy = false;

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "class") e.className = attrs[k];
      else e.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) {
      if (c) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return e;
  }

  function show(id, on) {
    $(id).hidden = !on;
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(load, ms);
  }

  function load() {
    fetch("/avado/api/status", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (s) {
        state = s;
        render();
        var fast = s.phase === "downloading" || s.phase === "starting" || s.speed_testing;
        schedule(fast ? 3000 : 10000);
      })
      .catch(function () {
        $("headline").textContent = "The assistant is starting";
        $("detail").textContent = "This page tries again by itself in a few seconds.";
        setLevel("info");
        schedule(5000);
      });
  }

  function post(path, body) {
    if (busy) return;
    busy = true;
    $("problem").hidden = true;
    fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Avado-Request": "1" },
      body: JSON.stringify(body || {}),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(j && j.error ? j.error : "That did not work. Please try again.");
          return j;
        });
      })
      .then(function (s) {
        state = s;
        render();
        schedule(2000);
      })
      .catch(function (e) {
        $("problem").textContent = e.message || "That did not work. Please try again.";
        $("problem").hidden = false;
      })
      .then(function () {
        busy = false;
      });
  }

  function setLevel(level) {
    $("status").setAttribute("data-level", level);
  }

  function render() {
    var s = state;
    $("version").textContent = s.version ? "Version " + s.version : "";

    var headline, detail = "", level = "info";
    var p = s.progress;
    switch (s.phase) {
      case "downloading":
        headline = "Downloading " + (s.target || "the model");
        if (p && p.stage === "verifying") detail = "Checking the download. Almost done.";
        else if (p && p.stage === "finishing") detail = "Almost done.";
        else if (p && p.total) {
          detail = p.done_plain + " of " + p.total_plain + " downloaded";
          if (p.speed_plain) detail += " (" + p.speed_plain + ")";
          detail += ".";
          if (p.eta_plain) detail += " Time left: " + p.eta_plain + ".";
        } else detail = "Starting the download.";
        detail += " You can close this page: the download goes on by itself.";
        break;
      case "retrying":
        headline = "The download paused";
        detail = "It starts again by itself in a minute and goes on where it stopped.";
        level = "warning";
        break;
      case "error":
        headline = "The download stopped";
        detail = "Nothing is lost. Choose \"Try again\" or another model below.";
        level = "warning";
        break;
      case "no-space":
        headline = "Not enough free space";
        detail = "Free up space on the box, then choose a model below.";
        level = "warning";
        break;
      case "choose":
        headline = "Choose a model";
        detail = "The assistant needs a model before it can answer. Pick one below.";
        break;
      case "ready":
        headline = "Your assistant is ready";
        detail = "It uses " + s.active + ".";
        level = "ok";
        break;
      default:
        headline = "The assistant is starting";
        detail = "This takes about a minute after the box or the app starts.";
    }
    if (s.phase === "downloading" && s.ready) {
      detail = "Your assistant keeps using " + s.active + " until this download is done. " + detail;
    }
    $("headline").textContent = headline;
    $("detail").textContent = detail;
    setLevel(level);

    var showBar = s.phase === "downloading" && p && p.total;
    show("progress", !!showBar);
    if (showBar) {
      $("bar").style.width = Math.max(1, Math.min(100, p.percent)) + "%";
      $("progress-text").textContent = p.percent + "%";
    }

    if (s.problem && (s.phase === "retrying" || s.phase === "error" || s.phase === "no-space")) {
      $("problem").textContent = s.problem;
      $("problem").hidden = false;
    }

    show("open", !!s.ready && s.onboarding === false);
    show("signup", s.onboarding === true);
    show("account", s.onboarding === true);
    show("retry", s.phase === "error" && !!s.failed && s.can_change);
    show("cancel", (s.phase === "downloading" || s.phase === "retrying") && s.can_change);
    show("signin-hint", !s.can_change && s.onboarding === false);

    // Speed
    show("speed-card", !!(s.speed || s.speed_testing));
    $("speed-text").textContent = s.speed_testing
      ? "Measuring how fast the assistant writes on this box. This takes up to a few minutes."
      : s.speed || "";
    show("speedtest", !!s.speed && !s.speed_testing && s.can_change);

    // Box and models
    var bits = [];
    if (s.box.ram_plain) bits.push(s.box.ram_plain + " of memory");
    if (s.box.free_plain) bits.push(s.box.free_plain + " free space");
    if (s.box.cpu) bits.push(s.box.cpu);
    $("box-line").textContent = bits.length ? "This box: " + bits.join(", ") + "." : "";

    var list = $("models");
    list.textContent = "";
    s.models.forEach(function (m) {
      list.appendChild(modelRow(m, s));
    });
  }

  var STATE_WORDS = {
    active: "In use",
    downloading: "Downloading",
    installed: "On this box",
  };

  function modelRow(m, s) {
    var title = el("div", { class: "title" }, [
      el("strong", { text: m.name }),
      el("span", { class: "muted", text: " · " + m.label + " · " + m.size_plain }),
    ]);
    var badges = el("div", { class: "badges" });
    if (STATE_WORDS[m.state]) badges.appendChild(el("span", { class: "badge " + m.state, text: STATE_WORDS[m.state] }));
    if (m.recommended) badges.appendChild(el("span", { class: "badge suggested", text: "Suggested for this box" }));

    var li = el("li", { "data-state": m.state }, [el("div", { class: "row" }, [title, badges])]);
    if (m.about) li.appendChild(el("p", { class: "about", text: m.about }));
    if (m.note) li.appendChild(el("p", { class: "note small", text: m.note }));

    if (s.can_change) {
      var actions = el("div", { class: "actions" });
      if (m.state === "available") {
        var b = el("button", { type: "button", class: "button small", text: "Download and use" });
        b.addEventListener("click", function () {
          var msg = "Download " + m.name + " (" + m.size_plain + ")? With normal home internet this takes from a few minutes to about an hour.";
          if (window.confirm(msg)) post("/avado/api/choose", { id: m.id });
        });
        actions.appendChild(b);
      } else if (m.state === "installed") {
        var use = el("button", { type: "button", class: "button small", text: "Use this one" });
        use.addEventListener("click", function () {
          post("/avado/api/choose", { id: m.id });
        });
        var rm = el("button", { type: "button", class: "button ghost small", text: "Remove" });
        rm.addEventListener("click", function () {
          if (window.confirm("Remove " + m.name + " from the box? This frees " + m.size_plain + ". Your chats stay.")) {
            post("/avado/api/remove", { id: m.id });
          }
        });
        actions.appendChild(use);
        actions.appendChild(rm);
      }
      if (actions.childNodes.length) li.appendChild(actions);
    }
    return li;
  }

  $("retry").addEventListener("click", function () {
    if (state && state.failed) post("/avado/api/choose", { id: state.failed });
  });
  $("cancel").addEventListener("click", function () {
    if (window.confirm("Stop the download? What is already downloaded is kept for a while, so starting again goes faster.")) {
      post("/avado/api/cancel");
    }
  });
  $("speedtest").addEventListener("click", function () {
    post("/avado/api/speedtest");
  });

  load();
})();
