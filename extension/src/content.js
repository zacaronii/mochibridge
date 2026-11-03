// Glue between the YouTube page and the background worker: samples the player once a second, sends heartbeats.
(function () {
  const tracker = new KBTracker();
  const HEARTBEAT_MS = 30000;
  let account = null;
  let lastSent = 0;

  document.addEventListener("kbridge-account", (e) => {
    try { account = JSON.parse(e.detail); } catch (_) { /* ignore */ }
  });

  function videoId() {
    const u = new URL(location.href);
    if (u.pathname === "/watch") return u.searchParams.get("v");
    const m = u.pathname.match(/^\/(?:shorts|live)\/([A-Za-z0-9_-]{11})/);
    return m ? m[1] : null;
  }

  function state() {
    const id = videoId();
    const player = document.querySelector("#movie_player, .html5-video-player");
    const video = player ? player.querySelector("video") : document.querySelector("video");
    if (!id || !video) return { videoId: id && video ? id : null };
    const title = (document.querySelector("ytd-watch-metadata h1, h1.ytd-watch-metadata") || {}).textContent
      || document.title.replace(/ - YouTube$/, "");
    return {
      videoId: id,
      playing: !video.paused && !video.ended && !video.seeking && video.readyState >= 3,
      adShowing: !!(player && player.classList.contains("ad-showing")),
      position: video.currentTime,
      duration: video.duration,
      title: (title || "").trim(),
    };
  }

  function send(snap, reason) {
    if (!snap || snap.seconds <= 0) return;
    chrome.runtime.sendMessage({ type: "progress", snap, account, reason }).catch(() => {});
  }

  function loop() {
    for (const done of tracker.tick(state())) send(done, "end");
    const now = Date.now();
    if (tracker.session && tracker.session.dirty && now - lastSent >= HEARTBEAT_MS) {
      tracker.session.dirty = false;
      lastSent = now;
      send(tracker.snapshot(), "heartbeat");
    }
  }

  setInterval(loop, 1000);
  for (const ev of ["pause", "ended"]) {
    document.addEventListener(ev, () => { loop(); send(tracker.snapshot(), ev); }, true);
  }
  window.addEventListener("pagehide", () => send(tracker.snapshot(), "leave"));
  document.addEventListener("visibilitychange", () => { if (document.hidden) send(tracker.snapshot(), "hidden"); });

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg.type === "current") reply({ account, snap: tracker.snapshot() });
  });
})();
