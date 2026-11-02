"""Inspect and copy native AetherMind stores without rewriting their experience."""

from contextlib import ExitStack
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from .durability import sync_directory
from .vendor_aethermind.aethermind import core
from .vendor_aethermind.aethermind.inheritance import prepare_inheritance
from .vendor_aethermind.aethermind.tree import TreeStore

PACKAGE_SCHEMA = 'goblin-aethermind-core/v1'


def _files(store):
    files = {}
    for path in sorted(store.rglob('*')):
        if path.is_symlink():
            raise ValueError('Core contains a symbolic link: ' + str(path.relative_to(store)))
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError('Core contains a non-file entry: ' + str(path.relative_to(store)))
        files[path.relative_to(store).as_posix()] = path
    return files


def _fingerprint(data):
    return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def inspect_core(project_root):
    """Use the bundled primitive's parser, audit and currentness rules read-only."""
    root = Path(project_root).expanduser().resolve()
    store = root / '.aethermind'
    report = {'project_root': str(root), 'store_exists': store.is_dir(),
              'format': core.FORMAT_VERSION, 'declared_format': None,
              'compatible': False, 'healthy': False, 'issues': [], 'warnings': [],
              'files': {}, 'identity': {}, 'corrections': [], 'unabsorbed_debt': [],
              'currentness': {}, 'audit': {}, 'texture': {},
              'derived_tree': {'state': 'absent', 'rebuild_pending': True}}
    if not store.is_dir():
        report['issues'].append({'file': '.aethermind', 'message': 'Native store does not exist'})
        return report
    try:
        paths = _files(store)
        for name, path in paths.items():
            report['files'][name] = _fingerprint(path.read_bytes())
        metadata = paths.get('project.json')
        if metadata is not None:
            identity = json.loads(metadata.read_text(encoding='utf-8'))
            if not isinstance(identity, dict):
                raise ValueError('project.json must contain an object')
            report['identity']['metadata'] = identity
            report['declared_format'] = identity.get('format_version')
        declared = report['declared_format']
        report['compatible'] = declared is None or declared == core.FORMAT_VERSION
        if not report['compatible']:
            report['issues'].append({'file': 'project.json', 'message': 'Unsupported declared format: ' + str(declared)})
        mind = core.AetherMind(root, create=False)
        layers_report = mind.layer_store.read_report()
        for issue in layers_report.issues:
            report['issues'].append({'file': 'layers.aem', **asdict(issue)})
        if layers_report.issues:
            return report
        layers = layers_report.layers
        if not layers:
            report['derived_tree']['rebuild_pending'] = False
        report['audit'] = mind.audit()
        if not report['audit']['healthy']:
            report['issues'].append({'file': 'layers.aem', 'message': 'Native hash-chain audit failed'})
        current = mind.currentness()
        report['currentness'] = {'history_count': len(current['history']),
                                'active_ids': [layer.id for layer in current['active_heads']],
                                'inactive': current['inactive'],
                                'unresolved_currentness_ids': [layer.id for layer in current['unresolved_currentness']]}
        for layer in layers:
            if layer.corrects or layer.supersedes or layer.rollback_of or layer.recurrence_of:
                report['corrections'].append({'id': layer.id, 'corrects': layer.corrects,
                                              'supersedes': layer.supersedes, 'rollback_of': layer.rollback_of,
                                              'recurrence_of': layer.recurrence_of})
        report['unabsorbed_debt'] = mind.brief_anchor(anchor=None, budget=4000)['unabsorbed_debt']
        mind.texture_store.read()
        texture = paths['texture.aem'].read_bytes() if 'texture.aem' in paths else b''
        report['texture'] = _fingerprint(texture)
        events = mind.read_events()
        report['events'] = {'count': len(events['events']), 'issues': events['issues']}
        for issue in events['issues']:
            report['issues'].append({'file': 'events.aem', 'message': str(issue)})
        if 'archive.aem' in paths:
            archived = core.LayerStore(paths['archive.aem']).read_report()
            report['archive_count'] = len(archived.layers)
            for issue in archived.issues:
                report['issues'].append({'file': 'archive.aem', **asdict(issue)})
        envelope = prepare_inheritance(root, anchor=None, budget=4000)
        report['identity']['inheritance'] = envelope['identity']
        if envelope['identity']['status'] == 'invalid':
            report['warnings'].append('Optional native inheritance identity is invalid')
        if 'tree.aem' in paths:
            try:
                # TreeStore's parser deliberately tolerates some incomplete data;
                # check its native TOML encoding too so corruption stays visible.
                core.tomllib.loads(paths['tree.aem'].read_text(encoding='utf-8'))
                nodes = TreeStore(paths['tree.aem']).index()
                complete = all((0, index) in nodes for index in range(len(layers)))
                report['derived_tree'] = {'state': 'readable' if complete else 'incomplete',
                                          'nodes': len(nodes), 'rebuild_pending': not complete}
            except (ValueError, TypeError, KeyError, UnicodeError):
                report['derived_tree'] = {'state': 'unreadable', 'rebuild_pending': True}
        report['healthy'] = not report['issues']
    except (OSError, ValueError, TypeError, KeyError) as error:
        report['issues'].append({'file': '.aethermind', 'message': str(error)})
    return report


