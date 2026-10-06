# Decisions — pr-eval-harvest (PRD)

A running log of the decisions that shaped this feature's requirements. One paragraph each: what was
decided, and why it was decided that way. This exists so a later session does not re-argue something
already settled, and so the reasoning survives when the PRD gets rewritten. Where a decision reverses
an earlier one, the reversal says so.

## 2026-09-04 — discovery session that produced `prd.md`

**The product is a CLI, not a pipeline.** eval-harvest is an agent-native CLI that a coding agent
harness drives, not an automated miner that runs end to end on its own. The reasoning is the GitHub
CLI: *"there's no point baking the LLM commands and work into the CLI tool itself because people
will just use Claude to execute it anyways. It's like the github CLI. You use claude to use the
github CLI."* Every command must also be usable directly by a human, in the same shape.

**No LLM inside the CLI.** The CLI vends facts and instructions; the driving coding agent supplies
every judgment. This is checkable — no model client in its dependencies. An LLM judge shipped
*inside a task* does not violate this, because the CLI only writes it as a file and Harbor runs it
at eval time.

**The primary customer is an SDE responsible for repositories who wants to automate PR approvals.**
Not someone choosing between review agents, not someone building a public benchmark. They arrive
not knowing what an eval is. Public versus private repository is not a meaningful distinction;
responsibility for the repository is.

**The purpose chain is fixed and short: PRs pile up → automate approvals → don't trust the agent →
build an eval to validate and tune it → turn automation on.** The eval is a prerequisite step
toward automated approvals, not a candidate response to a bottleneck. An earlier draft treated
"would an SDE with pileup reach for an eval?" as an open question needing validation; that was
rejected as manufactured complexity, and the chain above replaced it.

**The problem is validated by direct customer conversations, not assumed.** Teams adopting coding
agents repeatedly named PR pileup as a major blocker to getting value out of those agents, and the
persona — SDEs with no data science background who do not know where to start — recurs across those
conversations.
An earlier draft labelled the whole document a hypothesis; that was wrong. What remains unmeasured
is cost per tuning cycle and market sizing, neither of which gates the build.

**Scope ends when the task directory exists and is verified.** The CLI does not run evals, score
agents, rank them, or compute error bars. *"The CLI stops once the tasks are setup and put in
harbor."* This reverses the superseded PRD, four of whose seven stories were about running and
scoring.

**The deliverable is a self-contained Harbor task directory.** `environment/Dockerfile`,
`solution/`, `tests/`, `instruction.md`, `task.toml` — settled by reading a real Senior SWE-Bench
task rather than by design: *"Everything to build and create the task is actually in the directory
itself."* Verifiers are files inside the task, so "does the CLI ship verifiers?" was never a real
question. `task.toml`'s `[metadata.origin]` already carries repo, PR numbers, base commit, and
dates, so provenance needs no invented format.

**Labels come from a pull request's own review iterations, not from reverts or hot-fixes.** The
first push is a state human reviewers rejected, their comments are the specific defects a good
reviewer should have caught, and the approved state is what passing looks like — one PR, known
SHAs, findings quoted rather than inferred. This reverses the superseded PRD's revert-based
labelling and retires what that document called its largest risk: reverts are roughly 10% of merged
PRs, so a deny class needed ~500 PRs mined, which most repositories cannot supply. Review iteration
is plentiful, so a mid-size private repo becomes viable.

**The driving coding agent proposes labels; the CLI never does.** This directly reverses the
superseded PRD's constraint that *"no model proposing a label"* was allowed. The CLI reports
mechanical facts and records the agent's classification with the evidence behind it, so a bad rule
is findable rather than diffuse.

**There is no human verification step.** *"The agent should drive the creation and verification of
the task based on the instructions vended back from the CLI man pages and help."* An earlier draft
assumed the human spot-checks a sample; that was cut. The consequence is that the CLI's checks are
the only thing standing between an unsupervised agent and a plausible-looking worthless dataset.

**The CLI's documentation is the product.** Because the customer does not know what an eval is and
there is no human review step, `--help` and the man pages must carry the methodology, not just the
flags. This makes the kill criterion the inverse of the product: *"Agent produces garbage tasks
unsupervised."* If an unsupervised agent cannot produce valid datapoints from the CLI's help output
alone, shipping more CLI verbs will not fix it — which is why this should be tested against a stub
CLI with hand-written help before any mining logic is built.

