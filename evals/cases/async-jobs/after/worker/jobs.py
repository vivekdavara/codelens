import time


class Runner:
    def __init__(self, store, retry_delay: float = 1.0) -> None:
        self.store = store
        self.retry_delay = retry_delay
        self.done: list[str] = []

    async def flush(self) -> None:
        await self.store.save(self.done)
        self.done.clear()

    async def run(self, jobs) -> None:
        for job in jobs:
            while not await job.try_run():
                time.sleep(self.retry_delay)
            self.done.append(job.id)
        self.flush()
