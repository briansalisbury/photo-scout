# Security Policy

Photo Scout is a local tool with one optional network-facing part. This file
explains which is which, what a security report should cover, and how to send
one.

---

## Reporting a vulnerability

**Contact: <https://salisbury.social>**

Send a message there rather than opening a public issue. Include:

- what the flaw is and which file or endpoint it is in,
- how to reproduce it, ideally with the smallest input that shows it,
- what an attacker gets out of it.

You should get an acknowledgement within a week. Once a fix is out, credit goes
to you in the release notes unless you would rather it did not.

Please do not test against anyone else's published gallery. A local install and
a heart service you control are enough to demonstrate anything in scope.

---

## Supported versions

The `main` branch is the supported version. Photo Scout is not released on a
schedule and has no long-term support branches; fixes land on `main` and that is
where to get them.

---

## What is in scope

**The heart service (`hearts/app.py`).** This is the only component that listens
on a network. Anything that lets a visitor read data the API does not mean to
expose, write outside their own votes, crash the service, or reach the host is
in scope.

**The publish path (`photo_scout_ghost.py`).** Anything that leaks the Ghost
Admin API key or the heart admin token, sends either over a channel that does
not protect it, or uploads content the operator did not intend to publish.

**The generated gallery pages.** Both the local report and the published Ghost
page embed data derived from your file names, folder names and tags. Any input
that escapes into executable markup — script injection, event handlers, markup
breaking out of an attribute or a data block — is in scope.

**The read-only guarantee.** Photo Scout must never write, move or delete
anything under `--root`. Any input that causes it to is a security bug, not just
a defect. See CONTRIBUTING.md.

---

## What is not in scope

**Heart voting is not authenticated, by design.** A voter is identified by a
random token the browser mints and keeps in `localStorage`. Anyone can mint
another one. Clearing browser storage loses your hearts; a script can inflate a
count. This is a popularity signal on a photo gallery, not a ballot — the cost of
real identity was not worth what it buys. Reports that amount to "the counts can
be gamed" describe intended behaviour and are already in LIMITATIONS.md. A way to
crash the service, exhaust its disk, or read the admin-only data through the
public endpoints is a different matter and *is* in scope.

**Deployment mistakes.** Publishing the heart service on `0.0.0.0` instead of
loopback, leaving `HEARTS_ADMIN_TOKEN` unset, or committing a key to a repository
are operator errors. The shipped compose file, Dockerfile and proxy configs set
the safe defaults; if one of *those* is wrong, that is a bug and worth reporting.

**Scoring accuracy.** A model that misjudges a photograph is a limitation, not a
vulnerability.

**The decoders themselves.** Photo Scout hands your files to ffmpeg, Pillow and
LibRaw — hundreds of thousands of lines of native parsing code, and the largest
attack surface here by some distance. A malicious video or image that exploits
one of them runs with whatever privileges Photo Scout has. This is inherent to
processing media and is not something this project can fix; the response is to
keep those three current, which is why the version floors are what they are. If
you are ever scoring files from someone you do not trust, run it somewhere
disposable. Photo Scout itself is not sandboxed and does not claim to be.

**Third-party code.** Report issues in PyTorch, Pillow, rawpy, Flask, ffmpeg or
Ghost to those projects. If Photo Scout uses one of them in a way that makes an
otherwise harmless flaw exploitable, that part is ours — as is a version floor
in `requirements.txt` that is too low to be safe.

---

## Supply chain

Photo Scout downloads two models on first run and nothing else.

- **CLIP** is loaded from a pinned commit of `openai/clip-vit-large-patch14`
  rather than whatever the repository is currently serving. A model repository
  is mutable, and files in one can change how loading behaves.
- **The LAION-Aesthetic checkpoint** is verified against a SHA-256 before it is
  loaded, because a checkpoint is executed by the framework rather than merely
  read. A mismatch discards the file and stops the run.
- **Dependency floors** are set to the lowest release with no known advisory,
  and CI re-runs `pip-audit` weekly so an advisory published after the last
  commit still surfaces. Every push also runs Bandit and the full test suite.

A tampered model that gets past either pin, or a floor that has gone stale
against a published advisory, is worth reporting.

---

## What the tool handles

Worth knowing when judging the impact of something you have found:

- **The photo library is read-only.** No cache, sidecar or thumbnail is ever
  written inside `--root`. Everything goes to `--out`.
- **Credentials are never written to disk or logged.** The Ghost Admin API key
  and the heart admin token are read from `GHOST_ADMIN_KEY` and
  `HEARTS_ADMIN_TOKEN`, or from a flag, and only the length of a key is ever
  printed. Prefer the environment variables: a flag lands in your shell history
  and in the process list.
- **The heart database stores no personal data.** No IP addresses, no raw
  visitor tokens — only `sha256(per-install salt + token)`, so the database
  cannot be correlated with anything outside the service.
- **Folder and file names ARE published; paths are not.** The Ghost gallery
  shows each photograph's file name and its folder with any leading date
  removed, and groups the photographs into galleries named after their
  top-level folder — so anyone who can see the page can read those names. If a
  folder is called after a client, a child or an address, that goes up with it.
  What never leaves your machine is the rest of the path: the drive, the
  directories above the library, and the location of the report. The public
  heart endpoints carry opaque photo ids only; the one endpoint that returns
  paths, `/api/hearts/top`, requires the admin token.
- **`report.html` is for you, not for the web.** It carries full paths, folder
  names, dates and `file://` links by design — that is what makes it useful
  locally, and it is why the Ghost publisher builds separate markup rather than
  uploading the report. Don't serve the local report publicly.
