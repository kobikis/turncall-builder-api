# A knowledge base per document

ADR-0009 gave each agent one implicit knowledge base, linked once, and kept
TurnCall's retrieval mode out of the Console. The link it made was `prompt`:
the whole of every document in every reply's prompt. Measured on a
16.7k-character menu, that cost about a second of LLM time on every turn
(Gemini 3.8 Flash: ~0.4s to first token without it, 1.3-2.3s with it), so the
mode has to be choosable — and a menu and a one-paragraph FAQ want different
ones.

TurnCall sets the mode on an agent's **link to a knowledge base**, not on a
document. A mode per document therefore means a knowledge base per document:
each upload creates `builder-knowledge-<hex>`, links it in the chosen mode
(`prompt` unless chosen otherwise, which is what every document had), and
uploads into it. The original `builder-knowledge` stays one of the agent's
knowledge bases.

Chosen over adding a per-document mode to TurnCall, which would put a second,
narrower retrieval setting under the link's and change how the engine
assembles `prompt` and `auto` context — for a need the existing model already
expresses.

Consequences accepted:

- **One link per document.** An agent with ten documents has ten links;
  call-init sends them all (#69). `auto` documents are searched together, at
  the first `auto` link's `top_k`.
- **Delete removes the knowledge base** once it is empty, so nothing is left
  linked to nothing. A failed upload removes the one it just created.
- **A knowledge base made before this may hold several documents.** Changing
  one's mode changes all of them. When this was decided every existing one
  held exactly one document, so none was split.
- **Search covers every document**: each knowledge base is searched and the
  results merged, best first.

Supersedes ADR-0009's "documents accumulate in that one KB" and "retrieval
mode/top_k are not surfaced". Its other decisions — no registry column, the
knowledge bases found by listing the agent's project — stand.
