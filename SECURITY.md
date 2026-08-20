# Security boundary

Learning Agent is a single-owner personal application. It does not provide
multi-user accounts, tenant isolation, or a safe environment for arbitrary
code. The supported deployment boundary is deliberately small.

## Supported deployment modes

`DEPLOYMENT_MODE=local` is the default. `backend/run.py` binds to
`127.0.0.1`, rejects non-loopback bind arguments, and the ASGI middleware also
rejects requests when a direct server launch exposes a non-loopback socket.
Host headers and CORS origins must be explicit loopback authorities.

`DEPLOYMENT_MODE=server` is intended for one personal server behind HTTPS. It
fails during configuration loading unless all of the following are present:

- `SERVER_AUTH_TOKEN`: at least 32 bytes using bearer-token characters;
- `SERVER_PUBLIC_ORIGIN`: one exact `https://host[:port]` origin;
- `CORS_ORIGINS`: exactly that public origin.

All `/api/v1` routes, including settings, files, Runs, notifications and SSE,
require either the bearer token or a signed short-lived session cookie. Cookie
writes additionally require the exact Origin plus the matching CSRF cookie and
header. Server-mode ASGI requests must also arrive with an HTTPS request scope;
a trusted TLS-terminating proxy must therefore forward the original scheme.
The static application shell is public, but it contains no user data;
all data requests remain authenticated. The current frontend does not yet
provide a polished server login screen, so server deployment remains an
advanced setup until the H8 product and release gate is complete.

Never expose local mode through port forwarding or a reverse proxy. In server
mode, terminate TLS at a trusted proxy, preserve the exact public Host header,
and do not share the bearer token in URLs, logs, screenshots, issues, or chat.

## High-risk tools

This repository does not ship a capability-attested code sandbox.
`code_execute` is therefore absent from the model tool surface and rejected at
the direct tool and durable outbox boundaries in both deployment modes. The
retained host runner is an internal compatibility primitive with fixed
interpreter paths and a minimal environment; it is not a security sandbox and
is not a supported user capability.

Web and file reads are marked `external_untrusted`. Their content cannot grant
write or notification authority. A write after untrusted input needs an exact,
consumed durable approval tied to the same Run, invocation, tool call, request
digest and live claim. Email Runs are untrusted and read-only, except for the
database-verified reply to the original Intervention through an original
delivery channel.

Web fetches resolve and pin a public unicast IP before sending, preserve the
logical Host/TLS SNI, verify the connected peer, and repeat validation for each
redirect. Wire bytes, decoded bytes, total time, encoding and media type are
bounded independently. These guards do not make downloaded content trustworthy.

## Credentials and diagnostics

Keep `.env`, databases, Context projections, backups and `data/workspace/` out
of source control. `.env` updates validate every key/value before touching the
target, reject control characters and non-regular targets, and publish a 0600
file through a locked, fsynced atomic replace.

Configured model, mail, server-auth and VAPID credentials are removed from
tool observations, checkpoints, approval projections, Run events, Context
projections, diagnostic errors and Python log records. Tool arguments that
contain a configured credential or a redaction placeholder fail closed before
creating a durable invocation. Redaction is defense in depth, not permission
to paste credentials into messages or artifacts; rotate a credential if it may
have been exposed before this boundary was installed.

Automated tests use temporary databases, synthetic credentials, mocked DNS and
HTTP transports, and do not contact real model, mail or web providers.

## Reporting a problem

Report a reproducible boundary failure through the repository's private
security-reporting channel when available. Otherwise open a minimal issue that
contains no credentials, personal data, database contents, email bodies, or
private paths, and ask the maintainer for a private handoff method.

H6 establishes the runtime boundary described above. Packaging, secret
scanning in the release gate, external installation, continuous-use evidence
and seven-day retention remain H8 work and must not be inferred from this file.
