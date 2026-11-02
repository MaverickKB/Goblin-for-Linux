"""Read shipped product manuals through their catalog, without arbitrary file access."""
import json
import os
from pathlib import Path


class HelpDocuments:
    def __init__(self, root=None):
        if root is None:
            resources = os.environ.get('GOBLIN_RESOURCES')
            root = Path(resources) / 'Help' if resources else Path(__file__).resolve().parents[2] / 'Resources' / 'Help'
        self.root = Path(root).resolve()

    def documents(self):
        index = json.loads((self.root / 'index.json').read_text(encoding='utf-8'))
        documents = index.get('documents')
        if not isinstance(documents, list):
            raise ValueError('Help catalog has no documents list')
        for item in documents:
            name = item.get('file', '')
            if not isinstance(name, str) or not name or Path(name).name != name or name in ('.', '..'):
                raise ValueError('Help catalog file must be a filename')
        return documents

    def _read(self, item):
        path = (self.root / item['file']).resolve()
        if path.parent != self.root:
            raise ValueError('Help document is outside its catalog directory')
        return path.read_text(encoding='utf-8')

    def call(self, action='index', document='', query='', start_line=1, line_count=100):
        documents = self.documents()
        if action == 'index':
            return {'documents': documents}
        if action == 'read':
            if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
                raise ValueError('start_line must be a positive integer')
            if isinstance(line_count, bool) or not isinstance(line_count, int) or not 1 <= line_count <= 400:
                raise ValueError('line_count must be between 1 and 400')
            for item in documents:
                if item['id'] != document:
                    continue
                lines = self._read(item).splitlines()
                end = min(len(lines), start_line - 1 + line_count)
                return {'document': document, 'start_line': start_line, 'total_lines': len(lines),
                        'next_line': end + 1 if end < len(lines) else None,
                        'text': '\n'.join(lines[start_line - 1:end])}
            raise ValueError('Unknown help document; use action=index')
        if action == 'search':
            if not isinstance(query, str) or not query.strip():
                raise ValueError('Search query cannot be empty')
            matches = []
            for item in documents:
                if document and item['id'] != document:
                    continue
                for number, line in enumerate(self._read(item).splitlines(), 1):
                    if query.casefold() in line.casefold():
                        matches.append({'document': item['id'], 'line': number, 'text': line})
            return {'matches': matches}
        raise ValueError('Help action must be index, read or search')


def register_help_tool(registry):
    from .tools import object_schema
    documents = HelpDocuments()
    registry.register('app_help', 'Read this app’s user and developer manuals for setup, self-awareness, '
                      'architecture and modifying the app. Use index, read (paged lines), or search.',
                      object_schema({'action': {'type': 'string', 'enum': ['index', 'read', 'search']},
                                     'document': {'type': 'string'}, 'query': {'type': 'string'},
                                     'start_line': {'type': 'integer'}, 'line_count': {'type': 'integer'}}),
                      documents.call)
