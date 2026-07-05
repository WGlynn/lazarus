# Security Policy

## What Lazarus touches

- **It reads** the rule/primitive/memory files you point it at (via `corpus_path` and the configured globs) and the work-unit it audits (a diff, a response, a decision).
- **It applies fixes to your files by default, reversibly.** When a fix carries a concrete edit that matches its target exactly once, Lazarus applies it and backs up the original first, so `lazarus undo` restores it. Only unambiguous, uniquely-matched edits are ever written; a missing or ambiguous match is surfaced, never guessed. Disable entirely with `[apply] auto_apply = false`. Every write is reversible from the backup.
- **Sonar is fully local.** The perception/recall stage makes no network calls and needs no API key.
- **The judge is the only egress.** When the Lazarus judge runs, it sends the work-unit text and the shortlisted candidate rules to the Claude API. If your work-units or rule files contain secrets, they would be included in that request. Point the corpus at rules, keep secrets out of audited work, and treat the judge like any other call to a third-party model API.
- **API key handling.** The judge reads the key from the environment (`ANTHROPIC_API_KEY`). The key is never written to the ledger, the pending queue, the spool, or any log.
- **The ledger and pending queue** are local append-only JSONL files. They store rule ids, verdicts, and proposed patches, not credentials.

## Hooks

The Claude Code hooks are opt-in and installed deliberately (see `INSTALL_HOOKS.md`). A hook runs on your machine on Stop / PostToolUse / UserPromptSubmit; review what you wire before enabling it, especially any pre-gate that adds a synchronous judge call.

## Reporting a vulnerability

Please report suspected vulnerabilities privately via a GitHub security advisory on the repository, or by opening an issue that does not disclose exploit detail and asking for a private channel. We will acknowledge and respond as quickly as we can.
