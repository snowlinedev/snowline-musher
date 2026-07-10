"""`python -m snowline_musher` — serve on the configured bind.

The blessed way to run the service: it reads MUSHER_BIND_HOST /
MUSHER_BIND_PORT (loopback-first defaults, spec §4.1) so the bind knobs and
the advertised MUSHER_BASE_URL live in one config surface instead of drifting
apart in a hand-typed uvicorn command.
"""

import uvicorn

from snowline_musher import config


def main() -> None:
    uvicorn.run(
        "snowline_musher.app:app",
        host=config.bind_host(),
        port=config.bind_port(),
    )


if __name__ == "__main__":
    main()
