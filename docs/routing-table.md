# Routing table

This is the living starting table the orchestrator routes by. It starts as a set of researched
hypotheses and changes only when recorded evidence justifies it. Every change is logged below.

The rule is **quality first, then usage cost per finished task, then quota headroom, then speed**.

- Claude keeps a row only where it has a measured quality lead.
- Where quality is tied or unproven, the option that is cheaper per task wins. That is usually
  Codex, because GPT-6 models use 5–13x fewer tokens per task. At API list prices the matched tiers
  cost the same per token: Luna and Haiku, Sol and Sonnet.
- When two options are still close, the task goes to whichever subscription has more headroom.

## Current table

Effort is always passed explicitly and set per provider.

- Codex's `ultra` level is never used, because it starts its own subagents.
- `max` is never chosen automatically.

Usage figures are estimates:

- **Codex:** percent of a ChatGPT Plus five-hour window.
- **Claude:** percent of a Claude Max 5x session. These are provisional until the session size is
  measured locally.

| Task | First choice (effort) | Alternative (effort) | Escalate when… | Usage per task | Minutes | Confidence |
|---|---|---|---|---|---|---|
| Routine planning | GPT-6.1 Sol (high), plan reviewed by Opus 5.5 (medium) | Opus 5.5 (medium) | Ambiguous, architectural or irreversible plan: use the next row | Sol high 3–14% Plus | 4–12 | Low |
| Ambiguous or architectural planning | Opus 5.5 (medium; high if irreversible), with a time budget | GPT-6.1 Sol (high) | Astra (high) reviews irreversible plans; Fable (high) consults on architecture nobody can check by inspection | 0.4–2.7% Max 5x | 4–17 | Low |
| Single scoped coding (clear criteria, automated check) | GPT-6.1 Sol (medium) **and** Sonnet 5.5 (medium) as joint first choices, picked by headroom and run as paired tests | Haiku 5.5 (high) for a few named files under 100K context | Opus 5.5 (medium) after one failed check, or at once if the task proves ambiguous | Sol 1–2% Plus; Sonnet under 0.6% Max 5x | 2–6 | Low–medium |
| Substantial multi-file coding and debugging | Opus 5.5 (medium; high for unknown-cause bugs or a retry) | GPT-6.1 Sol (high) behind the full test suite; Sonnet 5.5 (medium) for fully specified migrations | Astra (high, then xhigh) after one failed attempt per vendor; Fable (high) last | Opus 1–9% Max 5x; Sol high 13–50% Plus | 20–60 | Medium |
| Mechanical edit (agentic) | Haiku 5.5 (medium) | GPT-6.1 Sol (low) | None; two failed checks reclassify it as scoped coding | Under 0.1% Max 5x; Sol about 1% Plus | 0.5–3 | Medium |
| Non-agentic transform (text, messages, formatting) | GPT-6 Luna (high), behind a deterministic check | Haiku 5.5 (medium) | None | About 0.05–0.2% Plus | Under 1 | Low |
| Codebase exploration (read-only) | Haiku 5.5 (medium) | GPT-6.1 Sol (low) above 100K context | Findings that drive a decision move to deep research | Under 0.4% Max 5x | 2–10 | Medium |
| Deep or web research | Opus 5.5 (high), with search and citation instructions | GPT-6.1 Sol (high) | Astra (high) cross-checks findings that drive irreversible decisions | 1–5% Max 5x | 10–35 | Low |
| Documentation | GPT-6.1 Sol (medium) | Sonnet 5.5 (high), told not to add unrequested files | Opus 5.5 (high) for architecture or security documents; Astra (medium) edits public-facing documents | Sol 3–8% Plus | 4–20 | Low |
| Security review | GPT-6 Astra (high), limited to the diff on Plus | Opus 5.5 (high) | Opus 5.5 (high) is a mandatory second reviewer for trust or authority changes | 2–10% Plus | 4–15 | Low |
| Verification and code review | Deterministic checks first, then the other vendor: GPT-6.1 Sol (high) for Claude-written work, Opus 5.5 (high) for Codex-written work | Same vendor, a different model | Astra (xhigh) for high-risk Claude-written diffs | 1–5% Plus or under 2% Max 5x | 2–12 | Low–medium |
| Hard end-to-end | Opus 5.5 (high; xhigh on retry), with a checklist and budget | GPT-6 Astra (high) on Pro plans; GPT-6.1 Sol (high) on Plus | Fable (high) last, from a reserved budget | 2.5–7% Max 5x | 45–90 | Low–medium |

The evidence behind these rows comes from two research reports kept on the `staging` branch,
"Codex and Claude model routing" and "Model routing cost and preferences". Community preference
enters only as a small prior worth a few paired runs.

## First paired tests

These rows are cheap to test and could change soonest:

1. **Scoped coding:** GPT-6.1 Sol (medium) against Sonnet 5.5 (medium). If they tie, Sol wins on
   cost.
2. **Mechanical edits:** GPT-6 Luna (high) against Haiku 5.5 (medium). If they tie, Luna wins on
   cost.
3. **Routine planning:** GPT-6.1 Sol (high) against Opus 5.5 (medium), judged by the reviewer from
   the other vendor.
4. **Documentation:** GPT-6.1 Sol (medium) against Sonnet 5.5 (high).
5. **Claude session size:** read the Claude Code usage meter before and after each run. This
   replaces the provisional Claude percentages.

## How the table changes

- **Review cadence:** at every feature merge, and at least every 2–3 hours of work, the primary
  agent reviews the new run records. It checks acceptance, rework, defects caught in review, usage
  per task and time.
- **Changing a row:** a row changes when paired evidence clears the bar.
  - A quality gap larger than noise, with enough paired cases, moves the row to the better
    option.
  - A tie moves it to the option that is cheaper per task.
- **Logging:** each change is logged below with its evidence and can be reverted. The owner
  reviews the log at each version merge.
- **Ordinary dogfood runs** show failure modes and usage, but never change a row on their own.

## Changelog

| Date | Row | Change | Evidence |
|---|---|---|---|
| 2026-10-31 | All | Initial table, rebalanced towards Codex where quality is tied or unproven | Both research reports; owner review |
