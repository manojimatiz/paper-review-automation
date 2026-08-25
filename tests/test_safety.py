"""Guards on the invariants that protect the user's files."""

import ast
from pathlib import Path

import pytest

from paper_automation.config import Config, is_final_name, is_review_name, sanitize
from paper_automation.storage.base import TargetExistsError

PACKAGE = Path(__file__).resolve().parent.parent / "paper_automation"

# Anything that could remove or relocate a file. copy2 is absent because
# LocalStorage legitimately copies.
_MODULE_CALLS = {
    ("os", "remove"), ("os", "unlink"), ("os", "rmdir"),
    ("os", "rename"), ("os", "replace"),
    ("shutil", "rmtree"), ("shutil", "move"),
}
# Method calls that take no argument are unambiguous on a Path.
_NULLARY_METHODS = {"unlink", "rmdir"}
# Path.rename/replace take exactly one argument; str.replace takes two or more,
# which is what separates moving a file from editing a string.
_UNARY_METHODS = {"rename", "replace"}


def find_destructive(source: str, label: str = "<snippet>") -> list[str]:
    """Locate calls that could delete or move a file.

    Parsed rather than pattern-matched: a text scan cannot tell code from the
    contents of a string, so a literal containing brackets — "original(s)" — reads
    as a single-argument .replace() and trips the guard for no reason.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - the package must always parse
        return [f"{label}: could not be parsed"]

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        owner = node.func.value
        argc = len(node.args)

        if isinstance(owner, ast.Name) and (owner.id, attr) in _MODULE_CALLS:
            offenders.append(f"{label}:{node.lineno}: {owner.id}.{attr}(...)")
        elif attr in _NULLARY_METHODS and argc == 0:
            offenders.append(f"{label}:{node.lineno}: .{attr}()")
        elif attr in _UNARY_METHODS and argc == 1:
            offenders.append(f"{label}:{node.lineno}: .{attr}(<one argument>)")
    return offenders


def test_package_contains_no_destructive_filesystem_calls():
    """Spec sections 33 and 49: the pipeline must never delete or move a file."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        offenders += find_destructive(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, "Destructive filesystem calls found:\n" + "\n".join(offenders)


def test_a_string_literal_containing_brackets_is_not_mistaken_for_a_move():
    """The exact false positive that a text scan produced."""
    assert find_destructive('text.replace("original(s)", "papers")') == []


@pytest.mark.parametrize(
    "snippet",
    [
        "os.remove(path)",
        "os.unlink(path)",
        "os.rename(a, b)",
        "os.replace(a, b)",
        "shutil.rmtree(folder)",
        "shutil.move(src, dst)",
        "path.unlink()",
        "folder.rmdir()",
        "path.rename(other)",
        "path.replace(other)",
    ],
)
def test_destructive_pattern_actually_matches_destructive_code(snippet):
    """Without this, a typo in the pattern would turn the guard above into a no-op."""
    assert find_destructive(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        'text.replace("a", "b")',
        'token.replace(" ", "").rstrip("%")',
        "shutil.copy2(src, dst)",
        "document.save(str(path))",
    ],
)
def test_destructive_pattern_ignores_safe_code(snippet):
    assert not find_destructive(snippet), snippet


def test_upload_refuses_to_overwrite(tmp_path):
    from paper_automation.storage import LocalStorage

    source = tmp_path / "new.docx"
    source.write_text("new", encoding="utf-8")
    target = tmp_path / "existing.docx"
    target.write_text("original", encoding="utf-8")

    with pytest.raises(TargetExistsError):
        LocalStorage().upload_file(source, target)

    assert target.read_text(encoding="utf-8") == "original"


# --- deterministic naming (spec section 32) -----------------------------------


def test_output_names(tmp_path):
    cfg = Config(research_papers_root=tmp_path)
    assert cfg.review_name("Vani") == "Vani_review.docx"
    assert cfg.final_name("Vani") == "Correct_Vani_paper.docx"


def test_names_are_recognised_by_role():
    assert is_review_name("Vani_review.docx")
    assert not is_review_name("Vani.docx")
    assert is_final_name("Correct_Vani_paper.docx")
    assert not is_final_name("Vani_review.docx")


def test_no_double_suffixes_are_ever_produced(tmp_path):
    """Re-deriving a name from an already-generated file must not compound it."""
    cfg = Config(research_papers_root=tmp_path)
    assert cfg.review_name("Vani") != "Vani_review_review.docx"
    assert not is_review_name(cfg.final_name("Vani"))


def test_default_scratch_is_outside_appdata():
    """Codex's Windows sandbox refuses to grant write capability under AppData.

    A scratch directory there fails with "no writable root capability SIDs" and every
    review silently produces no output, so this default must not drift back.
    """
    from paper_automation.config import DEFAULT_SCRATCH

    parts = [p.lower() for p in DEFAULT_SCRATCH.parts]
    assert "appdata" not in parts
    assert "onedrive" not in parts


def test_scratch_paths_stay_short(tmp_path):
    """Deep paths risk MAX_PATH problems, so the per-client segment is compact."""
    from paper_automation.config import Config
    from paper_automation.models import FolderState, Phase
    from paper_automation.phases import _scratch_for

    cfg = Config(research_papers_root=tmp_path, scratch_dir=tmp_path / "s")
    folder = FolderState(
        employee="An Extremely Long Employee Folder Name Paper",
        client="A Very Long Client Name That Goes On And On",
        folder=tmp_path,
    )

    path = _scratch_for(cfg, "20260808-090000", folder, Phase.REVIEW)

    assert len(path.name) <= 21
    assert path.is_dir()


def test_scratch_paths_are_unique_per_client(tmp_path):
    from paper_automation.config import Config
    from paper_automation.models import FolderState, Phase
    from paper_automation.phases import _scratch_for

    cfg = Config(research_papers_root=tmp_path, scratch_dir=tmp_path / "s")

    def scratch(employee, client):
        return _scratch_for(
            cfg, "run", FolderState(employee=employee, client=client, folder=tmp_path),
            Phase.REVIEW,
        )

    # Same client name under different employees must not collide.
    assert scratch("Manoj Paper", "Vani") != scratch("Janani Paper", "Vani")


def test_sanitize_strips_invalid_filename_characters():
    assert sanitize("Client/A") == "Client_A"
    assert sanitize('Bad:name*here?') == "Bad_name_here_"
    assert sanitize("   ") == "unnamed"
