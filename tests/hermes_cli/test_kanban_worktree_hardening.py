from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from hermes_cli import kanban_db as kb


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture(scope='session')
def seed_repo(tmp_path_factory):
    source = Path(kb.__file__).resolve().parents[1]
    if not (source / ".git").exists():
        source = (source / "venv").resolve().parent
    revision = git(source, 'rev-list', '--max-parents=0', 'HEAD').splitlines()[0]
    seed = tmp_path_factory.mktemp('worktree-seed')
    git(seed, 'init')
    git(seed, 'fetch', '--depth', '1', str(source), revision)
    git(seed, 'checkout', '--detach', 'FETCH_HEAD')
    return seed


@pytest.fixture
def repo(tmp_path, seed_repo):
    target = tmp_path / 'repo'
    subprocess.run(['git', 'clone', '--shared', '--no-tags', str(seed_repo), str(target)], check=True, capture_output=True)
    return target


def task(repo, identifier='t_fixture', branch=None):
    return SimpleNamespace(id=identifier, branch_name=branch, workspace_path=str(repo), repository_identity=None)


def resolve_job(arguments):
    repo, identifier, requested = arguments
    result = kb._resolve_worktree_workspace(task(Path(requested or repo), identifier))
    return tuple(map(str, result))


def test_same_and_distinct_requests_are_isolated(repo):
    requests = [(str(repo), name, None) for name in ['t_first', 't_first', 't_second', 't_third']]
    assert requests
    with ProcessPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(resolve_job, requests))
    assert results[0] == results[1]
    assert len(set(results)) == 3
    for (_, identifier, _), (path, branch) in zip(requests, results):
        assert Path(path) == repo / '.worktrees' / identifier
        assert branch == git(path, 'branch', '--show-current') == f'wt/{identifier}'
        assert git(path, 'rev-parse', '--show-toplevel') == path


@pytest.mark.parametrize('identifier', ['../escape', 'x/y', '/tmp/escape', '.', '..', '-option', 'x\\y', 'x\nname', ''])
def test_invalid_task_ids_fail_before_writes(repo, identifier):
    before = git(repo, 'worktree', 'list', '--porcelain')
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(repo, identifier))
    assert git(repo, 'worktree', 'list', '--porcelain') == before


@pytest.mark.parametrize('branch', ['-bad', 'bad..name', 'bad name', 'bad\nname', '@{-1}'])
def test_invalid_branch_rejected(repo, branch):
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(repo, branch=branch))
    assert not (repo / '.worktrees').exists()


def test_unrelated_directory_rejected(repo):
    foreign = repo / 'foreign'
    foreign.mkdir()
    marker = foreign / 'keep'
    marker.write_text('KEEP')
    with pytest.raises(ValueError):
        kb._ensure_git_worktree(repo, foreign, 'wt/test')
    assert marker.read_text() == 'KEEP'
    assert not (foreign / '.git').exists()


def test_wrong_branch_at_own_path_rejected(repo):
    target = repo / '.worktrees/t_fixture'
    kb._ensure_git_worktree(repo, target, 'another-branch')
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(target))
    assert git(target, 'branch', '--show-current') == 'another-branch'


def test_sibling_fallback_and_custom_target_survive(repo, tmp_path):
    sibling, _ = kb._resolve_worktree_workspace(task(repo, 't_sibling'))
    target, branch = kb._resolve_worktree_workspace(task(sibling))
    assert target == repo / '.worktrees/t_fixture'
    assert branch == 'wt/t_fixture'
    assert git(sibling, 'branch', '--show-current') == 'wt/t_sibling'
    custom = tmp_path / 'custom-target'
    kb._ensure_git_worktree(repo, custom, 'feature/custom')
    assert kb._resolve_worktree_workspace(task(custom, branch='feature/custom')) == (custom, 'feature/custom')


def test_subdirectory_of_linked_worktree_rejected(repo):
    linked, _ = kb._resolve_worktree_workspace(task(repo))
    nested = linked / 'nested'
    nested.mkdir()
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(nested))


@pytest.mark.linux_only
def test_symlink_and_traversal_paths_rejected(repo, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (repo / '.worktrees').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(repo))
    assert list(outside.iterdir()) == []
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(repo / '..' / 'repo'))
    (repo / '.worktrees').unlink()
    lock = repo / '.git/hermes-worktrees.lock'
    marker = outside / 'keep'
    marker.write_text('KEEP')
    lock.symlink_to(marker)
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(repo))
    assert marker.read_text() == 'KEEP'


