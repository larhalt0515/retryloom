"""Confirmed, repository-local instruction updates with immutable previews."""
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import os
from pathlib import Path
import secrets
import stat

from reflex.storage.lifecycle import load_lesson_state


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)


@dataclass(frozen=True)
class ApplyPreview:
    repository: Path
    lesson_id: str
    target: str
    before: bytes | None
    after: bytes
    target_identity: tuple | None
    parent_identities: tuple
    frozen_fingerprint: str
    evaluation_id: int
    report_hash: str
    diff: str
    already_applied: bool = False


@dataclass(frozen=True)
class ApplyResult:
    status: str
    target: str
    application_id: int | None = None


def _relative(target, lesson):
    if target in ('.reflex/skills', '.reflex/skills/'):
        return '.reflex/skills/' + _hash((lesson.lesson_hash + lesson.canonical_fingerprint).encode()) + '.md'
    if target not in ('AGENTS.md', 'CLAUDE.md'):
        raise ValueError('unsupported or unsafe apply target')
    return target


def _snapshot(root, target):
    parents = []
    current = root
    # Check every ancestor without resolving symlinks first.
    for ancestor in (*reversed(root.parents), root):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('unsafe repository parent (symlink/non-directory)')
        parents.append((str(ancestor), _identity(info)))
    missing = False
    for part in Path(target).parts[:-1]:
        current /= part
        if missing or not current.exists():
            if current.is_symlink():
                raise ValueError('symlink parent')
            missing = True
            parents.append((str(current), None))
        else:
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('unsafe target parent')
            parents.append((str(current), _identity(info)))
    path = root / target
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None, tuple(parents)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('target must be a regular non-hardlinked file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(fd)) != _identity(info):
            raise ValueError('target replaced while reading')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            content = stream.read()
    finally:
        os.close(fd)
    return content, _identity(info), tuple(parents)


def preview_apply(connection, repository: Path, lesson_id, target='AGENTS.md'):
    root = Path(os.path.abspath(repository))
    state = load_lesson_state(connection, lesson_id)
    lesson = state.frozen_lesson
    if state.effective_status not in ('replay_confirmed', 'applied'):
        raise ValueError('only replay_confirmed lessons are eligible')
    if root.resolve() != Path(lesson.trigger.repository).resolve():
        raise ValueError('lesson repository scope mismatch')
    relative = _relative(target, lesson)
    before, identity, parents = _snapshot(root, relative)
    marker = '<!-- reflex:' + _hash((lesson.lesson_hash + lesson.canonical_fingerprint).encode()) + ' -->'
    block = (marker + '\n' + lesson.rule + '\n<!-- /reflex -->\n').encode()
    if state.effective_status == 'applied':
        if state.application['target'] != relative or before is None or block not in before or before.count(marker.encode()) != 1:
            raise ValueError('applied target differs from audited content; new review required')
        return ApplyPreview(root, lesson_id, relative, before, before, identity, parents,
                            lesson.canonical_fingerprint, state.evaluation_id, state.report_hash, '', True)
    original = before or b''
    if marker.encode() in original:
        if block not in original or original.count(marker.encode()) != 1:
            raise ValueError('existing lesson marker differs from frozen rule')
        after = original
    else:
        after = original + (b'\n' if original and not original.endswith(b'\n') else b'') + block
    # UTF-8 instruction files only; preserve original bytes exactly.
    old_text, new_text = original.decode('utf-8'), after.decode('utf-8')
    diff = ''.join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                      fromfile=relative, tofile=relative))
    return ApplyPreview(root, lesson_id, relative, before, after, identity, parents,
                        lesson.canonical_fingerprint, state.evaluation_id, state.report_hash, diff)


