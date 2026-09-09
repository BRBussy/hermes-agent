"""Task evidence survives source disposal and fails closed on incomplete bytes."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_evidence as ev


@pytest.fixture
def evidence_task(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / '.hermes'))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / '.hermes'))
    monkeypatch.setenv('HERMES_KANBAN_ATTACHMENTS_ROOT', str(tmp_path / 'attachments'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    conn = kb.connect(tmp_path / 'board.db')
    root = tmp_path / 'workspace'
    root.mkdir()
    tid = kb.create_task(conn, title='Evidence fixture', execution_authority='Authorised isolated evidence test', workspace_kind='dir', workspace_path=str(root))
    (root / '.task-evidence').mkdir()
    (root / '.task-evidence/report.txt').write_text('review one\n')
    yield conn, tid, root
    conn.close()


def test_review_reopen_recover(evidence_task, tmp_path):
    conn, tid, root = evidence_task
    assert kb.request_review(conn, tid, summary='First review', metadata={'evidence_paths': ['.task-evidence']})
    first = ev.discover(conn, tid)
    assert len(first) == 1 and first[0]['status'] == 'retained'
    assert kb.reopen_review_task(conn, tid)
    (root / '.task-evidence/report.txt').write_text('review two\n')
    assert kb.request_review(conn, tid, summary='Second review', metadata={'evidence_paths': ['.task-evidence']})
    assert len(ev.discover(conn, tid)) == 2
    shutil.rmtree(root)
    dest = tmp_path / 'recovered'
    ev.recover(conn, tid, first[0]['attachment_id'], dest)
    record = first[0]['manifest']['artifacts'][0]
    assert (dest / record['storage_name']).read_text() == 'review one\n'
    assert kb.complete_task(conn, tid, summary='Retained review')
    assert len(ev.discover(conn, tid)) == 2


@pytest.mark.parametrize('selection', [[], ['missing'], ['.task-evidence', 'missing'], ['../private'], ['empty']])
def test_failed_selection_blocks_completion(evidence_task, selection):
    conn, tid, root = evidence_task
    (root / 'empty').mkdir()
    status = kb.get_task(conn, tid).status
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid, metadata={'evidence_paths': selection})
    assert kb.get_task(conn, tid).status == status
    assert not any(item.get('attachment_id') for item in ev.discover(conn, tid))
    assert (root / '.task-evidence/report.txt').is_file()


@pytest.mark.parametrize('target', ['file', 'directory', 'dangling'])
def test_symlinks_refused(evidence_task, target):
    conn, tid, root = evidence_task
    link = root / 'link'
    link.symlink_to(root / {'file': '.task-evidence/report.txt', 'directory': '.task-evidence', 'dangling': 'missing'}[target])
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid, metadata={'artifacts': ['link']})


def test_duplicate_and_readers(evidence_task):
    conn, tid, root = evidence_task
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence', '.task-evidence/report.txt']})
    items = ev.discover(conn, tid)
    assert len(items) == 1
    assert len(items[0]['manifest']['artifacts']) == 1
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(ev.verify, [items[0]['path']] * 12))
    assert results and all(r == results[0] for r in results)
    assert not kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    assert len(ev.discover(conn, tid)) == 1


@pytest.mark.parametrize('damage', ['missing', 'bytes', 'manifest', 'partial'])
def test_prior_evidence_corruption_blocks_completion(evidence_task, damage):
    conn, tid, root = evidence_task
    assert kb.request_review(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    item = ev.discover(conn, tid)[0]
    path = Path(item['path'])
    blob = path.parent / item['manifest']['artifacts'][0]['storage_name']
    before = blob.read_bytes()
    manifest = path.read_bytes()
    if damage == 'missing':
        blob.unlink()
    elif damage == 'bytes':
        blob.write_bytes(b'changed')
    elif damage == 'manifest':
        path.write_bytes(b'{}')
    else:
        broken = json.loads(manifest)
        broken['artifacts'] = []
        path.write_text(json.dumps(broken))
    assert ev.discover(conn, tid)[0]['status'] == 'unavailable'
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid)
    assert kb.get_task(conn, tid).status == 'review'
    blob.write_bytes(before)
    path.write_bytes(manifest)
    assert kb.complete_task(conn, tid)


def test_interrupted_copy_and_restore(evidence_task, monkeypatch):
    conn, tid, root = evidence_task
    original = ev._write
    def interrupt(path, data):
        if not Path(path).name.startswith('evidence-'):
            Path(path).write_bytes(data[:2])
            raise OSError('Injected interruption')
        original(path, data)
    monkeypatch.setattr(ev, '_write', interrupt)
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    assert not any(item.get('attachment_id') for item in ev.discover(conn, tid))
    failed = list(kb.task_attachments_dir(tid).glob('evidence-failed-*.json'))
    assert failed and json.loads(failed[0].read_bytes())['status'] == 'failed'
    monkeypatch.setattr(ev, '_write', original)
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})


def test_required_attachment_delete_refused(evidence_task):
    conn, tid, root = evidence_task
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    attachments = kb.list_attachments(conn, tid)
    assert attachments
    for attachment in attachments:
        with pytest.raises(ev.EvidenceError):
            kb.delete_attachment(conn, attachment.id)
        assert Path(attachment.stored_path).is_file()


def test_scratch_completion(evidence_task):
    conn, tid, root = evidence_task
    scratch_id = kb.create_task(conn, title='Scratch evidence', execution_authority='Authorised isolated evidence test')
    workspace = kb.resolve_workspace(kb.get_task(conn, scratch_id))
    disposable = workspace.with_name('disposable-scratch')
    workspace.rename(disposable)
    workspace = disposable
    kb.set_workspace_path(conn, scratch_id, workspace)
    (workspace / 'report.txt').write_text('Scratch bytes')
    assert kb.complete_task(conn, scratch_id, metadata={'artifacts': [str(workspace / 'report.txt')]})
    assert not workspace.exists()
    assert ev.discover(conn, scratch_id)[0]['status'] == 'retained'


def test_secret_path_refused(evidence_task):
    conn, tid, root = evidence_task
    (root / '.env').write_text('private fixture')
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid, metadata={'evidence_paths': ['.env']})


def test_repository_ignored_evidence(fixture, tmp_path):
    from hermes_cli.kanban_review import latest_submission
    conn, task, repo = fixture
    run = kb.claim_task(conn, task.id)
    assert run
    assert kb.request_review(conn, task.id, summary='Inspect source and retained evidence', reviewer='reviewer',
                             metadata={'worker_session_id': 'developer-evidence', 'evidence_paths': ['.task-evidence']},
                             expected_run_id=run.current_run_id)
    item = ev.discover(conn, task.id)[0]
    assert item['manifest']['review_state_id'] == latest_submission(conn, task.id)['state']['id']
    assert item['manifest']['head']
    source = Path(task.workspace_path) / '.task-evidence'
    shutil.rmtree(source)
    assert ev.discover(conn, task.id)[0]['status'] == 'retained'
    ev.recover(conn, task.id, item['attachment_id'], tmp_path / 'repository-recovery')


from tests.hermes_cli.test_kanban_worktree_retention import fixture


@pytest.mark.parametrize('member_name', ['report.txt', '../escape', '/absolute'])
def test_archive_import_provenance(evidence_task, tmp_path, member_name):
    import io
    import tarfile
    import hashlib
    conn, tid, root = evidence_task
    archive = tmp_path / 'surviving.tar'
    with tarfile.open(archive, 'w') as stream:
        member = tarfile.TarInfo(member_name)
        member.size = 9
        stream.addfile(member, io.BytesIO(b'recovered'))
    if member_name != 'report.txt':
        with pytest.raises(ev.EvidenceError):
            ev.import_archive(conn, tid, archive)
        return
    ev.import_archive(conn, tid, archive)
    item = ev.discover(conn, tid)[0]
    assert item['manifest']['provenance'] == {'kind': 'partial_recovery', 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    archive.unlink()
    ev.recover(conn, tid, item['attachment_id'], tmp_path / 'archive-recovery')


def test_notification_selection(evidence_task):
    conn, tid, root = evidence_task
    (root / 'public.txt').write_text('public deliverable')
    assert kb.complete_task(conn, tid, metadata={'artifacts': ['public.txt'], 'evidence_paths': ['.task-evidence']})
    events = [e for e in kb.list_events(conn, tid) if e.kind == 'completed']
    assert events and len(events[-1].payload['artifacts']) == 1
    assert Path(events[-1].payload['artifacts'][0]).read_text() == 'public deliverable'
    assert len(ev.discover(conn, tid)[0]['manifest']['artifacts']) == 2


def test_unsafe_manifest_member(evidence_task):
    conn, tid, root = evidence_task
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    item = ev.discover(conn, tid)[0]
    path = Path(item['path'])
    manifest = item['manifest']
    manifest['artifacts'][0]['storage_name'] = '../escape'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ev.EvidenceError):
        ev.verify(path)


def test_tool_and_cli_discovery(evidence_task, monkeypatch, capsys):
    import argparse
    from tools import kanban_tools
    from hermes_cli.kanban import _cmd_evidence
    conn, tid, root = evidence_task
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    monkeypatch.setenv('HERMES_KANBAN_DB', database)
    assert _cmd_evidence(argparse.Namespace(task_id=tid, recover=None, import_archive=None, manifest=None)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result and result[0]['status'] == 'retained'
    result = json.loads(kanban_tools._handle_show({'task_id': tid}))
    assert result['evidence'] and result['evidence'][0]['status'] == 'retained'


@pytest.mark.parametrize('missing', [False, True])
def test_board_relocation(evidence_task, tmp_path, monkeypatch, missing):
    from hermes_cli.kanban_transfer import _relocate_imported_rows
    conn, tid, root = evidence_task
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    source = kb.task_attachments_dir(tid)
    destination = tmp_path / 'imported-attachments'
    destination.mkdir()
    if not missing:
        shutil.copytree(source, destination / tid)
    monkeypatch.setenv('HERMES_KANBAN_ATTACHMENTS_ROOT', str(destination))
    _relocate_imported_rows(conn, 'imported')
    items = ev.discover(conn, tid)
    assert items and items[0]['status'] == ('unavailable' if missing else 'retained')


def test_copy_verification_guard(evidence_task, monkeypatch):
    conn, tid, root = evidence_task
    original = ev._write
    def corrupt(path, data):
        original(path, data)
        if not Path(path).name.startswith('evidence-'):
            Path(path).write_bytes(b'corrupt')
    monkeypatch.setattr(ev, '_write', corrupt)
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    assert kb.get_task(conn, tid).status != 'done'
    monkeypatch.setattr(ev, '_write', original)
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})


def test_checkpoint_registration(evidence_task, monkeypatch, capsys):
    import argparse
    from hermes_cli.kanban import _cmd_evidence
    conn, tid, root = evidence_task
    monkeypatch.setenv('HERMES_KANBAN_DB', conn.execute('PRAGMA database_list').fetchone()[2])
    assert _cmd_evidence(argparse.Namespace(task_id=tid, recover=None, import_archive=None, manifest=None, register=['.task-evidence'])) == 0
    assert json.loads(capsys.readouterr().out)['evidence_status'] == 'retained'
    assert ev.discover(conn, tid)[0]['manifest']['phase'] == 'checkpoint'


def test_secret_content_refused(evidence_task):
    conn, tid, root = evidence_task
    (root / 'private.txt').write_text('sk-' + 'syntheticfixture' * 3)
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid, metadata={'evidence_paths': ['private.txt']})
    assert kb.get_task(conn, tid).status != 'done'


def test_reader_during_copy(evidence_task, monkeypatch):
    import sqlite3
    conn, tid, root = evidence_task
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    original = ev._write
    observations = []
    def observe(path, data):
        with sqlite3.connect(database) as reader:
            observations.append(reader.execute('SELECT count(*) FROM task_attachments WHERE task_id = ?', (tid,)).fetchone()[0])
        original(path, data)
    monkeypatch.setattr(ev, '_write', observe)
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    assert observations and all(count == 0 for count in observations)
    assert ev.discover(conn, tid)[0]['status'] == 'retained'


def test_empty_manifest_refused(evidence_task):
    conn, tid, root = evidence_task
    path = root / 'empty-manifest.json'
    path.write_text(json.dumps({'schema': 1, 'status': 'retained', 'artifacts': []}))
    with pytest.raises(ev.EvidenceError):
        ev.verify(path)


def test_size_limit_refused(evidence_task, monkeypatch):
    conn, tid, root = evidence_task
    monkeypatch.setattr(kb, 'KANBAN_ATTACHMENT_MAX_BYTES', 4)
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    assert kb.get_task(conn, tid).status != 'done'


def test_failed_evidence_cannot_be_omitted(evidence_task):
    conn, tid, root = evidence_task
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid, metadata={'evidence_paths': ['missing.txt']})
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid)
    (root / 'missing.txt').write_text('Recovered required bytes')
    assert kb.complete_task(conn, tid, metadata={'evidence_paths': ['missing.txt']})
    assert all(item['status'] == 'retained' for item in ev.discover(conn, tid))


def test_partial_import_does_not_hide_missing_review(evidence_task, tmp_path):
    import io
    import tarfile
    conn, tid, root = evidence_task
    assert kb.request_review(conn, tid, metadata={'evidence_paths': ['.task-evidence']})
    first = ev.discover(conn, tid)[0]
    blob = Path(first['path']).parent / first['manifest']['artifacts'][0]['storage_name']
    blob.unlink()
    archive = tmp_path / 'partial.tar'
    with tarfile.open(archive, 'w') as stream:
        member = tarfile.TarInfo('recovered.txt')
        member.size = 7
        stream.addfile(member, io.BytesIO(b'partial'))
    ev.import_archive(conn, tid, archive)
    items = ev.discover(conn, tid)
    assert any(item['status'] == 'retained' for item in items)
    assert any(item['status'] == 'unavailable' for item in items)
    with pytest.raises(ev.EvidenceError):
        kb.complete_task(conn, tid)


def test_explicit_board_storage(evidence_task, monkeypatch):
    conn, tid, root = evidence_task
    monkeypatch.delenv('HERMES_KANBAN_ATTACHMENTS_ROOT')
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    with kb.connect(board='other') as other:
        other_id = kb.create_task(other, title='Other board fixture', board='other',
                                 execution_authority='Isolated board evidence test',
                                 workspace_kind='dir', workspace_path=str(root))
        assert kb.complete_task(other, other_id, metadata={'evidence_paths': ['.task-evidence']})
        item = ev.discover(other, other_id)[0]
        assert Path(item['path']).parent == kb.task_attachments_dir(other_id, board='other')
        assert not kb.task_attachments_dir(other_id, board='default').exists()
