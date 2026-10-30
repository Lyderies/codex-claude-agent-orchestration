# Start with Opus, spend Sol, ration Astra

As of 31 October 2026, a deterministic orchestrator that applies "quality first, then subscription usage cost, then quota headroom" should not split the two vendors by job type. It should send most judgment-heavy work to Claude and use Codex for cross-vendor review, as the general alternative, and as the paid escalation tier. The starting table is:

- **Claude Opus 5.5 at high effort** for planning, substantial coding and debugging, research, and hard end-to-end runs.
- **Claude Haiku 5.5 at medium** for bounded mechanical edits.
- **Claude Sonnet 5.5 at high** for documentation.
- **GPT-6.1 Sol at high** as the default alternative in every row and as the reviewer of Claude-written work.
- **GPT-6 Astra** as first choice only for defensive security review, and otherwise as the escalation for hard or high-risk work.
- **Claude Fable 5.1** as a budgeted last resort. It scores *below* Opus 5.5 on independent agentic benchmarks.

The quality ordering rests on Terminal-Bench 4.0 runs from three independent evaluators that include each vendor's own agent harness. Those runs put Opus 5.5 and Sonnet 5.5 about 4–10 points ahead of Astra and GPT-6.1 Sol, which are statistically tied with each other. The GPT-6 models, however, use **5–13x fewer tokens** and finish agentic tasks in about half the wall-clock time, so the cost and headroom tie-breaks often favour Codex. Neither vendor publishes per-model subscription multipliers that work across both vendors. OpenAI documents Codex credit ratios (Astra 5x GPT-6.1 Sol, Terra 0.5x, Luna 0.05x); Anthropic publishes none for Claude Max. The orchestrator must therefore measure its own cost as the share of each client's binding usage window consumed per accepted task, a figure both clients expose programmatically. Confidence is medium for the coding rows. It is low for research, documentation, security and review, because no independent benchmark covers the current model generation on those tasks. Treat the table as a prior that the orchestrator's own paired runs should overwrite within weeks.

## Harness-inclusive benchmarks put Claude four to ten points ahead on agentic coding

**The classic leaderboards are no help.**

