"""Installed command boundary for PyFinder workflow processes."""

import argparse
from dataclasses import dataclass
import importlib
import sys

from pyfinder.runtime import RuntimeBootstrapError, bootstrap_process


# Keep the earlier exception name importable for callers that treated the
# command boundary's bootstrap failure as one public failure category.
RuntimeBootstrapUnavailable = RuntimeBootstrapError


@dataclass(frozen=True)
class _WorkflowTarget:
    module_name: str
    callable_name: str
    accepts_arguments: bool


_WORKFLOW_TARGETS = {
    "continuous": _WorkflowTarget(
        "pyfinder.start_monitoring",
        "start_services",
        False,
    ),
    "playback": _WorkflowTarget(
        "pyfinder.playback",
        "run_cli",
        True,
    ),
}


def build_parser():
    """Build the command grammar without importing workflow dependencies."""
    parser = argparse.ArgumentParser(
        prog="pyfinder",
        description="Run a PyFinder workflow process.",
    )
    subparsers = parser.add_subparsers(dest="workflow", metavar="WORKFLOW")

    subparsers.add_parser(
        "continuous",
        help="Run continuous EMSC monitoring and scheduled processing.",
        description=(
            "Run continuous monitoring. ShakeMap remains disabled by default. "
            "Set PYFINDER_SHAKEMAP_ENABLED=true, PYFINDER_SHAKEMAP_URL and "
            "PYFINDER_SHAKEMAP_INPUT_DIRECTORY to enable it. Optional settings: "
            "PYFINDER_SHAKEMAP_CONFIGURATION (global), "
            "PYFINDER_SHAKEMAP_REQUEST_TIMEOUT_SECONDS (30), "
            "PYFINDER_SHAKEMAP_OVERWRITE (true). Booleans use true or false. "
            "The input directory must share underlying storage with the service."
        ),
    )

    playback_parser = subparsers.add_parser(
        "playback",
        help="Run selected or predefined events through the full workflow.",
        description=(
            "Run one immediate calculation per event through FinDer, ShakeMap, "
            "and configured notification handling. Omit --event-ids to use "
            "the predefined event list. --full-schedule includes all follow-ups. "
            "Provider requests use data available at execution time."
        ),
        # Reject the retired singular option instead of accepting it as an
        # abbreviation of --event-ids. The installed help is the public grammar.
        allow_abbrev=False,
    )
    playback_parser.add_argument(
        "--event-ids",
        nargs="+",
        metavar="ID",
        help="Provider event identifiers to process (default: predefined list).",
    )
    playback_parser.add_argument(
        "--full-schedule",
        action="store_true",
        help="Run all configured follow-ups instead of one immediate calculation.",
    )
    playback_parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Make each event's follow-ups due two minutes apart while retaining "
            "their nominal schedule identities; no timing effect without "
            "--full-schedule. Execution limits still apply."
        ),
    )
    playback_parser.add_argument(
        "--list",
        action="store_true",
        dest="list_events",
        help="List the predefined playback events and exit without calculations.",
    )
    playback_parser.add_argument(
        "--verbosity",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        type=str.upper,
        help="Application logging level (default: INFO).",
    )
    return parser


def bootstrap_runtime(workflow):
    """Validate fixed paths and own ParamWS logging before workflow import."""
    return bootstrap_process(workflow)


def dispatch(arguments, *, bootstrap=None, importer=None):
    """Bootstrap one process, then import and invoke its workflow callable."""
    target = _WORKFLOW_TARGETS[arguments.workflow]
    if bootstrap is None:
        bootstrap = bootstrap_runtime
    runtime_context = bootstrap(arguments.workflow)
    if importer is None:
        importer = importlib.import_module
    module = importer(target.module_name)
    workflow_callable = getattr(module, target.callable_name)
    if target.accepts_arguments:
        return workflow_callable(
            arguments,
            runtime_context=runtime_context,
        )
    return workflow_callable(runtime_context=runtime_context)


def main(argv=None):
    """Parse an installed command invocation and dispatch its workflow."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.workflow is None:
        parser.print_help()
        return 0

    try:
        return dispatch(arguments)
    except RuntimeBootstrapError as error:
        parser.exit(2, "pyfinder: {0}\n".format(error))


if __name__ == "__main__":
    sys.exit(main())
