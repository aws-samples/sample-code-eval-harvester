# PRD: eval-harvest — an agent-native CLI for building PR-review eval datapoints

> **Status: the problem is validated.** Teams adopting coding agents repeatedly name pull-request
> pileup as a major blocker to getting value out of those agents. The customers are SDEs with no
> data science background who do not know where to start. What is unmeasured is cost and sizing,
> not whether the problem is real — §4 marks each one.

---

## 1. North Star

> An SDE with pull requests piling up wants to automate approvals and does not trust an agent to do
> it. They point a coding agent at this CLI and get Harbor review-eval datapoints built from their
> own repository's PR review history — fast enough to have them today, without hand-labelling a
> pull request or learning eval methodology — so they can empirically validate their PR agent, tune
> it, and switch on automated approvals for low-risk changes.

The chain is short and every link is the customer's own: **PRs pile up → automate approvals →
don't trust the agent → build an eval to validate and tune it → turn automation on.** This tool
does one link, because it is the one nobody can currently do. The project's ambition — mining PR
history into an evaluation that *ranks* review agents — is staged: v1 builds and verifies the
datapoints; the scoring and ranking that turn them into a comparison are deferred (§10). A customer
who arrives expecting a leaderboard gets a runnable dataset, not a number, in v1, and the framing
must not promise otherwise.

One consequence, stated early because it ranks the failure modes: the endpoint is *automated
approval*, so a wrong **approve** is the expensive error and a wrong **deny** costs a human glance.
Anything built on these datapoints should treat the two asymmetrically.

Three phrases carry the rest of the document. **Does not know what an eval is** is why the CLI's
documentation is the product rather than its packaging: the customer cannot recognise a broken
datapoint, so the CLI has to. **Their own repository** is why labels come from mined review
history rather than a public benchmark. **Fast enough to have them today** is the benefit the
customer chose when offered trust and tuning-loop stability as alternatives.

## 2. Problem Statement

Pull requests pile up. Teams adopting coding agents repeatedly name it as a major blocker to
getting value out of those agents. The fix is to automate approvals — at least for low-risk
changes — and an SDE will not switch that on for code they are responsible for until they can show
the agent is good enough. Showing that means an eval, and building one requires a skill they do not
have. These are SDEs, not ML-evaluation people; in the customer's words *"most users won't really
even know what an eval is"* and they *"don't know where to start."*

So the automation stays off. They keep reviewing by hand, or they run the agent advisory-only and
tune it by impression, which never produces the evidence that would let them turn it on.

Building the eval by hand means deciding, per pull request, what a good reviewer should have
said — then encoding that into a runnable task with the right base commit, a sealed environment
that does not leak the answer, and a verifier that genuinely tests the right thing. That costs
`[TODO: measure — time an SDE to hand-build a 50-datapoint PR-review eval. No estimate exists;
nobody in this project has tried]` and produces something stale as soon as the repository moves.

The repository already holds the answer. Every pull request that went through review iteration
contains a state a human reviewer rejected, the specific defects they named, and the state they
approved — at known SHAs, with the findings written down by people who were actually looking.
Nobody mines it, because doing so correctly is fiddly in exactly the ways that are invisible
when you get them wrong.

## 3. Customer

- **Primary customer:** an SDE responsible for one or more repositories, with pull requests piling
  up, who wants to automate approvals for low-risk changes and will not switch that on until they
  can show the agent is good enough. They arrive not knowing what an eval is — someone or something
  pointed them at this repository. They work through a coding agent harness (Claude Code, Kiro, or
  similar) and expect to type a sentence, not learn a workflow. Public versus private repository is
  not a distinction that matters; **responsibility for the repository** is.
- **Key characteristics:** they have the raw material (a repository with real review history) and
  the agent they want to tune. What they lack is the eval and the methodology to build one. They
  cannot audit an eval's validity, which is the constraint the whole design bends around. And they
  are personally on the hook for what gets merged, which is why they need evidence rather than a
  demo.
- **Non-customers:**
  - Anyone making a purchasing decision between review agents. That needs paired statistics and
    error bars; this tool builds datapoints, it does not rank.
  - Anyone publishing a citable public benchmark. That needs dataset versioning, contamination
    control, and expert review — Senior SWE-Bench's apparatus, and a different product.
  - Anyone evaluating PR *authoring* or general coding agents. SWE-bench-shaped work.
  - Anyone with no review history to mine. A repository whose pull requests merge on first push
    with no review comments has nothing to harvest, and the CLI must say so rather than emit an
    empty dataset.
  - Anyone not on GitHub. §10.

## 4. Evidence & Validation

The problem is validated by direct conversations with teams adopting coding agents. Feasibility
rests on a real Harbor task the customer supplied during discovery. What is unmeasured is cost and
sizing, marked below.

