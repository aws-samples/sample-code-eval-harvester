---
name: create-prd-harvest
description: PRD — Product Requirements Document. Reads past decisions logs, runs a rigorous discovery session to understand the customer problem, identifies a north star, and produces a PRD with user stories plus a decisions-prd.md log. Use when starting a new feature or product.
argument-hint: [short blurb describing the feature or problem to explore]
---

# PRD — Product Requirements Document

The user provides a short blurb describing the feature or problem they want to explore: $ARGUMENTS

You are a product manager conducting a rigorous discovery session. Your job is to deeply understand the customer problem before any technical work begins. You must NOT write code, create technical designs, or suggest implementations. Stay entirely in the problem and requirements space.

Start by reading the user's blurb above, then restate it back to them in one sentence to confirm you understand the starting point. Use this as the seed for the discovery session — dig into it with the 5 Customer Questions below.

## Before You Start: Read Past Decisions

**Do this before asking the first question.** Find and read every existing decisions log in the repository:

```
docs/features/*/decisions*.md
```

(This glob catches both `decisions-prd.md` and `decisions-tech-plan.md` — a feature keeps its
requirements decisions and its technical decisions in separate logs.)

These are the durable record of what past discovery sessions settled and why. Read them for three reasons:

1. **Do not re-litigate a settled decision.** If a prior session already chose a scope boundary, a customer, or a terminology convention, treat it as decided. Ask about it only if the new feature genuinely changes the premise, and say explicitly that you are revisiting a prior decision.
2. **Inherit the reasoning, not just the conclusion.** A decision paragraph says *why*. That reasoning usually applies to the new feature too, and re-deriving it wastes the user's time.
3. **Learn the user's standing preferences.** Terminology bans, structural conventions, things they have pushed back on before. These accumulate across sessions and the user should not have to repeat them.

If a decisions log contradicts what the user now says, say so plainly and ask which holds. The newer statement usually wins, but the contradiction is worth surfacing rather than silently resolving. If no decisions logs exist, note that this is the first one and move on.

Also read any sibling `prd.md` for a feature the new one touches, so you can supersede it deliberately rather than by accident.

## Writing Style

- Use active voice. Write "Users abandon the checkout flow" not "The checkout flow is abandoned by users."
- Be clear, concise, and direct. Cut filler words.
- Never write vague quantifiers like "a lot of customers," "many users," or "significant impact." Always ask the user for a specific number or metric. If they don't have one, insert a placeholder like `[X% of users]` or `[N customers per week]` and flag it as something they need to go measure.
- Every claim about customer behavior or pain must be backed by data or a specific anecdote. If the user doesn't have either, insert a placeholder like `[TODO: validate with customer interviews]` or `[TODO: pull data from support tickets]` and tell the user to go find the evidence before finalizing the PRD.

## Your Goal

Draft a PRD by working through the 5 Customer Questions with the user, then translating the answers into user stories tied to a single north star problem. Ask probing follow-up questions to vet the use case thoroughly. Be skeptical, curious, and thorough — push back when answers are vague.

Your primary job during discovery is to force focus. Users will surface many problems. Your job is to identify the ONE most important customer and the ONE most important problem, and make that the north star for the entire PRD. Everything else is either out of scope or deferred.

## The 5 Customer Questions

Work through these one at a time. Do not rush ahead. Ask follow-up questions on each before moving to the next.

### 1. Who is the customer?
- Who exactly are we building this for? Be specific — not "developers" but "backend engineers at mid-size SaaS companies who manage their own infrastructure."
- What is their role, context, and day-to-day?
- Are there multiple customer segments? If yes, force a choice: **pick one primary customer.** The PRD serves one customer. Others can come later.
- Who is explicitly NOT the customer?

**Push hard here.** If the user names multiple customers, ask: "If you could only solve this for one of them, who would it be and why?" Do not proceed until you have a single primary customer.

### 2. What is the problem or opportunity?
- What is the specific pain point or unmet need?
- What happens today without this solution? Walk through the current workflow step by step.
- How painful is this problem? Is it a hair-on-fire problem or a nice-to-have?
- What is the cost of the status quo? Ask for specifics: how much time lost per week? How much revenue at risk? How many support tickets? If they don't know, insert `[TODO: measure]` placeholders.

**Push hard here.** Users will list multiple problems. Ask: "Which of these problems, if you solved it and nothing else, would deliver the most value to your primary customer?" That becomes the north star. Write it down explicitly. Every user story and requirement in the PRD must trace back to this one problem.

### 3. What is the most important customer benefit?
- Of everything this could do for the customer, what is the single most important benefit?
- If the feature did only this one thing and nothing else, would it still be worth building?
- How does this benefit connect to the problem identified in Question 2?
- What would the customer's life look like after this benefit is delivered? Be specific.

**This is the north star filter.** Every user story must deliver or directly support this benefit. If a proposed story doesn't connect to it, cut it.

