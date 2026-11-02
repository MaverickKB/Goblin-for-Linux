"""Observed file changes for native tool results.

A native file tool takes a snapshot before it mutates a path and another
snapshot after. This module compares the two snapshots and builds a real
unified diff. A snapshot limit is reported as an unavailable change with a
reason, never as a complete diff. Observation is evidence only: it never
changes what the tool itself does.

Only the native file tools in this registry take snapshots. Shell, remote,
and MCP tools are not crawled, so their receipts stay empty.
"""

from dataclasses import dataclass, field
import difflib
import hashlib
import os
import stat
from pathlib import Path


ADDED = 'added'
MODIFIED = 'modified'
DELETED = 'deleted'
BINARY = 'binary'
UNAVAILABLE = 'unavailable'
CHANGES = (ADDED, MODIFIED, DELETED, BINARY, UNAVAILABLE)

# A single file keeps up to this many bytes for the text diff.
MAX_FILE_BYTES = 2 * 1024 * 1024
# A directory snapshot is scoped to the affected subtree only, with these caps.
MAX_TREE_FILES = 200
MAX_TREE_TEXT_BYTES = 4 * 1024 * 1024
# A diff entry above this many characters is reported as unavailable.
MAX_DIFF_CHARS = 400000


@dataclass
class FileState:
    exists: bool = False
    text: str | None = None
    size: int = 0
    digest: str = ''
    # Empty when the whole text is available. Otherwise a plain reason.
    issue: str = ''


@dataclass
class PathObservation:
    exists: bool = False
    file: FileState | None = None
    # Relative posix path -> FileState, for a directory observation.
    children: dict = field(default_factory=dict)
    # Empty when the whole subtree was observed. Otherwise a plain reason.
    issue: str = ''


def observe_path(path):
    """Snapshot one path. Never raises; unreadable paths carry an issue."""
    try:
        return _observe_path(Path(path))
    except Exception as error:
        return PathObservation(issue='path observation failed (' + type(error).__name__ + ')')


def _observe_path(target):
    try:
        target.stat()
    except FileNotFoundError:
        return PathObservation()
    except OSError as error:
        return PathObservation(issue='path could not be inspected (' + type(error).__name__ + ')')
    if target.is_dir():
        return _observe_tree(target)
    state = _observe_file(target, MAX_FILE_BYTES)
    return PathObservation(exists=state.exists, file=state)


def _observe_tree(root):
    observation = PathObservation(exists=True)
    text_budget = MAX_TREE_TEXT_BYTES
    file_count = 0
    walk_errors = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False,
                                                onerror=walk_errors.append):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            if file_count >= MAX_TREE_FILES:
                observation.issue = ('only the first ' + str(MAX_TREE_FILES)
                                     + ' files were observed')
                return observation
            file_path = Path(dirpath) / name
            try:
                relative = file_path.relative_to(root).as_posix()
            except ValueError:
                continue
            text_cap = text_budget
            if text_cap > MAX_FILE_BYTES:
                text_cap = MAX_FILE_BYTES
            if text_cap < 0:
                text_cap = 0
            state = _observe_file(file_path, text_cap)
            observation.children[relative] = state
            file_count += 1
            if state.text is not None:
                text_budget -= state.size
    if walk_errors:
        observation.issue = ('some directories could not be read ('
                             + type(walk_errors[0]).__name__ + ')')
    return observation


def _observe_file(path, text_cap):
    try:
        info = path.stat()
    except FileNotFoundError:
        return FileState()
    except OSError as error:
        return FileState(issue='path could not be read (' + type(error).__name__ + ')')
    if not stat.S_ISREG(info.st_mode):
        return FileState(exists=True, size=info.st_size, issue='not a regular file')
    try:
        with path.open('rb') as handle:
            data = handle.read(text_cap + 1)
    except OSError as error:
        return FileState(exists=True, size=info.st_size,
                         issue='file could not be read (' + type(error).__name__ + ')')
    if len(data) > text_cap:
        if text_cap < MAX_FILE_BYTES:
            reason = 'directory text snapshot limit reached'
        else:
            reason = ('file is larger than the ' + str(MAX_FILE_BYTES)
                      + ' byte snapshot limit')
        return FileState(exists=True, size=info.st_size, issue=reason)
    digest = hashlib.sha256(data).hexdigest()
    if b'\x00' in data:
        return FileState(exists=True, size=len(data), digest=digest,
                         issue='binary content')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return FileState(exists=True, size=len(data), digest=digest,
                         issue='binary content')
    return FileState(exists=True, size=len(data), digest=digest, text=text)


def diff_observations(label, before, after):
    """Compare two observations of one path and return diff entry objects."""
    before_unknown = before is not None and before.issue and not _file_map(before)
    after_unknown = after is not None and after.issue and not _file_map(after)
    if before_unknown or after_unknown:
        return [_limited_entry(label, before, after)]
    before_files = _file_map(before)
    after_files = _file_map(after)
    relative_paths = set(before_files)
    relative_paths.update(after_files)
    entries = []
    for relative in sorted(relative_paths):
        first = before_files.get(relative)
        second = after_files.get(relative)
        if first is None and before is not None and before.issue:
            first = FileState(issue='not covered by the incomplete before observation')
        if second is None and after is not None and after.issue:
            second = FileState(issue='not covered by the incomplete after observation')
        if relative:
            entry_label = str(label) + '/' + relative
        else:
            entry_label = str(label)
        entries.extend(_compare_file(entry_label, first, second))
    if _observation_reasons(before, after):
        entries.append(_limited_entry(label, before, after))
    return entries