| Evidence | Source | Status |
|---|---|---|
| Pull-request pileup is a major blocker to getting value out of coding agents | Named unprompted by teams adopting coding agents | **Validated.** The strongest demand evidence in the document, and what makes review throughput worth attacking at all |
| The customer is an SDE with no data science background who does not know where to start | Recurring across teams adopting coding agents | **Validated.** Drives §1, §3, and the priority of US-1 — the CLI carries the methodology because the customer does not have it |
| The customer wants to automate PR approvals and will not switch that on until they can empirically validate the agent | Field experience with adopting teams | **Validated.** This is why the eval is a prerequisite rather than an optional measurement exercise |
| Teams already segregate risky code by directory path, so repository layout partially encodes change risk | Field experience with adopting teams | **Validated.** It is why US-4 can get a cheap structural signal rather than depending entirely on a judge |
| Harbor is the right output format to target: Senior SWE-Bench ships Harbor-compatible, and Harbor is one of its own source repositories | Senior SWE-Bench's public write-up | Validated |
| A Harbor task is a self-contained directory the CLI can emit as a single datapoint | A real Senior SWE-Bench task the customer supplied during discovery | Validated |
| Senior SWE-Bench documents no severity taxonomy, no finding-matching procedure, and no FP/FN formulas | Its public write-up — checked, absent | Validated as *absent*. The two severity axes in US-3 and US-4 are this project's design, not a borrowed one — §9 |
| A squash-merged pull request loses its pre-review state from the ordinary commit graph, so mining it needs that state recovered another way | Verified against live git | **Validated** as a constraint on mining, not a blocker |
| The Lambda MicroVMs execution environment can be added to Harbor without forking it | Harbor's extension model | Validated on paper; **[TODO: prove with one real run]** |
| Market size | *"all dev teams"* | **Not usable as stated.** `[TODO: size it from the demand pipeline]` |

**The assumption everything rests on.** *A pull request's pre-review state is a state a competent
reviewer should have pushed back on, and the review comments on it are what they should have
found.* This is much stronger footing than the superseded PRD's revert-based labelling — the
rejection is explicit rather than inferred, and the findings are quoted rather than derived. It
is still an assumption. Review comments include nits, style preferences, questions, and
bikeshedding alongside real defects, and a datapoint built from a nit teaches the eval that
nit-picking is correct review behaviour.

The validation is a hand audit and it is not optional: sample at least 20 emitted datapoints,
have a reviewer decide independently whether the flagged findings are things a review agent
*should* have caught, and record agreement. Until that audit exists, no score produced against
these datapoints may be quoted.

**The second assumption.** *Human review comments are a usable reference for what an agent should
find.* They are incomplete by construction — reviewers miss things. So coverage against them is
a lower bound, and an agent finding something the humans did not is not an error. Any scoring
built on these datapoints must treat unmatched agent findings as *uncredited* rather than false.
That constrains the reward object the CLI emits, which is in scope, even though the scoring is
not.

**The third assumption, and what an approve datapoint can certify.** *An approved state is one the
reviewers accepted — which is not the same as one that is defect-free.* By the second assumption the
review is incomplete, so an approved state can still harbour a latent defect nobody caught. Two
consequences, and they bite on the expensive-error axis (§1): an approve datapoint certifies "a
competent team approved this," not "this is safe"; and an agent that *blocks* an approve datapoint
may be right — it may have caught what the humans missed. So verdict-level scoring on approve
datapoints must treat an agent's block as *not necessarily wrong*, the same way finding-level scoring
treats an unmatched agent finding as uncredited rather than false. This also bounds what v1 can
measure about the "flagging noise" pain (§5): against an incomplete oracle, an unmatched agent
finding is ambiguous — noise, or a real miss — and separating the two needs the taste judge §10
defers. v1 captures the material to make that call later; it does not make it.

**What would convince the customer this is not worth building:** *"Agent produces garbage tasks
unsupervised."* Since there is no human review step (§7), the CLI's documentation and its
validation feedback are the only things standing between a coding agent and a plausible-looking
worthless dataset. If an unsupervised agent cannot produce valid datapoints from the CLI's help
output alone, the product has failed at its premise, and shipping more CLI verbs will not fix
it. This is measurable early and cheaply — see §8.

## 5. Current State & Pain

How the customer does this today:

1. Ships a PR-review agent into CI, advisory-only.
2. Notices it missing things, or flagging noise.
3. Changes the prompt, the model, or the context window.
4. Forms an impression from the next few PRs it comments on.
5. Cannot tell whether step 3 helped.
6. Leaves automated approvals switched off. Back to step 2.

Where it breaks:

- **Step 4 has no ground truth.** On a live pull request nobody has decided what the right review
  was, so "did the agent get it right?" is unanswerable.
- **Step 4 has no negatives.** The PRs in front of them are mostly fine. Watching an agent approve
  good code measures nothing about catching bad code, which is the job.
