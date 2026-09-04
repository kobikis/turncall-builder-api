# Contributing

Thanks for considering it. Read this before writing code — this repo carries a
different licence from the TurnCall engine, and contributing to it grants one
extra right that contributing to the engine does not.

## Licence

This repository is licensed under **FSL-1.1-ALv2** (Functional Source License,
Apache 2.0 future licence) — see [LICENSE.md](LICENSE.md). In short: you may
use, modify and redistribute it for any purpose *except* competing with us, and
each release converts to Apache 2.0 two years after it ships.

## Sign your commits (DCO)

This project uses the [Developer Certificate of Origin](https://developercertificate.org/).
It is a one-line promise that you wrote the code, or otherwise have the right to
contribute it. Sign off every commit:

```bash
git commit -s -m "your message"
```

That appends a `Signed-off-by: Your Name <your@email>` trailer. **No CLA, no
forms.** CI checks it, so an unsigned commit fails the build — `git commit
--amend -s` fixes the last one, `git rebase --signoff` fixes a series.

## Licensing of contributions

This repository is FSL-1.1-ALv2, and by contributing you also **grant Kobi
Kisos the right to relicense your contribution** under commercial or OEM terms.

That grant is what keeps the open-core model coherent. Without it, an
improvement you send could not ship in the commercial product, and — more
importantly — could not be included in the Apache 2.0 conversion this licence
promises, because that promise can only be made about code the licensor may
relicense.

Your contribution stays FSL-1.1-ALv2 in this repo and converts to Apache 2.0
on the same two-year schedule as everything around it. You keep the copyright
in what you wrote.

If you would rather not make that grant, the [TurnCall
engine](https://github.com/kobikis/turncall) is MIT, where inbound terms equal
outbound and no extra grant applies.

## Before you write code

**Bug fixes and documentation: open a PR.**

**Features: open an issue first.** Check [ROADMAP.md](ROADMAP.md) — it lists what is planned
and what is explicitly out of scope. Features here are roadmap-driven, so an
unsolicited feature PR is likely to be declined even when the code is good.

## What CI enforces

Every push and pull request runs the checks in `.github/workflows/ci.yml`.
Run them locally before pushing; there's no separate lint config to learn.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
