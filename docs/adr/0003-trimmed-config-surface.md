# Trimmed config surface

The Composer is given, and may only set, a trimmed subset of the TurnCall agent
schema: `name`, `system_prompt`, `first_message`, `llm` (provider+model), `voice`/TTS,
and the built-in `tools` (`end_call`, `transfer_call`). Everything else — STT,
VAD, smart turn, voicemail, S2S, analysis, custom/MCP tools, knowledge bases,
avatar — takes TurnCall defaults and is editable only via raw JSON.

Chosen over handing the LLM the full schema because the full schema bloats the
prompt and invites the model to fiddle with infrastructure a prompt-author
shouldn't touch. The cut keeps the LLM focused on the high-value behavioral
fields and keeps the editable form small. Trade-off: the composer can't, e.g.,
attach a knowledge base conversationally in V1 — a deliberate scope line, not an
oversight.
