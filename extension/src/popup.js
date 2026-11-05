const api = typeof browser !== "undefined" ? browser : chrome;
const $ = (id) => document.getElementById(id);

async function activeTabInfo() {
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/youtube\.com/.test(tab.url || "")) return null;
  try { return await api.tabs.sendMessage(tab.id, { type: "current" }); } catch (_) { return null; }
}

async function render() {
  const s = await api.storage.local.get(["enabled", "trackedAccount", "shareCookies", "lastStatus"]);
  $("enabled").checked = s.enabled !== false;
  $("shareCookies").checked = !!s.shareCookies;
  const info = await activeTabInfo();
  const acct = info && info.account && info.account.account;
  $("account").className = s.trackedAccount ? "muted" : "update";
  $("account").textContent = !s.trackedAccount
    ? "No account tracked yet: viewing isn't reported until you open youtube.com and click Track this account."
    : acct ? (acct === s.trackedAccount ? "This tab: the tracked account ✓" : "This tab: a different account (not tracked)")
    : "Tracked account set.";
  $("track").disabled = !acct;
  if (info && info.snap) {
    $("current").textContent = `${info.snap.title || info.snap.video_id} — ${Math.round(info.snap.seconds)} s counted`;
  }
  const l = s.lastStatus;
  if (l) {
    const when = new Date(l.at).toLocaleTimeString();
    $("last").textContent = l.ok ? `${when}: sent ${Math.round(l.seconds)} s · ${l.video || ""}`
      : `${when}: ${l.error || l.note}`;
  }
  api.runtime.sendMessage({ type: "bridgeStatus" }).then((st) => {
    if (!st || st.error) { $("bridge").textContent = st ? st.error : "unreachable"; return; }
    const latest = st.extension_latest, mine = api.runtime.getManifest().version;
    if (latest && latest !== mine) {
      api.storage.local.get(["updateAvailable", "bridge"]).then(({ updateAvailable, bridge }) => {
        if (!updateAvailable) return;
        const base = (bridge || "").replace(/\/$/, "");
        const zip = /firefox/i.test(navigator.userAgent) ? "mochibridge-firefox.zip" : "mochibridge-chrome.zip";
        $("update").innerHTML = "";
        const a = Object.assign(document.createElement("a"), { href: `${base}/download/${zip}`, target: "_blank",
                                                               textContent: `Update available: ${updateAvailable}` });
        $("update").append(a, document.createTextNode(` (you have ${mine}). Unzip it over the extension's folder, then click reload on the extensions page.`));
        $("update").hidden = false;
      });
    }
    const h = st.history || {};
    $("bridge").textContent = !st.cookies ? "no sign-in cookies on the bridge" : `${h.state || "not run yet"}`
      + (h.ok ? ` · last ok ${new Date(h.ok).toLocaleString()}` : "") + (h.error ? ` · ${h.error}` : "");
  });
  $("track").onclick = async () => {
    if (acct) await api.storage.local.set({ trackedAccount: acct });
    render();
  };
}

$("enabled").onchange = (e) => api.storage.local.set({ enabled: e.target.checked });
$("shareCookies").onchange = async (e) => {
  await api.storage.local.set({ shareCookies: e.target.checked });
  if (e.target.checked) $("shareNow").onclick();     // send once right away instead of waiting for the hourly check
};
$("shareNow").onclick = async () => {
  const r = await api.runtime.sendMessage({ type: "shareCookiesNow" });
  $("bridge").textContent = r && r.ok ? "sign-in sent ✓" : `failed: ${r && r.error}`;
};
render();
