import asyncio
import logging
import signal

from application import WhisplayAirPlayApp


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _main():
    setup_logging()
    app = WhisplayAirPlayApp()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await app.start()
    try:
        app_wait = asyncio.create_task(app.wait())
        signal_wait = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {app_wait, signal_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(_main())