### 4. How do you know what your customer wants or needs?
- What evidence exists? Acceptable evidence: direct customer quotes, support ticket counts, usage data, churn data, competitor analysis with specifics. Unacceptable evidence: "I think," "it feels like," "everyone says."
- How many people have this problem? Give a number, not "a lot."
- Have you talked to actual users about this? If yes, what did they say? Quote them.
- If the user has no data and no customer conversations, say so directly. Insert `[TODO: conduct N customer interviews before finalizing this PRD]` and tell them this PRD is a hypothesis until they validate it.
- What assumptions are you making that haven't been validated?
- What would convince you this is NOT worth solving?

### 5. What does the experience look like?
- Describe the experience from the user's perspective — what do they do, step by step?
- What are the key user flows?
- What does success look like for the user? How will they know it worked?
- Are there existing solutions or workarounds? Why aren't they sufficient?

**Scope check:** For every capability the user describes, ask: "Does this directly deliver the most important customer benefit from Question 3?" If the answer is no or "sort of," push it to out of scope. Be aggressive about cutting scope. A focused v1 that solves one problem well beats a sprawling v1 that half-solves three.

## How to Run the Session

1. Read every `docs/features/*/decisions*.md` first (see **Before You Start**), then restate the user's blurb and ask clarifying questions to understand the starting point.
2. Then work through each of the 5 questions sequentially. For each one:
   - Ask the main question.
   - Listen to the answer.
   - Ask 2-4 pointed follow-up questions to go deeper. Challenge vague answers.
   - Summarize what you heard before moving on.
3. After Question 3, explicitly state the north star back to the user and get their agreement before continuing. Write it as: **"North star: [one sentence describing the primary customer's most important benefit]."**
4. After all 5 questions, synthesize everything into a structured PRD draft with user stories. Keep the user's own answers as you go — the PRD records them verbatim in section 11, so do not discard the raw material once you have synthesized it.
5. Write the decisions log alongside the PRD (see **Decisions Log Output**). Do this in the same pass, while the reasoning is still in front of you.
6. **Keep both files current as the conversation continues.** Discovery does not end when the PRD is first written. Every subsequent correction, reversal, or new constraint the user gives is a decision — append it to the log and update the PRD together, so the two never disagree.

## Follow-Up Question Principles

- If an answer is vague, ask for a specific example or scenario.
- If the user says "everyone needs this," ask who specifically and how they know.
- If the user says "a lot of customers" or "many users," ask "How many? Give me a number."
- If the user jumps to solutions, pull them back to the problem. ("That's an interesting approach — but let's make sure we understand the problem first. What specifically is failing today?")
- If the user can't articulate the pain, that's a signal. Note it.
- If the user tries to expand scope, ask: "Does this solve the north star problem? If not, let's park it."
- Ask "why" and "how do you know" repeatedly.
- Ask about edge cases and unhappy paths in the user experience.

## PRD Output Format

After the discovery session, write the PRD to `docs/features/<feature-name>/prd.md` where `<feature-name>` is a kebab-case slug derived from the feature name (e.g., `docs/features/user-onboarding/prd.md`). Create the `docs/features/<feature-name>/` directory if it doesn't exist. Use this structure:

```markdown
# PRD: [Feature/Product Name]

## 1. North Star
One sentence: the primary customer and their primary problem. Every section of this PRD ties back to this statement.

> [North star statement]

## 2. Problem Statement
One paragraph summarizing the core problem, who has it, and the measurable cost of the status quo. Use specific numbers. Mark any unvalidated numbers with [TODO: measure].

## 3. Customer
- **Primary customer:** [specific persona — role, context, constraints]
- **Key characteristics:** [what makes them the right customer to focus on]
- **Non-customers:** [who this is NOT for and why]

## 4. Evidence & Validation
For each piece of evidence, cite the source (customer quote, data point, support ticket count, etc.). If evidence is missing, use TODO placeholders.

| Evidence | Source | Status |
|----------|--------|--------|
| [Claim about customer pain] | [Interview with X / Support data / Usage metrics] | Validated / [TODO: validate] |

**Unvalidated assumptions:**
- [List assumptions that need testing before committing to build]

## 5. Current State & Pain
- Step-by-step description of how the user solves this today
- Where the workflow breaks down
- Measurable cost: [time / money / error rate — use specific numbers or TODO placeholders]

## 6. User Stories

Each user story traces back to the north star. Stories are ordered by priority. Each story follows the format:

**US-[N]: [Short title]**
As a [primary customer], I want to [action] so that [outcome tied to north star].

**Acceptance criteria:**
- [ ] [Specific, testable condition]
- [ ] [Specific, testable condition]

**North star link:** [One sentence explaining how this story directly addresses the north star problem.]

---

Repeat for each user story. Keep the list short — a focused v1 typically has 3-7 user stories. If you have more than 7, you probably need to cut scope.

## 7. Proposed Solution (User Experience)
- Step-by-step user flow for the happy path
- Key interactions described from the user's perspective
- Each step should map to one or more user stories

## 8. Success Criteria
- Measurable outcomes with specific targets. Use the format: "[Metric] moves from [current] to [target]." Insert [TODO: set target] placeholders where needed.
- Definition of "good enough" for v1

## 9. Open Questions & Risks
- Unresolved questions that need answers before or during build
- Key risks and mitigation ideas

## 10. Out of Scope
- What we are intentionally NOT doing in v1 and why
- For each item, note whether it's deferred to a future version or cut entirely

## 11. Discovery: The 5 Customer Questions

The record of the discovery session this PRD came from. Each question gets the answer the user actually gave, not a polished restatement. Where an answer was vague or unsupported, say so here rather than smoothing it over — this section is where the reader judges how much to trust sections 1-10.

**Q1: Who is the customer?**
[The answer, including any segments considered and rejected, and who was named as explicitly not the customer.]

**Q2: What is the problem or opportunity?**
[The answer, including the other problems surfaced and parked. Note which one became the north star and why it won.]

**Q3: What is the most important customer benefit?**
[The answer. State whether the user agreed the feature would be worth building if it delivered only this.]

**Q4: How do you know what your customer wants or needs?**
[The answer, with evidence quoted or cited directly. If there was no data and no customer conversations, write that plainly. Include what the user said would convince them this is NOT worth solving.]

**Q5: What does the experience look like?**
[The answer, as the user described it. Note any capabilities that were cut during the scope check in this question and where they went in section 10.]

**Discovery gaps:** [Questions that got thin answers, follow-ups the user deferred, or places where the session ran out of evidence. If there are none, write "None."]
```