def test_dirty_primary_and_offline_recovery_preserved(repo):
    readme = repo / '.gitignore'
    readme.write_text(readme.read_text() + '\nTASK6 DIRTY\n')
    git(repo, 'add', '.gitignore')
    readme.write_text(readme.read_text() + 'TASK6 UNSTAGED\n')
    marker = repo / 'untracked.txt'
    marker.write_text('PRIMARY')
    primary_before = (readme.read_bytes(), marker.read_bytes(), git(repo, 'diff', '--binary'), git(repo, 'diff', '--cached', '--binary'), git(repo, 'rev-parse', 'HEAD'))
    target, branch = kb._resolve_worktree_workspace(task(repo))
    deliverable = target / 'deliverable.txt'
    deliverable.write_text('RECOVER')
    git(repo, 'remote', 'set-url', 'origin', 'file:///nonexistent/task6-remote')
    failed = subprocess.run(['git', '-C', str(repo), 'ls-remote', 'origin'], capture_output=True)
    assert failed.returncode != 0
    refs = git(repo, 'show-ref')
    assert kb._resolve_worktree_workspace(task(target)) == (target, branch)
    assert deliverable.read_text() == 'RECOVER'
    assert git(repo, 'show-ref') == refs
    assert primary_before == (readme.read_bytes(), marker.read_bytes(), git(repo, 'diff', '--binary'), git(repo, 'diff', '--cached', '--binary'), git(repo, 'rev-parse', 'HEAD'))


@pytest.mark.linux_only
def test_symlinked_git_metadata_rejected(repo, tmp_path):
    metadata = tmp_path / 'metadata'
    (repo / '.git').rename(metadata)
    (repo / '.git').symlink_to(metadata, target_is_directory=True)
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(repo))
    assert not (repo / '.worktrees').exists()


@pytest.mark.linux_only
def test_creation_holds_repository_lock(repo, monkeypatch):
    import sys

    branch_exists = kb._git_branch_exists
    observations = []

    def inspect_lock(anchor, branch):
        probe = subprocess.run([
            sys.executable, '-c',
            'import fcntl,sys\nwith open(sys.argv[1], "a+") as f:\n fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)',
            str(repo / '.git/hermes-worktrees.lock'),
        ], capture_output=True, text=True)
        observations.append(probe.returncode)
        assert probe.returncode != 0 and 'BlockingIOError' in probe.stderr
        return branch_exists(anchor, branch)

    monkeypatch.setattr(kb, '_git_branch_exists', inspect_lock)
    kb._resolve_worktree_workspace(task(repo))
    assert observations


@pytest.mark.parametrize('requested', ['relative/path', 'bad\npath'])
def test_unsafe_requested_paths_rejected(repo, requested):
    path = requested if requested.startswith('relative') else str(repo / requested)
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(Path(path)))
    assert not (repo / '.worktrees').exists()


def test_missing_lock_backend_rejects_creation(repo, monkeypatch):
    from hermes_cli import auth

    monkeypatch.setattr(auth, 'fcntl', None)
    monkeypatch.setattr(auth, 'msvcrt', None)
    with pytest.raises(RuntimeError, match='locking is unavailable'):
        kb._resolve_worktree_workspace(task(repo))
    assert not (repo / '.worktrees').exists()


def test_foreign_repository_worktree_rejected(repo, seed_repo, tmp_path):
    other = tmp_path / 'other'
    subprocess.run(['git', 'clone', '--shared', str(seed_repo), str(other)], check=True, capture_output=True)
    foreign = tmp_path / 'foreign'
    kb._ensure_git_worktree(other, foreign, 'wt/test')
    with pytest.raises(ValueError):
        kb._ensure_git_worktree(repo, foreign, 'wt/test')
    assert git(foreign, 'rev-parse', '--git-common-dir') == str(other / '.git')


@pytest.mark.linux_only
def test_nonregular_lock_rejected(repo):
    import os

    os.mkfifo(repo / '.git/hermes-worktrees.lock')
    with pytest.raises(ValueError, match='regular file'):
        kb._resolve_worktree_workspace(task(repo))
    assert not (repo / '.worktrees').exists()



def test_copied_worktree_metadata_rejected(repo):
    linked, branch = kb._resolve_worktree_workspace(task(repo))
    copied = repo / 'copied'
    copied.mkdir()
    (copied / '.git').write_bytes((linked / '.git').read_bytes())
    with pytest.raises(ValueError):
        kb._ensure_git_worktree(repo, copied, branch)
    with pytest.raises(ValueError):
        kb._resolve_worktree_workspace(task(copied))
    assert git(linked, 'branch', '--show-current') == branch
