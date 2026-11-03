"""Local, content-free setup timing. Timing events never grant permission.

The agent records milestones beside the actual checked operations; this module
does not execute them, infer readiness, inspect a host, or contact a service.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import time

PHASES = ('preflight', 'scan', 'install', 'activation', 'binding', 'admission', 'join', 'readiness')
STARTS = {'invitation_received', 'setup_observation_started'}
EVENTS = STARTS | {'approval_requested', 'approval_resolved', 'ready', 'blocked', 'cancelled'} | {
    phase+'_'+edge for phase in PHASES for edge in ('started', 'finished')}
TERMINAL = {'ready', 'blocked', 'cancelled'}


def summarize(events):
    if not events or events[0]['event'] not in STARTS:
        raise ValueError('Timing must start at invitation receipt or explicit partial observation')
    previous = -1
    seen, starts, intervals, approvals = set(), {}, {}, []
    waiting = None
    terminal = None
    for index, row in enumerate(events):
        if set(row) != {'event', 'monotonic_seconds'}:
            raise ValueError('Timing records contain only an event and monotonic time')
        event, timestamp = row['event'], row['monotonic_seconds']
        if (event not in EVENTS or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp) or timestamp < 0 or timestamp < previous or terminal):
            raise ValueError('Invalid, out-of-order or already completed timing record')
        if event in STARTS and index != 0:
            raise ValueError('Timing origin cannot change mid-attempt')
        previous = timestamp
        if event == 'approval_requested':
            if waiting is not None: raise ValueError('An approval wait is already open')
            waiting = timestamp
        elif event == 'approval_resolved':
            if waiting is None: raise ValueError('No approval wait is open')
            approvals.append((waiting, timestamp))
            waiting = None
        else:
            if event in seen: raise ValueError('A milestone cannot be overwritten')
            seen.add(event)
            if event in STARTS:
                continue
            if event.endswith('_started'):
                starts[event[:-8]] = timestamp
            elif event.endswith('_finished'):
                phase = event[:-9]
                if phase not in starts: raise ValueError('Phase finish requires a start')
                intervals[phase] = (starts[phase], timestamp)
            elif event in TERMINAL:
                if event == 'ready' and (waiting is not None or 'readiness' not in intervals or set(starts) != set(intervals)):
                    raise ValueError('Ready requires completed readiness and approval waits')
                terminal = event
    end = events[-1]['monotonic_seconds']
    waits = approvals + ([(waiting, end)] if waiting is not None else [])
    def duration(start, finish):
        dwell = sum(max(0, min(finish, b)-max(start, a)) for a, b in waits)
        return dict(elapsed_seconds=round(finish-start, 6), approval_wait_seconds=round(dwell, 6),
                    work_seconds=round(finish-start-dwell, 6))
    total = duration(events[0]['monotonic_seconds'], end)
    stages = {phase: dict(**duration(*intervals[phase]), complete=True) if phase in intervals else
              dict(**duration(starts[phase], end), complete=False) if phase in starts else None for phase in PHASES}
    return dict(status=terminal or 'in_progress', total=total, stages=stages,
        approval_wait_open=waiting is not None,
        installation_under_30s=(stages['install']['work_seconds'] < 30) if 'install' in intervals else None,
        invite_to_ready_under_30s=(total['work_seconds'] < 30) if terminal == 'ready' and events[0]['event'] == 'invitation_received' else None,
        invitation_receipt_measured=events[0]['event'] == 'invitation_received',
        evidence='caller_recorded_milestones_not_independent_host_acceptance')


def record(path, event):
    if event not in EVENTS: raise ValueError('Unknown timing event')
    path = Path(path).absolute()
    if any(p.is_symlink() for p in path.parents): raise ValueError('No symlink parent for timing')
    # Exclusive create on the first event, reuse only this owner-only regular
    # file after that. No parent creation, arbitrary payloads or secret fields.
    flags = os.O_RDWR | os.O_NOFOLLOW
    if event in STARTS: flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'r+') as output:
        info = os.fstat(output.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Timing file must be regular and owner-only')
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        events = [json.loads(line) for line in output if line.strip()]
        row = dict(event=event, monotonic_seconds=time.monotonic())
        report = summarize(events+[row])
        output.seek(0, os.SEEK_END)
        output.write(json.dumps(row, sort_keys=True)+'\n')
        output.flush()
        os.fsync(output.fileno())
    return report


@contextmanager
def measure(path, phase):
    """Bracket an actual operation. No historical timestamps or implicit consent.

    A failed operation leaves its stage unfinished and records blocked; callers
    must preserve that attempt rather than reset its clock.
    """
    if phase not in PHASES:
        raise ValueError('Unknown timing phase')
    record(path, phase+'_started')
    try:
        yield
    except BaseException:
        record(path, 'blocked')
        raise
    else:
        record(path, phase+'_finished')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('event', nargs='?', choices=sorted(EVENTS))
    args = parser.parse_args()
    try:
        if args.event:
            report = record(args.file, args.event)
        else:
            # JSONL needs the same no-follow/owner-only constraints as state.
            fd = os.open(args.file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('Timing file must be owner-only')
                fcntl.flock(source.fileno(), fcntl.LOCK_SH)
                report = summarize([json.loads(line) for line in source if line.strip()])
        print(json.dumps(report, sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit('Invalid or unavailable timing record; no host action performed') from None


if __name__ == '__main__':
    main()
