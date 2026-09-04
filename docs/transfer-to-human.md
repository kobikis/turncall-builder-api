# Transferring calls to a human

TurnCall's `transfer_call` builtin redirects the live Twilio leg to a phone
number — cold (blind bridge) or warm (the human hears a briefing, e.g. an
auto-generated conversation summary, before the caller is bridged). Warm mode
and the no-answer fallback need `PUBLIC_BASE_URL` on TurnCall. Transfers need
a PSTN leg: they work on Twilio calls, not WebRTC/WhatsApp.

The builder supports two patterns; the composer picks one by asking during
the interview.

## Fixed number — lives in the prompt

Tell the composer the number. It enables `transfer_call` and writes the
number + conditions into the system prompt:

> When the caller asks for a human, or you cannot resolve their issue, tell
> them you are transferring them, then call transfer_call with
> target_number "+15551234567" and a short transfer_message. If the human
> does not answer, apologize and offer to take a message.

No backend code involved. Change the number by editing the agent config.

## Dynamic number — lives in the Agent Backend

Tell the composer "it depends" (on-call rotation, caller's account manager,
department). It authors a `get_transfer_number` custom tool alongside
`transfer_call`, wired to the agent's generated backend like any other tool:

```
caller: "I want to speak to a person"
  → agent calls get_transfer_number          (your Agent Backend decides)
  → agent calls transfer_call(target_number=<result>)
```

The generated handler in `turncall-agent-<slug>/app.py` starts as a stub —
replace it with the real routing (CRM lookup, rota table, time-of-day):

```python
async def tool_get_transfer_number(args: dict, name: str = "get_transfer_number") -> dict:
    # your logic here — e.g. query the on-call rota
    return {"target_number": "+15551234567"}
```

Edits are safe: the builder git-commits the repo before any regeneration.
Tool calls are HMAC-signed by TurnCall (verified by the scaffold), so the
number source can't be spoofed by third parties.

## Deciding at call start instead

If the right human is known before the conversation begins (e.g. the caller's
account manager), managed call-init can inject it as context — but a
`get_transfer_number` tool resolved at transfer time is more robust for long
calls (shift changes) and is the default pattern here.
