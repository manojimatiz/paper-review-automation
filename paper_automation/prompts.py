"""Prompts for the review and revision stages.

Both prompts instruct the model to read from and write to named files in a scratch
directory. The orchestrator then renders the markdown it finds there, so the model
never names an output file or touches the client's folder.
"""

from string import Formatter

from .models import Phase

MANUSCRIPT_FILE = "manuscript.md"
REVIEW_FILE = "review.md"
OUTPUT_FILE = "output.md"

INTEGRITY_RULES = """\
SCIENTIFIC INTEGRITY — these override every other instruction:
- Never invent experimental results, datasets, citations, references, accuracy or
  other metric values, statistical tests, ablation studies, or experiments.
- Never assume a missing experiment was performed.
- Never alter a genuinely reported result to make the work look stronger.
- Never claim an experiment was conducted when the manuscript shows no evidence of it.
- Where information is absent, say exactly "Missing information" or
  "Requires author clarification". Do not fill the gap with a plausible guess.
"""

_REVIEW_SECTIONS = """\
1. Executive Summary
2. Overall Assessment
3. Major Strengths
4. Major Concerns
5. Detailed Section-wise Review
   5.1 Title   5.2 Abstract   5.3 Keywords   5.4 Introduction
   5.5 Literature Review   5.6 Research Gap and Novelty   5.7 Contributions
   5.8 Methodology   5.9 Dataset and Preprocessing   5.10 Experimental Setup
   5.11 Results   5.12 Discussion   5.13 Limitations   5.14 Conclusion
   5.15 References
6. Technical Issues
7. Reproducibility Issues
8. Numerical and Result Consistency Issues
9. Figure and Table Issues
10. Writing and Presentation Issues
11. Required Major Revisions
12. Required Minor Revisions
13. Final Reviewer Recommendation
"""


def grammar_review_prompt(client: str, original_filename: str, review_date: str) -> str:
    """Cheap language-only pass. Used while building, to keep token cost down.

    Deliberately narrow: no methodology, no experiments, no scientific judgement.
    Just the language errors, so a run costs a fraction of a full review.
    """
    return f"""\
You are a professional academic copy-editor. Check this manuscript for LANGUAGE
errors only.

Read the manuscript from `{MANUSCRIPT_FILE}` in the current directory.
Write your report as markdown to `{OUTPUT_FILE}` in the current directory.
Write no other files. Do not modify `{MANUSCRIPT_FILE}`.

Check ONLY these:
- grammar, tense and subject-verb agreement
- articles, prepositions and plurals
- punctuation and capitalisation
- spelling and consistent British/American usage
- awkward, unclear or overly long sentences
- inconsistent terminology

Do NOT assess methodology, experiments, results, novelty, citations or scientific
validity. Do not comment on whether the research is sound. Ignore those entirely.
Never change or question any number, metric, dataset name or model name.

Begin `{OUTPUT_FILE}` with this header:

# LANGUAGE REVIEW REPORT

**Manuscript:** {original_filename}
**Client:** {client}
**Review Date:** {review_date}
**Scope:** Grammar and language only

Then list the errors you found, grouped under `## Corrections`, each as one line:

- **<section or first few words of the sentence>** — "<the incorrect text>" should be
  "<the corrected text>" (<the rule, in a few words>)

Finish with `## Summary` giving the total count and the most common error types in
two or three sentences. Be concise: this is a copy-editing pass, not an essay.
"""


def grammar_revision_prompt(client: str, original_filename: str) -> str:
    """Apply the language corrections and nothing else."""
    return f"""\
You are a professional academic copy-editor. Apply language corrections to a
manuscript.

Read the original manuscript from `{MANUSCRIPT_FILE}` and the language report from
`{REVIEW_FILE}`, both in the current directory. `{MANUSCRIPT_FILE}` is
"{original_filename}", submitted by {client}.

Write the complete corrected manuscript as markdown to `{OUTPUT_FILE}` in the
current directory. Write no other files. Do not modify `{MANUSCRIPT_FILE}` or
`{REVIEW_FILE}`.

Apply the corrections in `{REVIEW_FILE}`, plus any further language errors you find.
Fix grammar, tense, agreement, articles, prepositions, punctuation, spelling and
awkward phrasing.

Change NOTHING else. Specifically, you must not:
- alter any number, metric, percentage, dataset name, model name or citation
- add, remove or reorder any section, table, figure or reference
- rewrite the scientific content, add explanation, or change what is claimed
- shorten or summarise anything

The output must be the SAME manuscript with the SAME structure and the SAME meaning,
with only the language corrected. Reproduce every section in full, including tables
and references, exactly as they appear apart from language fixes.

Leave no placeholder text such as "[TODO]" or "[INSERT RESULT]".
"""