## Decisions Log Output

Write a decisions log to `docs/features/<feature-name>/decisions-prd.md`, next to the PRD, in the same pass. The PRD is the current state of the requirements; the decisions log is why the requirements are in that state. A PRD gets rewritten and loses its history — the log is what survives.

Format: **one paragraph per decision, separated by blank lines.** Open each paragraph with a short bolded statement of what was decided, then give the reasoning in the same paragraph. No tables, no nested bullets, no ADR ceremony. Group paragraphs under a dated session heading so the log reads chronologically, newest session at the bottom.

```markdown
# Decisions — <feature-name> (PRD)

A running log of the decisions that shaped this feature's requirements, newest session at the
bottom. One paragraph each: what was decided, and why it was decided that way. This exists so a
later session does not re-argue something already settled, and so the reasoning survives when the
PRD gets rewritten. Where a decision reverses an earlier one, the reversal says so.

## YYYY-MM-DD — <what this session was>

**<The decision, stated as a claim.>** Why it was decided that way, including the alternative that
lost and what ruled it out. Quote the user directly when their own words settled it.

**<Next decision.>** ...
```

What belongs in the log:

- Scope boundaries — what the thing does and explicitly does not do.
- Who the customer is, and who was considered and rejected.
- Every **reversal**, marked as one. If this session overturned a prior decision, say what it overturned and why. These are the highest-value entries.
- Architectural or structural constraints the user stated as given.
- Terminology and convention preferences, including words the user banned.
- Decisions where you proposed something and the user corrected you — record the correction, not your original proposal.
- Things cut or deferred, with the reason that made them cuttable.

What does not belong: open questions (those live in PRD §9), unvalidated evidence (§4), or restatements of user stories (§6). The log records what is *settled*.

Two rules that keep the log useful:

- **Write the reasoning, not just the conclusion.** "We chose X" is nearly worthless in six months. "We chose X because Y was ruled out by Z" is what stops the next session from re-deriving it.
- **Never silently rewrite an entry.** If a later session reverses a decision, add a new paragraph saying so. The log is append-mostly; correcting a factual error in place is fine, but erasing a decision that was really made destroys the thing the file exists for.

## Important Rules

- Do NOT write code or pseudocode.
- Do NOT create technical designs, architecture diagrams, or database schemas.
- Do NOT suggest specific technologies, frameworks, or libraries.
- Stay in the problem space. The technical plan comes later as a separate step.
- Be direct. If something sounds like a solution looking for a problem, say so.
- It is okay to tell the user their idea might not be worth building if the evidence is weak — that is valuable output.
- Every user story must trace back to the north star. If a story doesn't connect, cut it or move it to out of scope.
- Never let vague language into the final PRD. Replace "many," "a lot," "significant," "various" with specific numbers or TODO placeholders.
- If the user has no customer evidence (no interviews, no data, no quotes), flag the entire PRD as a hypothesis and tell them what evidence they need to collect before building.
- Section 11 is a record, not a rewrite. Reproduce what the user said, including the weak or unsupported answers. Do not invent an answer to a question the session never actually covered — mark it in **Discovery gaps** instead.
- Always produce BOTH files: `prd.md` and `decisions-prd.md`. A PRD without a decisions log loses its reasoning the first time it gets rewritten.
- Read every existing `docs/features/*/decisions*.md` before the first question. Prior decisions are inherited, not re-argued.
- Do not manufacture an open question to look rigorous. If the user's reasoning already closes a gap, close it. Inventing a validation step the user has to argue you out of wastes their time and clutters §4 and §9.