- **Step 5 is the actual pain.** Tuning without a signal is not tuning. The customer described the
  trigger for reaching for this tool as *"they don't trust their PR review agent but don't know how
  to verify it works."*
- **Step 6 never resolves.** There is no threshold to clear, because there is nothing being
  measured — so the automation that would clear the pileup stays off indefinitely.
- **Nothing accumulates.** Every model release means starting the impression-forming over.

Measurable cost: `[TODO: measure — hours per tuning cycle, and how long teams sit at step 6]`.

## 6. User Stories

Seven stories, ordered by priority. US-1 is first because the kill criterion in §4 is about US-1.

---

**US-1: The CLI's documentation is sufficient to drive it unsupervised**

As an SDE, I want my coding agent to learn the whole workflow from the CLI's own help output,
so that I do not have to know what an eval is or how to build one.

**Acceptance criteria:**
- [ ] `--help` and the man pages describe not just each command's flags but *the methodology*:
      what makes a review datapoint valid, why the base commit must precede the change, why the
      instruction must not name the outcome, and what a good finding looks like versus a nit.
- [ ] A coding agent given only the repository name and the CLI's help output produces valid
      datapoints with no methodology in its own prompt and no human correction.
- [ ] Every error and refusal is written for a model to act on: it names the check that failed,
      the datapoint, the specific offending content, and the next command to run.
- [ ] The CLI contains no LLM call. It vends facts and instructions; the driving agent supplies
      the judgment. Verified by the absence of any model client in its dependencies.
- [ ] Every command is usable directly by a human, in the same shape, with the same output.

**North star link:** the customer does not have the methodology, so the CLI carries it. This
story *is* the kill criterion from §4.

---

**US-2: Mine a pull request's review iterations into good/bad datapoint candidates**

As an SDE, I want the CLI to surface each pull request's iteration history — what the first
push looked like, what reviewers said about it, what changed, what got approved — so that my
agent can turn one pull request into both a should-have-been-flagged datapoint and an
approve datapoint without me labelling anything.

**Acceptance criteria:**
- [ ] For a given pull request, the CLI reports each iteration: its tip SHA, its parent/base
      SHA, its diff against the base, and which review comments landed against it.
- [ ] The CLI reports the review verdicts and their timing, so an agent can tell a state that
      was rejected from a state that was approved.
- [ ] Squash-merged and rebased pull requests are handled: the pre-merge states a reviewer acted
      on remain recoverable even where the ordinary commit graph has lost them.
- [ ] The CLI reports mechanical facts and never a label. Classification is the driving agent's
      call, and the CLI records that call with the evidence behind it.
- [ ] Running the same command twice over a fixed forge snapshot produces identical output. (Live
      history can gain or lose a comment between runs; determinism is against fixed inputs, which is
      also how it is tested.)
- [ ] A repository whose pull requests merge without review iteration produces a refusal naming
      that fact, with the count that justified it, rather than an empty dataset.

**North star link:** this is *without hand-labelling*, and it is what makes a mid-size private
repository viable — every reviewed pull request yields data, not just the rare reverted one.

---

**US-3: Capture the human review as the datapoint's oracle**

As an SDE, I want the specific defects my reviewers named on a rejected state captured as that
datapoint's reference findings, so that "what should the agent have caught?" is quoted from my
team rather than invented.

**Acceptance criteria:**
- [ ] Each review comment is captured with its body, file path, line range, author role, and
      timestamp, and is bound to the iteration it was written against.
- [ ] The CLI exposes the material an agent needs to separate defect findings from nits,
      questions, approvals, and bot output — and records which way the agent decided, and why,
      per comment.
- [ ] **Every reference finding carries a severity: low, medium, or high.** High is the only
      blocking severity; medium and low do not block. Severity is captured per finding and drives
      the finding-level scoring, not the datapoint's verdict.
- [ ] **The expected verdict follows the human review, not a severity formula.** A datapoint built
      from a rejected state expects *block*; one built from an approved state expects *approve* —
      matching what the reviewers actually did. Coherence is enforced by invariant, not derivation:
      a reject datapoint must carry ≥1 substantive (non-nit) finding, and an approve datapoint must
      carry no high-severity finding — so "should this have blocked?" and "what was wrong with it?"
      cannot disagree within one datapoint.
- [ ] The evidence behind each severity assignment is recorded — what in the review indicated it,
      and what the assigning agent concluded — so a severity can be challenged without re-mining.
- [ ] A comment written after the iteration the datapoint is built from is still usable as a
      reference finding when it describes a defect present in that iteration, but never appears
      anywhere the evaluated agent can read.
- [ ] A pull request whose only review comments are nits is usable as an **approve** datapoint with
      those nits recorded as non-blocking, rather than discarded or mislabelled as a rejection.
- [ ] A pull request with no review comments at all is reported as unusable for a
      should-have-been-flagged datapoint, rather than emitted with an empty oracle.

