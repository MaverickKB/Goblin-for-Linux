"""Install native Goblin foundations through the embedded continuity library."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from . import locking
from .skills import parse_skill_text, valid_name
from .vendor_aethermind.aethermind.core import AetherMind


def _assets_root():
    resources = os.environ.get('GOBLIN_RESOURCES')
    if resources:
        return Path(resources) / 'Seeds'
    return Path(__file__).resolve().parents[2] / 'Resources' / 'Seeds'


def _load_pack(root):
    pack = json.loads((root / 'pack.json').read_text(encoding='utf-8'))
    if type(pack.get('version')) is not int or pack['version'] < 1:
        raise ValueError('Invalid foundation pack version')
    for section in ('layers', 'textures'):
        if not isinstance(pack.get(section), list) or not pack[section]:
            raise ValueError('Missing foundation ' + section)
        for item in pack[section]:
            if not isinstance(item, dict) or not isinstance(item.get('body'), str) or not item['body'].strip():
                raise ValueError('Invalid foundation body')
            if not isinstance(item.get('ctx'), str) or not item['ctx'].startswith('seed/'):
                raise ValueError('Invalid foundation context')
    if not isinstance(pack.get('skills'), list):
        raise ValueError('Missing foundation skill inventory')
    for name in pack['skills']:
        if not isinstance(name, str) or not valid_name(name):
            raise ValueError('Invalid foundation skill name')
        path = root / 'skills' / name / 'SKILL.md'
        parse_skill_text(path.read_text(encoding='utf-8'), name)
    return pack


def _body_hash(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _texture_hashes(text):
    bodies = set()
    block = []
    for line in text.splitlines():
        if re.fullmatch(r'\d{4}-\d{2}-\d{2} \[.+\]', line):
            _remember_texture(block, bodies)
            block = []
        else:
            block.append(line)
    _remember_texture(block, bodies)
    return bodies


def _remember_texture(block, bodies):
    while block and block[-1].strip() == '---':
        block.pop()
    body = '\n'.join(block).strip()
    if body:
        bodies.add(_body_hash(body))


def ensure_bootstrap(project_root, skills_root, assets_root=None, *, stage_skills=True):
    """Seed the caller-authorized app-owned project; preserve invalid stores intact.

    The host owns project selection. Startup seeds its default embedded core;
    explicit creation seeds a newly allocated app-owned core. Inspection does
    not call this. Core-only creation leaves the shared skill shelf unchanged.
    """
    result = {'status': 'unavailable', 'seed_version': None,
              'layers_appended': 0, 'layers_skipped': 0,
              'textures_appended': 0, 'textures_skipped': 0,
              'skills_staged': 0, 'skills_kept': 0}
    try:
        assets = Path(assets_root) if assets_root is not None else _assets_root()
        pack = _load_pack(assets)
        result['seed_version'] = pack['version']
        project = Path(project_root).expanduser().resolve()
        shelf = Path(skills_root).expanduser()
        project.mkdir(parents=True, exist_ok=True)
        with (project / '.goblin-bootstrap.lock').open('a') as handle:
            locking.flock(handle, locking.LOCK_EX)
            try:
                mind = AetherMind(project, create=True)
                known_layers = set()
                for layer in mind.read_layers():
                    known_layers.add(_body_hash(layer.body))
                known_textures = _texture_hashes(mind.read_texture())
                for layer in pack['layers']:
                    digest = _body_hash(layer['body'])
                    if digest in known_layers:
                        result['layers_skipped'] += 1
                        continue
                    mind.write_layer(type=layer['type'], body=layer['body'], ctx=layer['ctx'],
                                     conf=layer.get('conf', 1.0), markers=['seed'], author='seed')
                    known_layers.add(digest)
                    result['layers_appended'] += 1
                for texture in pack['textures']:
                    digest = _body_hash(texture['body'])
                    if digest in known_textures:
                        result['textures_skipped'] += 1
                        continue
                    mind.write_texture(body=texture['body'], ctx=texture['ctx'], author='seed')
                    known_textures.add(digest)
                    result['textures_appended'] += 1
                if stage_skills:
                    shelf.mkdir(parents=True, exist_ok=True)
                    for name in pack['skills']:
                        destination = shelf / name
                        if destination.exists() or destination.is_symlink():
                            result['skills_kept'] += 1
                            continue
                        try:
                            shutil.copytree(assets / 'skills' / name, destination)
                        except FileExistsError:
                            result['skills_kept'] += 1
                        else:
                            result['skills_staged'] += 1
                result['status'] = 'ready'
            finally:
                locking.flock(handle, locking.LOCK_UN)
    except Exception as error:
        # Store parse errors can contain private record text.
        result['error'] = 'Foundation bootstrap unavailable (' + type(error).__name__ + '). Existing memory was preserved.'
        result['error_class'] = type(error).__name__
    return result
