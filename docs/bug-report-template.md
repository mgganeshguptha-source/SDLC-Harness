# Bug report template — analysis mode

Use this for a run with `mode: analysis`. Commit the report to the story branch
as `stories/<feature_id>.md` (or `stories/<feature_id>/<feature_id>.md`). Put the
evidence files in `stories/<feature_id>/` on the same branch: log extracts as
`.log` or `.txt`, screenshots as images, request and response samples as `.json`.

Use mocked data only. The harness scans the report and every attachment for
PII/PHI before any model call and halts on a match.

The more evidence the report carries, the less the analysis has to ask. Without
a stack trace, log lines or reproduction steps, expect the run to halt with
questions instead of naming a root cause.

---

```markdown
Title: <one line — what is wrong>

**Requires a new or changed contract from:** None

Environment: <where it was seen: environment, release or branch, date>

Expected behaviour: <what should happen>

Actual behaviour: <what happens instead; the exact error text if there is one>

Steps to reproduce:
1. <step>
2. <step>

Frequency: <always / sometimes — and under what conditions>

Evidence:
- Stack trace: <paste here, or name the attached file>
- Log lines: <paste here, or name the attached file>
- Request / response: <name the attached file>

Suspected area (optional): <class, screen or endpoint, if known>

Already ruled out (optional): <what has been checked>

Attachments: <the files in stories/<feature_id>/ and what each shows>
```

---

## What comes back

A pull request into the story branch with one file,
`stories/<feature_id>/<feature_id>-bug-analysis.md`: symptom, execution path,
root cause with file and line, alternatives ruled out, fix options, impact
analysis, regression scope and open questions. No code is changed.

Review it, correct it in the pull request, fill its **Lead approval** section
and merge. Then run the harness again for the same `feature_id` with
`mode: build` to make the fix. Before that build run, add the acceptance
criteria for the fix to the report (the build run's context phase asks for them
otherwise).

## If the run halts with questions

The analysis could not establish a root cause from the evidence. Add what it
asks for to the report (or as files in the story folder) on the story branch,
then re-run with `resume: true` and the same base.
