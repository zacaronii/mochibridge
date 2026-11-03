// Runs in the page's own context (world: MAIN) to read YouTube's config, which the extension's isolated content
// script can't see: which Google account is signed in, and its session index for multi-account browsers.
(function () {
  function report() {
    try {
      const cfg = (window.ytcfg && window.ytcfg.data_) || {};
      document.dispatchEvent(new CustomEvent("kbridge-account", { detail: JSON.stringify({
        account: cfg.DATASYNC_ID || cfg.DELEGATED_SESSION_ID || null,
        sessionIndex: cfg.SESSION_INDEX ?? null,
        loggedIn: !!cfg.LOGGED_IN,
      }) }));
    } catch (e) { /* ignore */ }
  }
  report();
  document.addEventListener("yt-navigate-finish", report);
  setInterval(report, 60000);
})();
