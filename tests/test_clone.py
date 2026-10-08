import subprocess

from reflex.git.clone import CloneError, CloneManager
from reflex.git.repository import repository_state


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def make_repository(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Reflex Test")
    (root / "tracked.txt").write_text("base\n")
    git(root, "add", "tracked.txt")
    git(root, "commit", "-qm", "base")
    (root / "untracked.txt").write_text("preserve\n")
    return root


def test_each_run_is_an_independent_clone_and_original_is_unchanged(tmp_path):
    source = make_repository(tmp_path)
    before = repository_state(source)
    base_commit = before.head
    manager = CloneManager(source, tmp_path / "replay")

    first = manager.create("control-1", base_commit)
    second = manager.create("reflex-1", base_commit)
    (first / "tracked.txt").write_text("changed in first\n")
    git(first, "add", "tracked.txt")
    (second / "tracked.txt").write_text("changed in second\n")

    assert repository_state(source) == before
    assert repository_state(first).head == base_commit
    assert repository_state(second).head == base_commit
    assert (first / ".git").is_dir() and (second / ".git").is_dir()
    assert (first / "tracked.txt").read_text() != (second / "tracked.txt").read_text()

    assert manager.cleanup(first).removed
    assert manager.cleanup(second).removed


def test_cleanup_failure_is_reported_and_does_not_claim_removal(tmp_path, monkeypatch):
    source = make_repository(tmp_path)
    manager = CloneManager(source, tmp_path / "replay")
    clone = manager.create("control-1", repository_state(source).head)

    def fail_rmtree(path):
        raise OSError("forced cleanup failure")

    monkeypatch.setattr("reflex.git.clone.shutil.rmtree", fail_rmtree)
    result = manager.cleanup(clone)

    assert result.removed is False
    assert "forced cleanup failure" in result.error
    assert clone.exists()


def test_cleanup_rejects_paths_outside_replay_root(tmp_path):
    source = make_repository(tmp_path)
    manager = CloneManager(source, tmp_path / "replay")
    protected = tmp_path / "keep-me"
    protected.mkdir()
    (protected / "important.txt").write_text("preserve")

    result = manager.cleanup(protected)

    assert not result.removed
    assert protected.exists()
    assert (protected / "important.txt").read_text() == "preserve"


def test_run_name_cannot_escape_replay_root(tmp_path):
    source = make_repository(tmp_path)
    manager = CloneManager(source, tmp_path / "replay")

    try:
        manager.create("../outside", repository_state(source).head)
    except CloneError as error:
        assert "invalid run name" in str(error)
    else:
        raise AssertionError("path traversal run name was accepted")


def test_snapshot_detects_existing_untracked_content_change(tmp_path):
    source = make_repository(tmp_path)
    before = repository_state(source)
    assert repository_state(source) == before
    (source / "untracked.txt").write_text("modified\n")
    after = repository_state(source)
    assert before.untracked == after.untracked
    assert before.untracked_hashes != after.untracked_hashes
    assert before != after


def test_snapshot_does_not_hash_external_symlink_target(tmp_path):
    source = make_repository(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("external")
    (source / "link").symlink_to(outside)
    before = repository_state(source)
    outside.write_text("changed external")
    assert repository_state(source) == before
    assert "link" not in dict(before.untracked_hashes)
