"""Worker: claims tasks, runs the Runtime, keeps the lease alive, maps outcomes to queue states."""
from __future__ import annotations

import threading
import traceback
import uuid
from typing import Callable

from .llm import FatalRunError, QuotaExhausted, TransientRunError
from .queue import TaskQueue
from .runtime import Runtime


class Worker:
    def __init__(self, queue: TaskQueue, runtime_factory: Callable[[], Runtime], worker_id: str | None = None,
                 lease_s: float = 120.0, poll_s: float = 1.0):
        self.queue, self.factory = queue, runtime_factory
        self.id = worker_id or f"w_{uuid.uuid4().hex[:6]}"
        self.lease_s, self.poll_s = lease_s, poll_s

    def run_once(self) -> str | None:
        """Process at most one task. Returns the task id handled (or None if idle)."""
        self.queue.resume_ready()
        task = self.queue.claim(self.id, self.lease_s)
        if task is None:
            return None
        tid = task["id"]
        stop = threading.Event()

        def beat():
            while not stop.wait(self.lease_s / 3):
                self.queue.heartbeat(tid, self.id, self.lease_s)
        hb = threading.Thread(target=beat, daemon=True)
        hb.start()
        try:
            out = self.factory().run(tid)
            if out.status == "completed":
                self.queue.finish(tid, "completed", out.result)
            elif out.status == "awaiting_approval":
                self.queue.finish(tid, "awaiting_approval", {"approval_ids": out.approval_ids})
            elif out.status == "waiting_input":
                self.queue.finish(tid, "waiting_input", {"question": out.question})
            else:                                   # needs_human / failed
                self.queue.finish(tid, "needs_human", out.result, error=out.summary)
        except FatalRunError as e:                  # retrying cannot help: stop now, with a readable reason
            self.queue.finish(tid, "dead", error=str(e))
        except QuotaExhausted as e:                 # plan limit: wait for the reset, keep the checkpoint, burn no attempts
            self.queue.defer(tid, str(e), e.retry_after_s)
        except TransientRunError as e:
            self.queue.fail_transient(tid, str(e))
        except Exception as e:                      # unexpected bug: retry a couple of times, then dead-letter with the traceback
            self.queue.fail_transient(tid, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"[:1500])
        finally:
            stop.set()
        return tid

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            if self.run_once() is None:
                stop.wait(self.poll_s)