**North star link:** the automation gate is severity-shaped — a high finding must block, a
nitpick must not. Without severity on findings, the eval cannot measure the thing that decides
whether auto-approval is safe to switch on.

---

**US-4: Classify how risky the change itself is**

As an SDE, I want each datapoint to record how risky its change is, so that I can measure my agent
separately on the low-risk changes I would actually let it auto-approve and the high-risk ones I
never would.

**Acceptance criteria:**
- [ ] Every datapoint carries a change-risk classification — low, medium, or high — independent of
      its findings and of its verdict. A low-risk change can still have been rejected; a high-risk
      change can still have been fine.
- [ ] Risk is determined two ways and both are recorded: **structurally**, from where the change
      lands — path rules the customer supplies, because teams already segregate risky code by
      directory — and **by classification**, where the driving agent judges the change and records
      its reasoning.
- [ ] Where the two disagree, the datapoint records the disagreement rather than silently
      preferring one. A path rule and a judgment pointing opposite ways is information about the
      change, and it is the customer's call which wins.
- [ ] Path rules are a first-class inspectable artefact, versioned with the dataset like the rubric,
      so a team can correct their own risk map without rebuilding datapoints.
- [ ] The CLI reports the risk distribution across the dataset, so the customer learns whether they
      have enough low-risk datapoints to justify switching automation on for that class — before
      they trust a number computed over a dataset that is mostly high-risk changes.

**North star link:** the customer wants automated approvals *for low-risk changes*. An aggregate
score across all risk levels cannot authorise that; a score on the low-risk slice can.

---

**US-5: Build the rubric from the team's own conventions**

As an SDE, I want the eval to judge review quality against how *my* team reviews code, so that
a datapoint does not penalise my agent for missing something my team does not care about.

**Acceptance criteria:**
- [ ] The CLI's documentation instructs the driving agent to construct a rubric before building
      any datapoint, and describes how: overlay the repository's own conventions on a standard
      base of things any review should check.
- [ ] The CLI surfaces the repository's stated conventions where they exist as files, so the
      agent has something concrete to overlay rather than guessing.
- [ ] The rubric is a first-class artefact: written down, inspectable, versioned with the
      dataset, and reusable across datapoints.
- [ ] A datapoint records which rubric version it was built against.

**North star link:** the customer's benefit is a signal they can tune against. A rubric that
does not describe their team produces a signal that moves for the wrong reasons.

---

**US-6: Emit a self-contained Harbor task directory**

As an SDE, I want each datapoint to be a complete Harbor task I can hand to `harbor run`, so
that the CLI's output is the thing I actually needed and not an intermediate format.

**Acceptance criteria:**
- [ ] Each datapoint is a single self-contained Harbor task — the change under review, the
      instruction, the sealed environment, and the verifier — that runs under `harbor run` with
      no external state required to build or run it.
- [ ] The datapoint records its provenance — which pull request, at which base commit, and when —
      so any datapoint traces back to the pull request it came from.
- [ ] The environment is sealed against the repository's own future: the post-change state, the
      review outcome, and anything else that reveals the answer are absent from everything the
      evaluated agent can reach.
- [ ] The reference answer used to prove the verifier responds to a known-good submission ships
      with the datapoint but is never placed where the evaluated agent can read it.
- [ ] The judge and the rubric it grades against ship inside the task, so the eval is
      self-contained: the CLI writes them, the harness runs them.
- [ ] The evaluated agent cannot reach or tamper with the verifier's scoring.
- [ ] `instruction.md` presents the same text across datapoints modulo declared slots, so an
      agent cannot infer a verdict from how a task is phrased.

**North star link:** *fast enough to have them today* means the output runs. A near-miss format
puts the customer back into work they cannot do.

---

**US-7: Verify each datapoint is testing the right thing**

As an SDE, I want the CLI to tell my agent when a datapoint it just built is broken, so that an
unsupervised run does not produce a dataset that looks fine and measures nothing.

**Acceptance criteria:**
- [ ] Per datapoint, the CLI checks that the base commit exists, that the change applies cleanly
      at it, that the oracle findings reference files and lines that exist in the change, and
      that the rubric is coherent.
- [ ] Per datapoint, the CLI checks that the answer is absent from everywhere the evaluated agent
      can read: no review comments, no follow-up commits, no merge decision, no pull request
      number, no revert. Including the non-obvious channels — `objects/info/alternates`,
      `packed-refs`, the commit graph, `refs/replace`, reflog entries outside reachability,
      `include.path` in the git config.
- [ ] A scan that finds nothing must prove it scanned something: a planted control token, and a
      refusal when the scan comes back without it.
- [ ] Every check has been watched to fail. The invariant gets broken deliberately, the check
      goes red, the break is reverted — and that is recorded, because a guard nobody has watched
      fail is decorative.
