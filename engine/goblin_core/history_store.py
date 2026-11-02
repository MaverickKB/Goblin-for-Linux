"""One durable active history selection, retaining every generation's files."""
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .durability import sync_directory


class HistoryStore:
    def __init__(self, home, slug):
        self.home = Path(home)
        self.slug = slug
        self.path = self.home / 'history' / (slug + '.json')
        self._lock = threading.RLock()
        if self.path.exists():
            self.document = json.loads(self.path.read_text(encoding='utf-8'))
            if self.document.get('version') != 1:
                raise ValueError('Unsupported history catalog version')
            self.root(self.document['active'])
        else:
            self.document = {'version': 1, 'active': 'legacy', 'histories': [
                {'id': 'legacy', 'label': 'Original history', 'created': 0,
                 'path': 'chat/' + slug}]}

    @property
    def active(self):
        return self.document['active']

    def root(self, identifier):
        for item in self.document['histories']:
            if item['id'] == identifier:
                root = (self.home / item['path']).resolve()
                if self.home.resolve() not in root.parents:
                    raise ValueError('History path must belong to this profile')
                return root
        raise ValueError('Unknown history: ' + str(identifier))

    def list(self):
        with self._lock:
            result = []
            for item in self.document['histories']:
                result.append({'id': item['id'], 'label': item['label'],
                               'created': item['created'], 'active': item['id'] == self.active})
            return result

    def create(self):
        with self._lock:
            identifier = uuid.uuid4().hex
            document = json.loads(json.dumps(self.document))
            document['histories'].append({'id': identifier, 'label': 'History ' + time.strftime('%Y-%m-%d %H:%M'),
                                          'created': time.time(), 'path': 'histories/' + self.slug + '/' + identifier})
            self._write(document)
            return identifier

    def select(self, identifier):
        with self._lock:
            self.root(identifier)
            document = json.loads(json.dumps(self.document))
            document['active'] = identifier
            self._write(document)

    def _write(self, document):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(document, output, ensure_ascii=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
        sync_directory(self.path.parent)
        self.document = document
