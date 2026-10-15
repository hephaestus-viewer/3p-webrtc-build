# WebRTC for Hephaestus

The [Hephaestus viewer](https://github.com/hephaestus-viewer/hephaestus) builds its own libwebrtc (Second Life voice)
instead of using Linden Lab's prebuilt package. This repository holds what that build needs: the pinned versions
(`VERSION`), the patches (`patches/`) and the build script (`heph-build-webrtc.py`). The packages are published as this
repository's releases, which the viewer's `heph-webrtc` vcpkg port downloads. The name and the release layout follow
Linden Lab's [3p-webrtc-build](https://github.com/secondlife/3p-webrtc-build); the build itself does not use autobuild.

- **Base:** [shiguredo/webrtc-build](https://github.com/shiguredo-webrtc-build/webrtc-build) at **m156.8078.3.2**, target
  `windows_x86_64`. Shiguredo's own patches are applied by its `run.py` as usual.
- **On top:** Linden Lab's audio patches from [secondlife/3p-webrtc-build](https://github.com/secondlife/3p-webrtc-build),
  release **m144.7559.06.20** (commit `7c51890`), rebased onto m155; all of them applied to m156 unchanged. They are in
  `patches/`, applied in the order listed in `patches/series`.

## Building

```
python heph-build-webrtc.py --viewer <Hephaestus viewer checkout>
```

It checks out shiguredo at the tag in `VERSION`, fetches the WebRTC source (first time only: about 20 GB, over an
hour), applies `patches/` on a fresh `heph/build` branch, builds, packages, and writes
`webrtc-<tag>-heph<revision>-<platform>.tar.xz` plus a `.sha256` to `<tools>\artifacts\webrtc\` (and to
`<backup>\webrtc\` when `--backup-dir` or `HEPH_BACKUP_DIR` names a backup folder). `<platform>` is Linden Lab's
platform name (`windows64` for shiguredo's `windows_x86_64`); `<tools>` is `--tools-dir`, `HEPH_TOOLS`, or
`D:\HephaestusTools`. A `heph-build.json` inside the archive records the shiguredo tag, the WebRTC commit, this
repository's commit, and each patch's SHA-256. `--check` only reports whether shiguredo has a newer release.

With `--viewer` (or `HEPH_VIEWER_DIR`), the script then points the viewer checkout's `heph-webrtc` port at the new
archive (version, SHA-512, platform); commit that change in the viewer.

The port downloads the archive from this repository's release `<tag>-heph<revision>` (for example
`m156.8078.3.2-heph1`), so **upload each new archive there** (the script prints the release name). Until it is
uploaded, local builds still work: the script copies the archive into vcpkg's `downloads` folder, where vcpkg finds it
by its SHA-512.

Needs Visual Studio 2026 with its **C++ Clang Compiler for Windows** component, and the Windows SDK that Chromium's
`build/vs_toolchain.py` names, with Debugging Tools for Windows. Since m156, shiguredo's `run.py` merges WebRTC's Rust
objects into `webrtc.lib` with that component's `llvm-ar` and `llvm-objcopy`; VS 2026 installs them under
`VC\Tools\Llvm\x64\bin`, which the script puts on PATH for `run.py`.

**Updating to a newer shiguredo release:** set `SHIGUREDO_TAG` (and `HEPH_REVISION=1`) in `VERSION`, run the script,
and fix any patch in `patches/` that no longer applies. Bump `HEPH_REVISION` for patch or setting changes on the same tag.

## The patches

| Patch | What it does | Rebase onto m155 |
|---|---|---|
| `bug_8759_workaround` | Disables one echo-canceller debug check (webrtc bug 8759) that the 48 kHz upsampling below would trip. Release builds compile it out | Applied as-is |
| `upsample-to-48khz-for-echo-cancellation-for-now` | Runs echo cancellation at 48 kHz | Applied as-is |
| `disable_mute_of_audio_processing` | Keeps audio processing running while muted, because several voice channels share one processing module | Ported: m155's `MuteStream()` is simpler than the code LL patched; same effect (never signal "output will be muted") |
| `crash_on_fatal_error` | Logs and then crashes on WebRTC fatal errors | Applied as-is |
| `windows_add_192k` | Despite the name, LL's main Windows audio patch: adds 192 kHz to the probed rates; uses `WAVEFORMATEXTENSIBLE` and falls back to the device's closest format with WASAPI auto-conversion; stops Windows ducking other apps' audio (`IAudioClientDuckingControl`); marks voice as communications audio; drives playout from a timer instead of device events | Ported: 16 of 21 hunks applied; the other 5 hit declarations that m155 hoisted to the top of `InitPlayout()`/`InitRecording()`, and were redone there. The capture-side `IAudioClient2` code was given its own block, as LL already does on the render side, so the functions' `goto Exit` paths don't jump past its initialisation |
| `windows_media_audio_category` | Uses the Media audio category in the `core_audio_utility_win` path, to avoid ducking | Applied as-is |
| `windows_format_error_message` | Readable Core Audio error messages | Applied as-is |

## Hephaestus patches

Linden Lab builds from [webrtc-sdk/webrtc](https://github.com/webrtc-sdk/webrtc) (LiveKit's fork of WebRTC), not from
Google's WebRTC, and `llwebrtc` relies on a small API and one behaviour that only the fork has. Shiguredo builds Google's WebRTC, so these
patches bring them across rather than changing `llwebrtc`, which keeps LL's and Firestorm's voice code mergeable.

| Patch | What it does |
|---|---|
| `device_observer` | webrtc-sdk's device-change API, Windows parts (from its `m144_release` branch): `AudioDeviceObserver` (`OnDevicesUpdated()`), and `SetObserver()` / `GetPlayoutDevice()` / `GetRecordingDevice()` on `AudioDeviceModule`, `AudioDeviceGeneric` and `AudioDeviceModuleImpl`. `AudioDeviceWindowsCore` registers an `IMMNotificationClient` with the device enumerator and reports device state changes (headset plugged in or removed, device enabled or disabled), as webrtc-sdk does. Two hardenings: the observer pointer is atomic (Core Audio calls back on its own thread while the viewer can clear the observer on shutdown), and registration is skipped if the enumerator failed to create. As in webrtc-sdk, `GetPlayoutDevice()`/`GetRecordingDevice()` return -1 on Windows |
| `audio_transport_while_active` | webrtc-sdk lets `AudioDeviceBuffer::RegisterAudioCallback()` replace the audio transport while media is active (with a warning); Google's WebRTC refuses. `llwebrtc` starts the microphone (for the level meter) before the voice engine registers its transport, so without this the first voice connection after login ran with no transport: a buzz in the headset (LL's timer-driven playout repeating stale audio) until voice was reset. The transport pointer is made atomic, since it is now swapped while the audio threads read it |

The fork also keeps `AudioProcessing::Config::EchoCanceller::mobile_mode`, which Google removed along with the mobile
echo canceller. `llwebrtc` only ever set it to `false` (the default), so those two lines were removed from `llwebrtc`
instead of patching the field back in.

## LL patches not carried over

| Patch | Why |
|---|---|
| `windows_add_deps`, `windows_silence_warnings` | Shiguredo's own patches of the same name; m155 has newer versions that do the same and more |
| `fix_mocks` | Test-only mocks. `GetStats` is already in m155, and `SetAudioDeviceSink` is not part of m155's `AudioDeviceModule` (nor used by `llwebrtc`) |
| `add_license_dav1d` | Only marks test-only libraries (catapult, googletest, …) as license-free for `generate_licenses.py`. Tests are off in our build, and the m155 package's `NOTICE` (25 libraries) generates without it |
| macOS patches (`airpod_fixes`, `Revert-mac-…`, `apple_prefix`) | Windows only for now; revisit for the macOS port |
| `jni_prefix`, `nacl_armv6_2`, `android_webrtc_version` | Android/NaCl only |

LL's unmerged `roxie/fix-audio-dropout` branch (an experiment from February 2026) is not included.

## Regenerating the patches

The rebase lives on branch `heph/m155-ll-patches` in the webrtc source checkout
(`<tools>\webrtc-build\_source\windows_x86_64\webrtc\src`), one commit per patch. Each file in `patches/` is
`git diff` of one commit.