**Verifying a datapoint is not running an eval.** The CLI checks that a datapoint is well-formed
and testing the right thing — base commit exists, patch applies, oracle findings reference real
lines, answer absent from everywhere the evaluated agent can read. It does not run trials against
it. A proposed 3× oracle / 3× no-op gate was rejected for conflating the two: *"You have to verify
the eval is testing the right things. You're not responsible for running the eval."*

**Severity is two axes, both low/medium/high.** Finding severity — how bad an individual finding is,
where high is the only blocking severity and medium and low do not block (see the verdict decision
below, which also fixes the datapoint verdict to follow the human review rather than a severity
formula). Change risk — how dangerous the change itself is,
which decides whether a PR is eligible for automated approval at all. They are orthogonal: a
low-risk change can contain a high finding, and a high-risk change can be clean. Senior SWE-Bench's
three requirement tiers were considered as a basis and rejected in favour of plain low/medium/high,
on the grounds of keeping it simple.

**Change risk is determined two ways, and both are recorded.** Structurally, from path rules —
teams already segregate risky code by directory, so the repository layout partially encodes the
answer for free — and by classification, an LLM judge call the driving agent makes. The structural
half is cheap, auditable, and incomplete; the classification half covers what the layout does not
and is variable. Where the two disagree, the datapoint records the disagreement rather than picking
a winner, because that disagreement is information about the change and the resolution is the
customer's call.

**A wrong approve is the expensive error; a wrong deny costs a human glance.** This follows from
the endpoint being automated approval, and it ranks the failure modes. Anything built on these
datapoints should treat the two asymmetrically.

**The rubric is per-team and a first-class artefact.** The CLI's documentation instructs the driving
agent to build a rubric before any datapoint, by overlaying the repository's own conventions on a
standard base. The rubric and the risk-map path rules are both written down, inspectable, and
versioned with the dataset, so a team can correct their own map without rebuilding datapoints.

**Speed is the benefit, chosen over trust and over tuning-loop stability.** *"Speed: an eval exists
at all, today."* This sits in tension with a customer who cannot audit an eval's validity, and the
tension is recorded rather than resolved: the CLI must be fast *and* refuse, because the customer
will not catch what it lets through.

**Harbor is the format and Lambda MicroVMs is the execution substrate.** Harbor because it is the
standard the customer is betting on, corroborated by Senior SWE-Bench shipping Harbor-compatible
and Harbor being one of its own source repositories. Lambda MicroVMs via a dedicated execution
environment built as its own workstream, not part of the CLI.

**Senior SWE-Bench is inspiration, not a template.** *"I'm not saying do it exactly. I'm saying
draw inspiration from their multi pronged evaluators."* Two corrections came out of reading it
rather than citing it: it documents no severity taxonomy, no finding-matching procedure, and no
FP/FN formulas — so the severity design here is original — and it evaluates code *authoring*, so
its oracle patch is the answer where ours is the input. What transfers cleanly is the
multi-mechanism reward stack and the habit of proving a reward mechanism responds correctly to a
known-good and a known-bad submission before trusting it.

**Terminology: say low/medium/high, and never say "load-bearing."** Plain words for the severity
ladder, and the phrase "load-bearing" is banned from this feature's documents.

**Cut, with the reasons that made them cuttable:** the conformance specification and its 196
requirements, the extension wire protocol, the content-addressed store, the evidence graph,
publication and governance — all machinery for making independent implementations agree, and there
is one implementation and one customer. Also cut: synthetic or injected defects, since findings come
from the repository's own reviewers; PR authoring evaluation; and the automated-approval CI
integration itself, since this tool produces the evidence for that decision rather than acting on
it.

**Deferred with a seam kept:** aggregating severity into scores and the taste judge (severity is
captured in v1 because the datapoint is meaningless without it; deciding what fraction of high
findings authorises automation is a scoring question). Also deferred: non-GitHub forges, eval types
other than PR review, and pooling history across repositories.

