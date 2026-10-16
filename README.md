![EZVIZ logo](https://play-lh.googleusercontent.com/IWq0I5J6hfwMxERjdhMsHh675Ae3sk8aBlLOpEjesmfgW6BztOvFS7rceqcCOAI1aG7pcwpPXc96CWoMjt6kEg)

# EZVIZ SD Card Recordings for Home Assistant

**Early prototype / research build — not yet a complete playback replacement for EZVIZ Studio.**

> **0.1.12:** Alpha Project
A Home Assistant custom integration intended to expose EZVIZ camera SD-card recordings through **Media → EZVIZ SD Card Recordings**, with a year/month/day hierarchy and on-demand local caching.

## Current status

- [x] HACS-compatible custom integration skeleton and UI config flow.
- [x] EZVIZ account authentication using [`pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi).
- [x] Media Source year → month → day browser.
- [x] Query SD-card recording descriptors for a selected day through the legacy `search_records` API (validated with an H8c account; still needs in-integration testing).
- [x] Map full sticker serials to the short API serial returned by the account device list.
- [x] On-demand download/cache when a descriptor provides a direct HTTPS media URL.
- [x] Experimental native SD-card playback downloader for descriptors without direct HTTPS media URLs, implemented through the pinned `pyEzvizApi` cloud-playback pull-request commit.
- [ ] End-to-end validation of native playback on an EZVIZ C6N and H8c. The user has tested archive listing, but has not yet reported the result of testing the new downloader.

**Important limitation:** the native playback dependency is pinned to an open, unmerged upstream pull request. It has reports of manual validation by its author, but this integration build has not yet been tested on the user's camera. When selected, a complete clip is downloaded and remuxed before playback; true segmented direct streaming is not implemented.

The integration does **not** sync the whole SD card by default. **Media Sync is Off by default.** If enabled, it checks for new recordings every 15 minutes and caches the latest rolling 24 hours automatically, or a custom rolling window of 1–168 hours. Videos and thumbnails are stored per camera under `.storage/ezviz_sd_card_recordings/<entry_id>/video/` and `.storage/ezviz_sd_card_recordings/<entry_id>/thumb/`. Cached files are served through an authenticated Home Assistant API endpoint rather than the public `/local/` route.

Uncached clips are queued in the background. A persistent notification shows the camera name, recording timestamp, download status and progress when available; when completed it says **Download media success**. After it completes, select the clip again to play it. FFmpeg generates a JPEG thumbnail for cached clips. Per-camera **Configure Media** options control day/recording order, Media Sync, automatic or custom-hour synchronization, and an optional unique cold-storage path. Changing the path migrates existing videos and thumbnails. **Delete all cache and recording** removes only local cached files; it does not erase recordings on the camera's SD card.

The integration includes local `brand/icon.png` and `brand/dark_icon.png` assets for Home Assistant 2026.3 and newer. Older Home Assistant versions may need the icon registered through the Home Assistant brands repository.

### HACS (Recommended)
1. Click the button below to add this custom repository to HACS\
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?category=integration&repository=ezviz_sd_card_recordings&owner=renatobasa88)
2. Install "Google Find My Device" from HACS
3. Restart Home Assistant
4. Add the integration through the UI

## Install (manual)

1. Copy `custom_components/ezviz_sd_card_recordings` into Home Assistant's `config/custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**.
4. Search for **EZVIZ SD Card Recordings** and enter the EZVIZ account, password, region API hostname, camera serial, and channel.
5. Open **Media → EZVIZ SD Card Recordings → camera → year → month → day**.

The region dropdown includes Singapore/Asia (`apiisgp.ezvizlife.com`), Europe (`apiieu.ezvizlife.com`), and a custom-hostname option. Serial accepts either the full serial printed on the sticker (for example `CS-C6N-R101-1G2WF0120240506CCRRBC8610065`) or the short API serial (`BC8610065`). `pyEzvizApi` may request an MFA verification code during setup; the flow supports a prompted code, but the behavior can vary by account/region.

## Install through HACS (after publishing this repository)

Add the repository as a **custom repository** in HACS, category **Integration**. Then download it and restart Home Assistant. This project is not yet published to the HACS default store.

## What happens when a recording is selected?

1. Home Assistant asks the media source to resolve that recording.
2. The integration checks its local cache.
3. If no cached file exists, the integration starts a background download and displays a persistent notification. Select the clip again after completion.
4. The clip is downloaded to the per-camera `video/` cache, a thumbnail is generated in `thumb/`, and playback is served from an authenticated API route.

## Runtime requirements

- Home Assistant with custom integrations enabled.
- Python version supported by your Home Assistant release.
- Internet access to the EZVIZ API and cloud VTM playback service.
- FFmpeg in the Home Assistant runtime for MPEG-PS to MP4 remuxing.
- The integration manifest pins `pyEzvizApi` to the source ZIP for commit `a7710099624008e113269daed0f7f41e3d0b510e` from [open PR #266](https://github.com/RenierM26/pyEzvizApi/pull/266), because the PyPI release does not yet include `source="cloud-playback"`. This avoids requiring `git` inside the Home Assistant image. After replacing the integration, restart Home Assistant. If playback still fails, logs now include the loaded library path, reported package version, and missing `save_clip()` parameters. Never download recordings from cameras you do not own or administer.

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

1. Verify native SD-card playback/download with the pinned pyEzvizApi cloud-playback implementation on C6N/H8c models.
2. Add richer download progress reporting and cache retention policy.
4. Investigate segmented direct proxy playback to avoid downloading the whole clip before playback, and test H.265 browser compatibility.

## References

- [`pyEzvizApi`](https://github.com/RenierM26/pyEzvizApi) — account API client and SD-card record descriptor listing.
- [`ezhikstract`](https://github.com/damianjude/ezhikstract) — parser/extractor for cards mounted as a local filesystem; it is not a network downloader.
- [Home Assistant Media Source developer documentation](https://developers.home-assistant.io/docs/core/platform/media_source/).
- [Home Assistant Tapo Control media source](https://github.com/JurajNyiri/HomeAssistant-Tapo-Control/blob/main/custom_components/tapo_control/media_source.py) — useful architectural reference for an on-demand recording browser.

## License

MIT. This project is independent and is not affiliated with EZVIZ or Home Assistant.
