"""Delay unlock until direct-transfer children have been stopped and reaped."""
from contextlib import contextmanager
import signal


class StopRequested(BaseException):
    pass


@contextmanager
def protect():
    watched=(signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous={sig:signal.getsignal(sig) for sig in watched}
    def stop(signum, frame):
        # Prevent a second signal from interrupting transport cleanup. The
        # exception is caught by the transfer primitive, which terminates and
        # waits for both process groups before the surrounding locks release.
        for sig in watched: signal.signal(sig, signal.SIG_IGN)
        raise StopRequested('stop requested: '+str(signum))
    try:
        for sig in watched: signal.signal(sig, stop)
        yield
    finally:
        for sig, handler in previous.items(): signal.signal(sig, handler)
