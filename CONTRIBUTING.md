# Contributing to OpenRing

OpenRing is a small project. Bug reports, hardware tested-on
confirmations, and feature ideas are all welcome. Pull requests are
welcome too — but please open an issue first for anything non-trivial
so we don't both spend an afternoon on something that doesn't fit.

## Before you start

1. **Read the relevant docs.** [README.md](README.md),
   [ROADMAP.md](ROADMAP.md), and the doc under `docs/` that maps to
   what you're touching.
2. **Check the roadmap.** Many items are pre-scoped with acceptance
   criteria; pick one and say so on the issue.
3. **Look at existing patterns.** OpenRing borrows heavily from
   [ScarGuard](https://github.com/sentania-labs/scarguard); if you're
   adding a new sidecar service, the existing `notifier` is your
   template.

## Reporting bugs

Open an issue. Include:

- What happened, what you expected.
- Steps to reproduce.
- Hardware (which Pi, which camera, which host OS).
- OpenRing version (about page in the web UI, or `git rev-parse HEAD`).
- Logs (`docker compose logs` for the host stack,
  `journalctl -u openring-*` on the Pi).

## Suggesting features

Open an issue. Frame it as the user problem you're trying to solve,
not the implementation you have in mind. We'll figure out the shape
together.

## Pull requests

### Before you open one

- Open an issue first for anything non-trivial. This avoids wasted
  effort if the approach doesn't fit the charter.
- One concern per PR. Don't bundle a refactor with a feature.
- Documentation-only and small bug-fix PRs are welcome without prior
  discussion.

### Development setup

OpenRing's host stack runs as a Docker Compose stack; you don't need
a Pi to develop the web service or notifier.

```bash
git clone https://github.com/<your-fork>/openring.git
cd openring
pip install ruff mypy types-PyYAML types-requests types-redis pytest pillow
```

To work on the device-side without real hardware, the firmware tests
use `gpiozero`'s `MockFactory` to fake button presses. See
`services/doorbell-firmware/tests/`.

### Code standards

- Python 3.11 across the board.
- Type hints on every function. mypy in CI.
- Pydantic models for data structures.
- Logging via the stdlib `logging` module, structured args.
- No over-engineering. A doorbell is a doorbell.
- See [CLAUDE.md](CLAUDE.md) for the full standards list.

### Linting and type checking

These must pass before you submit a PR (CI runs the same commands):

```bash
ruff check services/detector/src services/web/src \
            services/notifier/src services/doorbell-firmware/src \
            shared

MYPYPATH=services/web/src:shared \
  python3 -m mypy services/web/src shared \
    --ignore-missing-imports --explicit-package-bases

MYPYPATH=services/notifier/src:shared \
  python3 -m mypy services/notifier/src shared \
    --ignore-missing-imports --explicit-package-bases

MYPYPATH=services/doorbell-firmware/src:shared \
  python3 -m mypy services/doorbell-firmware/src shared \
    --ignore-missing-imports --explicit-package-bases
```

### Self-review protocol (AI-assisted changes)

If you're using an AI coding assistant on a non-trivial change, run a
self-review subagent before considering the task done:

1. Run `git diff HEAD` to capture the uncommitted changes.
2. Spawn a review subagent with this prompt:

   > "Review the following diff for: correctness, error handling,
   > style consistency with the OpenRing Python conventions
   > (type hints, Pydantic models, structured logging),
   > security considerations specific to a local-only smart
   > doorbell, and any deviation from the project Charter
   > (see CLAUDE.md §Charter). Be direct about issues. Diff:
   > [paste diff]"

3. Address everything Critical and High flagged by the review before
   marking the task complete.

This applies to any change to `services/`, `shared/`, the
docker-compose files, Dockerfiles, or the config schema. It does not
apply to docs-only changes, dependency bumps without logic changes,
or whitespace.

### PR process

1. Fork, branch from `main`, name it descriptively
   (`feat/two-way-audio`, `fix/heartbeat-clock-skew`).
2. Make the change.
3. Run lint + types + tests (above).
4. Push and open the PR. Link the related issue. Keep the description
   short — the diff and the issue together should explain the change.

CI runs lint, type check, tests, and image builds on every PR.

## Architecture notes

If you're new to the codebase:

| Service | What it does | Can develop without hardware? |
|---|---|---|
| `web` | FastAPI + Jinja UI, SQLite, config management | Yes |
| `notifier` | Redis subscriber, dispatches notifications | Yes |
| `detector` | RTSP ingest, YOLO inference, event publishing | Yes (any RTSP source — even a webcam published via ffmpeg) |
| `doorbell-firmware` | Pi-side button + RTSP wrapper | Yes (mocks for GPIO; real hardware to validate) |

Services on the host communicate via Redis pub/sub. Config lives in a
single `config/openring.yml`. See
[CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) when it exists (TBD,
v0.1).

## Design decisions that aren't up for negotiation

These are intentional and won't change without a Charter-level
discussion:

1. **Docker Compose**, not Kubernetes.
2. **Single config file**, not per-service.
3. **Redis pub/sub**, not Kafka or RabbitMQ.
4. **SQLite**, not Postgres.
5. **Python 3.11.**
6. **Snapshots and clips are files on disk.**
7. **Local-only outbound traffic** — no telemetry, no cloud relay,
   no auto-update phone-home. Ever. See CLAUDE.md §Charter.

## License

OpenRing is [MIT licensed](LICENSE). By contributing, you agree your
contributions ship under the same license.
