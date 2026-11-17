"""Private bridge diagnostics filtered before disk writes.

Native and Python stderr enter a pipe. Only its reader holds the log descriptor.
Records and the whole log are bounded. An oversized record suppresses the remaining
stderr stream because its discarded bytes may contain a private-key header. Writes to a
full pipe fail instead of blocking startup. Abrupt termination may lose pending
diagnostics. Unrecognised credentials have the limits described in credentials.
"""

import atexit
import contextlib
import faulthandler
import os
import re
import sys
import threading
import time
from pathlib import Path

from dromon.credentials import scrub_text
from dromon.privatefiles import open_log

MAX_RECORD_BYTES = 65536
MAX_LOG_BYTES = 1048576


def capture(path: str, *, descriptor: int = 2) -> bool:
    """Validate the log before redirecting stderr. Refuse a log failure."""
    try:
        opened = open_log(Path(path))
    except OSError:
        return False
    try:
        saved = os.dup(descriptor)
    except OSError:
        os.close(opened)
        return False
    try:
        reader, writer = os.pipe()
    except OSError:
        os.close(saved)
        os.close(opened)
        return False
    secrets = tuple(
        value
        for name, value in os.environ.items()
        if re.search("TOKEN|SECRET|KEY|PASS|CREDENTIAL|SESSION", name.upper())
    )
    written = 0

    def write(text: str) -> None:
        nonlocal written
        data = scrub_text(text, secrets).encode("utf-8", errors="replace")
        if written + len(data) > MAX_LOG_BYTES:
            return
        with contextlib.suppress(OSError):
            os.write(opened, data)
            written += len(data)

    def mark(what: str) -> None:
        write(
            f"dromon bridge {what}, pid {os.getpid()}, at "
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        )

    def drain() -> None:
        pending = bytearray()
        suppressed = False
        private_key = False
        try:
            while True:
                chunk = os.read(reader, 4096)
                if not chunk:
                    break
                if suppressed:
                    continue
                for byte in chunk:
                    if suppressed:
                        break
                    if byte == 10:
                        text = pending.decode("utf-8", errors="replace")
                        if "-----BEGIN " in text and "PRIVATE KEY-----" in text:
                            private_key = True
                            write("[private key omitted]\n")
                        elif not private_key:
                            write(text + "\n")
                        if "-----END " in text and "PRIVATE KEY-----" in text:
                            private_key = False
                        pending.clear()
                    elif len(pending) < MAX_RECORD_BYTES:
                        pending.append(byte)
                    else:
                        # The discarded suffix could hide a key header. There is
                        # no safe boundary at the next newline, which may begin
                        # the key body. Drain without logging until process exit.
                        suppressed = True
                        pending.clear()
                        write("[stderr suppressed after oversized diagnostic]\n")
            if pending and not suppressed and not private_key:
                write(pending.decode("utf-8", errors="replace"))
        except OSError:
            pass
        finally:
            os.close(reader)

    worker = threading.Thread(target=drain, name="dromon-bridge-log", daemon=True)
    try:
        os.set_blocking(writer, False)
        os.dup2(writer, descriptor)
        mark("started")
        worker.start()
    except (OSError, RuntimeError):
        with contextlib.suppress(OSError):
            os.dup2(saved, descriptor)
        os.close(reader)
        os.close(opened)
        return False
    finally:
        os.close(writer)
        os.close(saved)
    with contextlib.suppress(OSError, ValueError, RuntimeError):
        faulthandler.enable(file=descriptor)

    def finish() -> None:
        with contextlib.suppress(OSError, ValueError):
            sys.stderr.flush()
        with contextlib.suppress(OSError):
            os.close(descriptor)
        worker.join(1.0)
        if not worker.is_alive():
            mark("ended normally")
            os.close(opened)
        # A stuck writer retains its descriptor until process exit. Closing it here
        # could redirect that writer to a reused descriptor during shutdown.

    atexit.register(finish)
    return True
