# Native SD-card playback research notes

## Known boundary

The `pyEzvizApi` listing API returns descriptors for recordings stored on a camera's SD card, and a direct HTTPS media URL is not guaranteed. An open PR, [#266](https://github.com/RenierM26/pyEzvizApi/pull/266), adds `EzvizClient.save_clip(source="cloud-playback")` for requesting an SD-card playback range over VTM and writing MPEG-PS. Version 0.1.4 pins that PR's reviewed commit and remuxes to MP4 with FFmpeg. The PR is not merged or published to PyPI, so this adapter remains experimental.

Do not use `pyezvizapi stream dump` as a substitute for archive playback: that command captures the camera's live VTM stream, not the selected historical SD-card segment.

## Recommended next experiment on the owner's C6N

1. From a trusted Python environment with `pyezvizapi==1.0.5.0`, authenticate and query a short time range that is known to contain a recording:

   ```bash
   pyezvizapi --token-file ezviz_token.json --json sdcard_videos \
     --serial CAMERA_SERIAL --channel 1 \
     --start-time "2026-10-16T04:00:00" \
     --stop-time "2026-10-16T04:05:00"
   ```

2. Keep the raw descriptor locally. Before sharing for debugging, remove serial numbers, account identifiers, tokens, media keys, access tickets, signed URLs, and any camera/location labels.
3. Confirm whether the descriptor includes a stable record identifier, start/end time, media source address, stream protocol, channel, and codec information.
4. Compare an EZVIZ Studio/app playback session against the descriptor. Determine which local or cloud endpoint the official client asks to start playback and how it signals seek/stop.
5. Implement a dedicated `NativeSdCardDownloader` provider with bounded duration, cleanup in `finally`, partial-file handling, integrity/codec validation, and no logging of payloads or credentials.
6. Add a small sanitized fixture and offline tests before enabling native download by default.

## Acceptance criteria for the downloader

- Select a record by stable identifier and retrieve that exact interval, not the live stream.
- Preserve the camera's native video/audio codec where possible; use FFmpeg remuxing rather than transcoding unless compatibility requires it.
- Fail without publishing a partial cache file if authentication, capture, decryption, or remuxing fails.
- Do not write to or modify the SD card.
- Test on the C6N and at least one other supported model before documenting broader compatibility.


## Implementation notes

- Native descriptors are formatted as Home Assistant-local/camera API wall-clock `YYYYMMDDTHHMMSSZ` strings. A live C6N probe showed UTC-converted timestamps returned VTM result `5402` with no session, while the same wall-clock timestamps returned result `0` and media packets. Despite the trailing `Z`, do not convert the descriptor timestamps to UTC before the playback request.
- `save_clip(source="cloud-playback", output_format="mpegps", decrypt_video=True)` writes a temporary MPEG-PS file.
- FFmpeg remuxes using stream copy (`-c copy`) without transcoding, and only publishes the MP4 after successful validation.
- Temporary partial files are removed after success or failure. This is on-demand full-clip download, not a true streaming proxy.
- If Home Assistant does not replace its already installed PyPI version with the VCS dependency, logs will say the `save_clip` native playback method is unavailable; verify dependency installation after restart.


## Dependency mismatch seen in Home Assistant

If logs show `save_clip() got an unexpected keyword argument 'cloud_playback_begin_time'`, Home Assistant imported a `pyEzvizApi` build that does not match the source commit declared by this integration. The pinned commit includes `cloud_playback_begin_time` and `cloud_playback_end_time` in `EzvizClient.save_clip()`. Version 0.1.5 uses a direct source ZIP instead of a Git VCS URL, so no `git` executable is needed to install the dependency. If the method-signature guard still fails after restart, check the logged import path and force-install the same archive inside the Home Assistant container.


## Preserve API wall-clock time for VTM playback

The owner's C6N probe confirmed the playback endpoint expects the same wall-clock time returned by `search_records()`. For a recording returned as `2026-10-09T09:06:23` in `Asia/Jakarta`, the successful VTM timestamp is `20261009T090623Z`; converting it to `20261009T020623Z` yields result `5402`, no session, and no media. The integration now normalizes record datetimes to Home Assistant's configured timezone, then formats those wall-clock fields without converting to UTC. Offline tests cover UTC-aware input converted to the configured local wall clock and already-local input.
