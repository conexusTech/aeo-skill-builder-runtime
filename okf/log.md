# OKF Log

## 2026-09-02

- **Update** — Bundle authored. This repo had no OKF bundle before today, despite the
  workspace `CLAUDE.md` and the conversion playbook both asserting it carried one with 26
  concepts; that claim was measured on a different machine. Concepts written from the code:
  two endpoints, nine modules, the config document, the emit-only boundary, two outbound
  integrations, and the deploy runbook. Also added `scripts/okf-check.mjs` with its proof
  suite, a `gate` entry point, and CI running that same entry point.

- **Learning** — **`docs/skill-builder-runtime-plan.md` is entirely stale and contradicts
  the code.** Its header still reads "planned, not built. No skill-builder runtime exists
  yet", and it lists adding `GET /ping` as outstanding — that endpoint exists and the
  runtime is deployed. Sibling request documents in `docs/` were explicitly closed when
  fulfilled; this one never was. It is exactly the shape of document an agent reads and
  believes.

- **Learning** — **The deploy status tables in `README.md` and `CLAUDE.md` are unreliable
  by their own admission.** One previously sat at v24 while v33 was live — nine versions of
  undetected drift. Both now instruct the reader to verify with `provision.py --check`
  instead. The bundle therefore does not restate a deployed version number; the runbook
  names the command that answers it.

- **Learning** — `app/skill_builder/README.md` states that some of its own wording still
  reads from the pre-split vantage point, when this code lived inside
  `aeo-agent-service`. A self-declared partial staleness that has not been resolved.

- **Learning** — The gate notes `lint`, `typecheck` and `build` as absent: this repo has
  no linter, no type checker and no build step of its own. It does have a real pytest suite
  (299 tests, passing), which the gate runs. `pytest` is deliberately not in
  `requirements.txt` so the runtime image stays minimal, so CI installs it explicitly —
  rather than the gate quietly reporting green with no tests run.
