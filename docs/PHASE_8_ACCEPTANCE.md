# Phase 8 Acceptance

## Scope

Phase 8 packages JourneyOps for recruiter review. It does not deploy or modify production, publish a release, or
start another phase. The legacy Planner implementation remains unchanged.

## Delivered

- Complete request constraints in the Vue form: origin, budget, travelers, pace, daily window and walking limit.
- API v2 submission, real Worker/JourneyGraph progress events, structured retry and diagnostic trace ID.
- Existing source, transport, validation, human review, version, diff and rollback views retained.
- Deterministic keyless Demo mode with isolated Compose port and volumes.
- Browser-safe runtime settings; server Secret mutation disabled by default and access-code guarded when enabled.
- AMap-only browser map path with build-time domain-restricted JS credentials.
- Container log rotation, resource limits, health checks and non-root application runtime.
- Chinese/English README, Before/After architecture, upstream changelog, ADR, HTTPS examples and demo script.

## Acceptance Evidence

| Check | Evidence | Status |
| --- | --- | --- |
| Frontend production build | `npm run build` | Passed |
| Compose demo config | base + `docker-compose.demo.yaml` | Passed |
| Compose staging config | base + `docker-compose.staging.yaml` | Passed |
| Backend lint and tests | Recorded after final verification below | Pending final run |
| Offline 36-case evaluation | `docs/EVALUATION_REPORT.md` | Existing baseline; rerun pending |
| Keyless end-to-end demo | Oracle staging isolated stack | Pending deployment |
| Screenshots | `docs/assets/phase8/` | Pending deployment |
| Production isolation | production containers and data are not targeted | Enforced |
| Release tag | requires explicit owner confirmation | Not published |

## Release Boundary

The implementation may be committed and pushed to `staging`. A version tag or GitHub Release is a public
publishing action and must not be created until the repository owner explicitly approves the final release name
and contents.