- [ ] A datapoint failing any check is not emitted. Overriding is possible, explicit, and
      recorded in the datapoint.
- [ ] Failures are reported so the driving agent can fix them and retry, naming the check, the
      datapoint, the offending content, and what to do about it.

**North star link:** the customer cannot tell a valid datapoint from an invalid one, and there
is no human review step. This story is the only thing between an unsupervised agent and a
worthless dataset.

---

## 7. Proposed Solution (User Experience)

The happy path, from the customer's side, is one sentence typed into a coding agent:

1. **They ask for it.** "Build me a PR-review eval from `our-org/our-repo`." That is the whole
   of their involvement in the normal case.
2. **The agent reads the CLI.** `--help`, man pages. It learns the workflow *and* the
   methodology from the tool rather than from its own prompt. *(US-1)*
3. **The agent builds a rubric and a risk map** from the repository's conventions and layout, and
   writes both down. The risk map is the path rules that say which directories hold changes the
   customer would never auto-approve. *(US-4, US-5)*
4. **The agent asks the CLI what the repository holds.** Pull requests with review iteration,
   their SHAs, their diffs, their review comments — facts, no labels. *(US-2, US-3)*
5. **The agent classifies, on both axes.** Per pull request: which states a reviewer rejected, which
   comments are real defects rather than nits, **how severe each finding is**, and **how risky the
   change is** — the last one against the path rules and by its own judgment, recording both. Every
   decision is recorded with its evidence. *(US-2, US-3, US-4)*
6. **The agent emits datapoints** as Harbor task directories, and the CLI validates each one.
   Failures come back as instructions the agent can act on, so it fixes and retries. *(US-6,
   US-7)*
7. **They get a dataset** they can hand to `harbor run`, with its risk and severity distribution
   reported — so they can see whether they have enough low-risk datapoints to justify the decision
   they actually care about. If it could not be built, they get a refusal that says why in terms
   they can act on — usually "this repository does not review its pull requests enough to mine."

The human never reads a task. Verification is the agent's job, driven by the CLI's checks — the
customer chose this explicitly: *"the agent should drive the creation and verification of the
task based on the instructions vended back from the CLI man pages and help."*

### Constraints the customer brings

Given, not chosen. Mechanism belongs in the design doc.

- **Output is Harbor format**, because Harbor is the standard the customer is betting on and
  Senior SWE-Bench already ships into it.
- **Execution is Lambda MicroVMs.** The eval that proves the core bet (§8) runs its trials on a
  Lambda MicroVMs execution environment for Harbor, built as part of this project and added to
  Harbor without forking it. How it is built is mechanism (the design doc); that it must exist is
  the constraint. The CLI itself depends on none of it.
- **No LLM inside the CLI.** The customer's reasoning: *"there's no point baking the LLM commands
  and work into the CLI tool itself because people will just use Claude to execute it anyway.
  It's like the GitHub CLI."* An LLM judge shipped *inside a task* does not violate this — the
  CLI writes it as a file, Harbor runs it at eval time.
- **GitHub, via `gh`.** Other forges are out of scope (§10).
- **The CLI does not run evals** and does not score agents. It builds and verifies datapoints.

## 8. Success Criteria

| Metric | Current | Target |
|---|---|---|
| Datapoints an unsupervised coding agent builds that pass every US-7 check, with no methodology in its prompt and no human correction | 0, no CLI exists | `[TODO: set target — this is the kill criterion from §4 and needs a number before build starts. A straw man is 80% first-attempt, 100% after the agent acts on the CLI's feedback]` |
| Human attention from "build me an eval" to a runnable dataset | days of their own work, or never | one sentence typed, plus reading the final report `[TODO: validate against a real user]` |
| Wall-clock to build a dataset once the agent is driving — the chosen #1 benefit, "an eval exists today" | unmeasured | `[TODO: validate — straw man: ≤30 min for a 20-datapoint dataset on a reference repo, dominated by the forge round-trips needed to fetch the pull requests]` |
| Pull requests hand-labelled | all of them, or none and no ground truth | 0 |
| Datapoints per reviewed pull request | 0 | ≥1, and 2 where a pull request has both a rejected and an approved state |
| Datapoints carrying both a finding-severity and a change-risk classification | 0 | 100%. A datapoint missing either cannot speak to the automation decision |
| Low-risk datapoints with at least one blocking finding — the class that decides whether auto-approval is safe | unmeasured | `[TODO: measure on a real repository. If low-risk changes essentially never contain high-severity findings, the automation decision needs no eval and this tool is answering a question nobody has]` |
| Reference findings judged by an independent human to be things a review agent should have caught | unmeasured | ≥ `[TODO: set threshold]`, over ≥20 sampled datapoints, before any score is quoted (§4) |
| US-7 checks that have been watched to fail | 0 | all of them, recorded |
| Datapoints emitted with a leaked answer | unknown; nothing detects it today | 0 |

