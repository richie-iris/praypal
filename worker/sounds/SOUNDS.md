# Modern Futuristic Sound Profile

A cohesive, minimalist sonic vocabulary designed for modern voice companions. Built with harmonic sine/glass overtones, zero harsh clicks, and strict amplitude caps.

## The Vocabulary

| Sound | Length | Frequency / Character | Means | Fired by |
|---|---|---|---|---|
| [`pal-chime.wav`](./pal-chime.wav) | 0.52s | Lydian Glass Triad (F5/C6/A6) | **Line connected.** Signature sonic logo | Initial connect / shield taking floor |
| [`page_flip.wav`](./page_flip.wav) | 0.12s | Haptic Glass Micro-Tap (920Hz) | **Memory captured.** Subtle confirmation tap | `db_tool` memory saves |
| [`cheery_sparkle.wav`](./cheery_sparkle.wav) | 0.28s | Ascending Crystal Triad (E6/G#6/B6) | **Scheduled action.** Reminder or future job saved | `db_tool` reminder creation |
| [`thinking_shimmer.wav`](./thinking_shimmer.wav) | 0.85s | Pulsing Ambient Harmonic Pad (3.5Hz LFO) | **Information search.** Looking up data | `web_search` queries |
| [`line_connected.wav`](./line_connected.wav) | 0.22s | Ascending Two-Tone Glass (C5 → E5) | Third party joined conference | `rt_bridge` |
| [`line_ended.wav`](./line_ended.wav) | 0.22s | Descending Two-Tone Glass (E5 → C5) | Third party disconnected | `rt_bridge` |
| [`listening_on.wav`](./listening_on.wav) | 0.18s | Soft Low-Pass Focus Pulse (554Hz) | Companion muted, silent listening mode | ScamGuard shield |
| [`listening_off.wav`](./listening_off.wav) | 0.18s | Soft Harmonic Return Pulse (740Hz) | Companion resumed speaking | ScamGuard shield |

## The two rules

Enforced in `rt_audio._earcon_allowed`, not left to call sites:

1. **Never stack.** Tool cues fire through `create_task`, so a turn that saved
   three things used to play three page-flips on top of each other — noise where
   a single *"I wrote that down"* was meant. Minimum 0.7s between any two.
2. **Never repeat quickly.** The same earcon twice inside a couple of seconds
   reads as a malfunction rather than as two events. Minimum 2.5s between two of
   the same.

A dropped cue is dropped silently, and that is safe by construction: **nothing
the caller needs may live only in a sound.** Every earcon accompanies something
she also says. They are punctuation, not sentences.

## Why `page_flip` is worth its noise

It is the chattiest cue by far — every memory write triggers it. That is
deliberate. The product's whole promise is that she remembers you, and a small
sound at the moment of writing is the only evidence a caller gets on the call
itself. Silence there would make the memory invisible until the next call.

The rate gate is what makes it bearable: one flip per turn, not one per row.

## Removed

- `chaching.wav` — nothing referenced it, and a cash-register sound is the wrong
  register entirely for a companion an older adult calls when the house is quiet.
- `iris-chime.wav` — byte-identical to `pal-chime.wav`, left over from before the
  rename. One brand, one chime.

## Adding one

Don't, unless it earns a row in the table above. A vocabulary of eight is
learnable; a vocabulary of fifteen is noise, and the caller cannot ask what a
sound meant.
