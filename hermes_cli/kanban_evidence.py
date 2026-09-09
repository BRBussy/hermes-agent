"""Immutable, verified task evidence in the board's attachment store."""

import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid


class EvidenceError(ValueError):
    """Required evidence could not be retained or verified."""


def _read(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise EvidenceError('Parent traversal is forbidden')
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(child, 'rb') as stream:
            before = os.fstat(stream.fileno())
            from hermes_cli.kanban_db import KANBAN_ATTACHMENT_MAX_BYTES
            if not stat.S_ISREG(before.st_mode) or before.st_size > KANBAN_ATTACHMENT_MAX_BYTES:
                raise EvidenceError('Evidence must be a regular file within the attachment size limit')
            data = stream.read(KANBAN_ATTACHMENT_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if len(data) > KANBAN_ATTACHMENT_MAX_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise EvidenceError('Evidence changed while reading')
            return data
    finally:
        os.close(fd)


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if _read(path) != data:
        raise EvidenceError('Copied evidence does not match source bytes')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True).encode()


def verify(path):
    """Verify a complete manifest and every relative blob before returning it."""
    path = Path(path)
    manifest = json.loads(_read(path))
    if not isinstance(manifest, dict):
        raise EvidenceError('Manifest must be an object')
    if manifest.get('schema') != 1 or manifest.get('status') != 'retained' or not manifest.get('artifacts'):
        raise EvidenceError('Manifest is incomplete')
    seen = set()
    for record in manifest['artifacts']:
        name = record['storage_name']
        if not isinstance(name, str) or Path(name).name != name or name in {'.', '..'} or name in seen:
            raise EvidenceError('Invalid or duplicate storage name')
        seen.add(name)
        data = _read(path.parent / name)
        if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
            raise EvidenceError('Retained evidence hash or size mismatch')
    return manifest


def _directory(conn, task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli.sqlite_safe_read import _canonical_db_path
    if os.environ.get('HERMES_KANBAN_ATTACHMENTS_ROOT', '').strip():
        return kb.task_attachments_dir(task_id)
    database = _canonical_db_path(conn)
    if database:
        path = Path(database)
        if path == (kb.kanban_home() / 'kanban.db').resolve():
            return kb.task_attachments_dir(task_id, board=kb.DEFAULT_BOARD)
        if path.name == 'kanban.db' and path.is_relative_to(kb.boards_root().resolve()):
            return path.parent / 'attachments' / task_id
    return kb.task_attachments_dir(task_id)


def discover(conn, task_id):
    """Return current verification status, including missing registered manifests."""
    from hermes_cli import kanban_db as kb
    results = []
    for attachment in kb.list_attachments(conn, task_id):
        if attachment.uploaded_by != 'evidence-manifest':
            continue
        item = {'attachment_id': attachment.id, 'path': attachment.stored_path}
        try:
            data = _read(attachment.stored_path)
            expected = attachment.filename.split('.')[0].removeprefix('evidence-')
            if hashlib.sha256(data).hexdigest() != expected:
                raise EvidenceError('Manifest hash mismatch')
            item['manifest'] = verify(attachment.stored_path)
            item['status'] = 'retained'
        except (OSError, ValueError, KeyError, TypeError) as exc:
            item.update(status='unavailable', error=type(exc).__name__)
        results.append(item)
    registered = {Path(item['path']).name for item in results}
    directory = _directory(conn, task_id)
    for path in sorted(directory.glob('evidence-*.json')):
        if path.name not in registered:
            item = {'path': str(path), 'status': 'failed_copy' if path.name.startswith('evidence-failed-') else 'unregistered'}
            try:
                receipt = json.loads(_read(path))
                item['resolved'] = any(r.get('manifest', {}).get('declarations') == receipt.get('declarations')
                    and r.get('manifest', {}).get('run_id') == receipt.get('run_id')
                    and r.get('manifest', {}).get('created_at_ns', 0) >= receipt.get('created_at_ns', 0)
                    for r in results if r.get('status') == 'retained')
            except (OSError, ValueError, TypeError, AttributeError):
                item['resolved'] = False
            results.append(item)
    return [item for item in results if not item.get('resolved')]


def require_retained(conn, task_id):
    for item in discover(conn, task_id):
        if item['status'] != 'retained' and not item.get('resolved'):
            raise EvidenceError('Earlier required evidence is unavailable. Inspect task evidence before completion')


def preserve(conn, task_id, metadata, phase, *, import_root=None):
    """Retain explicit artifact/evidence_paths selections before the transaction commits.

    Paths select regular files or non-empty directories inside the workspace.
    Callers must supply sanitised outputs. Known credential names and recognised
    text secrets are rejected. Binary content requires caller review.
    """
    from hermes_cli import kanban_db as kb
    metadata = dict(metadata or {})
    metadata.pop('evidence_manifest', None)
    metadata.pop('evidence_status', None)
    declared = []
    for key in ('artifacts', 'evidence_paths'):
        if key not in metadata:
            continue
        values = metadata[key]
        if not isinstance(values, (tuple, list)) or not values or any(not isinstance(p, str) or not p.strip() for p in values):
            raise EvidenceError('Evidence declarations require a non-empty list of paths')
        declared.extend(values)
    if not declared:
        if phase in {'completed', 'review_requested'}:
            require_retained(conn, task_id)
        return metadata
    task = kb.get_task(conn, task_id)
    if not task or not (task.workspace_path or import_root):
        raise EvidenceError('Declared evidence requires a task workspace')
    root = Path(import_root or task.workspace_path).absolute()
    directory = _directory(conn, task_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or any(p.is_symlink() for p in directory.parents):
        raise EvidenceError('Attachment storage must not use symbolic links')
    if directory.resolve().is_relative_to(root.resolve()):
        raise EvidenceError('Evidence storage must be outside the workspace')
    provenance = metadata.get('evidence_provenance', {'kind': 'generated'})
    if not isinstance(provenance, dict) or set(provenance) - {'kind', 'archive_sha256'}:
        raise EvidenceError('Invalid evidence provenance')
    if provenance.get('kind') not in {'generated', 'replacement', 'partial_recovery'}:
        raise EvidenceError('Invalid evidence provenance kind')
    if 'archive_sha256' in provenance:
        digest = provenance['archive_sha256']
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise EvidenceError('Invalid archive digest')
    receipt_id = uuid.uuid4().hex
    receipt = {'schema': 1, 'id': receipt_id, 'task_id': task_id,
               'run_id': task.current_run_id, 'phase': phase, 'created_at': int(time.time()), 'created_at_ns': time.time_ns(),
               'workspace_kind': task.workspace_kind, 'workspace': str(root),
               'repository_identity': task.repository_identity, 'approved_base': task.approved_base,
               'review_state_id': (metadata.get('review_state') or {}).get('id'),
               'reviewed_state_id': metadata.get('reviewed_state_id'),
               'provenance': provenance,
               'status': 'copying', 'declarations': sorted(set(declared)), 'artifacts': []}
    paths = set()
    _write(directory / f'evidence-pending-{receipt_id}.json', _json(receipt))
    try:
        for raw in declared:
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            if '..' in path.parts or not path.is_relative_to(root):
                raise EvidenceError('Evidence must be selected from the task workspace')
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                raise EvidenceError('Evidence symbolic links are forbidden')
            selected = list(path.rglob('*')) if path.is_dir() else [path]
            if not selected:
                raise EvidenceError('Evidence selection is empty')
            for source in selected:
                if source.is_symlink():
                    raise EvidenceError('Evidence symbolic links are forbidden')
                if source.is_dir():
                    continue
                paths.add(source)
        if not paths:
            raise EvidenceError('Evidence manifest must contain files')
        if len(paths) > 1024:
            raise EvidenceError('Evidence selection exceeds 1024 files')
        state_before = None
        if import_root is None and (task.workspace_kind == 'worktree' or task.repository_identity):
            from hermes_cli.kanban_review import capture_state
            state_before = capture_state(task)
            receipt['source_state_id'] = state_before['id']
            receipt['head'] = state_before['head']
        from agent.redact import redact_sensitive_text
        total = 0
        for index, source in enumerate(sorted(paths)):
            relative = str(source.relative_to(root))
            if any(p in {'.git', '.env', '.ssh', '.codex', 'auth.json', 'credentials.json'} or p.startswith('.env.') for p in source.relative_to(root).parts):
                raise EvidenceError('Credential and private state paths are forbidden')
            data = _read(source)
            total += len(data)
            from hermes_cli.kanban_db import KANBAN_ATTACHMENT_MAX_BYTES
            if total > 8 * KANBAN_ATTACHMENT_MAX_BYTES:
                raise EvidenceError('Evidence selection exceeds eight attachment size limits')
            try:
                content = data.decode('utf-8')
            except UnicodeDecodeError:
                content = None
            if content is not None and redact_sensitive_text(content, force=True) != content:
                raise EvidenceError('Evidence contains a recognised secret')
            name = f'{receipt_id}-{index}-{kb._safe_attachment_name(source.name)}'
            _write(directory / name, data)
            receipt['artifacts'].append({'id': f'{receipt_id}:{index}', 'source': relative,
                'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
                'storage_name': name, 'retention': 'required'})
        for record in receipt['artifacts']:
            if hashlib.sha256(_read(root / record['source'])).hexdigest() != record['sha256']:
                raise EvidenceError('Evidence changed during retention')
        if state_before is not None and capture_state(task) != state_before:
            raise EvidenceError('Source state changed during retention')
        receipt['status'] = 'retained'
        data = _json(receipt)
        manifest_path = directory / f'evidence-{hashlib.sha256(data).hexdigest()}.json'
        _write(manifest_path, data)
        verify(manifest_path)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        for record in receipt['artifacts']:
            conn.execute('INSERT INTO task_attachments (task_id, filename, stored_path, size, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                         (task_id, record['storage_name'], str(directory / record['storage_name']), record['size'], 'retained-evidence', receipt['created_at']))
        conn.execute('INSERT INTO task_attachments (task_id, filename, stored_path, size, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                     (task_id, manifest_path.name, str(manifest_path), len(data), 'evidence-manifest', receipt['created_at']))
        if phase in {'completed', 'review_requested'}:
            require_retained(conn, task_id)
        metadata['evidence_manifest'] = manifest_path.name
        metadata['evidence_status'] = 'retained'
        retained = [str(directory / r['storage_name']) for r in receipt['artifacts']]
        if 'artifacts' in metadata:
            public = []
            for raw in metadata['artifacts']:
                selected = Path(raw) if Path(raw).is_absolute() else root / raw
                for record in receipt['artifacts']:
                    source = root / record['source']
                    stored = str(directory / record['storage_name'])
                    if (source == selected or source.is_relative_to(selected)) and stored not in public:
                        public.append(stored)
            metadata['artifacts'] = public
        if 'evidence_paths' in metadata:
            metadata['evidence_paths'] = retained
        return metadata
    except Exception as exc:
        receipt['status'] = 'failed'
        receipt['error_type'] = type(exc).__name__
        try:
            _write(directory / f'evidence-failed-{receipt_id}.json', _json(receipt))
        except (OSError, EvidenceError):
            pass
        raise EvidenceError(f'Evidence retention failed ({type(exc).__name__}). Source workspace must be retained') from exc


def recover(conn, task_id, manifest_id, destination):
    """Recover verified bytes into a new private directory with original provenance."""
    items = [item for item in discover(conn, task_id) if item.get('attachment_id') == manifest_id]
    if len(items) != 1 or items[0]['status'] != 'retained':
        raise EvidenceError('A verified retained manifest is required')
    item = items[0]
    destination = Path(destination).absolute()
    if '..' in destination.parts or any(p.is_symlink() for p in destination.parents):
        raise EvidenceError('Unsafe recovery destination')
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    source_root = Path(item['path']).parent
    for record in item['manifest']['artifacts']:
        _write(destination / record['storage_name'], _read(source_root / record['storage_name']))
    _write(destination / Path(item['path']).name, _read(item['path']))
    verify(destination / Path(item['path']).name)
    return str(destination)


def import_archive(conn, task_id, archive_path):
    """Retain regular tar members as partial recovery with the archive's byte identity."""
    import io
    import tarfile
    import tempfile
    from hermes_cli import kanban_db as kb
    archive_bytes = _read(archive_path)
    with tempfile.TemporaryDirectory(prefix='evidence-import-') as temporary:
        imported_root = Path(temporary)
        names = set()
        total = 0
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:*') as archive:
            for member in archive:
                member_path = Path(member.name)
                if member_path.is_absolute() or '..' in member_path.parts or not member_path.parts:
                    raise EvidenceError('Unsafe archive member path')
                if member.isdir():
                    continue
                if not member.isfile() or member.name in names:
                    raise EvidenceError('Archive requires unique regular files')
                names.add(member.name)
                total += member.size
                if len(names) > 1024 or member.size > kb.KANBAN_ATTACHMENT_MAX_BYTES or total > 8 * kb.KANBAN_ATTACHMENT_MAX_BYTES:
                    raise EvidenceError('Archive exceeds evidence limits')
                destination = imported_root / member_path
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.extractfile(member) as stream:
                    data = stream.read(kb.KANBAN_ATTACHMENT_MAX_BYTES + 1)
                _write(destination, data)
        if not names:
            raise EvidenceError('Archive is empty')
        with kb.write_txn(conn):
            return preserve(conn, task_id, {
                'evidence_paths': sorted(names),
                'evidence_provenance': {'kind': 'partial_recovery', 'archive_sha256': hashlib.sha256(archive_bytes).hexdigest()},
            }, 'archive_import', import_root=imported_root)
