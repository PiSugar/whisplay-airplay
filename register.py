import logging
import sys

from hardware.whisplay_daemon import WhisplayDaemonProxy

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    daemon = WhisplayDaemonProxy()
    if not daemon.ping():
        print("whisplay-daemon is not available; skipped app registration")
        return 0
    daemon.register()
    print("registered whisplay-airplay with whisplay-daemon")
    return 0


if __name__ == "__main__":
    sys.exit(main())
