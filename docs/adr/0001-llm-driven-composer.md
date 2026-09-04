# LLM-driven composer with structured output

The Composer is a single LLM loop (Claude), not a hardcoded question wizard.
Each turn we send the message history plus the trimmed TurnCall agent schema and
force a structured-output tool `compose(action, question?, agent_config?)`: the
model returns `action: "ask"` with one follow-up question while anything is
ambiguous, or `action: "finalize"` with a complete config once it has enough.

Chosen over a deterministic decision-tree wizard because the whole value of
cloning Vapi Composer is that the LLM both *writes the system prompt* and
*decides what it still needs to ask* — a fixed wizard is a different, dumber
product. Trade-off: non-deterministic flow and LLM cost per turn, accepted
because the loop is ~50 lines and the quality of the generated prompt is the
point.
