# Contributing to Learning Agent

Thanks for helping improve Learning Agent. The project is evolving from a Hy3-powered personal learning application into a reusable, evidence-driven Agent Harness while keeping the current product path stable.

## Before you start

For non-trivial changes, please open or join an Issue first. This is especially important for changes to the Agent Runtime, tool protocol, state transitions, memory/context semantics, scheduling, or model-provider behavior.

Good contributions include:

- reproducible bug fixes with regression tests;
- focused improvements to tool/runtime reliability;
- clearer extension points for model providers, tools, memory, scheduling, and notification adapters;
- documentation that makes a real workflow easier to reproduce;
- tests for recovery, idempotency, state transitions, and boundary conditions.

Please avoid large speculative rewrites or adding provider-specific logic directly to core runtime code when a narrow interface can contain it.

## Development setup

Start from the repository README and `.env.example`. Keep credentials out of commits and tests.

Backend tests:

```bash
pytest
```

Frontend validation should include a production build from `frontend/`:

```bash
npm install
npm run build
```

If your change affects a real integration, document the smoke test you performed in the PR. Tests should not require contributors to own private production credentials.

## Branch and pull-request workflow

1. Create a focused branch from the current development base.
2. Keep one logical change per PR.
3. Add or update tests for behavioral changes.
4. Run the relevant backend/frontend checks locally.
5. Explain the user-visible or maintainer-visible reason for the change, not only the implementation.
6. Call out migrations, compatibility changes, new environment variables, and security implications.

A useful PR description should answer:

- What changed?
- Why is this change needed?
- How was it validated?
- What could regress?
- Does it change persisted state, API contracts, or provider/tool behavior?

## Runtime design principles

When touching the Agent Harness, preserve these boundaries unless an Issue explicitly agrees to change them:

- **Agent policy** decides what to do.
- **Tools** perform bounded actions through typed contracts.
- **Context and memory** provide scoped information rather than hidden global state.
- **Durable state** owns recovery/idempotency semantics.
- **Schedulers/notifiers** trigger work but should not duplicate Agent policy.
- **Model providers** translate provider-specific transport/capability details without absorbing Agent behavior.

Provider capability differences should be explicit. Do not emulate unsupported behavior silently just to make two providers look identical.

## Tests and reliability

Regression tests are expected for bug fixes when practical. Pay particular attention to:

- retry and idempotency behavior;
- interrupted runs and recovery;
- archived/read-only state;
- queued user intent;
- scope isolation between users, plans, runs, and sub-agents;
- tool-call validation and malformed model output;
- database migrations and foreign-key integrity.

Do not weaken an existing assertion merely to make a new implementation pass.

## Security and privacy

Never commit API keys, mail credentials, tokens, user data, or private learning artifacts. New integrations should use environment configuration and document the minimum required permissions.

If you believe you found a vulnerability that could expose credentials or private user data, do not include working secrets or sensitive data in a public Issue. Contact the maintainer privately first.

## Documentation

Update README/docs when a contributor-facing command, environment variable, extension point, migration, or supported guarantee changes. Keep roadmap items clearly separated from implemented behavior.

## Review

Maintainers may ask for a smaller scope, more tests, or a design discussion before merging. That is normal: the goal is to keep the harness understandable and safe to extend, not simply to maximize feature count.