def review_prompt(client: str, original_filename: str, review_date: str) -> str:
    return f"""\
You are a senior reviewer for a Q1 international journal. Review the manuscript
rigorously and at the standard you would apply to a top-tier submission.

Read the manuscript from `{MANUSCRIPT_FILE}` in the current directory.
Write your complete review as markdown to `{OUTPUT_FILE}` in the current directory.
Write no other files. Do not modify `{MANUSCRIPT_FILE}`.

{INTEGRITY_RULES}
Begin `{OUTPUT_FILE}` with this header:

# RESEARCH PAPER REVIEW REPORT

**Manuscript:** {original_filename}
**Client:** {client}
**Review Date:** {review_date}
**Overall Recommendation:** <Accept | Minor Revision | Major Revision | Reject>

Then use exactly this section structure, as markdown headings:

{_REVIEW_SECTIONS}
Assess every aspect that applies to this manuscript: title, abstract, keywords,
introduction, background, literature review, research gap, novelty, contributions,
methodology, dataset, preprocessing, feature engineering, model architecture,
experimental setup, training procedure, hyperparameters, validation strategy,
cross-validation, statistical validation, baselines, comparative experiments,
ablation studies, results, tables, figures, confusion matrices, ROC/AUC analysis,
performance metrics, discussion, limitations, reproducibility, ethics where
relevant, conclusion, references, grammar, academic writing, technical
consistency, numerical consistency, and claims versus evidence.

Identify critical, major and minor issues, along with missing information,
methodological weaknesses, reproducibility problems, unsupported claims,
overclaims, contradictions, numerical inconsistencies, figure/table
inconsistencies, citation problems, formatting problems and language problems.

Present each issue as:

**Location:** <section, and quote the relevant text>
**Problem:** <what is wrong>
**Why it matters:** <the consequence for validity, clarity or reproducibility>
**Recommended correction:** <specific, actionable>
**Priority:** <Critical | Major | Minor>

Be specific and evidence-based. Quote the manuscript rather than paraphrasing when
identifying a problem. A vague criticism is not useful to the author.
"""


def revision_prompt(client: str, original_filename: str) -> str:
    return f"""\
You are a senior scientific manuscript revision expert. Revise a manuscript so it
addresses a reviewer's report.

Read the original manuscript from `{MANUSCRIPT_FILE}` and the reviewer report from
`{REVIEW_FILE}`, both in the current directory. `{MANUSCRIPT_FILE}` is
"{original_filename}", submitted by {client}. `{REVIEW_FILE}` is the reviewer's
report on it.

Write the complete revised manuscript as markdown to `{OUTPUT_FILE}` in the current
directory. Write no other files. Do not modify `{MANUSCRIPT_FILE}` or `{REVIEW_FILE}`.

{INTEGRITY_RULES}
If the reviewer asks for an experiment, dataset, analysis or citation that cannot be
produced from the material in `{MANUSCRIPT_FILE}`, do NOT fabricate it. Instead do
one of these, whichever is more honest:
- state the constraint plainly in the Limitations section, or
- soften the affected claim so it matches the evidence that actually exists.

Address every reviewer issue that can be resolved by rewriting. Specifically:
- Correct technical writing, structure, clarity and logical flow.
- Improve academic language and precision.
- Resolve contradictions and inconsistencies between sections.
- Ensure tables, figures and the surrounding text agree with each other.
- Keep every reported metric identical to the original unless the review shows the
  original number was internally inconsistent — and if you change one, add a short
  note under a "Revision Notes" heading at the end explaining precisely why.

Preserve exactly, without alteration:
- all legitimate experimental results and reported metric values
- the genuine methodology and experimental design
- dataset names, model names and citations

Output the full revised manuscript, not a diff, summary or list of changes. It must
be a complete, submission-ready document that stands on its own, retaining all
original sections (title, abstract, keywords, introduction, related work,
methodology, results, discussion, conclusion, references) unless the review
explicitly calls for a structural change.

Leave no placeholder text of any kind. Never emit "[TODO]", "[INSERT RESULT]",
"[ADD CITATION]", "[PLACEHOLDER]" or similar. If you cannot supply something, write
prose that states the limitation instead.
"""


