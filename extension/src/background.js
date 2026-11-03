// Background worker: sends heartbeats to the bridge (only for the tracked account), keeps the last status for the
// popup, and optionally refreshes the bridge's YouTube sign-in cookies.
const DEFAULTS = { bridge: "", token: "", enabled: true, trackedAccount: null,
                   shareCookies: false, lastCookieShare: 0 };
const api = typeof browser !== "undefined" ? browser : chrome;

async function settings() {
  return { ...DEFAULTS, ...(await api.storage.local.get(Object.keys(DEFAULTS))) };
}

async function call(path, options) {
  const s = await settings();
  if (!s.bridge || !s.token) throw new Error("bridge URL or API token not set (extension settings)");
  const base = s.bridge.replace(/\/$/, "");
  if (!(await api.permissions.contains({ origins: [new URL(base).origin + "/*"] }))) {
    throw new Error("no access to the bridge yet: open the extension settings and click Save");
  }
  const r = await fetch(base + path, {
    ...options, headers: { "authorization": "Bearer " + s.token, "content-type": "application/json",
                           ...(options && options.headers) } });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`.slice(0, 200));
  return r.json();
}

async function status(update) {
  await api.storage.local.set({ lastStatus: { at: Date.now(), ...update } });
}

api.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "progress") {
    handleProgress(msg).then(() => reply({ ok: true }), (e) => reply({ ok: false, error: String(e) }));
    return true;
  }
  if (msg.type === "bridgeStatus") {
    call("/api/youtube/status").then((st) => { checkUpdate(st).catch(() => {}); reply(st); },
                                     (e) => reply({ error: String(e) }));
    return true;
  }
  if (msg.type === "shareCookiesNow") {
    shareCookies(true).then(() => reply({ ok: true }), (e) => reply({ ok: false, error: String(e) }));
    return true;
  }
});

async function handleProgress(msg) {
  const s = await settings();
  const acct = msg.account && msg.account.account;
  if (!s.enabled) return;
  if (!s.trackedAccount || acct !== s.trackedAccount) {
    await status({ ok: false, note: "not the tracked account; nothing sent" });
    return;
  }
  await api.storage.local.set({ lastSessionIndex: msg.account.sessionIndex });
  try {
    await call("/api/youtube/progress", { method: "POST", body: JSON.stringify({ ...msg.snap, account: acct }) });
    await status({ ok: true, video: msg.snap.title, seconds: msg.snap.seconds });
  } catch (e) {
    await status({ ok: false, error: String(e) });
  }
}

async function shareCookies(force) {
  const s = await settings();
  if (!s.shareCookies && !force) return;
  if (!force && Date.now() - s.lastCookieShare < 20 * 3600 * 1000) {
    const st = await call("/api/youtube/status").catch(() => null);
    if (!st || (st.cookies && st.history && st.history.state !== "signed_out")) return;
  }
  const cookies = await api.cookies.getAll({ domain: "youtube.com" });
  const { lastSessionIndex } = await api.storage.local.get("lastSessionIndex");
  await call("/api/youtube/cookies", { method: "POST",
    body: JSON.stringify({ cookies, session_index: lastSessionIndex ?? null }) });
  await api.storage.local.set({ lastCookieShare: Date.now() });
}

// Google rotates the sign-in cookies every few minutes while YouTube is open and soon retires the old ones, so a
// copy shared earlier stops working. Re-share shortly after each rotation (debounced: one rotation changes several).
const SIGN_IN = new Set(["SID", "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS",
                         "LOGIN_INFO"]);
api.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (removed || !SIGN_IN.has(cookie.name) || !cookie.domain.endsWith("youtube.com")) return;
  settings().then((s) => { if (s.shareCookies) api.alarms.create("kbridge-cookies-soon", { delayInMinutes: 1 }); });
});

// "Update available" badge: the bridge reports the version of the extension zip it serves.
function newer(a, b) {
  const pa = String(a).split(".").map(Number), pb = String(b).split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) > (pb[i] || 0);
  }
  return false;
}

async function checkUpdate(st) {
  st = st || await call("/api/youtube/status").catch(() => null);
  const latest = st && st.extension_latest;
  const update = latest && newer(latest, api.runtime.getManifest().version) ? latest : null;
  await api.storage.local.set({ updateAvailable: update });
  await api.action.setBadgeText({ text: update ? "↑" : "" });
  if (update) await api.action.setBadgeBackgroundColor({ color: "#c0392b" });
}

api.runtime.onStartup.addListener(() => checkUpdate().catch(() => {}));
api.runtime.onInstalled.addListener(() => checkUpdate().catch(() => {}));
api.alarms.create("kbridge-cookies", { periodInMinutes: 60 });
api.alarms.onAlarm.addListener((a) => {
  if (a.name === "kbridge-cookies") { shareCookies(false).catch(() => {}); checkUpdate().catch(() => {}); }
  if (a.name === "kbridge-cookies-soon") shareCookies(true).catch(() => {});
});
