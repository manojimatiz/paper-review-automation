"""SHA-256 file identity used for version and duplicate detection."""

from paper_automation.fingerprint import Fingerprint


def test_identical_content_yields_identical_hash(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    assert Fingerprint.of(a).sha256 == Fingerprint.of(b).sha256


def test_different_content_yields_different_hash(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    assert Fingerprint.of(a).sha256 != Fingerprint.of(b).sha256


def test_fingerprint_records_size(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"twelve bytes")
    assert Fingerprint.of(path).size == 12


def test_fingerprint_survives_a_large_file(tmp_path):
    """Hashing streams in chunks, so this must not load the whole file at once."""
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (3 * (1 << 20) + 17))  # a few chunks plus a remainder
    fp = Fingerprint.of(path)
    assert fp.size == 3 * (1 << 20) + 17
    assert len(fp.sha256) == 64