# --- task mode ----------------------------------------------------------------
#
# "grammar" is a cheap language-only pass, used while the pipeline is being built
# so a test run costs a fraction of a full review. "full" is the Q1 review and
# scientific revision. Switching is one setting in config.toml; neither set of
# prompts is deleted.

TASK_MODES = ("grammar", "full")


def for_mode(mode: str):
    """Return (review_prompt, revision_prompt) for the configured task mode."""
    if mode == "grammar":
        return grammar_review_prompt, grammar_revision_prompt
    return review_prompt, revision_prompt


# --------------------------------------------------------- admin-edited prompts

# What an admin may substitute into a prompt body. Anything else is rejected at
# save time rather than blowing up mid-run with a KeyError.
PLACEHOLDERS = {
    Phase.REVIEW: ("client", "original_filename", "review_date"),
    Phase.REVISE: ("client", "original_filename"),
}

# Appended to every custom prompt, after the admin's text. The pipeline owns the
# file contract and the integrity rules: a model that wrote to a different file
# would break the orchestrator, and one told to invent results would defeat the
# entire point of validation.py. An admin edits *what to look for*, never
# *where to write* or *whether the results have to be real* (spec section 23).
def _contract(phase: Phase) -> str:
    inputs = (
        f"Read the manuscript from `{MANUSCRIPT_FILE}` in the current directory."
        if phase is Phase.REVIEW
        else f"Read the original manuscript from `{MANUSCRIPT_FILE}` and the review "
             f"from `{REVIEW_FILE}`, both in the current directory."
    )
    do_not_modify = (
        f"Do not modify `{MANUSCRIPT_FILE}`."
        if phase is Phase.REVIEW
        else f"Do not modify `{MANUSCRIPT_FILE}` or `{REVIEW_FILE}`."
    )
    return f"""

--- REQUIRED OUTPUT FORMAT (these instructions take precedence) ---

{inputs}
Write your output as markdown to `{OUTPUT_FILE}` in the current directory.
Write no other files. {do_not_modify}

{INTEGRITY_RULES}"""


class PromptError(ValueError):
    pass


def validate_custom(body: str, phase: Phase) -> None:
    """Raise PromptError if a body would fail at run time.

    Checked when it is saved rather than when a run uses it, so a typo surfaces
    to the person making the edit instead of failing every paper that night.
    """
    if not (body or "").strip():
        raise PromptError("The prompt cannot be empty.")

    allowed = set(PLACEHOLDERS[phase])
    try:
        used = {
            name for _, name, _, _ in Formatter().parse(body) if name
        }
    except ValueError as exc:
        raise PromptError(f"Unbalanced {{ or }} in the prompt: {exc}") from exc

    unknown = sorted(used - allowed)
    if unknown:
        raise PromptError(
            f"Unknown placeholder(s): {', '.join('{%s}' % u for u in unknown)}. "
            f"Available here: {', '.join('{%s}' % a for a in sorted(allowed))}."
        )


def render_custom(
    body: str, phase: Phase, client: str, original_filename: str, review_date: str = ""
) -> str:
    """Fill in an admin-edited body and append the non-negotiable contract."""
    values = {"client": client, "original_filename": original_filename}
    if phase is Phase.REVIEW:
        values["review_date"] = review_date
    return body.format(**values) + _contract(phase)


def default_body(phase: Phase, task_mode: str) -> str:
    """The built-in prompt as editable text, for seeding the editor.

    Rendered with the placeholders left in, so "start from the current one" gives
    an admin something that already works.
    """
    review_fn, revision_fn = for_mode(task_mode)
    fn = review_fn if phase is Phase.REVIEW else revision_fn
    if phase is Phase.REVIEW:
        return fn("{client}", "{original_filename}", "{review_date}")
    return fn("{client}", "{original_filename}")
