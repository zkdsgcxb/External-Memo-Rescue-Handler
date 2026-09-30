"""One fault-time blocking operation, fenced until its result is owned or freed.

Workers publish values, never controller state. A result held for poll remains
fenced because it may own a candidate fd. Abandon permanently closes admission
to this executor and gives its eventual result to exactly one cleanup callback.
"""
import _thread
import os
import time


def _bounded(text, size, *, tail=False):
    encoded = text.encode('utf-8', errors='replace')
    return (encoded[-size:] if tail else encoded[:size]).decode('utf-8', errors='ignore')


def _traceback_locations(trace):
    """Keep bounded frame locations without reading or caching source files."""
    frames = []
    while trace is not None and len(frames) < 16:
        code = trace.tb_frame.f_code
        filename = _bounded(code.co_filename, 1024, tail=True)
        function = _bounded(code.co_name, 128)
        frames.append(f'  File "{filename}", line {trace.tb_lineno}, in {function}\n')
        trace = trace.tb_next
    return _bounded(''.join(frames), 4096, tail=True)


def _error(exc):
    try:
        return {'type': _bounded(type(exc).__name__, 128),
                'message': _bounded(str(exc), 3072),
                'traceback': _traceback_locations(exc.__traceback__)}
    except BaseException:
        # Formatting must not strand an owned fd or prevent late cleanup.
        return {'type': type(exc).__name__[:128],
                'message': 'Exception formatting failed', 'traceback': ''}


class _Pending:
    def __init__(self, kind, token, release):
        self.kind = kind
        self.token = token
        self.release = release
        self.fence = None
        self.outcome = None
        self.cleanup = None
        self.cancelled = False
        self.abandoned = False
        self.worker_ident = None


class OwnedOperation:
    def __init__(self, owner_fd):
        self._owner_fd = owner_fd
        self._lock = _thread.allocate_lock()
        self._pending = None
        self._token = 0
        self._abandoned = False
        self._cleanup_error = None
        self._notify_fd = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)

    def fileno(self):
        """Completion notification for select; only poll consumes its counter."""
        with self._lock:
            if self._notify_fd is None:
                raise ValueError('Operation notification fd is closed')
            return self._notify_fd

    def fence_fd(self):
        """Only the current worker may pass its live fence to a subprocess."""
        with self._lock:
            pending = self._pending
            if (pending is None or pending.fence is None
                    or pending.worker_ident != _thread.get_ident()):
                raise RuntimeError('Only the current operation worker owns its fence fd')
            return pending.fence

    @property
    def busy(self):
        with self._lock:
            return self._pending is not None

    @property
    def cleanup_error(self):
        with self._lock:
            return None if self._cleanup_error is None else dict(self._cleanup_error)

    @staticmethod
    def _close_fence(pending):
        if pending.fence is not None:
            fd, pending.fence = pending.fence, None
            os.close(fd)

    def _close_notification(self):
        if self._notify_fd is not None:
            fd, self._notify_fd = self._notify_fd, None
            os.close(fd)

    def start(self, kind, fn):
        if not isinstance(kind, str) or not kind or not callable(fn):
            raise ValueError('An operation requires a kind and callable')
        with self._lock:
            if self._abandoned:
                raise RuntimeError('Abandoned operation executor cannot restart')
            if self._pending is not None:
                raise RuntimeError('Previous operation has not been consumed')
            # No threads, events or fd duplication during healthy monitoring.
            import threading
            self._token += 1
            pending = _Pending(kind, self._token, threading.Event())
            thread = threading.Thread(target=self._run, args=(pending, fn),
                                      name='guard-owned-operation', daemon=True)
            pending.fence = os.dup(self._owner_fd)
            self._pending = pending
            try:
                # _run takes the lock before fn. Even if start raises after
                # creating a native thread, cancellation wins before any I/O.
                thread.start()
            except BaseException:
                pending.cancelled = True
                self._pending = None
                self._close_fence(pending)
                raise
            return pending.token

    def _run(self, pending, fn):
        with self._lock:
            if pending.cancelled:
                return
            pending.worker_ident = _thread.get_ident()
        started = time.monotonic()
        value = None
        error = None
        try:
            value = fn()
        except BaseException as exc:
            error = _error(exc)
        outcome = {'kind': pending.kind, 'token': pending.token, 'value': value,
                   'error': error, 'elapsed': time.monotonic() - started}
        with self._lock:
            pending.outcome = outcome
            # The descriptor remains owned until this worker and any abandoned
            # result cleanup finish. Never let an fd reuse receive this wakeup.
            try:
                os.eventfd_write(self._notify_fd, 1)
            except BlockingIOError:
                pass  # A saturated eventfd is already readable.
        # Keep this one daemon thread and its fence while an unread result
        # owns resources. poll or abandon selects exactly one next owner.
        pending.release.wait()
        with self._lock:
            abandoned = pending.abandoned
            cleanup = pending.cleanup
        if not abandoned:
            return  # poll transferred the result and released its fence.
        try:
            cleanup(outcome)
        except BaseException as exc:
            error = _error(exc)
            with self._lock:
                self._cleanup_error = error
        finally:
            with self._lock:
                self._close_fence(pending)
                if self._pending is pending:
                    self._pending = None
                self._close_notification()

    def poll(self):
        with self._lock:
            pending = self._pending
            if self._abandoned or pending is None or pending.outcome is None:
                return None
            try:
                os.eventfd_read(self._notify_fd)
            except BlockingIOError:
                pass
            self._close_fence(pending)
            self._pending = None
            pending.release.set()
            return pending.outcome

    def abandon(self, cleanup):
        if not callable(cleanup):
            raise ValueError('Abandon requires a cleanup callable')
        with self._lock:
            self._abandon(cleanup)

    def _abandon(self, cleanup):
        if self._abandoned:
            return
        self._abandoned = True
        pending = self._pending
        if pending is not None:
            pending.abandoned = True
            pending.cleanup = cleanup
            pending.release.set()
        else:
            self._close_notification()

    def close(self, cleanup=None):
        """Close an idle executor, or explicitly discard owned task resources."""
        with self._lock:
            if self._abandoned:
                return
            if self._pending is not None and cleanup is None:
                raise RuntimeError('Pending operation requires an explicit cleanup callback')
            if cleanup is not None and not callable(cleanup):
                raise ValueError('Close requires a cleanup callable')
            self._abandon(cleanup if cleanup is not None else lambda outcome: None)
