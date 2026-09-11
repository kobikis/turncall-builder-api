---
name: speech-to-speech
description: Use when the user wants the most natural, real-time or ultra-low-latency voice, or names OpenAI Realtime, Gemini Live or Nova Sonic. Also covers voicemail detection for outbound agents.
---

# Speech-to-speech and voicemail

## When s2s is right

Only when the user explicitly wants the most natural / real-time /
ultra-low-latency voice: "as human as possible", "no lag", "real-time". Cascade
is the default and stays the default otherwise.

Set `pipeline_mode: "s2s"` and an `s2s` block:

| Provider | Model | Voices |
|---|---|---|
| `openai` (default) | `gpt-4o-realtime-preview` | alloy, ash, ballad, coral, echo, sage, shimmer, verse |
| `google` | `models/gemini-3.1-flash-live-preview` | Aoede, Charon, Fenrir, Kore, Leda, Orus, Puck, Zephyr |
| `aws` | `amazon.nova-2-sonic-v1:0` | matthew, tiffany, amy, lupe, carlos |

Pick the voice from the chosen provider's set — they are not interchangeable.
Use `aws` when the user wants it running in their own AWS account, and set
`aws.region` (never credentials).

In s2s the `tts` block does not apply: the voice comes from `s2s.voice`.

## Voicemail

Enable `voicemail_detection` ONLY for outbound / dialer agents placing calls
that may reach voicemail, when the user wants a message left. Set
`voicemail_detection.enabled = true` and a `voicemail_message`.

Cascade only. **Never combine voicemail detection with s2s.** Leave it off for
inbound-only agents.