def export_core(project_root, destination_archive):
    """Take raw copies under native file locks; never replace an existing export."""
    root = Path(project_root).expanduser().resolve()
    store = root / '.aethermind'
    if not store.is_dir():
        raise ValueError('Native store does not exist')
    destination = Path(destination_archive).expanduser().absolute()
    if destination.exists():
        raise FileExistsError(destination)
    if store == destination.parent.resolve() or store in destination.parent.resolve().parents:
        raise ValueError('Export destination must be outside the source core')
    paths = _files(store)
    contents = {}
    with ExitStack() as stack:
        for name in ('layers.aem', 'texture.aem', 'events.aem', 'archive.aem'):
            if name not in paths:
                continue
            handle = stack.enter_context(paths[name].open('rb'))
            stack.enter_context(core._FileLock(handle))
            contents[name] = handle
        captured = {}
        for name, path in paths.items():
            if name in contents:
                captured[name] = contents[name].read()
            else:
                captured[name] = path.read_bytes()
        if set(_files(store)) != set(paths):
            raise ValueError('Core changed during export; source is preserved')
        for name, path in paths.items():
            if path.read_bytes() != captured[name]:
                raise ValueError('Core changed during export: ' + name)
    files = {}
    for name, data in captured.items():
        files['.aethermind/' + name] = _fingerprint(data)
    manifest = {'schema': PACKAGE_SCHEMA, 'files': files,
                'snapshot': 'Native file advisory locks and unchanged-file verification'}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.core-export-', delete=False) as output:
            temporary = Path(output.name)
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json', json.dumps(manifest, sort_keys=True, indent=2))
                for name, data in captured.items():
                    archive.writestr('.aethermind/' + name, data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, destination)
        sync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'path': str(destination), 'manifest': manifest}


def import_core(archive_path, cores_root):
    """Validate and create an isolated native copy; leave activation to the caller."""
    source = Path(archive_path).expanduser().resolve()
    parent = Path(cores_root).expanduser().resolve()
    target = None
    try:
        with zipfile.ZipFile(source) as archive:
            names = set()
            for item in archive.infolist():
                parts = item.filename.split('/')
                path = PurePosixPath(item.filename)
                if (path.is_absolute() or '\\' in item.filename or '..' in parts
                        or any(not part or part == '.' for part in parts) or stat.S_ISLNK(item.external_attr >> 16)):
                    raise ValueError('Unsafe core archive entry: ' + item.filename)
                if item.filename in names:
                    raise ValueError('Duplicate core archive entry: ' + item.filename)
                names.add(item.filename)
            manifest = json.loads(archive.read('manifest.json'))
            if not isinstance(manifest, dict) or manifest.get('schema') != PACKAGE_SCHEMA:
                raise ValueError('Unsupported core archive schema; original archive is preserved')
            files = manifest.get('files')
            if not isinstance(files, dict) or names != set(files) | {'manifest.json'}:
                raise ValueError('Core archive inventory does not match its manifest')
            for name, expected in files.items():
                if not name.startswith('.aethermind/') or not isinstance(expected, dict):
                    raise ValueError('Invalid native core file entry')
                data = archive.read(name)
                if _fingerprint(data) != expected:
                    raise ValueError('Core archive hash mismatch: ' + name)
            parent.mkdir(parents=True, exist_ok=True)
            target = Path(tempfile.mkdtemp(prefix='import-', dir=parent))
            (target / '.aethermind').mkdir()
            for name in files:
                destination = target.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as output:
                    output.write(archive.read(name))
                    output.flush()
                    os.fsync(output.fileno())
            sync_directory(target)
        report = inspect_core(target)
        return {'project_root': str(target), 'inspection': report, 'repairs': []}
    except BaseException:
        if target is not None:
            shutil.rmtree(target)
        raise


def create_core(destination_parent, *, seeded, skills_root):
    """Create a separate native core, optionally adding the packaged foundations."""
    from .bootstrap_seeds import ensure_bootstrap
    if not isinstance(seeded, bool):
        raise ValueError('seeded must be a boolean')
    parent = Path(destination_parent).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix='core-', dir=parent))
    try:
        core.initialize_store(target)
        bootstrap = None
        if seeded:
            bootstrap = ensure_bootstrap(target, skills_root, stage_skills=False)
            if bootstrap['status'] != 'ready':
                raise ValueError(bootstrap.get('error') or 'Foundation bootstrap could not complete')
        inspection = inspect_core(target)
        if not inspection['healthy']:
            raise ValueError('Created core did not pass native inspection')
        sync_directory(target)
        sync_directory(parent)
        return {'project_root': str(target), 'inspection': inspection,
                'seeded': seeded, 'bootstrap': bootstrap}
    except BaseException:
        shutil.rmtree(target)
        raise