def _observation_reasons(before, after):
    reasons = []
    if before is not None and before.issue:
        reasons.append('before: ' + before.issue)
    if after is not None and after.issue:
        reasons.append('after: ' + after.issue)
    return reasons


def _limited_entry(label, before, after):
    reasons = _observation_reasons(before, after)
    row = _entry(str(label), '', UNAVAILABLE,
                 'observation limited: ' + '; '.join(reasons))
    row['bytes_before'] = _total_size(before)
    row['bytes_after'] = _total_size(after)
    return row


def _file_map(observation):
    files = {}
    if observation is None:
        return files
    if observation.file is not None:
        files[''] = observation.file
    for relative in observation.children:
        files[relative] = observation.children[relative]
    return files


def _total_size(observation):
    if observation is None:
        return 0
    if observation.file is not None:
        return observation.file.size
    total = 0
    for relative in observation.children:
        total += observation.children[relative].size
    return total


def _compare_file(label, before, after):
    if before is None:
        before = FileState()
    if after is None:
        after = FileState()
    if (before.issue and not before.exists) or (after.issue and not after.exists):
        return [_problem_entry(label, UNAVAILABLE, before, after)]
    if not before.exists and not after.exists:
        if before.issue or after.issue:
            return [_problem_entry(label, UNAVAILABLE, before, after)]
        return []
    if before.exists and after.exists:
        if before.digest and after.digest and before.digest == after.digest:
            return []
        if before.issue or after.issue:
            if _is_binary(before) or _is_binary(after):
                return [_problem_entry(label, BINARY, before, after)]
            return [_problem_entry(label, UNAVAILABLE, before, after)]
        patch = _unified_patch(label, before.text or '', after.text or '', MODIFIED)
        if not patch:
            return [_entry(label, '', MODIFIED,
                           'content changed without a line-level text difference')]
        return _text_entry(label, MODIFIED, patch, '', before, after)
    if before.exists:
        return _one_sided_entry(label, DELETED, before, after)
    return _one_sided_entry(label, ADDED, after, before)


def _is_binary(state):
    return state.issue == 'binary content'


def _one_sided_entry(label, change, present, missing):
    if change == DELETED:
        before = present
        after = missing
    else:
        before = missing
        after = present
    if present.issue:
        if _is_binary(present):
            return [_problem_entry(label, BINARY, before, after)]
        return [_problem_entry(label, UNAVAILABLE, before, after)]
    if change == DELETED:
        patch = _unified_patch(label, present.text or '', '', DELETED)
        detail = '' if patch else 'deleted an empty text file'
    else:
        patch = _unified_patch(label, '', present.text or '', ADDED)
        detail = '' if patch else 'created an empty text file'
    return _text_entry(label, change, patch, detail, before, after)


def _text_entry(label, change, patch, detail, before, after):
    if len(patch) > MAX_DIFF_CHARS:
        detail = ('the unified diff is ' + str(len(patch))
                  + ' characters, above the ' + str(MAX_DIFF_CHARS)
                  + ' character representation limit')
        return [_problem_entry(label, UNAVAILABLE, before, after, detail)]
    return [_entry(label, patch, change, detail)]


def _problem_entry(label, change, before, after, detail=''):
    if not detail:
        detail = 'content could not be compared as text'
    reasons = []
    if before.issue:
        reasons.append('before: ' + before.issue)
    if after.issue:
        reasons.append('after: ' + after.issue)
    if reasons:
        detail = detail + ' (' + '; '.join(reasons) + ')'
    row = _entry(label, '', change, detail)
    row['bytes_before'] = before.size if before.exists else 0
    row['bytes_after'] = after.size if after.exists else 0
    return row


def _entry(path, patch, change, detail):
    if change not in CHANGES:
        raise ValueError('unknown change kind: ' + str(change))
    row: dict = {'path': str(path), 'patch': str(patch), 'change': change}
    if detail:
        row['detail'] = detail
    return row


def _patch_lines(text):
    parts = text.split('\n')
    result = []
    for index, part in enumerate(parts):
        if index < len(parts) - 1:
            result.append(part + '\n')
        elif part:
            result.append(part)
    return result


def _unified_patch(label, before_text, after_text, change):
    from_label = '/dev/null' if change == ADDED else label
    to_label = '/dev/null' if change == DELETED else label
    before_lines = _patch_lines(before_text)
    after_lines = _patch_lines(after_text)
    lines = difflib.unified_diff(before_lines, after_lines, fromfile=from_label,
                                 tofile=to_label, n=3)
    result = []
    for line in lines:
        if line.endswith('\n'):
            result.append(line)
        else:
            result.append(line + '\n')
            result.append('\\ No newline at end of file\n')
    return ''.join(result)


def counterpart_path(label, from_root, to_root):
    """Map a diff entry path from one root to the other root of the same move."""
    if label == from_root:
        return to_root
    prefix = from_root + '/'
    if label.startswith(prefix):
        return to_root + '/' + label[len(prefix):]
    return ''
