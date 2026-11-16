# Security

## Reporting a problem

Please report security problems privately, not in a public issue:

1. Open <https://github.com/BenCodeland/till/security/advisories/new>
   (the repository's **Security** tab, then **Report a vulnerability**).
2. Say what you found, how to reproduce it, and which version or commit
   you tried.

You'll get a reply within a week. Once a fix is published, the advisory is
made public, with credit to you if you want it.

## Supported versions

Till is a young, one-maintainer project. Only the latest release gets
security fixes.

| Version | Supported |
|---|---|
| 0.1.x | yes |

Till is built and tested on **Linux**. macOS and Windows are untested, so a
problem that only shows up there may not be fixable quickly.

## What the security model covers

Till is meant for one person on their own computer.

- **Only this computer can connect.** Till listens on `127.0.0.1` only.
  Other computers on your network can't reach it.
- **There is no login.** Anyone, or any program, with an account on the
  same computer can open Till and change the books. Use it on a computer
  that is yours.
- **Other websites can't use your browser to reach it.** A change that
  your browser says comes from another site is refused, and so is any
  `Host` other than `127.0.0.1`, `localhost` or `[::1]`, which blocks
  DNS-rebinding tricks. The API only accepts JSON bodies, so a web form on
  another site can't post to it. Programs on this computer, such as
  scripts using `curl`, send none of those browser signals and are let
  through, as the API is meant for them.
- **No outside network requests of its own.** Till's own code only ever
  connects to this computer. The one exception is a command you run
  yourself: `till telegram-brief` hands today's summary to the Hermes
  assistant, which sends it through Telegram. Otherwise your books stay on
  this computer unless you copy them somewhere.

These are in scope for a report: another computer or website reaching
Till, a way round the checks above, or Till losing or corrupting the
books.

## What it doesn't cover

- Other people or programs with an account on the same computer, since
  there is no login.
- Exposing Till to a network yourself, for example with a reverse proxy
  or port forwarding. It isn't built for that. If you need to reach it
  from another device, use an SSH tunnel to the computer it runs on.
- The security of the computer itself, its backups, or the AI assistant
  you connect to the API.
