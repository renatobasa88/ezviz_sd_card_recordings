![EZVIZ logo](https://play-lh.googleusercontent.com/IWq0I5J6hfwMxERjdhMsHh675Ae3sk8aBlLOpEjesmfgW6BztOvFS7rceqcCOAI1aG7pcwpPXc96CWoMjt6kEg)

# EZVIZ SD Card Recordings for Home Assistant

**Early prototype / research build — not yet a complete playback replacement for EZVIZ Studio.**

> **0.4.0:** Alpha Project
A Home Assistant custom integration intended to expose EZVIZ camera SD-card recordings through **Media → EZVIZ SD Card Recordings**, with a year/month/day hierarchy and on-demand local caching.

## Current status

- [x] HACS-compatible custom integration skeleton and UI config flow.
- [x] EZVIZ account authentication using [`pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi).
- [x] Media Source year → month → day browser.
- [x] Query SD-card recording descriptors for a selected day through the legacy `search_records` API (validated with H8c and C6N accounts).
- [x] Map full sticker serials to the short API serial returned by the account device list.
- [x] On-demand download/cache when a descriptor provides a direct HTTPS media URL.
- [x] Native SD-card playback downloader for descriptors without direct HTTPS media URLs, implemented through the pinned `pyEzvizApi` cloud-playback pull-request commit.
- [x] Human-readable, chronologically sortable cache filenames (`HHMMSS_HHMMSS_<hash8>.mp4`).
- [ ] End-to-end validated **browser playback** — see Known issues below.

**Important limitation:** the native playback dependency is pinned to an open, unmerged upstream pull request. It has reports of manual validation by its author, and has been exercised against H8c and C6N cameras in the Singapore region, but results vary by model and firmware. When selected, a complete clip is downloaded and remuxed before playback; true segmented direct streaming is not implemented.

The integration does **not** sync the whole SD card by default. **Media Sync is Off by default.** If enabled, it checks for new recordings every 15 minutes and caches the latest rolling 24 hours automatically, or a custom rolling window with no upper bound (useful for large SD cards holding months or years of recordings). Videos and thumbnails are stored per camera under `.storage/ezviz_sd_card_recordings/<entry_id>/video/` and `.storage/ezviz_sd_card_recordings/<entry_id>/thumb/`. Cached files are served through an authenticated Home Assistant API endpoint rather than the public `/local/` route.

Uncached clips are queued in the background. A persistent notification shows the camera name, recording timestamp, download status and progress when available; when completed it says **Download media success**. After it completes, select the clip again to play it. FFmpeg generates a JPEG thumbnail for cached clips. Per-camera **Configure Media** options control day/recording order, Media Sync, automatic or custom-hour synchronization, and an optional unique cold-storage path. Changing the path migrates existing videos and thumbnails. **Delete all cache and recording** removes only local cached files; it does not erase recordings on the camera's SD card.

The integration includes local `brand/icon.png` and `brand/dark_icon.png` assets for Home Assistant 2026.3 and newer. Older Home Assistant versions may need the icon registered through the Home Assistant brands repository.

## Known issues

### HEVC playback in browsers does not work

EZVIZ VTM delivers SD-card recordings as **HEVC (H.265)** video wrapped in MPEG-PS. The integration preserves that stream whenever possible instead of re-encoding it, because transcoding to H.264 degrades the image — the missing-slice artefacts that are common in EZVIZ streams get re-encoded into permanent green blocks and a second layer of lossy compression is applied on top of the intact parts of the frame. Users have reported that the original HEVC played in VLC looks noticeably better than the transcoded H.264 result.

The trade-off is that **most cached recordings cannot be played inside Home Assistant's Media Browser**:

- **Firefox** — no HEVC support at all; clicking play shows a blank page.
- **Chrome** — depends on hardware decoding; behaviour varies by platform and often fails.
- **Safari** — supports HEVC, but relies on OS-level codecs that not every Home Assistant install can reach.

### Workaround: download and play manually

Until a browser-friendly path is implemented, use the following workflow for any recording you actually want to watch:

1. Open **Media → EZVIZ SD Card Recordings → camera → year → month → day**.
2. Select the recording. A persistent notification appears and the clip is downloaded and cached in the background.
3. Once the notification shows **Download media success**, fetch the file with one of these methods:
   - **Direct API route:** `http://<your-ha>/api/ezviz_sd_card_recordings/media/<entry_id>/<YYYY-MM-DD>/<record_key>/video`
   - **Filesystem:** copy from `.storage/ezviz_sd_card_recordings/<entry_id>/video/YYYY/MM/DD/<record_key>.mp4` via SSH, Samba, or the Home Assistant file editor.
4. Open the downloaded file in **VLC**, **mpv**, or any native player that decodes HEVC.

### Residual artefacts and missing audio

Even in a native player, some recordings are not pristine:

- **Green, black, or frozen blocks covering 1/3 to 4/5 of the frame.** These are missing slices in the source stream: VTM did not deliver all the data for those frames. The integration disables FFmpeg's error concealment (`-ec 0`) so damaged regions appear as black or frozen areas instead of bright green, but it cannot reconstruct data that was never sent. Short motion-triggered clips (under about 60 seconds) are the most affected.
- **No audio on some clips.** The stream carries MP2 audio, which MP4 containers do not support. The integration transcodes it to AAC when the header is intact; if it is not, the track is dropped and the file is saved video-only. This is a source-side limitation.
- **`decrypt_video=True` breaks downloads.** Setting `decrypt_video=True` in `save_clip` makes the VTM session fail outright. The integration hardcodes `decrypt_video=False`.
- **`cloud_client_type=4` is less stable than `3`.** Client type 3 is verified across H8c and C6N cameras in the Singapore region. Type 4 intermittently delivers incomplete slices or fails to bootstrap.

The integration logs the first 32 bytes of every raw VTM response and the strategy that succeeded (`copy+audio-aac`, `copy+silent`, `transcode-h264-aac`, `transcode-h264-silent`) so that failures can be diagnosed from the Home Assistant log.

### Breaking change: cached filenames from earlier versions

Recording cache filenames changed in v0.4.0 from an opaque 20-character hash to a sortable `HHMMSS_HHMMSS_<hash8>` format. Files stored with the old naming are not matched by the new path lookup and remain on disk as orphans.

**Before upgrading, or immediately after:**

1. Go to **Settings → Devices & Services → EZVIZ SD Card Recordings → Configure**
2. Choose **"Delete all cache and recording"**
3. Confirm

Recordings on the physical SD card are untouched. New downloads will use the new time-based naming immediately.

### HACS (Recommended)
1. Click the button below to add this custom repository to HACS\
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?category=integration&repository=ezviz_sd_card_recordings&owner=renatobasa88)
2. Install "EZVIZ SD Card Recordings for Home Assistant" from HACS
3. Restart Home Assistant
4. Add the integration through the UI

## Install (manual)

1. Copy `custom_components/ezviz_sd_card_recordings` into Home Assistant's `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**.
4. Search for **EZVIZ SD Card Recordings** and enter the EZVIZ account, password, region API hostname, camera serial, and channel.
5. Open **Media → EZVIZ SD Card Recordings → camera → year → month → day**.

The region dropdown includes Singapore/Asia (`apiisgp.ezvizlife.com`), Europe (`apiieu.ezvizlife.com`), South America (`apiisa.ezvizlife.com`), North America (`apiius.ezvizlife.com`), India (`apiiindia.ezvizlife.com`), China (`api.ys7.com`), and a custom-hostname option. Serial accepts either the full serial printed on the sticker (for example `CS-C6N-R101-1G2WF0120240506CCRRBC8610065`) or the short API serial (`BC8610065`). `pyEzvizApi` may request an MFA verification code during setup; the flow supports a prompted code, but the behavior can vary by account/region.

## Install through HACS (after publishing this repository)

Add the repository as a **custom repository** in HACS, category **Integration**. Then download it and restart Home Assistant. This project is not yet published to the HACS default store.

## What happens when a recording is selected?

1. Home Assistant asks the media source to resolve that recording.
2. The integration checks its local cache.
3. If no cached file exists, the integration starts a background download and displays a persistent notification. Select the clip again after completion.
4. The clip is downloaded to the per-camera `video/` cache, a thumbnail is generated in `thumb/`, and playback is served from an authenticated API route.
5. If the browser cannot decode the cached file (HEVC), download it and open it in a native player. See **Known issues** above.

## Runtime requirements

- Home Assistant with custom integrations enabled.
- Python version supported by your Home Assistant release.
- Internet access to the EZVIZ API and cloud VTM playback service.
- FFmpeg in the Home Assistant runtime for MPEG-PS to MP4 remuxing and thumbnail generation.
- The integration manifest pins `pyEzvizApi` to the source ZIP for commit `a7710099624008e113269daed0f7f41e3d0b510e` from [open PR #266](https://github.com/RenierM26/pyEzvizApi/pull/266), because the PyPI release does not yet include `source="cloud-playback"`. This avoids requiring `git` inside the Home Assistant image. After replacing the integration, restart Home Assistant. If playback still fails, logs include the loaded library path, reported package version, and missing `save_clip()` parameters. Never download recordings from cameras you do not own or administer.

## Privacy and security

- Credentials are stored in the Home Assistant config entry, as is common for integrations of this type.
- Session tokens are kept in Home Assistant's `.storage/ezviz_sd_card_recordings/` directory with restrictive file permissions where supported.
- Recordings and thumbnails are stored in `.storage/ezviz_sd_card_recordings/<entry_id>/video/` and `thumb/` by default and served through an authenticated API endpoint. A configured cold-storage path can place them outside the config/backup tree. Treat both the Home Assistant config directory and any cold-storage directory as sensitive.
- Do not post logs, config-entry exports, token files, MFA codes, or camera serials publicly.

## Development and tests

Pure utility tests use only the Python standard library:

```bash
python -m unittest discover -s tests -v
```

The tests do not contact EZVIZ or require camera credentials. Integration-level validation requires a Home Assistant development environment.

## Roadmap

1. Investigate segmented direct proxy playback to avoid downloading the whole clip before playback.
2. Explore browser-friendly delivery options that do not degrade image quality: an on-the-fly H.264 transcode served only to the browser (keeping the HEVC original in cache), or a client-side WebCodecs/WebAssembly HEVC decoder for the Media Browser.
3. Add richer download progress reporting and a cache retention policy.
4. Expand camera model coverage beyond H8c and C6N.

## References

- [`pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi) — account API client and SD-card record descriptor listing.
- [`ezhikstract`](https://github.com/damianjude/ezhikstract) — parser/extractor for cards mounted as a local filesystem; it is not a network downloader.
- [Home Assistant Media Source developer documentation](https://developers.home-assistant.io/docs/core/platform/media_source/).
- [Home Assistant Tapo Control media source](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control/blob/main/custom_components/tapo_control/media_source.py) — useful architectural reference for an on-demand recording browser.

## License

MIT. This project is independent and is not affiliated with EZVIZ or Home Assistant.
