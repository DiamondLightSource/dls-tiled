"""Interface for ``python -m dls_tiled``."""

import sys
from argparse import REMAINDER, ArgumentParser
from collections.abc import Sequence

from . import __version__

__all__ = ["main"]


def main(args: Sequence[str] | None = None) -> None:
    """Argument parser for the CLI."""
    args = list(sys.argv[1:] if args is None else args)
    if args[:1] == ["serve"]:
        # Everything after serve is tiled's, including options such as --help
        # that argparse would otherwise take for its own
        from .node_path import serve as serve_tiled

        return serve_tiled(args[1:])
    parser = ArgumentParser(prog="dls-tiled")
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
    # Only for the help text: main hands serve to tiled before parsing
    serve.add_argument("args", nargs=REMAINDER)
    parser.parse_args(args)


if __name__ == "__main__":
    main()
