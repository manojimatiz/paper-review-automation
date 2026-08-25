"""Validation must catch fabrication and truncation, not just missing files."""

from paper_automation import docx_io, validation

ORIGINAL = """\
# A Study of Things

## Abstract
We evaluate ResNet on CIFAR-10.

## Introduction
Prior work is limited.

## Results
The model achieved an accuracy of 94.20% and an F1 of 0.912 on CIFAR-10.

## Conclusion
The approach works.

## References
[1] Someone. A paper. 2020.
"""


def build_final(tmp_path, markdown):
    target = tmp_path / "Correct_X_paper.docx"
    docx_io.build(markdown, target)
    return target


def test_clean_revision_passes(tmp_path):
    revised = ORIGINAL.replace("Prior work is limited.", "Prior work is limited in scope.")
    result = validation.validate_revision(ORIGINAL, revised, build_final(tmp_path, revised))
    assert result.ok, result.summary()


def test_inflated_metric_is_caught(tmp_path):
    """The core fabrication guard: a result must not improve during revision."""
    revised = ORIGINAL.replace("94.20%", "97.80%")
    result = validation.validate_revision(ORIGINAL, revised, build_final(tmp_path, revised))
    assert not result.ok
    assert any("accuracy" in i and "97.80" in i for i in result.issues)


def test_invented_metric_is_caught(tmp_path):
    revised = ORIGINAL.replace(
        "## Conclusion", "The AUC was 0.981.\n\n## Conclusion"
    )
    result = validation.validate_revision(ORIGINAL, revised, build_final(tmp_path, revised))
    assert not result.ok
    assert any("auc" in i.lower() for i in result.issues)


def test_placeholder_text_is_caught(tmp_path):
    revised = ORIGINAL.replace("The approach works.", "The approach works [TODO].")
    result = validation.validate_revision(ORIGINAL, revised, build_final(tmp_path, revised))
    assert not result.ok
    assert any("Placeholder" in i for i in result.issues)


def test_various_placeholder_forms_are_caught():
    for text in ("[INSERT RESULT]", "[ADD CITATION]", "[PLACEHOLDER]", "[FIX THIS]", "[TBD]"):
        assert validation.check_placeholders(f"Body {text} more"), text


def test_truncated_revision_is_caught(tmp_path):
    revised = "# A Study of Things\n\n## Abstract\nShort.\n"
    result = validation.validate_revision(ORIGINAL, revised, build_final(tmp_path, revised))
    assert not result.ok
    assert any("truncated" in i for i in result.issues)


def test_missing_final_file_is_caught(tmp_path):
    result = validation.validate_revision(ORIGINAL, ORIGINAL, tmp_path / "absent.docx")
    assert not result.ok
    assert any("was not created" in i for i in result.issues)


def test_unreadable_final_file_is_caught(tmp_path):
    broken = tmp_path / "broken.docx"
    broken.write_text("not a docx", encoding="utf-8")
    result = validation.validate_revision(ORIGINAL, ORIGINAL, broken)
    assert not result.ok
    assert any("not a readable" in i for i in result.issues)


def test_dropped_dataset_name_is_a_warning_not_a_failure(tmp_path):
    """Worth flagging, but not proof of fabrication, so it must not block."""
    revised = ORIGINAL.replace("CIFAR-10", "the dataset")
    result = validation.validate_revision(ORIGINAL, revised, build_final(tmp_path, revised))
    assert any("CIFAR-10" in w for w in result.warnings)


def test_missing_section_is_a_warning(tmp_path):
    revised = ORIGINAL.replace("## References\n[1] Someone. A paper. 2020.\n", "")
    result = validation.validate_revision(ORIGINAL, revised, build_final(tmp_path, revised))
    assert any("reference" in w.lower() for w in result.warnings)


# --- false positives observed in a real Claude revision -----------------------
#
# Each of these was flagged as fabrication on a genuine run. A validator that fires
# on ordinary academic prose trains the user to ignore it, which defeats the point.


def test_section_cross_reference_is_not_a_metric(tmp_path):
    revised = ORIGINAL.replace(
        "The approach works.",
        "The split is not documented (see Section 3.3), so accuracy is not verifiable.",
    )
    assert validation.check_metrics(ORIGINAL, revised) == []


def test_section_range_cross_reference_is_not_a_metric(tmp_path):
    """The cue sits before the first number, so the range tail needs handling too."""
    revised = ORIGINAL.replace(
        "The approach works.",
        "Because the protocol is undocumented (Sections 3.2-3.3), the accuracy "
        "of 94.20% cannot be reproduced.",
    )
    assert validation.check_metrics(ORIGINAL, revised) == []


def test_hyperparameter_is_not_a_metric():
    revised = ORIGINAL.replace(
        "The approach works.",
        "We trained with a learning rate of 0.001, and accuracy was unchanged.",
    )
    assert validation.check_metrics(ORIGINAL, revised) == []


def test_derived_percentage_point_gap_is_not_fabrication():
    revised = ORIGINAL.replace(
        "The approach works.",
        "The observed 5.1-percentage-point accuracy gap is not conclusive.",
    )
    assert validation.check_metrics(ORIGINAL, revised) == []


def test_rescaling_a_value_is_not_fabrication():
    """Reporting F1 0.912 as "91.2%" is presentation, not a new result."""
    revised = ORIGINAL.replace(
        "an F1 of 0.912", "an F1 of 91.2% (0.912)"
    )
    assert validation.check_metrics(ORIGINAL, revised) == []


def test_rescaling_still_catches_a_genuinely_different_value():
    revised = ORIGINAL.replace("an F1 of 0.912", "an F1 of 95.7%")
    issues = validation.check_metrics(ORIGINAL, revised)
    assert any("95.7" in i for i in issues)


def test_known_value_accepts_either_scale():
    originals = {0.912, 94.20}
    assert validation.is_known_value("91.2", originals)
    assert validation.is_known_value("0.912", originals)
    assert not validation.is_known_value("95.7", originals)


def test_metric_extraction():
    values = validation.metric_values("Accuracy was 94.20% and the F1 reached 0.91.")
    assert values["accuracy"] == {"94.20"}
    assert values["f1"] == {"0.91"}


def test_folder_completeness_check(tmp_path):
    original, review, final = (tmp_path / n for n in ("o.docx", "r.docx", "f.docx"))
    docx_io.build("# Original", original)
    docx_io.build("# Review", review)

    result = validation.validate_folder_complete(original, review, final)

    assert not result.ok
    assert any("Missing final" in i for i in result.issues)
