"""Account repository discovery and inactive card preparation."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess


def repository_helper(command, repository=None, *, authority=None):
    from hermes_cli.kanban_admission import _operator
    from hermes_cli.config import load_config_readonly
    _operator()
    if command not in {'list', 'inspect', 'clone'}:
        raise ValueError('Unsupported repository operation')
    if command == 'clone' and (not isinstance(authority, str) or not authority.strip()):
        raise ValueError('Repository cloning requires explicit task-specific Git authority')
    config = (load_config_readonly() or {}).get('kanban', {})
    executable = config.get('workspace_helper') or shutil.which('task-workspace')
    if not executable:
        raise ValueError('The account workspace helper is unavailable. Ask the operator to provision it')
    environment = dict(os.environ)
    if config.get('repository_root'):
        environment['TASK_WORKSPACE_ROOT'] = config['repository_root']
    args = [executable, command]
    if repository is not None:
        args.append(repository)
    result = subprocess.run(args, capture_output=True, text=True, timeout=150, env=environment)
    if result.returncode:
        raise ValueError('Repository preparation failed. Inspect the repository identity and account access')
    if command == 'clone':
        return repository_helper('inspect', repository)
    return json.loads(result.stdout)


def inspect_directory(raw):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_admission import _git
    from hermes_cli.banner import _canonical_github_remote
    result = {'server': socket.gethostname(), 'path': None, 'kind': 'scratch',
              'readable': False, 'writable': False, 'searchable': False}
    if not raw or not raw.strip():
        return result
    root = kb._checked_worktree_path(Path(raw.strip()))
    if not root.is_dir():
        raise ValueError('Project directory must be an existing absolute server directory')
    result.update(path=str(root), readable=os.access(root, os.R_OK),
                  writable=os.access(root, os.W_OK), searchable=os.access(root, os.X_OK))
    if not all(result[key] for key in ('readable', 'writable', 'searchable')):
        raise ValueError('Project directory requires account read, write and search access')
    top = kb._git_toplevel(root)
    if top is not None:
        if top != root or not (root / '.git').is_dir():
            raise ValueError('Select the primary Git checkout root')
        result.update(kind='worktree', repository=_canonical_github_remote(_git(root, 'remote', 'get-url', 'origin')),
                      head=_git(root, 'rev-parse', 'HEAD'), branch=_git(root, 'branch', '--show-current'))
    else:
        result['kind'] = 'dir'
    return result
