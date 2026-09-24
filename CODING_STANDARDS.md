# Coding Standards

These are the rules for all Python written in this repository. They are opinionated on
purpose. The overarching goal: **code that is easy to read, easy to test, and easy to port to
Rust (via PyO3) later.** When a choice is unclear, pick the option that a Rust translator would
thank you for.

## 1. Classes as namespaces, not objects

- **Every file has a class.** The class is the unit of encapsulation and the file's public face.
- **Prefer class methods (`@classmethod`) and static methods over instance methods.** We use
  classes to group related behaviour, not to carry mutable state. Reach for an instance only
  when there is genuine per-instance state to hold.
- **Avoid stateful objects.** If a function does not need `self`, it should not take `self`.
  Pass data in, return data out. This maps cleanly onto Rust's free functions and `impl` blocks
  with no hidden lifetime or ownership surprises.

```python
class PullRequestSurveyor:
    """Groups the operations that read PR history. Holds no state."""

    @classmethod
    def survey_repository(cls, repository_name: str) -> RepositorySurvey:
        pull_requests = cls.fetch_all_pull_requests(repository_name)
        return cls.summarize_pull_requests(pull_requests)

    @staticmethod
    def fetch_all_pull_requests(repository_name: str) -> list[PullRequest]: ...
```

## 2. Naming: verbose and unambiguous

- **Be descriptive. Verbosity beats cleverness.** `fetch_all_pull_requests` over `fetch_prs`,
  `review_decision_accuracy` over `acc`.
- No single-letter names except a trivial loop index, and prefer a named element even then.
- Function and method names are verb phrases (`compute_`, `fetch_`, `validate_`). Classes and
  Pydantic models are noun phrases.

## 3. Shallow functions, no deep nesting

- **Flatten nesting by extracting functions.** If you are writing a loop whose body is more than
  a few lines, pull the body into its own well-named function and call it from the loop. A loop
  that calls `process_one_pull_request(pr)` n times reads far better than a loop with fifty
  lines inside it.
- Prefer early returns / guard clauses over nested `if/else`.
- Aim for one level of indentation inside a function body wherever you can.

```python
# Good: the loop is a sentence; the work lives in a named function.
@classmethod
def summarize_pull_requests(cls, pull_requests: list[PullRequest]) -> RepositorySurvey:
    summaries = [cls.summarize_one_pull_request(pr) for pr in pull_requests]
    return RepositorySurvey(pull_request_summaries=summaries)
```

## 4. Type hints, always

- **Every function, method, parameter, and return value is fully type-hinted.** No exceptions.
- `mypy` runs in strict mode and must pass. Untyped code does not merge.
- Use precise types (`list[PullRequest]`, `dict[str, int]`), not bare `list`/`dict`/`Any`.
  `Any` is a smell — if you need it, leave a comment saying why.

## 5. Pydantic for structured data

- **Model every non-trivial data structure as a Pydantic class.** Validation happens at the
  boundary; the rest of the code trusts the types.
- Prefer immutable models (`model_config = ConfigDict(frozen=True)`) — this matches Rust's
  default of immutability and makes the eventual port a mechanical translation.
- Keep models as plain data. Behaviour lives on the namespace classes (section 1), not on the
  data models.

## 6. Comments where they earn their place

- Comment the **why**, not the **what**. Well-named code explains what it does; comments explain
  the non-obvious reason it does it that way.
- Every class and every public method gets a one-line docstring.
- Do not narrate obvious lines. A comment that restates the code is noise.

## 7. Testing: sparingly, and for real

- **Write the test first.** Write the failing test, then build until it passes.
- **Verify the test tests something.** Break the code deliberately and watch the test go red
  before you trust it green. A test you have never watched fail is decorative.
- **Test actual functionality, not trivia.** We do not want a thousand tests pinning down
  getters. Test behaviour and the invariants that matter; skip the noise.
- **Write for testability:** pure functions, data in / data out, no hidden state or I/O buried
  in logic. If something is hard to test, that is a design signal — fix the design.

## 8. Porting to Rust (PyO3) is a design constraint

Keep the future port in mind while writing today's Python:

- Free-standing functions and immutable data translate directly; deep object graphs and mutable
  shared state do not.
- Keep I/O (network, subprocess, filesystem) at the edges, in thin, obvious layers. Pure logic
  in the middle is what gets ported first.
- Avoid dynamic tricks (monkeypatching, `__getattr__` magic, runtime type mutation) that have no
  Rust equivalent.

## 9. Tooling

Linting and type-checking are wired into `mise` tasks and run against `ruff`/`mypy` config in
`pyproject.toml`. Run them frequently — not just at the end.

```bash
mise run lint        # ruff check + format --check
mise run lint-fix    # ruff check --fix + format
mise run typecheck   # mypy (strict)
mise run check       # lint + typecheck + tests
```

- **Line length: 130.**
- **Ruff** for linting and formatting. Fix lint findings as you go, not in a big cleanup pass.
- **Mypy strict** for static type checking. Green mypy is a merge gate.
