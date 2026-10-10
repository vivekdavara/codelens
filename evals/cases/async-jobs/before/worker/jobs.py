import time


class Runner:
    def __init__(self, store, retry_delay: float = 1.0) -> None:
        self.store = store
        self.retry_delay = retry_delay
        self.done: list[str] = []

    def flush(self) -> None:
        self.store.save(self.done)
        self.done.clear()

    def run(self, jobs) -> None:
        for job in jobs:
            while not job.try_run():
                time.sleep(self.retry_delay)
            self.done.append(job.id)
        self.flush()