**Good enough for v1:** one real repository, a dataset a coding agent built end to end with no
human correction, every US-7 check implemented and watched to fail, the output running under
`harbor run` on Lambda MicroVMs, and the §4 finding audit completed and written down. If the
audit fails, v1 ships as a working builder with a documented reason not to trust its findings
yet — a real outcome, not a failure to deliver.

## 9. Open Questions & Risks

**The kill criterion is the main risk, and it is testable first.** If a coding agent given only
the CLI's help output produces plausible-looking garbage, nothing downstream matters. Mitigation:
test this on a stub CLI with hand-written help before building the mining logic. It is the
cheapest experiment in the project and it gates everything.

**Nits versus defects is the labelling problem, relocated rather than solved.** The superseded
PRD's risk was that reverts are rare; this design's risk is that review comments are noisy. A
datapoint whose oracle finding is a style preference teaches the eval that bikeshedding is good
review. Mitigation: the §4 audit, and the CLI recording the agent's classification per comment
so a bad rule is findable rather than diffuse. `[TODO: measure the ratio of substantive findings
to nits on a real repository. It sizes the problem and it is cheap]`.

**Severity is two axes, not one, and both are required.** This was the largest open question in the
document and the customer resolved it:

1. **Finding severity** — how bad an individual finding is. **Low, medium, high**, with **high the
   only blocking severity**; medium and low do not block. Severity is captured per finding to drive
   finding-level scoring; the datapoint's verdict follows the human review (US-3), not a severity
   formula. This is what makes the eval able to measure the automation gate rather than just verdict
   agreement. US-3.
2. **Change risk** — how dangerous the change itself is, independent of what was found. **Low,
   medium, high.** This decides whether a pull request is *eligible* for automated approval at
   all, since the customer only wants automation on low-risk changes. US-4.

Same three levels on both axes, and they are orthogonal: a low-risk change can contain a high
finding, and a high-risk change can be clean. Conflating them produces a dataset that cannot
answer either question.

Change risk is determined two ways, and neither alone is sufficient: **structurally**, from path
rules — teams already segregate risky code by directory, so the repository layout partially encodes
the answer for free — and **by classification**, an LLM judge call the driving agent makes. The
structural half is cheap, auditable, and incomplete; the classification half covers what the layout
does not and is variable. Recording both, and recording their disagreements, is what US-4 requires.

Neither axis is borrowed — Senior SWE-Bench documents no severity taxonomy at all.

**Senior SWE-Bench evaluates authoring, not review.** Its segments are Design-and-Build and
Investigate-and-Fix — the agent writes the patch and the reward stack grades it. Here the patch
is the *input* and the review is graded, which inverts what `solution/` means and what the
verifier compares. The methodology transfers as inspiration, per the customer, not as a template.
What transfers cleanly: the multi-mechanism reward stack, the requirement tiers, and the habit of
proving a reward mechanism responds correctly to a known-good and a known-bad submission before
trusting it.

**An LLM judge in the scoring path is variable.** Out of scope to *run*, in scope to *emit*.
Open question the design doc must answer: how a datapoint captures a judgment so it can be
recomputed and challenged without re-running the evaluated agent.

**Reviewing an iteration tip is not reviewing what shipped.** A squash-merged pull request's
merged content can differ from any branch tip. Sealing the tip is what makes rejected and
approved states structurally identical — without it, every negative would be distinguishable from
every positive by shape alone. The cost is a fidelity gap that has to be stated, not hidden.

**Whether the Lambda MicroVMs environment can be added without forking Harbor is unproven by
dispatch.** It is settled on paper only. `[TODO: prove it with one real dispatched trial]` before
the design depends on it.

**Lambda MicroVMs access and cost are unmeasured.** `[TODO: confirm credentials, bucket, build
role, and quota exist]`. Less critical here than in the superseded PRD, since this tool does not
dispatch runs — but US-7's checks and any oracle probe need somewhere to execute.

**Cost and sizing are unmeasured.** §4. The problem is validated; what nobody has put a number on
is how long a tuning cycle takes today, or how large the addressable set is. Neither gates the
build.

## 10. Out of Scope

**Deferred, with a seam kept:**
- Running evals, scoring agents, ranking, error bars, paired comparison — the superseded document's
  US-4 through US-7. The seam is that datapoints carry enough captured data — full review text,
  per-comment classifications, finding severities, change risk, rubric version — that scorers can be
  layered on without re-mining.
- **Aggregating** severity into scores, and the taste judge. Severity is *captured* in v1 because
  the datapoint is meaningless without it (US-3, US-4); deciding what fraction of high findings caught
  authorises automation is a scoring question, and scoring is not this tool's job.
- The automated-approval gate itself. The customer's endpoint is switching automation on; this tool
  produces the evidence for that decision, not the CI integration that acts on it.
