# Changelog

All notable changes to Till are written down here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version
numbers follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- How to report a security problem privately, and what the security
  model covers: [SECURITY.md](SECURITY.md).
- How to run the tests and how outside changes get in:
  [CONTRIBUTING.md](CONTRIBUTING.md).
- The planned work, in plain words: [ROADMAP.md](ROADMAP.md).
- Templates for bug reports and feature requests.
- Package details: the readme, author, project links and supported
  platforms.

## [0.1.0] - 2026-11-15

The first public release.

### Added

- Invoices and quotes: draft, mark as sent, part-paid, paid and void. A
  quote converts into an invoice, and a print view saves as a PDF.
- Double-entry books: every sent invoice, payment and expense is a
  balanced journal entry, with profit and loss and money owed by age read
  from the journal. Mistakes are corrected by reversal.
- Today: cash, this month's income and spending, what's overdue with a
  ready-to-send reminder, drafts and jobs on the calendar.
- Customers, jobs and a calendar that exports as `.ics`.
- Monthly retainers, issued with `till tick`.
- A reviews inbox for pasted reviews and your replies.
- Currency and tax rate settings.
- Backup and restore, a sample business to try (`till demo`), and an
  optional start-on-login install for Linux.
- A local JSON API at `/api`, with an optional skill for the Hermes
  assistant.

[Unreleased]: https://github.com/BenCodeland/till/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/BenCodeland/till/releases/tag/v0.1.0
