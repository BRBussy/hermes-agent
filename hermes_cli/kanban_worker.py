"""Process evidence for dispatcher-owned Kanban attempts."""
from __future__ import annotations

import os
from pathlib import Path
import platform

from gateway.status import get_process_start_time


def process_identity(pid, task_id, run_id, claim_lock):
    import psutil
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        env = process.environ()
        expected = {
            'HERMES_KANBAN_TASK': task_id,
            'HERMES_KANBAN_RUN_ID': str(run_id),
            'HERMES_KANBAN_CLAIM_LOCK': claim_lock,
        }
        if any(env.get(key) != value for key, value in expected.items()):
            return None
        start = get_process_start_time(pid)
        if start is None:
            return None
        identity = dict(pid=pid, start=start, host=platform.node(), task_id=task_id,
                        run_id=run_id, claim_lock=claim_lock)
        if Path('/proc/self/ns/pid').exists():
            identity['namespace'] = os.readlink(f'/proc/{pid}/ns/pid')
            identity['boot'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return identity
    except (OSError, psutil.Error, ValueError):
        return None


def identity_alive(identity):
    return process_identity(identity['pid'], identity['task_id'], identity['run_id'],
                            identity['claim_lock']) == identity


def validate_launch_namespace():
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('CODEX_SANDBOX'):
        raise RuntimeError('Use the persistent gateway or Kanban daemon to launch workers')
    path = Path('/proc/self/status')
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith('NSpid:') and len(line.split()) > 2:
                raise RuntimeError('Worker dispatch from a nested PID namespace is unsupported')


def startup_timeout():
    from hermes_cli.config import load_config_readonly
    value = (load_config_readonly() or {}).get('kanban', {}).get('worker_startup_timeout_seconds', 120)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError('kanban.worker_startup_timeout_seconds must be positive')
    return value


def signal_identity(identity, sig):
    import signal
    import psutil
    pid = identity['pid']
    if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
        descriptor = os.pidfd_open(pid)
        try:
            if not identity_alive(identity):
                raise ProcessLookupError('Worker identity changed')
            signal.pidfd_send_signal(descriptor, sig)
        finally:
            os.close(descriptor)
    else:
        process = psutil.Process(pid)
        if not identity_alive(identity):
            raise ProcessLookupError('Worker identity changed')
        process.send_signal(sig)


def can_observe_identity(identity):
    if identity.get('host') != platform.node():
        return False
    if identity.get('boot'):
        try:
            if identity['boot'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
                return True
            return identity.get('namespace') == os.readlink('/proc/self/ns/pid')
        except OSError:
            return False
    return True


def active_worker_exists(conn, task_id):
    import json
    rows = conn.execute('SELECT identity FROM worker_attempts WHERE task_id = ? AND identity IS NOT NULL',
                        (task_id,)).fetchall()
    for row in rows:
        identity = json.loads(row[0])
        if not can_observe_identity(identity) or identity_alive(identity):
            return True
    return False
