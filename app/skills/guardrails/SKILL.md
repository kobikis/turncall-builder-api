---
name: guardrails
description: Use when the user wants the agent to stay off certain subjects — no medical, legal or financial advice, no discussing competitors or pricing.
---

# Guardrails

Set `guardrails.prohibited_topics` to a short list of the subjects the agent
must refuse to discuss or advise on, in the user's own words ("medical advice",
"competitor pricing").

The platform enforces them as a refusal rule, so you do NOT also need to restate
them in the system_prompt.

What you SHOULD put in the system_prompt is the turn back: a refusal that leaves
the caller with nothing to do next is a bad call. Say what the agent can do
instead, and carry the conversation forward.
