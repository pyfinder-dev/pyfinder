"""Retired direct manager CLI; retained as a development reference."""

def run_cli(arguments, *, runtime_context):
    """Run the existing on-demand manager from parsed CLI arguments."""
    options = {
        "verbosity": arguments.verbosity,
        "with_seiscomp": False,
        "event_id": arguments.event_id,
        "test": arguments.test,
        "use_library": False,
    }

    options["command_line_args"] = "pyfinder on-demand " + " ".join(
        f"--{key.replace('_', '-')} {value}"
        for key, value in options.items()
    )
    
    # If the test mode is enabled, set the event_id to the test event
    if options["test"]:
        options["event_id"] = pyfinderconfig["general"]["test-event-id"]
    
    # Execute the FinDer manager, which will call either the FinDer library 
    # or executable based on the options
    process_logger = customlogger.file_logger(
        runtime_context.process_log_path,
        module_name="OnDemand",
        rotate=True,
        overwrite=False,
        level=getattr(logging, options["verbosity"]),
    )
    application_configuration = runtime_context.isolated_configuration(
        pyfinderconfig
    )
    manager = FinDerManager.for_on_demand(
        options=options,
        configuration=application_configuration,
        logger=process_logger,
    )
    solution = manager.run(event_id=options["event_id"])
    if solution is not None:
        print(f"FinDer solution: {solution}")
    else:
        print("No FinDer solution returned.")
    return 0


if __name__ == "__main__":
    from pyfinder.cli import main

    sys.exit(main(["on-demand", *sys.argv[1:]]))