**The datapoint verdict follows the human review, not a severity formula; high is the only blocking
severity.** An earlier rule derived `expected_verdict = block if any finding is high else approve`.
Two problems surfaced while shaping it: medium severity had no defined verdict, and a severity-derived
verdict could contradict what the reviewers actually did (a human-rejected, medium-only iteration
would emit as `approve`). Resolved: `expected_verdict` follows `--kind` — a reject datapoint (built
from a rejected state) expects `block`, an approve datapoint expects `approve`. Severity is captured
per finding to drive finding-level scoring and the deferred automation-gate aggregation, not to
derive the verdict. Only **high** blocks; medium and low do not. Coherence is enforced by invariant,
not derivation: a reject datapoint must carry ≥1 substantive finding (else `empty-oracle`), and an
approve datapoint may carry no high-severity finding (else `emit` refuses). This dissolves the medium
gap and grounds the verdict in real reviewer behaviour. Chosen over "medium also blocks" (rejected as
more conservative than the data supports — teams routinely approve with open medium comments) and
over keeping the severity-derived rule (rejected: it leaves the reject-emits-as-approve contradiction
in place).

**What an approve datapoint can certify was made explicit — a third assumption.** An approved state
is one reviewers *accepted*, not one that is *defect-free*; by the second assumption the review is
incomplete. So an approve datapoint certifies "a competent team approved this," not "this is safe,"
and an agent that blocks an approve datapoint may be right (it caught what the humans missed).
Verdict-level scoring on approve datapoints must treat an agent block as not-necessarily-wrong, the
same way finding-level scoring treats an unmatched agent finding as uncredited. This connects two
PRD assumptions that had been stated separately, and it bounds v1: against an incomplete oracle the
"flagging noise" pain cannot be fully measured without the taste judge §10 defers — v1 captures the
material to make that call later, it does not make it.

**Not every review comment is a defect, and a datapoint built from a nit is worse than no
datapoint.** Review comments mix real defects with style preferences, questions, bikeshedding, and
bot output. A should-have-been-flagged datapoint whose oracle finding is a nit teaches the eval that
nit-picking is correct review behaviour — the exact failure the tool exists to help the customer
avoid. So separating substantive findings from nits is the driving agent's first classification job,
recorded per comment with the reasoning behind each call, and the split has consequences downstream:
a pull request whose only comments are nits is not discarded but emitted as an approve datapoint with
the nits recorded non-blocking, while a reject datapoint must carry ≥1 substantive finding to exist
at all. This is the labelling problem the revert-based approach carried, relocated rather than
dissolved — the old risk was that reverts are rare, the new one is that comments are noisy — and it
is left as a stated risk rather than engineered away, because the ratio of substantive findings to
nits is unmeasured and cheap to measure on a real repository.

**A pull request's pre-review state must be recoverable even when the commit graph has lost it.**
Modern GitHub pull requests are commonly squash-merged or rebased, which collapses or rewrites the
iteration history so the state a reviewer actually rejected is no longer reachable from the ordinary
commit graph — verified against live git, not assumed. Mining therefore cannot rely on walking merged
history; it has to recover the rejected and approved iteration tips another way, since the forge
retains them even when the graph does not. The customer described the mechanism directly: *"If the
first iteration has an issue, there might be another commit. Both those commits squashed might be the
actual oracle or you might be able to use both. The first one as a bad example, second as a good."*
The consequence carried forward as a stated fidelity gap: reviewing an iteration tip is not the same
as reviewing the squashed content that actually merged, so a datapoint built from a tip is faithful
to what the reviewer saw, not necessarily to what shipped.

**Reject and approve datapoints must be indistinguishable by shape, or the eval measures phrasing
instead of review skill.** A reject datapoint is built from a rejected iteration and an approve
datapoint from an approved one, so the two would otherwise differ in ways an evaluated agent could
read off without doing any review — a tell in the instruction text, a visible verdict, a recoverable
outcome. Sealing the answer is therefore not sufficient on its own: `instruction.md` must present the
same text across datapoints modulo declared slots, the environment must be sealed against the
repository's own future so the post-change state and merge decision are unreachable, and the
reference answer that proves the verifier responds to a known-good submission ships with the task but
never where the evaluated agent can read it. Because the customer cannot audit any of this, structural
identity is stated as a build invariant rather than left to the driving agent's discretion.

**Smaller PRD fixes from review.** A straw-man speed target was added, since the chosen #1 benefit
(speed) had no measurable criterion. Determinism acceptance criteria now say "fixed forge snapshot"
rather than "unchanged history". The §1 framing now states the ranking ambition is staged — v1
delivers datapoints, not a leaderboard. The banned phrase "load-bearing" was confirmed removed. The
kill-criterion target remains an explicit `[TODO]` — a number the customer sets, deliberately not
invented here.