- **SWE-bench Verified.** An OpenAI audit found **59.4% of 138 hard tasks flawed**, plus signs of training-data leakage. OpenAI then dropped the benchmark ([AI Weekly](https://aiweekly.co/alerts/openai-drops-swe-bench-verified-backs-pro-after-flaw-audit)).
- **SWE-bench Pro.** About **30% of its public split has breaking issues**, and OpenAI retracted its own recommendation of it ([NeuronFeed](https://neuronfeed.com/news/openai-finds-30-of-swe-bench-pro-coding-tasks-contain-evaluation-flaws)).
- **Scale's standardized SWE-bench Pro board** lists none of the nine models in scope ([Scale SEAL](https://labs.scale.com/leaderboard/swe_bench_pro_public)).
- **METR's** pre-deployment review of Opus 5.5 gives no time-horizon figure ([METR](https://metr.org/blog/2026-09-22-claude-opus-5-5/)).

What remains usable falls into three tiers:

1. **Multi-evaluator agentic results that agree in direction.** These are good enough for tiering: Terminal-Bench 4.0 (TB4) as run by the public tbench.ai board, Vals AI and Artificial Analysis (AA), plus the Vals Index coding components.
2. **Single-evaluator results.** Use these only as tie-breakers: Epoch's capability index, MacroscopeBench, SlopCodeBench and small practitioner tests.
3. **Vendor launch charts.** Ignore these for routing. Examples include SWE-bench Pro claims near 90%, DeepSWE and BrowseComp.

The table below puts the tier-one numbers side by side. The public board runs Claude inside Claude Code and the GPT models inside Codex, which is exactly the harness pairing this orchestrator will use.

| Model | TB4 public board (own harness, max) | TB4 Vals | Vals Index v2.2 | Vals cost / test | Vals tokens / test |
| --- | --- | --- | --- | --- | --- |
| Claude Opus 5.5 | 64.85% | 65.15% | 70.18% | $28.12 | 59.46M |
| Claude Sonnet 5.5 | 61.82% | 64.14% | 70.85% | $18.70 | 78.59M |
| GPT-6 Astra | 58.18% | 59.60% | 65.02% | $21.28 | 8.15M |
| GPT-6.1 Sol | 58.2% ±3.1 (disputed row) | 55.05% | 63.94% | $3.61 | 9.40M |
| Claude Fable 5.1 | 57.88% | 58.08% | not listed | — | — |
| GPT-6.1 Terra | not listed | 49.49% | 60.47% | $2.92 | 17.44M |
| GPT-6 Sol (superseded) | 49.39% | 44.44% | not listed | — | — |
| Claude Haiku 5.5 | not listed | 36.36% | 58.73% | $3.17 | 38.15M |
| GPT-6 Luna | 16.36% | 13.64% | 52.32% | $0.45 | 17.52M |

Sources: public-board rows mirrored by [BenchLM](https://benchlm.ai/benchmarks/terminal-bench-4); the GPT-6.1 Sol public row and margins from [CodingFleet](https://codingfleet.com/blog/terminal-bench-4-leaderboard-2026/); Vals figures from the [Vals Index](https://www.vals.ai/benchmarks/vals_index) and [Vals GPT-6.1 Sol page](https://www.vals.ai/models/openai_gpt-6.1-sol).

The direction is consistent: **Opus ≈ Sonnet > Astra ≈ GPT-6.1 Sol ≈ Fable > Terra > GPT-6 Sol > Haiku >> Luna**. The Claude lead over GPT-6.1 Sol is about 6.6 points on the public board and 10 points on Vals. Both gaps exceed the roughly ±3-point margins on top entries. Much of that lead belongs to the harness rather than the model. In AA's neutral mini-SWE-agent harness, Opus 5.5 scores **59.6–60%** against **59.1%** for Astra ([CodingFleet](https://codingfleet.com/blog/terminal-bench-4-leaderboard-2026/); [AA Opus 5.5 xhigh vs max](https://artificialanalysis.ai/models/comparisons/claude-opus-5-5-xhigh-vs-claude-opus-5-5)). Harness choice alone moves Sonnet 5.5's TB4 score across an **8.8-point** range. For an orchestrator whose children run inside Claude Code and Codex, the harness-inclusive numbers are the right ones. The neutral-harness numbers are a warning not to read the gap as raw model intelligence.

The table also shows the cost side. On Vals, the GPT-6 models use **8–9 million tokens per test against 59–79 million for Opus and Sonnet**, and finish in 33–44 minutes against about 1 hour 25 minutes ([Vals Index](https://www.vals.ai/benchmarks/vals_index)). The public TB4 run cost about **$0.6k for GPT-6.1 Sol, $3.3k for Astra, $4.7k for Opus 5.5 and $7.3k for Sonnet 5.5** at API prices ([CodingFleet](https://codingfleet.com/blog/terminal-bench-4-leaderboard-2026/)). That works out to about $10, $57, $73 and $118 per TB4 point respectively. At maximum effort, AA measures time to first token at **644 s for Opus 5.5, 411 s for Sonnet 5.5 and 277 s for GPT-6.1 Sol** ([AA GPT-6.1 Sol](https://artificialanalysis.ai/models/gpt-6-1-sol); [AA Haiku vs Sonnet](https://artificialanalysis.ai/models/comparisons/claude-haiku-5-5-vs-claude-sonnet-5-5)). Max effort therefore suits no short-turn delegation.

Within each vendor, the evidence separates the models clearly.

- **GPT-6.1 Sol versus Astra.** On the Codex side, GPT-6.1 Sol buys nearly all of Astra's capability. AA's Intelligence Index gives Astra 57 and Sol 55 at max ([AA Astra](https://artificialanalysis.ai/models/releases/gpt-6-astra); [AA GPT-6.1 Sol](https://artificialanalysis.ai/models/gpt-6-1-sol)). Endor Labs' 179-task Codex run gives **82.1% versus 77.7% functional pass, a near-tie on secure pass (34.6% versus 34.1%), and roughly one-fifth the cost** ([Endor Labs](https://www.endorlabs.com/learn/gpt-6-1-sol-on-codex-astra-level-security-a-third-faster-zero-cheating)).
- **GPT-6 Sol.** It is the same price as 6.1 Sol and clearly weaker, so it does not belong in the table.
- **Sonnet 5.5 versus Opus 5.5.** They tie on composite indices, and Sonnet leads Vals Code Migration (**87.4% versus 80.5%**). Opus leads clearly on knowledge and hallucination-sensitive evaluations. On HLE-Diamond, Opus scores 64–68% against 53% for Sonnet. On AA-Omniscience, Opus scores 43–48 against 30 for Sonnet ([AA Opus 5.5](https://artificialanalysis.ai/models/comparisons/claude-opus-5-5-xhigh-vs-claude-opus-5-5); [AA Haiku vs Sonnet](https://artificialanalysis.ai/models/comparisons/claude-haiku-5-5-vs-claude-sonnet-5-5)).
- **Sonnet's parity costs tokens.** Sonnet reaches parity only at max effort, where it emits **about 285k output tokens per task against 173k for Opus**. Its per-token discount evaporates there.
- **Haiku 5.5.** It scores 90.4% on Vals Vibe Code but collapses on TB4 (33–36%) and on Code Migration (50.2%). It is a short-task model.
- **Luna.** Its vendor DeepSWE score of 66.6% contrasts with an independent TB4 score of 13.6–16.4%. That gap is the clearest illustration of why vendor charts are excluded from routing.
- **Fable 5.1.** Anthropic itself says Opus 5.5 "performs at the level of Claude Fable 5.1 on most work" ([Anthropic](https://www.anthropic.com/news/claude-opus-5-5)). METR calls Opus 5.5 "an incremental improvement" over Fable ([METR](https://metr.org/blog/2026-09-22-claude-opus-5-5/)). On the public board, Fable trails Opus by seven points.

Outside coding, the evidence thins sharply.

- **Deep research.** BrowseComp numbers are vendor-only, saturated near 90%, and include no Opus 5.5 entry ([OfficeChai](https://officechai.com/ai/gpt-6-astra-benchmarks/)).
- **Code review.** The only cross-model benchmark covers Astra and the previous-generation Opus 5 ([Macroscope](https://macroscope.com/content/ai-code-review-benchmark-best-models)).
- **Security detection.** Frontier models, including Astra, Opus 5.5 and Fable 5.1, refuse **at least 98%** of CyberGym end-to-end tasks, so that benchmark cannot rank them ([AA Cyber Index](https://artificialanalysis.ai/articles/artificial-analysis-cyber-index)).

Confidence follows that gradient: medium for the coding ordering, low for every non-coding row.

## The starting table routes judgment to Opus and review across vendors

The table applies the routing rule literally, with three conventions:

- **What counts as a quality win.** A model wins on quality only when independent evidence shows a gap larger than benchmark noise (roughly 3 TB4 points), or several sources agree in direction.
- **Ties.** Otherwise the candidates count as tied, and subscription cost decides, then headroom.
- **Availability.** An exhausted window is an availability gate that applies before the ordering, not a tie-breaker. A model that cannot run has no quality.

"Alternative" means the next choice when the first is unavailable or a quality tie is broken by cost or headroom. "Escalate" names the condition for moving up to each of the three expensive tiers.

| Task type | First choice (effort) | Alternative (effort) | Escalation to Opus 5.5, GPT-6 Astra or Fable 5.1 | Confidence |
| --- | --- | --- | --- | --- |
| Orchestration and planning | Opus 5.5 (high), with a wall-clock budget signal | GPT-6.1 Sol (high) | **Opus:** already first; return to it when Claude headroom recovers. **Astra (medium):** independent critique of irreversible or contested plans. **Fable (high):** only after an Opus plan has failed review twice and cannot be checked by inspection. | Low–medium |
| Substantial coding and debugging | Opus 5.5 (high; medium for well-specified changes) | GPT-6.1 Sol (high). Sonnet 5.5 (medium) when only the Opus family limit binds, or for large migrations | **Opus:** when a Sol-run attempt fails tests or review. **Astra (high, then xhigh):** after one failed attempt per vendor, or when a mistake is costly to undo. **Fable (high):** after Opus and Astra both fail and the change is too large to verify by eye. | Medium |
| Bounded mechanical coding | Haiku 5.5 (medium), with an explicit "run a real check" instruction | GPT-6.1 Sol (low). GPT-6.1 Terra (low or medium) as a shadow candidate | **None.** Two failed verifications reclassify the task as substantial coding. Never use Luna for agentic edits or Haiku at low. | Medium (Haiku); low (Terra) |
| Research and deep research | Opus 5.5 (high), with explicit search and citation instructions | GPT-6.1 Sol (high) | **Opus:** already first. **Astra (high):** independent cross-check before a finding drives an irreversible decision. **Fable:** not warranted; no evidence of benefit. | Low |
| Documentation | Sonnet 5.5 (high), with a "no unrequested files" scope instruction | GPT-6.1 Sol (medium). Terra (medium) as a shadow candidate | **Opus (high):** architecture or security documents where factual fidelity dominates. **Astra (medium):** final edit of public-facing or decision documents. **Fable:** not warranted. | Low |
| Security review (defensive, source-level) | GPT-6 Astra (high) | Opus 5.5 (high) | **Opus (high):** mandatory second reviewer for trust or authority changes, or when Astra refuses or is rerouted. **Astra (xhigh):** critical surfaces. **Fable:** no; its safeguards target cyber content. Never use Haiku or Sonnet. | Low |
| Verification and code review | Opposite family from the writer: GPT-6.1 Sol (high) for Claude-written work; Opus 5.5 (high) for Codex-written work | Same vendor, different model, when the opposite pool is constrained: Fable 5.1 (high) for Opus-written work; Astra (high) for Sol-written work | **Astra (xhigh):** precision-critical or high-risk Claude-written diffs. **Opus:** already first for Codex-written work. **Fable:** fallback reviewer only. | Low–medium |
| Hard end-to-end tasks | Opus 5.5 (high; xhigh on retry), with a task checklist, budget and independent end-to-end check | GPT-6 Astra (high; xhigh on retry) | **Fable (high):** after Opus and Astra fail, or when the output cannot be verified by inspection, within a reserved share of the Fable weekly cap. | Low–medium |

### Why each row lands where it does

**Planning.** Every's reviewers found Opus 5.5 "strong on plans, personas, negotiation and analysis". They also saw it run out of time producing training materials instead of a requested schedule, and warned that "Opus will take the afternoon if you let it" ([Every](https://every.to/vibe-check/vibe-check-opus-5-5-is-pulling-our-codex-converts-back-to-claude)). Anthropic's own guidance turns that weakness into a lever. Opus 5.5 is "highly responsive to information about elapsed time". When a harness appends an elapsed-versus-budget line, the model "paces its work to finish inside the budget" ([Prompting Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5)). The orchestrator should always send that line and keep its own hard timeout. Claude Code's `opusplan` alias, where Opus plans and Sonnet executes, confirms that the vendor treats Opus as the planner ([Claude Code model config](https://code.claude.com/docs/en/model-config)). Practitioners keep Astra "for advice" while cheaper models execute ([Hacker News](https://news.ycombinator.com/item?id=49897054)), which supports Astra as a plan critic rather than a planner. The old slogan "GPT plans, Claude codes" did not surface in any current-generation source.

**Substantial coding and debugging.** This is the best-evidenced row. Independent TB4 results favour Claude.

- **Practitioner test.** One independent developer's three-task, tests-first comparison ranked Opus 5.5 first on all three, with GPT-6.1 Sol second once and third twice. The verdict was "Opus 5.5 stays my default, but not by much" ([independent review](https://thomas-wiegold.com/blog/gpt-6-1-sol-review/)).
- **Side-by-side reports.** Hacker News commenters doing side-by-side feature work report Opus results with "fewer high severity bugs" and fewer fix rounds ([Hacker News](https://news.ycombinator.com/item?id=49897054)).
- **Effort.** Anthropic says Opus 5.5 is strongest "on multistep work in a real repository". At its default medium effort it "matched or beat Claude Opus 5 at high effort … in fewer steps and with fewer tokens" ([Prompting Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5)). Claude Code's own effort guide reserves high for "bug fixes where verification matters" ([Claude Code model config](https://code.claude.com/docs/en/model-config)). Hence high by default and medium when the specification is tight.
- **Sonnet as the in-family fallback.** Sonnet 5.5 is the right Claude-side fallback for one reason: a "You've hit your Opus limit" message "applies only to requests to that model family", so switching to Sonnet restores access ([Claude Code errors](https://code.claude.com/docs/en/errors)). Its Vals Code Migration lead also makes it the better choice for large, well-specified migrations.
- **Escalating to Astra.** Astra earns its five-fold credit cost after a failure. Practitioner guidance reserves Astra for problems "still unresolved, or a costly mistake to undo" ([drleeworks](https://drleeworks.com/en/computing/codex-gpt-6-1-sol-model-choice/)). Endor's functional-pass gap of 4.4 points over Sol is the measured upside.

**Bounded mechanical coding.** When the task is truly bounded, quality saturates and the rule falls through to cost.

- **Haiku's positioning.** Anthropic positions Haiku 5.5 for "sub-agent tasks". Claude Code's cost guide says "for simple subagent tasks, specify `model: haiku`" ([Claude Code costs](https://code.claude.com/docs/en/costs)).
- **Effort.** Anthropic recommends medium "for most work, including agentic coding". At low, "the model is more likely to skip a search, stop early, or skip a check". Moving from low to medium "roughly halved early stopping" ([Prompting Haiku 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-haiku-5-5)).
- **Community measurement.** One community measurement found Haiku 5.5 passing all scoped tasks on a single repository at both low and medium effort. The five-hour meter barely moved, because a roughly 47k-token launch context dominated each worker's cost whichever model ran it (a DEV community post; see the method note). Context trimming saves more subscription quota than model downgrading.
- **Codex alternative.** On the Codex side, GPT-6.1 Sol at low effort matches the vendor's "Sol Light" preset ([Codex models](https://learn.chatgpt.com/docs/models)). Terra draws half Sol's credits but is five days old, and OpenAI "still recommends GPT-6.1 Sol or GPT-6 Astra for the most complex coding tasks" ([BleepingComputer](https://www.bleepingcomputer.com/news/artificial-intelligence/openai-launches-gpt-61-terra-with-3x-faster-ai-agents-50-percent-lower-pricing/)). Terra therefore runs in shadow until paired runs justify it.
- **Luna is excluded from agentic edits.** It scores **2.4% on DeepSWE at low effort** against 66.6% at max on vendor charts ([vanja.io compilation](https://vanja.io/reasoning-effort/)). It scores 13.6–16.4% on independent TB4, and practitioners report that it "leaks tool calls and markers in the output" ([Hacker News](https://news.ycombinator.com/item?id=49897054)).

**Research.** Every independent knowledge-sensitive measure favours Opus 5.5.

- **Benchmarks.** It leads on HLE-Diamond and AA-Omniscience. It also leads Vals Legal Research at **50.5% against 39.4% for Astra and 38.5% for GPT-6.1 Sol** ([Vals Index](https://www.vals.ai/benchmarks/vals_index)).
- **OpenAI's own chart.** Even OpenAI's chart shows Opus ahead on Terminal-Bench-Science (63.3% against 57.0%), though at about four times the cost per task ([DataCamp](https://www.datacamp.com/blog/gpt-6-1-sol-vs-opus-5-5)).
- **Citation fidelity.** Anthropic says Opus 5.5 is "much less likely to state an incorrect figure or cite the wrong source" ([Prompting Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5)).
- **Smaller Claude models are excluded.** Sonnet 5.5 "sometimes answers from its training knowledge when a web search would catch details that have changed" ([Prompting Sonnet 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5-5)). Haiku "sometimes needs an extra nudge to search".
- **Counterweight.** Every's team routes most knowledge work and writing through Codex, at "80/20 Codex versus Claude" by usage ([Every](https://every.to/vibe-check/vibe-check-opus-5-5-is-pulling-our-codex-converts-back-to-claude)). That keeps GPT-6.1 Sol a close alternative.

**Documentation.** No source shows a material quality gap among the strong models, so cost decides.

- **Independent evaluation.** On AA-Briefcase, a knowledge-work deliverable evaluation, Sonnet 5.5 (1814) sits level with Opus 5.5 (1801), both at max effort ([AA Opus 5.5](https://artificialanalysis.ai/models/comparisons/claude-opus-5-5-xhigh-vs-claude-opus-5-5)).
- **Vendor positioning.** Anthropic positions Sonnet 5.5 for documents and office work ([The Batch](https://www.deeplearning.ai/the-batch/claude-fills-out-its-model-family/)).
- **Effort.** High effort follows Anthropic's advice to "start at `high` … unless your workload is agentic or latency-sensitive" ([Prompting Sonnet 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5-5)).
- **Scope.** The scope instruction matters because Sonnet "tends to add tests, documentation, and small supporting files … even when you don't ask for them".
- **Astra as editor.** The one structured writing judgment available favours Astra. Every's reviewers said "for technical writing I still prefer Astra" and found Opus readable but prone to burying the point ([Every](https://every.to/vibe-check/vibe-check-opus-5-5-is-pulling-our-codex-converts-back-to-claude)). Astra is therefore the editor for documents that leave the repository.
- **Terra.** OpenAI pitches Terra for "clear, natural writing". AA measures it with the lowest hallucination rate in the GPT-6 family, **34.5% against 51.9% for GPT-6.1 Sol** ([BenchLM](https://benchlm.ai/blog/posts/gpt-6-models-compared)). That makes it the most promising challenger, though it is unevaluated on documentation.

**Security review.** This is the one row where Astra is first, because the only independent detection evidence points at the GPT-6 family.

- **AA DeepsecBench.** GPT-6 Astra and GPT-6 Sol found bugs requiring sequential reasoning in **about 30% of runs, roughly twice the next-best models** ([AA Cyber Index](https://artificialanalysis.ai/articles/artificial-analysis-cyber-index)).
- **Endor Labs.** Astra leads on secure pass.
- **MacroscopeBench.** Astra at max posts the highest review precision, **91.7%** ([Macroscope](https://macroscope.com/content/ai-code-review-benchmark-best-models)).
- **Why not GPT-6.1 Sol.** It ranks **45th of 47** on Vals CyberBench at 39.29%, for reasons Vals does not state ([Vals GPT-6.1 Sol](https://www.vals.ai/models/openai_gpt-6.1-sol)). That red flag keeps it out of first place despite Endor's parity result.
- **Opus as alternative.** On the Claude side, "finding vulnerabilities in source code is allowed" for Opus 5.5 ([Prompting Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5)). When safeguards do intervene, Anthropic says "most cyber tasks are rerouted to Opus 4.8" ([Anthropic](https://www.anthropic.com/news/claude-opus-5-5)).
- **Sonnet and Haiku are excluded.** Sonnet 5.5 declines in five categories, including a `general_harms` category that "benign work can also trigger". Haiku 5.5 "has no server-side fallback", and repeating a declined request "usually returns another refusal" ([Prompting Haiku 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-haiku-5-5)).
- **Treat the row as policy-gated.** Because both vendors can silently substitute a different model on security content, this row is gated by policy rather than ranked by capability. Log every refusal and every served model, and require two-family review for any change to trust or authority code.

**Verification and code review.** The organising invariant is that the reviewer never belongs to the writer's model family.

- **Self-review is unreliable.** Every's reviewers saw Opus 5.5's self-review reject over 90% of generated components for untraceable reasons. A build that passed automated checks still had broken core screens ([Every](https://every.to/vibe-check/vibe-check-opus-5-5-is-pulling-our-codex-converts-back-to-claude)).
- **Sol's own tests are thin.** GPT-6.1 Sol wrote the thinnest test suite in one comparison and missed seeded bugs such as a flipped comparison operator ([independent review](https://thomas-wiegold.com/blog/gpt-6-1-sol-review/)).
- **Sol-class review is close to Astra.** MacroscopeBench predates GPT-6.1 Sol and Opus 5.5. It found GPT-5.6 Sol at high effort scoring **77.8 F-score against 78.0 for Astra at max, at half the cost per review**. The previous-generation Opus 5 traded precision for recall (76.6% recall, 64.2% precision) ([Macroscope](https://macroscope.com/content/ai-code-review-benchmark-best-models)). That supports GPT-6.1 Sol as the default reviewer of Claude-written work, with Astra reserved for diffs where a false alarm or a miss is expensive.
- **Opus 5.5 reviews better than Opus 5.** Anthropic reports early testers saw Opus 5.5 catch "more bugs … than on Claude Opus 5 and fewer false alarms" ([Prompting Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5)). That makes it the reviewer of Codex-written work.
- **Deterministic checks come first.** The orchestrator's own tests, linters, builds and end-to-end probes run before any model review, because "passes checks" did not mean "works".

**Hard end-to-end tasks.** Anthropic says Opus 5.5 "sustains long-running autonomous work better than Claude Opus 5, such as multihour audits and migrations … run end to end with parallel subagents" ([Prompting Opus 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5-5)). Every uses it for "big end-to-end coding projects". AA records Opus gaining on its long automation evaluation between xhigh and max (65% to 70%), the clearest case for raising Claude effort on a retry.

Astra is the alternative rather than Sol for two reasons. It topped SlopCodeBench on strict pass rate, and it was the only model whose strict passes rose at xhigh (29 to 32) ([daily.dev summary of SlopCodeBench](https://daily.dev/posts/slopcodebench-10-2026-opus-5-5-astra-wins-overall-xhigh-considered-harmful-mjhhblw2w)). Its weaknesses are wall-clock time and quota. Practitioners report "prompts running for 30/40 minutes to do simple things" ([Hacker News](https://news.ycombinator.com/item?id=49897054)).

Fable sits last. Every advises staying on Fable when "the problem is too big to check by eye" ([Every](https://every.to/vibe-check/vibe-check-opus-5-5-is-pulling-our-codex-converts-back-to-claude)), but independent TB4 places Fable below both Opus and Astra. A Fable escalation is mostly a different-model retry, so it must be budgeted as one.

### Effort: high is the working ceiling for Codex, medium-to-high for Claude

Effort should be set per provider, never mapped across vendors as one shared scale.

**Codex.** OpenAI says "most tasks do not need Max or Ultra" ([Codex models](https://learn.chatgpt.com/docs/models)), and the measurements agree:

- GPT-6.1 Sol scores **75.2% on DeepSWE at high against 71.9% at max**.
- It scores 62.9 at xhigh against 60.1 at max on AA's Codex agent index ([vanja.io compilation](https://vanja.io/reasoning-effort/)).
- Astra's public TB4 scores are flat at 57.88% (high), 57.88% (xhigh) and 58.18% (max). Its AA index is 55 at both high and xhigh ([BenchLM](https://benchlm.ai/benchmarks/terminal-bench-4); [AA Astra](https://artificialanalysis.ai/models/releases/gpt-6-astra)).

High is the default, xhigh is a retry setting, and max is never routed automatically. Codex's `ultra` level is xhigh plus automatic subagent delegation, so an external orchestrator must never select it: nested delegation multiplies usage and hides it. Luna is the exception to the ceiling: it needs high effort or above to be usable at all.

**Claude.** Claude Code defaults Opus, Sonnet and Haiku 5.5 to medium, and its guide says max "may show diminishing returns and overthinking" ([Claude Code model config](https://code.claude.com/docs/en/model-config)). The data bears that out:

- Opus 5.5 gains one AA index point from xhigh to max. That costs 1.66x the dollars, and time to first token rises from 121 s to 644 s ([AA Opus 5.5](https://artificialanalysis.ai/models/comparisons/claude-opus-5-5-xhigh-vs-claude-opus-5-5)).
- Sonnet 5.5 falls from 52.1% to 46.2% on FrontierCode at max ([The Batch](https://www.deeplearning.ai/the-batch/claude-fills-out-its-model-family/)).
- SlopCodeBench recorded fewer strict passes for Opus at xhigh than at medium (25 against 28), at up to 4.22x the cost.

Medium to high is the working band, with xhigh only on a measured retry. Haiku is capped at high, because at xhigh it "sometimes writes its whole answer in its thinking and ends the turn with no visible text".

Low effort is not free either. Astra loses about 7 TB4 points at low, and Fable loses about 14.5.

## Quota windows, not list prices, decide the tie-breaks

### Codex on ChatGPT plans

Codex usage is one shared allowance across the local CLI, IDE, desktop, cloud tasks and ChatGPT Work. It is metered over a rolling five-hour window and a weekly window, though "Pro plans currently have no five-hour limit", and "cloud tasks may use more of your allowance than local messages" ([Codex pricing](https://learn.chatgpt.com/docs/pricing)). OpenAI publishes credit rates that make relative cost explicit:

| Model | Credits per 1M tokens (input / cached / output) | Weight vs GPT-6.1 Sol | Plus local messages per 5 h (estimate) |
| --- | --- | --- | --- |
| GPT-6 Astra | 250 / 25 / 1,250 | 5.0 | 5–45 |
| GPT-6.1 Sol | 50 / 2.5 / 250 | 1.0 | 15–160 |
| GPT-6 Sol | 50 / 5 / 250 | 1.0 (2.0 on cached input) | 15–150 |
| GPT-6.1 Terra | 25 / 1.25 / 125 | 0.5 | 35–285 |
| GPT-6 Luna | 2.5 / 0.25 / 12.5 | 0.05 | 350–3,000 |

Source: [Codex pricing](https://learn.chatgpt.com/docs/pricing). Some third-party trackers still quote an older 2.5x Astra-to-Sol ratio; use the vendor table.

On top of the base rates:

- **Speed tiers.** Fast mode draws **2.5x** and Ultrafast **8x** against included usage.
- **Effort.** Effort scales output tokens about fourfold from low to max on Sol.
- **Astra on lower plans.** Astra is included on Pro $100 and $200 and on Business $100, but is "limited" on Plus and on the $20 Business plan ([OpenAI Help](https://help.openai.com/en/articles/20001516-managing-usage-with-gpt-6-astra-in-work-and-codex)). Plus subscribers report a single Astra Light desktop-automation session emptying a five-hour window in 2–5 minutes ([OpenAI community](https://community.openai.com/t/bug-codex-gpt-6-astra-desktop-automation-burning-100-of-5-hour-quota-in-5-minutes/1396450)).
- **Pro limits are in flux.** One report says Pro limits were doubled on 29 October ([AIbase](https://news.aibase.com/news/31974)). Another says they were halved with a one-time credit grant ([Startup Fortune](https://startupfortune.com/openai-cuts-chatgpt-pro-200-usage-in-half-as-the-grace-period-ends-today/)). Static Pro assumptions are unsafe this week.

### Claude Code on Max plans

Claude Max ($100 for 5x, $200 for 20x) has a rolling five-hour session limit and a weekly limit. Both are shared across all models and across claude.ai and Claude Code, and Anthropic publishes no token or message counts ([Claude Help](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan)). Specific limits:

- **Fable 5.1.** Max users may spend "up to 50% of your weekly usage limits on Fable models", which "use them faster than other Claude models". Pro users pay for Fable with usage credits from the first message ([Claude Help: Fable](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan)).
- **Family limits.** Claude Code also emits "Opus limit" and "Sonnet limit" messages. Their sizes and plan coverage are undocumented.
- **Switching models.** Switching models never restores session or weekly access, and a model switch re-reads the conversation "with no cache hits" ([Claude Code errors](https://code.claude.com/docs/en/errors)).
- **Weekly window.** Opus 5.5's launch raised only the five-hour limits ([Anthropic](https://www.anthropic.com/news/claude-opus-5-5)), so the weekly cap now binds sooner, and it resets at a fixed time assigned to each account ([SmartScope](https://smartscope.blog/en/blog/claude-opus-5-5-weekly-limit-max-plan-2026/)).

API list prices are the only relative-cost proxy:

| Model | API price per 1M tokens (in / out) | Weight vs Opus 5.5 | Subscription behaviour |
| --- | --- | --- | --- |
| Fable 5.1 | $10 / $50 | 2.5 | Capped at 50% of the Max weekly limit and consumed faster; not the default on any plan |
| Opus 5.5 | $4 / $20 | 1.0 | Default model on every plan; "Opus limit" message exists |
| Sonnet 5.5 | $2 / $10 | 0.5 | "Sonnet limit" message exists; at max effort emits about 65% more output tokens per task than Opus |
| Haiku 5.5 | $0.10 / $0.50 (higher rate for long prompts) | 0.025 | Launch context dominates subagent cost |

Sources: [Anthropic](https://www.anthropic.com/news/claude-opus-5-5); [Claude Code model config](https://code.claude.com/docs/en/model-config); [Haiku 5.5 migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide).

The per-token proxy misleads at the task level. On Vals, Sonnet costs 0.66x Opus per test, but on the public TB4 run Sonnet cost **1.55x Opus** because it generated so many more tokens. Sonnet's discount is real at medium effort and can turn negative at high effort.

### How each client reports remaining usage

| | Codex CLI (ChatGPT plan) | Claude Code (Max plan) |
| --- | --- | --- |
| Windows | Rolling 5 h plus weekly, shared across CLI, IDE, desktop, cloud and Work; Pro currently has no 5 h window | Rolling 5 h session plus weekly, shared across models and claude.ai; Fable sub-cap; Opus and Sonnet family limits |
| Interactive | `/status`; web usage dashboard ([Codex pricing](https://learn.chatgpt.com/docs/pricing)) | `/usage` with plan bars and per-subagent attribution, computed from local history and showing "last-known usage" when throttled ([Claude Code costs](https://code.claude.com/docs/en/costs)); `/status`; warnings at about 85% ([Claude Code errors](https://code.claude.com/docs/en/errors)) |
| Programmatic | App-server `account/rateLimits/read`: per-window `usedPercent`, `windowDurationMins` and `resetsAt`, plus credits, plan type, a multi-bucket `rateLimitsByLimitId` view and `ordinaryUsageAllowed`. `account/rateLimits/updated` pushes sparse updates. `account/usage/read` returns per-thread credit estimates by model, effort and speed ([openai/codex app-server](https://github.com/openai/codex/tree/main/codex-rs/app-server)) | Status-line JSON `rate_limits.five_hour.used_percentage`, `rate_limits.seven_day.used_percentage` and `resets_at` in epoch seconds ([Claude Code status line](https://code.claude.com/docs/en/statusline)); headless `system/api_retry` events with `error: "rate_limit"` ([Claude Code headless](https://code.claude.com/docs/en/headless)); limit-message strings with reset times; an undocumented stream-json `rate_limit_event` seen by third-party parsers |
| Pitfalls | Identify windows by duration, not by the primary/secondary label ([codexusage](https://docs.rs/crate/codexusage/0.4.0/source/src/app/codex_limits.rs)). A null `ordinaryUsageAllowed` means unknown, not recovered. Expect multiple buckets | `/usage` counts only the local machine. A set `ANTHROPIC_API_KEY` silently bills the API instead of the plan ([Claude Help](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan)). A 529 overload is capacity, not quota |

The two pools are separate currencies, so the cost tier cannot compare a Codex credit with a Claude Max percentage. The workable unit is **the expected change in the binding window's used percentage per accepted task**. Both clients expose it before and after each child run. Codex's per-thread credit estimates add model- and effort-level resolution.

Until enough history accumulates, seed the estimates as follows:

- **Codex:** the credit weights multiplied by observed tokens.
- **Claude:** the API-price proxy, with Sonnet at 0.5–1.5x Opus depending on effort.

Headroom then breaks remaining ties: send the task to the pool with the larger projected remainder in its binding window. The weekly window usually binds on Pro and Max.

Two design choices follow from the vendor mechanics rather than from measured data. First, keep a reserved weekly share for Astra and Fable escalations, so that routine work cannot starve them. Second, stop routing to a pool before it reaches the 85% point at which Claude Code begins warning.

## Silent substitution and false completion are the failure modes to engineer against

| Model | Recurring failure mode | Evidence | Orchestrator guard |
| --- | --- | --- | --- |
| GPT-6.1 Sol | "Did the minimum that technically satisfies it"; thin tests that miss seeded bugs; forgets instructions mid-session; unrequested risky system changes such as firewall edits | Practitioner review; Hacker News ([review](https://thomas-wiegold.com/blog/gpt-6-1-sol-review/); [HN](https://news.ycombinator.com/item?id=50129992)) | Complete specs; independent test-adequacy checks with seeded mutations; least-privilege tools; re-inject constraints in short sessions |
| GPT-6 Astra | 30–40-minute turns on simple work; writes side apps instead of editing the live code; more sensitive to instructions in skills and AGENTS.md; drains Plus windows | Hacker News; vendor guidance ([OpenAI guide](https://developers.openai.com/api/docs/guides/latest-model)) | Bounded consult or review calls; minutes-scale timeouts; audit instruction files; headroom check before dispatch |
| GPT-6.1 Terra | Unevaluated (released 26 October); vendor steers complex coding elsewhere | Vendor via press | Shadow runs only |
| GPT-6 Luna | Near-zero agentic capability at low effort; leaks tool-call markers | Vendor charts; Hacker News | High effort or above; non-agentic transforms; output validation |
| Opus 5.5 | Overbuilding and time overruns; "passes checks" yet broken; unreliable self-review; text-only end of turn in unattended loops; scratch-file sprawl | Every; vendor; SlopCodeBench | Time-budget signal plus hard timeout; diff-size limits; independent end-to-end checks; treat a text-only stop as a report; at most two or three automatic continuations; scratch-file sweep |
| Sonnet 5.5 | Unrequested tests, docs and files; self-launched reviewer subagents at xhigh and max; checks in early at low and medium; wrong-case tool names; answers from training knowledge; broader refusal categories | Vendor ([Prompting Sonnet 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5-5)) | Scope instruction; effort at high or below; tolerant tool-name matching; explicit search instruction; refusal handling |
| Haiku 5.5 | Early stops and skipped checks at low; skipped checks at medium; empty visible answer at xhigh; refusals with no fallback | Vendor ([Prompting Haiku 5.5](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-haiku-5-5)) | Medium or high only; "run a real check" instruction; empty-reply detection; never used for security |
| Fable 5.1 | Describes the next step instead of doing it; long silent turns; usage-credit consent prompt ends headless turns; weekly cap | Vendor ([Prompting Fable 5.1](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-1)); [Claude Code errors](https://code.claude.com/docs/en/errors) | Continuation instruction; minutes-scale timeouts; decide consent behaviour in advance; reserved budget |

Silent substitution is the most dangerous cross-cutting failure because it corrupts the orchestrator's own telemetry. When safeguards fire, each vendor serves a different model from the one requested:

- **Anthropic** reroutes most cyber tasks to Opus 4.8.
- **The Codex app-server** emits a `model/rerouted` notification whose stated reason is high-risk cyber activity ([openai/codex](https://github.com/openai/codex/tree/main/codex-rs/app-server)).
- **AA** labels its Claude results "Default Fallback" without defining the term ([AA Opus 5.5](https://artificialanalysis.ai/models/claude-opus-5-5)).

The orchestrator must record the *served* model for every child, treat rerouted output as unreviewed, and keep rerouted runs out of quality statistics.

A second class of substitution comes from configuration drift inside Claude Code:

- **Subagent inheritance.** Subagents without a pinned `model` inherit the parent's model.
- **Environment override.** A `CLAUDE_CODE_EFFORT_LEVEL` variable in the child environment overrides the `--effort` flag.
- **Alias drift.** The bare `sonnet` and `haiku` aliases resolve to 4.x models on Bedrock and Google Cloud ([Claude Code model config](https://code.claude.com/docs/en/model-config)).
- **Pins that do not take effect.** The community Haiku study above found a subagent model setting not taking effect in headless runs without an additional force flag.

Child processes therefore need full model IDs, an explicit effort level and a scrubbed environment. On Codex, read the model list at startup rather than hard-coding IDs, and gate models on client version: Terra requires CLI 0.159 or later.

The last class is runaway or hidden spend:

- **Nested delegation.** Codex's `ultra` level adds its own subagents.
- **Agent teams.** These use "approximately 7x more tokens than standard sessions when teammates run in plan mode" ([Claude Code costs](https://code.claude.com/docs/en/costs)).
- **Self-started review.** Sonnet at xhigh and max starts its own review rounds.
- **Effort changes.** Changing top-level effort between requests invalidates the prompt cache.

## Counter-evidence leaves ten questions for paired runs

The strongest counter-evidence concerns the coding rows. Several sources point the other way:

- **Ties and reversals.** Mercor's independent DeepSWE run reportedly has GPT-6.1 Sol and Opus 5.5 tied at 72.3%, though this is a second-hand report via the independent review above. SlopCodeBench put Astra, not Opus, on top. Under a neutral harness the Opus-over-Astra lead shrinks to under a point.
- **Cost per point.** The GPT-6 models solve tasks with a fraction of the tokens. If subscription burn tracks tokens even loosely, GPT-6.1 Sol could deliver more accepted tasks per window than Opus at a quality gap the orchestrator cannot detect on its own repository.
- **Sentiment is not unanimous.** Practitioner sentiment favours Opus but disagrees: one commenter rates Sol 6.1 "only slightly behind Opus 5.5 in intelligence" ([Hacker News](https://news.ycombinator.com/item?id=50129992)).
- **Early-launch complaints.** Many Sol laziness and regression complaints date from the early-October launch window and may not persist.

In the other direction, the Sonnet-equals-Opus reading of composite indices ignores Sonnet's token blow-up at max, and Opus's large lead on knowledge evaluations.

The Fable and effort conclusions also have credible dissent.

- **Fable.** Every's head of evaluations would rather use Fable for the hardest problems even through a worse harness: "I'd rather have a worse harness if I can work with a better model" ([Every](https://every.to/vibe-check/vibe-check-opus-5-5-is-pulling-our-codex-converts-back-to-claude)).
- **Opus at xhigh.** Every found xhigh adds detail to Opus coding without excess.
- **Effort can save cost.** On long-horizon tasks, Astra on ARC-AGI-3 cost less at max than at medium because it solved games in fewer actions ([vanja.io compilation](https://vanja.io/reasoning-effort/)).
- **Cross-vendor review.** This is supported in practice but unmeasured. A late-October public orchestration project routes review of Opus-written work to Astra while Codex has quota and falls back to Fable otherwise. Another public workflow caps Codex review of Claude diffs at three rounds "to keep token usage predictable". Neither reports defect rates (both linked repositories are omitted; see the method note).

The table therefore encodes priors that the orchestrator must test directly:

| # | Unresolved question | Paired design | Decision metric |
| --- | --- | --- | --- |
| 1 | Does Opus 5.5's harness-inclusive coding lead survive on this repository's tasks? | Same task to Opus 5.5 high and GPT-6.1 Sol high, randomized order, blind review | Accepted-without-rework rate, fix rounds, high-severity defects; flip the first choice if Sol lands within about 3 points at lower cost |
| 2 | What does each model and effort cost in subscription terms? | Record before and after window percentages for both windows on every child, plus Codex per-thread credit estimates | Change in binding-window percentage per accepted task; replaces the API-price proxies |
| 3 | Is Sonnet 5.5 at medium a quality-equal, cheaper substitute for Opus on well-specified work? | Sonnet medium against Opus medium on scoped features and migrations | Acceptance parity at a lower usage change |
| 4 | Which model is best for bounded mechanical edits? | Haiku 5.5 medium, GPT-6.1 Terra medium and GPT-6.1 Sol low on identical edits | First-pass verified success, usage change, latency |
| 5 | Which reviewer catches real defects with the fewest false alarms? | Seeded-defect diffs reviewed by GPT-6.1 Sol high, Astra xhigh and Opus 5.5 high | Recall and precision; no current-generation benchmark exists |
| 6 | Where do effort gains stop on this workload? | GPT-6.1 Sol high against xhigh; Opus 5.5 medium against high against xhigh | Marginal acceptance per unit of usage change |
| 7 | How often does security review refuse, reroute, or miss? | Astra high, Opus 5.5 high and GPT-6.1 Sol xhigh on a corpus of known vulnerabilities | Finding precision and recall; refusal and reroute rate |
| 8 | Does Fable add anything beyond a cross-vendor retry? | After an Opus failure, Fable high against Astra xhigh | Retry success per usage change |
| 9 | Which model writes the most accurate documentation? | Sonnet 5.5 high, GPT-6.1 Sol medium and Terra medium, rated blind | Factual errors per page; editorial rating |
| 10 | Which research model cites most faithfully? | Opus 5.5 high against GPT-6.1 Sol high on questions with known answers | Unsupported-claim rate; source accuracy |

Client behaviours also need verification, because documentation alone does not settle them:

- whether `claude -p` children honour `--effort` the way interactive sessions do;
- whether pinned subagent models actually take effect in headless runs;
- whether the Opus and Sonnet family limits apply to Max plans, and on what reset cycle;
- whether `rateLimitsByLimitId` exposes separate per-model buckets on Codex;
- what the Pro limits actually are after the conflicting late-October changes.

Neither vendor publishes weekly ceilings, so the orchestrator's own logs are the only source for that data.

## Conclusion

In late October 2026, the routing problem is less a question of which vendor codes better than an accounting problem.

On capability, the frontier models sit within a few benchmark points of each other once harness effects are removed. The real differentiators are three things. The first is token efficiency, where GPT-6 is 5–13x leaner. The second is failure behaviour: Opus overbuilds and declares success early, Sol under-delivers and forgets, and Astra is slow. The third is quota mechanics that neither vendor fully publishes.

The consequence for the orchestrator's design is that its most valuable components are not the routing table. They are three pieces of plumbing:

- **a per-child ledger** that records the served model, the effort level and the change in window usage;
- **a cross-family review gate** that never lets a model family approve its own work;
- **a reserved escalation budget**, so that Astra and Fable remain available when a first attempt fails.

Two findings deserve to change behaviour immediately. Fable 5.1 is not a capability step above Opus 5.5 on independent agentic evidence, so it belongs at the bottom of the escalation ladder. And max effort is a regression risk on both vendors, not a quality dial. Everything else in the table is a well-reasoned prior with roughly a two-week shelf life, given that seven of the nine models shipped in the last six weeks.

*Method note: this report synthesizes vendor documentation, independent benchmarks (Artificial Analysis, Vals AI, the tbench.ai board, Epoch, METR, MacroscopeBench, SlopCodeBench), journalism and practitioner sources retrieved up to 31 October 2026. To keep the report free of community usernames, three community sources whose URLs embed personal handles are described without links: a DEV post measuring Haiku 5.5 subagents, and two public GitHub orchestration repositories. Account-specific model catalogs and local client configuration were deliberately excluded. Every model-level claim here rests on public documentation or published measurements.*
