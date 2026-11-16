# Contributing

Thanks for taking an interest in Till. Bug reports, ideas and small fixes
are all welcome.

## Reporting a bug or asking for something

Open an issue and pick the template that fits. For a security problem,
please don't open a public issue: see [SECURITY.md](SECURITY.md).

What's already planned is in [ROADMAP.md](ROADMAP.md), so check there
before asking for a feature.

## Running the tests

You need Python 3.11 or newer.

```bash
git clone https://github.com/BenCodeland/till.git
cd till
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests use temporary books and never touch `data/`. The installer tests
run against a fake home directory and a fake `systemctl`, so they don't
change your login session. Tests that start Till use their own ports, not
the default 7740.

## How changes get in

Till's history is kept in the maintainer's own working copy and published
here from it. Because of that, **pull requests are read but not merged**
on GitHub. If you send one, the maintainer applies the change in the
working copy, tests it there and publishes it in a later commit, with
credit to you in the changelog.

That makes a few things more useful than usual:

- A clear description of the problem, and a test that shows it, matter
  more than the exact code.
- Keep a change small and about one thing.
- Open an issue first for anything bigger than a fix, so no one spends
  time on something that won't fit.

## Ground rules for code

- Money is integer cents and quantities are thousandths; no floats.
- The books are double-entry: every posting balances, and mistakes are
  corrected by a reversal, never by changing or deleting an entry.
- No outside network requests, and no new dependency without a good
  reason.
- Every change comes with tests, and the whole suite passes.

By contributing, you agree that your work is published under the
[MIT licence](LICENSE).
