# The frame tap: sending frames to SmoovPlay from any app

The frame tap is how an app hands its video frames straight to SmoovPlay. PS5 Remote Play already works this way:
a small patch to chiaki-ng ([`chiaki_tap/`](../chiaki_tap/)) copies each frame the PS5 sends into shared memory,
and SmoovPlay reads it from there. Any other app can write the same shared memory: a streaming client, a capture
tool, an OBS plugin, a game.

> [!NOTE]
> **Experimental.** Decoded frames are tested with a stand-in writer ([`scripts/frametap_send.py`](../scripts/frametap_send.py)),
> not yet with another app. In the launcher it's **Frame tap (for developers)** on the Play page; from a terminal, `run.py --source tap`
> (see [Try it](#try-it)). The layout below is version 2 and may still change; a change will bump the version.

## Why not just capture the app's window?

SmoovPlay can already copy another app's window (**App window** on the Play page). That works with any app and
needs nothing from it, but SmoovPlay only gets the finished picture. Through the tap, the app hands over each frame
before it's shown, with what it knows about it:

| | App window | Frame tap |
|---|---|---|
| Works with | Any app, unchanged | Apps that write the tap |
| Repeated frames (a 30 fps game on a 60 Hz output) | Guessed by comparing pictures | Exact: the app says so, or (compressed frames) each frame's size shows it |
| Frames damaged by network loss | Not known: blended into the generated frames | Flagged, and held instead of blended |
| The app's window | Must stay visible and uncovered | Can be minimised or covered |
| Delay | Plus a screen capture | Straight from the app's memory |

## Two kinds of frames

- **Compressed** (H.264 or H.265, layout version 1): what a streaming client receives, before decoding. SmoovPlay
  decodes it on the graphics card (NVDEC). This is what chiaki-ng sends. A decoded repeat is never an exact copy,
  but changes far less than a new frame, so SmoovPlay tells repeats by comparing the pictures (it learns the line
  between the two from the last few frames) without being told.
- **Decoded** (NV12 or BGRA, layout version 2): finished pictures, for an app that only has those. SmoovPlay
  copies each one to the graphics card and converts it. Repeats are the app's word on each frame if it gives one
  ([slot flags](#slots)), else SmoovPlay compares the pictures.

## Try it

```
.venv\Scripts\python run.py --source tap --display
.venv\Scripts\python scripts\frametap_send.py testdata\clip_1080p60.mp4 --hints    # in another terminal
```

Either can start first. `run.py --tap NAME` reads another name (in the launcher: set `SMOOVPLAY_TAP=NAME` before
starting it). `frametap_send.py` options:

- `--format bgra` sends BGRA instead of NV12.
- `--variable` sends a picture only when it changes.
- Without `--hints`, it doesn't say which frames are repeats.

## The shared memory

Windows only. Three named objects, all opened with "create, or open if it exists", so either side can start first:

| Object | Name | What |
|---|---|---|
| File mapping | `Local\smoovplay-tap` (any name; `run.py --tap NAME`) | The header and the frame slots: always **4096 + 64 MiB = 67,112,960 bytes** |
| Auto-reset event | the name + `-frame` | Set after each frame, so the reader wakes at once |
| File mapping (optional) | the name + `-audio` | The [sound ring](#sound-optional): 64 + 512 × 8192 = 4,194,368 bytes |

chiaki-ng's tap is named `Local\chiaki-frametap`. Create the mapping with
`CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, 67112960, name)` and map all of it. All
numbers are little-endian. Times are `QueryPerformanceCounter` values; the header carries their frequency.

## Header (the first 4096 bytes)

| Offset | Type | Field | Who writes it | |
|---|---|---|---|---|
| 0 | u32 | `magic` | writer | `0x50544643` ('CFTP') while a stream is on; 0 while the header is being written, and after the stream ends |
| 4 | u32 | `version` | writer | 1: compressed frames, fixed slots. 2: decoded frames, slots as below |
| 8 | u32 | `slot_count` | writer | Version 1: 32. Version 2: see [slots](#slots) |
| 12 | u32 | `slot_size` | writer | Version 1: 2 MiB (2,097,152). Version 2: see [slots](#slots) |
| 16 | u32 | `codec` | writer | 0 H.264, 1 H.265, 2 H.265 HDR10 (PQ, BT.2020), 16 NV12, 17 BGRA |
| 20 | u32 | `width` | writer | Pixels. 1920 × 1080 is what SmoovPlay is tuned for (its TensorRT engines are built for it) |
| 24 | u32 | `height` | writer | |
| 28 | u32 | `max_fps` | writer | Refreshes per second (60). SmoovPlay expects a 60 Hz stream |
| 32 | u64 | `write_seq` | writer | Frames written so far. Bumped after each frame is complete |
| 40 | u32 | `idr_request` | both | Compressed only: the reader sets 1 when it needs a keyframe; the writer sets 0 and sends one |
| 44 | u32 | `session` | writer | Bumped at each stream start; the reader starts over when it changes |
| 48 | u64 | `qpc_freq` | writer | `QueryPerformanceFrequency` |
| 56 | u64 | `reader_heartbeat` | reader | A `QueryPerformanceCounter` time, refreshed while SmoovPlay reads |
| 64 | u32 | `features` | writer | Bits: 1 presses buttons for the delay test, 2 holds its sound back by `audio_delay_ms`, 4 writes the sound ring, 8 *variable*: sends a frame only when the picture changes, 16 honours `quit_request` |
| 68–87 | | delay test | both | chiaki-ng only: see [`frametap.h`](../chiaki_tap/frametap.h) |
| 88 | u32 | `audio_delay_ms` | reader | How long SmoovPlay asks the app to hold its sound back, to meet the picture (feature 2) |
| 92 | u32 | `colour` | writer | NV12 only: 0 auto (BT.709 from 720 lines up, else BT.601; limited range), 1 BT.709 limited, 2 BT.709 full, 3 BT.601 limited, 4 BT.601 full |
| 96 | u32 | `event_count` | writer | Linux only: bumped after each frame and woken as a futex (the reader sleeps on it) |
| 100 | u32 | `quit_request` | both | chiaki-ng only (feature 16): SmoovPlay sets 1 when the player quits (end the stream, put the PS5 to rest) or 2 (end it only); the writer sets 0 and does so |

The rest of the 4096 bytes is reserved (zero).

**Start a stream:**
1. Set `magic` to 0.
2. Fill in the fields.
3. Set `write_seq` to 0.
4. Add 1 to `session`.
5. Set `magic` last.

**End it:** set `magic` to 0, then set the event.

## Slots

Slot *i* starts at `4096 + i × slot_size`, and frame number *n* goes in slot `n % slot_count`. Each slot has a
64-byte header, then the frame:

| Offset | Type | Field | |
|---|---|---|---|
| 0 | u64 | `seq` | The frame's number. All ones (`0xFFFF…`) while the slot is being rewritten |
| 8 | u64 | `qpc_arrival` | When the frame reached this PC, or was made. SmoovPlay measures its delay from here |
| 16 | u32 | `size` | Bytes of frame data |
| 20 | i32 | `frames_lost` | Frames lost just before this one (network loss). 0 normally |
| 24 | u32 | `recovered` | 1 for the first frame after a loss that repairs the picture (a keyframe) |
| 28 | u32 | `flags` | Bit 0: truncated (didn't fit the slot). Bit 1: a repeat of the frame before. Bit 2: new content. Neither: not known |
| 64 | | data | `size` bytes |

**Write a frame:**
1. Read `write_seq`: call it *n*.
2. In slot `n % slot_count`, set `seq` to all ones.
3. Write the other slot fields and the data.
4. Set `seq` to *n*.
5. Set `write_seq` to *n* + 1.
6. Set the event.

Use a memory barrier between the steps, and an interlocked write for `write_seq`. The writer never waits for the
reader: if SmoovPlay falls behind, it skips ahead.

SmoovPlay reads a slot, copies it, and checks `seq` again. If the slot changed while it was copying, it drops that
frame.

**Version 1, compressed frames:**
- Slots: 32 of 2 MiB.
- Frames are Annex B byte streams with start codes.
- Send the codec header (SPS/PPS, plus the VPS for H.265) at the start of the stream and with each keyframe.
- Send one frame per refresh (60 a second), repeats included, and answer `idr_request`.

**Version 2, decoded frames:**
- Rows are packed tightly, with no padding.
  - NV12 is `width × height` bytes of Y, then `width × height / 2` of interleaved Cb, Cr.
  - BGRA is `width × height × 4` bytes. Alpha is ignored.
- Slots: each slot is the frame plus its 64-byte header, rounded up to a whole MiB. As many as fit in 64 MiB, at most
  32; at least 4 have to fit.
  - 1080p BGRA: 8 slots of 8 MiB.
  - 1080p NV12: 21 slots of 3 MiB.
  - 1440p BGRA: 4 slots of 15 MiB.
  - 4K BGRA doesn't fit.

  `framegen.chiaki_tap.raw_layout(fmt, width, height)` works these out. If the header gives a layout that doesn't
  fit in 64 MiB, SmoovPlay uses version 1's.
- Send one frame per refresh, repeats included. Set flag bit 1 or 2 if you know which frames are repeats.
- If your app draws only when it likes, set feature 8 and send each picture once. SmoovPlay then puts the pictures on
  a steady 60 Hz beat, as it does for window capture.

## Sound (optional)

SmoovPlay doesn't play the app's sound: the app plays its own. If the app also copies its sound into the sound
ring (and sets feature 4), SmoovPlay's instant replay records it.

The ring is its own mapping, the name plus `-audio`, laid out as in [`frametap.h`](../chiaki_tap/frametap.h)
("Sound ring"):
- 64-byte header with magic 'CFTA', then 512 slots of 8192 bytes.
- Each slot holds interleaved signed 16-bit samples, timed by `qpc_arrival`.

If the app can hold its sound back by `audio_delay_ms` (feature 2), sound and picture stay in step. Without it, the
sound comes about SmoovPlay's delay ahead of the picture, as it does with window capture.

## Code to start from

- **Python:** `FrameTap.begin()` and `FrameTap.push()` in
  [`framegen/chiaki_tap.py`](../framegen/chiaki_tap.py) write both kinds of frame, and `AudioTap` writes the sound.
  [`scripts/frametap_send.py`](../scripts/frametap_send.py) is a complete decoded-frame writer.
  [`scripts/frametap_fake.py`](../scripts/frametap_fake.py) is a compressed one, a stand-in for a PS5.
- **C:** [`chiaki_tap/frametap.c`](../chiaki_tap/frametap.c) is chiaki-ng's writer for compressed frames and sound
  (AGPL 3.0, like chiaki-ng). [`docs/frame-tap-writer.c`](frame-tap-writer.c) is a small decoded-frame writer
  (MIT), checked against SmoovPlay's reader by `tests/test_tap_c_writer.py`.
- **The reader:** [`framegen/tap.py`](../framegen/tap.py) for decoded frames, and `ChiakiSource` in
  [`framegen/chiaki_tap.py`](../framegen/chiaki_tap.py) for compressed ones.