- Non-GitHub forges.
- Eval types other than PR review. The CLI is "agent-native for creating evals" in ambition;
  v1 does one kind.
- Pooling history across repositories.

**Cut:**
- **A human verification step.** The customer removed it deliberately: the agent verifies, using
  the CLI's checks. This is deliberate, not an omission.
- **An LLM anywhere inside the CLI.** §7.
- **Synthetic or injected defects.** The findings come from the repository's own reviewers.
- **The conformance specification and everything serving it** — 196 requirements, L1/L2/L3 levels,
  the gates that police them. Those make independent implementations provably agree; there is one
  implementation and one customer.
- **The extension wire protocol, content-addressed store, and evidence graph.** Machinery for a
  general harvesting platform. This mines one source through one path.
- **Publication, witnessed tags, governance, a public leaderboard.**
- **PR authoring evaluation and coding-agent benchmarks generally.**

## 11. Discovery: The 5 Customer Questions

**Q1: Who is the customer?**

Offered four segments: eval author, agent developer, benchmark builder, and "you, right now."
The customer chose **eval author**, with an important qualification: *"But the purpose is to
build an eval first so they can tune a PR agent to do code reviews for them. In order to do that
they need to build an eval for their PR agent."* So the eval is instrumental; tuning is the goal.

On the agent harness's role, they rejected the framing that it might be an implementation detail:
*"The CLI consumer is claude code /kiro etc.. But the CLI should be able to be used on it's own.
The main reason is because there's no point baking the LLM commands and work into the CLI tool
itself because people will just use Claude to execute it anyways. It's like the github CLI. You
use claude to use the github CLI."* This is the sharpest answer in the session and it settles the
architecture question the superseded PRD got backwards.

Asked whether the repository is private or public, they rejected the axis: *"The goal is their
own repos they're responsible for. But those can be private or public so that distinction
doesn't make sense."*

Asked whether someone comparing several review agents is still a customer, they cut it: *"This is
to build an eval. Not to use that eval to make vendor decisions."*

Explicitly not the customer: purchasers, public-benchmark builders, and — a late and significant
addition under Q2 — anyone who needs the tool to understand eval methodology, because the target
user does not.

**Q2: What is the problem or opportunity?**

Offered four framings: labels without hand-labelling, PR→Harbor being fiddly, no eval existing at
all, and steering the agent to do it right. The answer blended the last two: *"Building an eval
for a PR agent is difficult and time consuming. But with this tool it can be done automatically
with human verification / iteration. The CLI is meant to provide consistent tooling for coding
agents to build this for them. The ultimate goal is to have an eval that can be used to measure /
tweak performance of an eval agent."* (Note "human verification" here; Q5 later removed it.)

Asked what they have tried to build themselves: *"Nothing yet."* (This is about building an eval,
not about customer research — see Q4, where the engagement evidence came out.) And then the most
consequential sentence of the session: *"Most users wont really even know what an eval is. They
come from an SDE background but are building agents to fit into their SDLC."* That reshaped §1,
§3, and the priority of US-1.

Asked for the trigger moment: *"They don't trust their PR review agent but don't know how to
verify it works. Someone or something pointed them at this repo they can use to build an
evaluation for their PR agent."*

Asked for a market size number: *"The market size is all dev teams."* This was pushed back on as
neither a number nor accurate, and replaced with a `[TODO: measure]` in §4. No alternative size was
supplied — though the same customer-conversation pipeline that surfaced the demand evidence is a
place to get one.

Parked: the mechanical-correctness framing and the labels-without-hand-labelling framing, both of
which survive as user stories (US-6, US-7, US-2) rather than as the north star.

**Q3: What is the most important customer benefit?**

Offered speed, trust, and tuning-loop stability. The customer chose **speed** — *"Speed: an eval
exists at all, today"* — and did not qualify it.

Asked whether approve/deny alone suffices for v1, they said no, pointing at Senior SWE-Bench:
*"Yes it's pass/fail but it's also the number of low medium, high issues found compared to a
human / false positives and negatives. and LLM Judge taste. It should be multi pronged."* On
being told the page contains no severity taxonomy and no FP/FN formulas, and that its segments
evaluate authoring rather than review, they clarified the intent: *"I'm not saying do it exactly.
I'm saying draw inspiration from their multi pronged evaluators. CLI builds tasks but it grabs
the info needed to run the verifiers we want. They're different actions in the CLI."*

Pressed later on whether to adopt Senior SWE-Bench's three requirement tiers instead of inventing
severity buckets, the customer answered the question I had been asking wrongly — there are two
severity axes, not one:

> *"Severity buckets are important because it determines 1 if automatic approval should be done or
> not and 2 severity is also on the PR feedback itself. Like it should block if a critical issue is
> found. but a nitpick is whatever. So severity on findings is one thing and severity of the code
> change as a function of risk is also important. The risk factor is an LLM Judge / classification
> problem and partially solved by separating out risky code changes into different directory paths
> so it's solved two ways."*

