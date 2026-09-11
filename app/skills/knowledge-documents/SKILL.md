---
name: knowledge-documents
description: Use whenever the user has attached documents — a menu, price list, policy, FAQ. Read them before writing the system_prompt or authoring tools.
---

# Attached documents

The user's documents are readable at `/documents/`. **Read them.** This is the
difference between authoring a plausible agent and authoring theirs.

1. `ls /documents/` to see what is there.
2. `read_file` each one that matters, with `limit=1000`.
3. Write the system_prompt and the tools around what you actually found.

A menu is not "a menu": it is sections, item names, prices and modifiers. An
order tool authored after reading it takes the real item names; one authored
before takes a guess.

The agent itself receives the full contents of these documents at call time, so
the system_prompt should tell it to answer from its knowledge base rather than
repeating the content into the prompt.

Some files cannot be extracted — a scanned PDF has no text layer. `ls` shows
what is there; if reading yields nothing, say so plainly and work from what the
user tells you rather than inventing detail. The agent still receives the file.
