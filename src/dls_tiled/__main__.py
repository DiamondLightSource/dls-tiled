"""Interface for ``python -m dls_tiled``."""

from argparse import REMAINDER, ArgumentParser
from collections.abc import Sequence

from . import __version__

__all__ = ["main"]


def main(args: Sequence[str] | None = None) -> None:
    """Argument parser for the CLI."""
    parser = ArgumentParser()
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser(
        "serve",
        help="tiled serve, telling the Diamond access policy where nodes are "
        "created (e.g. dls-tiled serve config config.yml)",
        add_help=False,
    )
    serve.add_argument("args", nargs=REMAINDER)
    parsed = parser.parse_args(args)
    if parsed.command == "serve":
        from .node_path import serve as serve_tiled

        serve_tiled(parsed.args)


if __name__ == "__main__":
    main()
