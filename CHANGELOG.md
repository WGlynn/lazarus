# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow semantic versioning.

## [Unreleased]

### Added
- **Auto-apply (default on), reversible.** Lazarus now applies a fix when it carries a concrete edit that matches its target exactly once, backing up the original first so `lazarus undo` reverts it. Missing, ambiguous, or advisory fixes are surfaced, never guessed. Disable with `[apply] auto_apply = false`. This replaces the earlier propose-never-apply contract; the apply mechanism (`apply_fix`/`undo_last`) and the `lazarus undo` command are new.
- **v2 concurrency (async retro-audit).** A non-blocking launcher (Stop / PostToolUse) spawns a detached background runner that runs the identical v1 engine off the critical path and writes findings to a pending queue; an injection hook surfaces them on the next turn. The v1 sync path is untouched and stays authoritative when selected. Includes the launcher, runner, pending queue, injector, and an opt-in shift-left pre-gate.
- **Trigger policy (`[async.trigger]`, opt-in, off by default).** Gates the expensive judge on the cheap Sonar signal so cost tracks risk density rather than a clock or token count. Sonar runs on every work-unit; the judge fires only when the top Sonar score clears a risk-weighted bar (high-risk work-units get a lower bar). A ledger-driven controller adaptively tunes the bar from the SURFACED/DECLINED accept-rate and persists it beside the ledger.
- **Shadow sampling for the trigger gate (`shadow_epsilon`, opt-in).** The accept-rate controller was blind to false negatives (real catches in the population it skips); shadow sampling force-judges a deterministic fraction of below-bar units to measure that recall and lowers the bar when it clears a floor.
- **Proactive priming (`lazarus prime`).** The preventative dual of `audit`: surfaces the buried rules relevant to UPCOMING work (a prompt, a plan, a diff) up front, so a rule is available before the work instead of caught after. Offline, no API key, same engine, new trigger point.
- Offline async-cycle demo (`examples/async_demo/run_async_demo.py`) and a runnable command in the README, matching the v1 demo's zero-key green oracle.
- Continuous integration (GitHub Actions): ruff + pytest + both offline demos across Python 3.9 / 3.11 / 3.13.
- `CONTRIBUTING.md`, `SECURITY.md`, and this changelog.

### Fixed
- Corrected the placeholder project URLs to the real repository (`github.com/WGlynn/lazarus`).
- Repository is lint-clean under ruff (unresolved `NoReturn` annotations, an unused import, and a dead assignment).

## [0.1.0]

### Added
- Initial release. **Sonar** (offline keyword-overlap perception over a rule/primitive/memory corpus) and **Lazarus** (a per-candidate Claude judge that asks whether a buried rule would have changed the finished work and proposes fixes). Append-only ledger with anti-nag suppression. Offline, credential-free demo. Opt-in Claude Code hooks. MIT licensed.