That produced US-3's severity criteria and US-4 outright, and it is the reason §10 defers severity
*aggregation* while keeping severity *capture* in v1. The path-rules half is a genuinely cheap
signal that had not occurred to me: the repository layout already encodes part of the answer,
because teams segregate risky code that way for their own reasons.

They did not directly answer "would you build it if it delivered only speed." The choice of speed
over trust, in a product whose user cannot audit trust, is a tension §8 records rather than
resolves: the CLI must be fast *and* refuse, because the customer will not catch what it lets
through.

**Q4: How do you know what your customer wants or needs?**

Asked what they had tried themselves, the customer said *"Nothing yet"* — no prior attempt to build
this. Market size was asserted as *"all dev teams"* and pushed back on; no number replaced it.

On the strength of that, this document was first drafted as a blanket hypothesis. The customer
corrected it, and the correction is the real evidence base: *"I've interviewed many customers. They
don't know where to start and most are SDEs that have no data science background. Nearly all of my
recent customer conversations cited code PR pileup as a major issue to getting value out of the
coding agents. So that hypothesis is tested against many customer conversations."*

Asked in a follow-up whether the jump from pileup to *eval* needed separate validation, the
customer rejected the framing and gave the chain directly: *"PRS are piling up, I want to automate
them but I don't trust agents to do it. So I build an Eval to empirically validate my PR agent and
tweak it to give me trust in turning on automated approvals for low risk tasks. Therefore I need to
build the eval first. This solves that problem. Don't make this complicated."* The eval is a
prerequisite step toward automated approvals, not a candidate response to a bottleneck. §1 records
the chain and drops the manufactured gap.

Mechanism evidence is genuinely strong and came from artefacts rather than assertion: the customer
supplied a real Senior SWE-Bench task — `instruction.md`, `task.toml`, and a screenshot of the
directory tree showing `environment/Dockerfile`, `solution/`, `tests/` — and said *"Everything to
build and create the task is actually in the directory itself."* That single artefact resolved the
CLI's output boundary, the provenance format, and the container-sealing technique.

Kill criterion, chosen from four options: *"Agent produces garbage tasks unsupervised."*

**Q5: What does the experience look like?**

The customer's answers here mostly took things away.

On the CLI's boundary: *"The CLi stops once the tasks are setup and put in harbor."*

On how the change reaches the container: *"This is an implementation detail. Add the .patch file
to the task. The task.toml file has the info needed to run it. base repo, parent commit, path
location, etc."*

On the deny signal — the question this session most needed answered, and the answer reframed the
whole design: *"You need to think about what a modern PR might look like. Specifically we're
starting with github. you'll use the github CLI to get the PR. This is why you need an LLM to
format them and make sense of the PR. If the first iteration has an issue, there might be another
commit. Both those commits squashed might be the actual oracle or you might be able to use both.
The first one as a bad example, second as a good. The goal is to look at the options and get a
good representative sample of good and bad PRs."* This retires the superseded PRD's largest
risk — that reverted pull requests are too rare to build a deny class from.

On graded output: *"This is LLM as a judge. The CLI should have instructions and a man page
instructing the coding agent to first construct a rubric or paste in the teams code conventions
to overlay on top of standard things it checks for."* This became US-5.

On the human's role, cutting the "human verification" from Q2: *"The agent should drive the
creation and verification of the task based on the instructions vended back from the CLI man
pages and help."*

On build-time validation, correcting a confusion in the question: *"I don't think you know what an
Eval does. You have to verify the eval is testing the right things. You're not responsible for
running the eval. Just to build the eval datapoints so that we can use them to evaluate a PR
agent against the oracle."* The proposed 3× oracle / 3× no-op gate conflated verifying a datapoint
with running an eval. US-7 is the corrected version: verify the datapoint, do not run the eval.

**Discovery gaps:**

- **No market size.** "All dev teams" was offered and pushed back on; nothing replaced it, though
  the same customer-conversation pipeline that surfaced the demand evidence could size it.
- **The demand evidence is secondhand in this document.** The count and the persona come
  from the customer's summary of their own interviews, not from notes or quotes reproduced here.
  Worth attaching the source if anyone downstream needs to defend the number.
- **No cost measurement of the status quo.** How long a tuning cycle takes today, and how many
  happen before a team gives up on their review agent, are both unknown.
- **"Would you build it for speed alone?" was never answered**, so the speed-versus-refusal
  tension in §8 is unresolved by the customer.
- **The nits-versus-defects ratio is unmeasured** and it is now the main labelling risk
  (§9). It is cheap to measure and was not measured during discovery.
- **"Agent-native CLI for creating evals" is broader than this PRD.** Only PR review is scoped.
  Whether the CLI's shape should anticipate other eval types was not discussed.