def _open_parent(preview, created):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        full_parent = preview.repository / Path(preview.target).parent
        expected = dict(preview.parent_identities)
        current = Path('/')
        if _identity(os.fstat(fd)) != expected.get('/'):
            raise ValueError('repository ancestor changed')
        for part in full_parent.parts[1:]:
            current /= part
            if str(current) not in expected:
                raise ValueError('unrecorded target parent')
            expected_identity = expected[str(current)]
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if expected_identity is not None or current == preview.repository or preview.repository not in current.parents:
                    raise ValueError('existing repository parent disappeared')
                os.mkdir(part, 0o700, dir_fd=fd)
                created.append(current)  # Directory cleanup is best effort, never recursive.
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            if expected_identity is not None and _identity(os.fstat(child)) != expected_identity:
                os.close(child)
                raise ValueError('target parent identity changed')
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_at(fd, name):
    try:
        filefd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None, None
    try:
        info = os.fstat(filefd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('unsafe target')
        with os.fdopen(filefd, 'rb', closefd=False) as stream:
            return stream.read(), _identity(info)
    finally:
        os.close(filefd)


def _expect_at(fd, name, expected):
    try:
        actual = _read_at(fd, name)
    except (OSError, ValueError) as error:
        raise ValueError('target changed; new preview required') from error
    if actual != expected:
        raise ValueError('target changed; new preview required')


def _replace_at(fd, name, content, mode, expected, written_state=None):
    temp = '.reflex-apply-' + secrets.token_hex(16)
    try:
        filefd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
        with os.fdopen(filefd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            replacement_identity = _identity(os.fstat(stream.fileno()))
        _expect_at(fd, name, expected)
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        # Record the inode installed by replace even if the directory fsync fails.
        if written_state is not None:
            written_state.append((content, replacement_identity))
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


def commit_apply(connection, preview, confirmed: bool):
    if confirmed is not True:
        return ApplyResult('declined', preview.target)
    if connection.in_transaction:
        raise ValueError('Apply requires an independent database transaction')
    fd = None
    changed = False
    written_state = []
    created = []
    name = Path(preview.target).name
    connection.execute('BEGIN IMMEDIATE')
    try:
        current = preview_apply(connection, preview.repository, preview.lesson_id,
                                '.reflex/skills/' if preview.target.startswith('.reflex/skills/') else preview.target)
        if current != preview:
            raise ValueError('target or lesson changed; new preview required')
        if preview.already_applied:
            connection.rollback()
            return ApplyResult('already_applied', preview.target)
        fd = _open_parent(preview, created)
        # Compare the held directory and full path again before replacement.
        actual = (preview.repository / preview.target).parent.stat()
        if (actual.st_dev, actual.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
            raise ValueError('target parent changed')
        before, identity = _read_at(fd, name)
        if before != preview.before or identity != preview.target_identity:
            raise ValueError('target changed; new preview required')
        mode = stat.S_IMODE(identity[2]) if identity else 0o644
        if preview.after != preview.before:
            _replace_at(fd, name, preview.after, mode,
                        (preview.before, preview.target_identity), written_state)
            changed = True
        result, written_identity = _read_at(fd, name)
        if result != preview.after or (written_state and (result, written_identity) != written_state[0]):
            raise ValueError('write verification failed')
        # Refuse detached/replaced parent paths before recording success.
        verified, verified_identity, _ = _snapshot(preview.repository, preview.target)
        path_parent_identity = _identity((preview.repository / preview.target).parent.lstat())
        if (verified != preview.after or verified_identity != written_identity or
                path_parent_identity != _identity(os.fstat(fd))):
            raise ValueError('target path changed after write')
        lesson = load_lesson_state(connection, preview.lesson_id).frozen_lesson
        cursor = connection.execute(
            'INSERT INTO applications(lesson_id,lesson_hash,frozen_fingerprint,repository,target,evaluation_id,report_hash,before_hash,after_hash,from_status,to_status,outcome,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (lesson.id, lesson.lesson_hash, lesson.canonical_fingerprint, str(preview.repository), preview.target,
             preview.evaluation_id, preview.report_hash, _hash(preview.before) if preview.before is not None else None,
             _hash(preview.after), 'replay_confirmed', 'applied', 'applied' if changed else 'already_present',
             datetime.now(timezone.utc).isoformat()))
        updated = connection.execute("UPDATE lessons SET status='applied' WHERE id=? AND status='replay_confirmed'", (lesson.id,))
        if updated.rowcount != 1:
            raise ValueError('lesson lifecycle changed')
        connection.commit()
        return ApplyResult('applied', preview.target, cursor.lastrowid)
    except BaseException as error:
        connection.rollback()
        if written_state and fd is not None:
            try:
                # Never restore through a held fd after its directory was detached
                # or moved: it may now refer outside the selected repository.
                _, _, rollback_parents = _snapshot(preview.repository, preview.target)
                current_parent = (preview.repository / preview.target).parent.lstat()
                if _identity(current_parent) != _identity(os.fstat(fd)):
                    raise ValueError('target parent detached; rollback refused')
                original_parents = dict(preview.parent_identities)
                for path, identity in rollback_parents:
                    expected = original_parents.get(path)
                    if expected is not None and identity[:3] != expected[:3]:
                        raise ValueError('repository ancestor changed; rollback refused')
                _expect_at(fd, name, written_state[0])
                if preview.before is None:
                    # Recheck immediately before removing our newly created file.
                    _expect_at(fd, name, written_state[0])
                    os.unlink(name, dir_fd=fd)
                    os.fsync(fd)
                else:
                    _replace_at(fd, name, preview.before, stat.S_IMODE(preview.target_identity[2]),
                                written_state[0])
            except BaseException as rollback_error:
                raise RuntimeError(f'Apply failed; rollback failed: {rollback_error}') from error
        raise
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
