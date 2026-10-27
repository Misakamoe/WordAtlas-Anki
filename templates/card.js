(function () {
  "use strict";

  var namespace = "__wordAtlasCard";
  if (window[namespace] && typeof window[namespace].cleanup === "function") {
    window[namespace].cleanup();
  }

  var roots = document.querySelectorAll(".wa-card");
  var root = roots[roots.length - 1];
  if (!root) return;

  // Format displayed text only, preserving note fields and any inline markup.
  var tip = root.querySelector(".wa-tip p");
  if (tip) {
    var textNodes = document.createTreeWalker(tip, NodeFilter.SHOW_TEXT);
    var previous = "";
    var textNode;
    while ((textNode = textNodes.nextNode())) {
      var spaced = (previous + textNode.nodeValue)
        .replace(/([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])([A-Za-z0-9])/g, "$1 $2")
        .replace(/([A-Za-z0-9])([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])/g, "$1 $2");
      textNode.nodeValue = spaced.slice(previous.length);
      previous = textNode.nodeValue.slice(-1) || previous;
    }
  }

  var replaySelector = ".replay-button, .soundLink, a[href^='playsound:'], a[href^='sound:']";
  var players = [];
  var listeners = [];
  var observer;
  var disposed = false;
  var playSerial = 0;
  var status = root.querySelector(".wa-audio-status");

  function listen(target, name, handler, capture) {
    target.addEventListener(name, handler, !!capture);
    listeners.push(function () {
      target.removeEventListener(name, handler, !!capture);
    });
  }

  function message(text) {
    if (!status || disposed) return;
    status.textContent = text;
    status.hidden = !text;
  }

  function stopControl() {
    var wrapper = root.querySelector(".wa-native-stop");
    return wrapper && wrapper.querySelector(replaySelector);
  }

  function interruptNative() {
    var control = stopControl();
    if (!control || typeof control.click !== "function") return false;
    try {
      // Anki owns the replay action and queue; no private command/index guessing.
      control.click();
      return true;
    } catch (error) {
      return false;
    }
  }

  function playing(player, value) {
    if (!player.button) return;
    player.button.classList.toggle("is-playing", value);
    player.button.setAttribute("aria-pressed", value ? "true" : "false");
    player.button.setAttribute("aria-label", (value ? "Stop " : "Play ") + player.label);
    player.button.title = (value ? "Stop " : "Play ") + player.label;
  }

  function stopPlayer(player) {
    player.audio.pause();
    try { player.audio.currentTime = 0; } catch (error) { /* No media loaded yet. */ }
    playing(player, false);
  }

  function stopAll() {
    playSerial += 1;
    players.forEach(stopPlayer);
  }

  function showControls(player, text) {
    player.media.hidden = false;
    player.audio.controls = true;
    if (player.button) player.button.hidden = true;
    if (player.pill) {
      player.pill.removeAttribute("role");
      player.pill.removeAttribute("tabindex");
      player.pill.removeAttribute("aria-label");
    }
    message(text);
  }

  function playManual(player) {
    if (disposed || !player.media.hidden) return;
    if (!player.audio.paused && !player.audio.ended) {
      stopAll();
      message("");
      return;
    }
    stopAll();
    if (!interruptNative()) {
      showControls(player, "Native audio cannot be interrupted here. Wait for it to finish, then use the audio controls.");
      return;
    }
    var attempt = playSerial;
    message("");
    try {
      // This is only called directly by a click or keyboard activation.
      var result = player.audio.play();
      if (result && typeof result.catch === "function") {
        result.catch(function () {
          if (disposed || attempt !== playSerial) return;
          playing(player, false);
          showControls(player, "Playback did not start. Use the audio controls to retry, and check that this recording is synced.");
        });
      }
    } catch (error) {
      showControls(player, "Playback did not start. Use the audio controls to retry, and check that this recording is synced.");
    }
  }

  function activatePill(pill, label, action) {
    if (!pill) return;
    pill.setAttribute("role", "button");
    pill.setAttribute("tabindex", "0");
    pill.setAttribute("aria-label", "Play " + label);
    listeners.push(function () {
      pill.removeAttribute("role");
      pill.removeAttribute("tabindex");
      pill.removeAttribute("aria-label");
    });
    listen(pill, "click", function (event) {
      if (!pill.hasAttribute("role")) return;
      event.preventDefault();
      event.stopPropagation();
      action();
    });
    listen(pill, "keydown", function (event) {
      if (!pill.hasAttribute("role") || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) action();
    });
    listen(pill, "keyup", function (event) {
      if (!pill.hasAttribute("role") || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      event.stopPropagation();
    });
  }

  var manualWrappers = root.querySelectorAll(".wa-manual");
  Array.prototype.forEach.call(manualWrappers, function (wrapper) {
    var audio = wrapper.querySelector("audio.wa-manual-audio");
    var media = wrapper.querySelector(".wa-manual-media");
    if (!audio || !media) return;
    var player = {
      audio: audio,
      media: media,
      button: wrapper.querySelector(".wa-manual-button"),
      pill: wrapper.querySelector(".wa-ipa-pill"),
      label: wrapper.getAttribute("data-audio-label") || "recording"
    };
    players.push(player);
    audio.preload = "none";
    playing(player, false);

    if (player.button && stopControl()) {
      media.hidden = true;
      player.button.hidden = false;
      listen(player.button, "click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        playManual(player);
      });
      listen(player.button, "keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.stopPropagation();
          if (event.repeat) event.preventDefault();
        }
      });
      listen(player.button, "keyup", function (event) {
        if (event.key === "Enter" || event.key === " ") event.stopPropagation();
      });
      activatePill(player.pill, player.label, function () { playManual(player); });
    } else {
      showControls(player, "Native audio cannot be interrupted here. Wait for it to finish, then use the audio controls.");
    }

    listen(audio, "play", function () {
      if (disposed) return;
      players.forEach(function (other) {
        if (other !== player) stopPlayer(other);
      });
      if (!media.hidden) {
        playSerial += 1;
        if (!interruptNative()) {
          message("Native audio cannot be interrupted here and may overlap. Pause this player until automatic audio finishes.");
        } else {
          message("");
        }
      }
      playing(player, true);
    });
    listen(audio, "pause", function () { playing(player, false); });
    listen(audio, "ended", function () { playing(player, false); });
    listen(audio, "error", function () {
      playing(player, false);
      showControls(player, "This recording could not be loaded. Check media sync, then retry with the audio controls.");
    });
  });

  Array.prototype.forEach.call(root.querySelectorAll(".wa-native-audio"), function (wrapper) {
    var replay = wrapper.querySelector(replaySelector);
    if (!replay) return;
    var label = wrapper.getAttribute("data-audio-label") || "recording";
    replay.setAttribute("aria-label", "Play " + label);
    replay.title = "Play " + label;
    var group = wrapper.closest(".wa-pronunciation");
    if (group) {
      activatePill(group.querySelector(".wa-ipa-pill"), label, function () {
        replay.click();
      });
    }
  });

  var stop = stopControl();
  if (stop) stop.setAttribute("tabindex", "-1");

  // Capture before Anki's original handler, without changing its replay action.
  listen(document, "click", function (event) {
    var target = event.target.nodeType === 1 ? event.target : event.target.parentElement;
    var replay = target && target.closest(replaySelector);
    if (!replay || replay.closest(".wa-native-stop")) return;
    stopAll();
    if (stopControl()) message("");
  }, true);

  listen(document, "keydown", function (event) {
    var target = event.target;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
    var replayKey = (event.key === "Enter" || event.key === " ") && target && target.closest && target.closest(replaySelector);
    if (replayKey || event.key === "r" || event.key === "R" || event.code === "KeyR" || event.keyCode === 82) {
      stopAll();
      if (stopControl()) message("");
    }
  }, true);

  function cleanup() {
    if (disposed) return;
    disposed = true;
    if (observer) observer.disconnect();
    listeners.forEach(function (remove) { remove(); });
    stopAll();
    // Restore the HTML-only fallback if the same root is initialized again.
    players.forEach(function (player) {
      player.media.hidden = false;
      if (player.button) player.button.hidden = true;
      if (player.pill) {
        player.pill.removeAttribute("role");
        player.pill.removeAttribute("tabindex");
        player.pill.removeAttribute("aria-label");
      }
    });
    if (window[namespace] && window[namespace].cleanup === cleanup) {
      delete window[namespace];
    }
  }

  window[namespace] = { cleanup: cleanup };
  listen(window, "pagehide", cleanup);
  listen(document, "visibilitychange", function () {
    if (document.hidden) stopAll();
  });
  if (typeof MutationObserver !== "undefined") {
    observer = new MutationObserver(function () {
      if (!document.documentElement.contains(root) || players.some(function (player) {
        return !root.contains(player.audio);
      })) cleanup();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }
}());
