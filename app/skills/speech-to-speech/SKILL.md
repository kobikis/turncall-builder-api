---
name: speech-to-speech
description: Use when the user wants the most natural, real-time or ultra-low-latency voice, wants the agent to handle being interrupted or talked over, or names GPT-Live, OpenAI Realtime, Gemini Live, Nova Sonic, Grok or xAI. Also covers voicemail detection for outbound agents.
---

# Speech-to-speech and voicemail

## When s2s is right

Only when the user explicitly wants the most natural / real-time /
ultra-low-latency voice: "as human as possible", "no lag", "real-time". Cascade
is the default and stays the default otherwise.

Set `pipeline_mode: "s2s"` and an `s2s` block:

| Provider | Model | Voices |
|---|---|---|
| `openai` (default) | `gpt-realtime-2.1` | alloy, ash, ballad, coral, echo, sage, shimmer, verse |
| `openai_live` | `gpt-live-1` | same set as `openai` |
| `google` | `models/gemini-3.1-flash-live-preview` | Aoede, Charon, Fenrir, Kore, Leda, Orus, Puck, Zephyr |
| `aws` | `amazon.nova-2-sonic-v1:0` | matthew, tiffany, amy, lupe, carlos |
| `xai` | `grok-voice-3.0` | eve (default), ara, rex, leo, sal, or a voice of the account's own |
| `elevenlabs` | `eleven_realtime_v1_mini` | brian (default), chris, george, jessica, liam, lily, matilda, michael, sarah, will, or a voice id of the account's own |

Pick the voice from the chosen provider's set — they are not interchangeable.
Use `aws` when the user wants it running in their own AWS account, and set
`aws.region` (never credentials).

**Choose `openai_live` when interruption is the point.** `gpt-live-1` is full
duplex: it listens and speaks at the same time and decides for itself when to
stop on being talked over, where `openai` takes strict turns. Reach for it when
the user describes people interrupting, talking over the agent, thinking out
loud, background noise, or "it should feel like a real phone call". Prefer plain
`openai` when they just want low latency, since it is cheaper and better
understood.

**Choose `xai` when the user asks for Grok** (Grok Voice, from xAI). Use
`grok-voice-3.0`; `grok-voice-2.0` is deprecated, so only when they name it.
Grok has no temperature, output token limit or custom endpoint: never set
`temperature`, `max_tokens` or `base_url` on an `xai` block — TurnCall refuses
the agent if you do.

**Choose `elevenlabs` when the user asks for ElevenLabs speech-to-speech**
(Eleven Realtime). `eleven_realtime_v1_mini` is the default, cheap and fast;
use `eleven_realtime_v1_max` when the agent leans on tools or has to reason.
`extra.language` (e.g. `"es"`) sets the language it hears and answers in, and
`extra.eagerness` (`low`, `medium`, `high`) how soon it takes its turn. Never
set `base_url`, and keep `max_call_duration_seconds` at 7200 or less.

In s2s the `tts` block does not apply: the voice comes from `s2s.voice`.

## Voicemail

Enable `voicemail_detection` ONLY for outbound / dialer agents placing calls
that may reach voicemail, when the user wants a message left. Set
`voicemail_detection.enabled = true` and a `voicemail_message`.

Cascade only. **Never combine voicemail detection with s2s.** Leave it off for
inbound-only agents.
