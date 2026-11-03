const api = typeof browser !== "undefined" ? browser : chrome;
const $ = (id) => document.getElementById(id);
api.storage.local.get(["bridge", "token"]).then((s) => {
  $("bridge").value = s.bridge || "";
  $("token").value = s.token || "";
});
// Saving asks the browser for access to the bridge's address (the extension has none by default).
$("save").onclick = async () => {
  const bridge = $("bridge").value.trim().replace(/\/$/, "");
  let origin;
  try { origin = new URL(bridge).origin; } catch (_) { $("out").textContent = "Enter the bridge URL, e.g. https://mochibridge.example.com"; return false; }
  const granted = await api.permissions.request({ origins: [origin + "/*"] });
  await api.storage.local.set({ bridge, token: $("token").value.trim() });
  $("out").textContent = granted ? "Saved." : "Saved, but the browser didn't allow access to the bridge. Click Save again and allow it.";
  return granted;
};
$("test").onclick = async () => {
  if (!(await $("save").onclick())) return;
  const r = await api.runtime.sendMessage({ type: "bridgeStatus" });
  $("out").textContent = r && !r.error ? "Connected ✓" : `Failed: ${r && r.error}`;
};
