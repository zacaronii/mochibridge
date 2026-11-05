# Deploying mochibridge

## Setup

1. Copy `docker-compose.yml` and `env.example` (as `.env`) into a folder on the server, and fill in `.env`:
   - **Jellyfin:** Dashboard → API Keys. `JELLYFIN_USER` picks whose plays count.
   - **Kavita:** user menu → Settings → Auth Keys.
   - **Audiobookshelf:** Settings → API Keys, created *for your user*, not root.
   - `BRIDGE_API_TOKEN`: generate with `openssl rand -hex 24`.
2. Put the bridge on the Docker network(s) where kechimochi and your media servers run (`networks:` in the compose
   file), so it reaches them by container name.
3. Copy `../config.example.yaml` to `${APPDATA}/mochibridge/config.yaml` and adjust it:
   - server URLs, `timezone`, and `web.kechimochi_url` (kechimochi as your browser reaches it). The kechimochi
     addresses can instead come from the `KECHIMOCHI_URL` / `KECHIMOCHI_PUBLIC_URL` environment variables, which
     override the file;
   - the libraries you want tracked; `kbridge libraries` lists them all.
   
   Keep `mode: dry_run` for now.
4. `docker compose up -d`, then check the logs and `pending` (below). In dry-run mode the bridge records what it would send without writing anything.
5. When the proposed entries and logs look right, set `mode: live` and restart. If you used Nagare's kechimochi sync, run the migration first (see the main README).

The web UI has no login. Publish it only on a LAN address, or put it behind a reverse proxy with a LAN-only (or VPN-only) allowlist. The compose file shows both options.

Mode (`dry_run`/`live`) and which sources run are changed only in `config.yaml`, followed by a restart. Everything else can be changed from the Settings page, which saves to `config.yaml` with comments kept (the previous version goes to `config.yaml.bak`) and applies it immediately.

**Unraid Compose Manager:** it runs each project as `-p <name, lowercased, with . space - turned into _>`. If you start the stack by hand, pass the same `-p` name, or Compose Manager won't show it as running.

## Day to day

```sh
docker logs -f mochibridge                                                # what it's doing
docker exec mochibridge python -m kbridge -c /data/config.yaml pending    # unsent logs, skips, events
docker exec mochibridge python -m kbridge -c /data/config.yaml check      # connectivity
docker exec mochibridge python -m kbridge -c /data/config.yaml libraries  # tracked libraries
docker exec -it mochibridge python -m kbridge -c /data/config.yaml jellyfin-debug --minutes 5
```

- **Update:** `docker compose pull && docker compose up -d`. The image includes the browser extension, so the extension's update badge appears once the new version is running.
- **State:** `/data` holds `config.yaml`, `bridge.sqlite` (desired state, outbox, events), `youtube-cookies.txt` and `backups/`.

## Rollback after a migration

`kbridge migrate --apply` writes its backups to the `--out` folder (default under `/data/backups/`).
1. Stop the bridge.
2. Restore kechimochi from `kechimochi_full_backup.zip`: kechimochi → Settings → Import full backup. For extra safety, also copy kechimochi's data folder yourself before migrating.
3. Replace the bridge's `/data/bridge.sqlite` with `bridge-before.sqlite` from the same folder before starting it again.

## YouTube

Three inputs feed one `youtube` source (see the main README for what each covers and how accurate it is). There's one kechimochi entry per channel, and existing entries are reused when their extra_data has a matching `youtube.channel_id`. Videos without kana in the title or channel name go to Review (`unknown_language: review`). A channel can be ignored for good from Mappings.

Watch history must be turned on for the account.

### Sign-in cookies (history reader)
1. In a private window, sign in to youtube.com with the account to track only.
2. Export cookies for youtube.com in Netscape format, with an extension such as "Get cookies.txt LOCALLY" (Chrome) or "cookies.txt" (Firefox), allowed in private windows.
3. Close the private window **without signing out**.
4. Bridge → Settings → YouTube → upload the file. Delete your local copy afterwards.

The file is stored as `/data/youtube-cookies.txt` (mode 600) and never shown back. If the Status page shows the history reader **signed out**, repeat this, or let the extension do it (below).

**Why it signs out:** while YouTube is open in a browser, Google rotates that browser's sign-in cookies every few minutes and soon retires the old ones. A copy taken from a browser you keep using therefore expires shortly after the browser rotates it. With **Keep the history reader signed in** ticked, the extension re-shares the cookies within a minute of each rotation. While the browser is closed nothing rotates, so the bridge's copy keeps working. Cookies from a private window that was then closed are never rotated; if you use those, turn the extension's sharing off, or it will replace them.

### Browser extension
Download it from Settings → YouTube.
- **Chrome / Edge / Brave:**
  1. Unzip into a folder you'll keep, e.g. `~/Extensions/mochibridge` (not Downloads). The browser runs the extension from that folder, so deleting it breaks the extension.
  2. Open `chrome://extensions` and turn on **Developer mode**.
  3. Click **Load unpacked** and choose the folder.
- **Firefox:** `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on** → pick `manifest.json` from the unzipped folder. This lasts until restart; for permanent use, sign it as an unlisted add-on on addons.mozilla.org.

Then:
1. Open the extension's **Settings**, enter the bridge URL and the **API token** (bridge → Settings → YouTube → API token), and click **Save**. The browser asks for access to the bridge's address; allow it. **Test connection** should say Connected.
2. Open youtube.com signed into the account to track, open the extension popup, and click **Track this account**. Only that account is reported.
3. Optionally tick **Keep the history reader signed in**.

**Updating:** when the bridge has a newer version, the extension icon shows a red **↑** badge and the popup links the download. Unzip it over the same folder (replace the files) and click reload on `chrome://extensions`. The extension's ID is pinned in its manifest, so its settings survive even if you load it from a different folder.

### iOS Shortcut ("Log to kechimochi")
Build it once in the Shortcuts app:
1. Create a new shortcut. In its settings (ⓘ), turn on **Show in Share Sheet** and set the input to **URLs** and **Safari web pages**.
2. **Ask for Input**: Number, prompt "Minutes watched (blank = whole video)". Allow it to be empty.
3. **Get Contents of URL**:
   - URL: `https://<your bridge>/api/youtube/log`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <API token>`
   - Request Body: **JSON**, with `url` = *Shortcut Input* and `minutes` = *Provided Input*
4. **Get Dictionary Value** `message` from *Contents of URL* → **Show Notification** (or Show Result).

Use it from the YouTube app: Share → "Log to kechimochi". The phone must be able to reach the bridge (LAN or VPN).
