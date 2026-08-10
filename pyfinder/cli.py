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
    "on-demand": _WorkflowTarget(
        "pyfinder.findermanager",
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
    )

    playback_parser = subparsers.add_parser(
        "playback",
        help="Replay one or more predefined EMSC alerts.",
    )
    playback_parser.add_argument(
        "--event-id",
        nargs="+",
        help="Replay only the specified predefined event identifiers.",
    )
    playback_parser.add_argument(
        "--list",
        action="store_true",
        dest="list_events",
        help="List the predefined playback events and exit.",
    )

    on_demand_parser = subparsers.add_parser(
        "on-demand",
        help="Process one event identifier on demand.",
    )
    event_source = on_demand_parser.add_mutually_exclusive_group(required=True)
    event_source.add_argument(
        "--event-id",
        help="Event identifier to query and process.",
    )
    event_source.add_argument(
        "--test",
        action="store_true",
        help="Use the configured test event identifier.",
    )
    on_demand_parser.add_argument(
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
