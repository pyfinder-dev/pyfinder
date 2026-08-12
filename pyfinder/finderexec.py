# -*- coding: utf-8 -*-
""" Module for executing the FinDer executable, namely the FinDer file. """

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import logging
import math
from numbers import Real
import os
import subprocess
import sys
import json
from datetime import datetime
from pyfinder.utils import customlogger
from pyfinder.finderutils import (FinderChannelList, FinderChannel,
                                  FinderSolution, FinderRupture,
                                  FinderEvent)
from pyfinder.finderutils import (read_event_solution_from_file,
                                  read_rupture_polygon_from_file,
                                  read_finder_channels_from_file)
from pyfinder.utils.calculator import Calculator
from pyfinder.utils.dataformatter import (FinDerInputFormatter,
                                          get_epoch_time)
from pyfinder.utils.station_merger import RawStationMeasurement
from pyfinder.workspace import select_workspace_path

class FinDerExecutable(object):
    """ Class for executing the FinDer executable. """

    @staticmethod
    def _resolve_live_mode(value):
        """Return the configured FinDer mode without accepting truthy values."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized_value = value.lower()
            if normalized_value == "yes":
                return True
            if normalized_value == "no":
                return False
        raise ValueError(
            "finder-executable.finder-live-mode must be a Boolean or the "
            "case-insensitive string 'yes' or 'no'"
        )

    @staticmethod
    def _resolve_artificial_point_margin_percent(value):
        """Return a finite nonnegative percentage as a float."""
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(
                "finder-executable.artificial-point-margin-percent must be a "
                "real number, not a Boolean"
            )
        try:
            normalized_value = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(
                "finder-executable.artificial-point-margin-percent must be a "
                "finite real number"
            ) from exc
        if not math.isfinite(normalized_value) or normalized_value < 0:
            raise ValueError(
                "finder-executable.artificial-point-margin-percent must be "
                "finite and greater than or equal to zero"
            )
        return normalized_value

    def __init__(
        self,
        options: dict,
        configuration: dict,
        finder_configuration_name,
        finder_configuration,
        logger=None,
    ):
        # Options from the command line arguments
        self.options: dict = options

        if (
            not isinstance(finder_configuration_name, str)
            or not finder_configuration_name.strip()
        ):
            raise ValueError(
                "finder_configuration_name must be a non-empty string"
            )
        if (
            not isinstance(finder_configuration, Mapping)
            or not finder_configuration
        ):
            raise ValueError(
                "finder_configuration must be a non-empty mapping"
            )
        try:
            execution_configuration = deepcopy(dict(finder_configuration))
        except Exception as exc:
            raise ValueError(
                "finder_configuration cannot be isolated for execution"
            ) from exc

        self.finder_configuration_name = finder_configuration_name
        self.finder_configuration = execution_configuration

        if not isinstance(configuration, Mapping):
            raise ValueError("application configuration must be a mapping")
        try:
            execution_application_configuration = deepcopy(configuration)
        except Exception as exc:
            raise ValueError(
                "application configuration cannot be isolated for execution"
            ) from exc

        finder_executable_configuration = (
            execution_application_configuration.get("finder-executable")
        )
        if not isinstance(finder_executable_configuration, Mapping):
            raise ValueError(
                "application setting finder-executable must be a mapping"
            )
        required_settings = (
            "finder-live-mode",
            "artificial-point-margin-percent",
        )
        for setting_name in required_settings:
            if setting_name not in finder_executable_configuration:
                raise ValueError(
                    "application setting finder-executable.{} is required".format(
                        setting_name
                    )
                )

        # One private snapshot supplies every decision for this invocation,
        # even if a caller later changes or reloads its own configuration.
        self.configuration: dict = execution_application_configuration
        self.is_live_mode = self._resolve_live_mode(
            finder_executable_configuration["finder-live-mode"]
        )
        self.artificial_point_margin_percent = (
            self._resolve_artificial_point_margin_percent(
                finder_executable_configuration[
                    "artificial-point-margin-percent"
                ]
            )
        )

        # Path to the FinDer executable
        self.executable_path: str = self.configuration["finder-executable"]["path"]

        # Manager and provider diagnostics remain on this process-owned logger.
        # Only the locked FinDer workspace phase temporarily replaces it.
        self.logger = (
            logger if logger is not None else logging.getLogger(__name__)
        )

        # Working directory. It will be created for each event id
        self.working_directory: str = None

        # Path to the FinDer configuration file
        self.finder_file_config_path: str = None

        # Event ID used by the FinDer executable
        self.finder_event_id: str = None

        # Channels used by the FinDer executable
        self.finder_used_channels: FinderChannelList = None

        # FinDer solution (channels used, rupture, event)
        self.finder_solution: FinderSolution = None

    def get_finder_solution_object(self) -> FinderSolution:
        """
        Returns either the raw amplitude-based or FinDer-processed FinderSolution
        based on the configuration setting under 'shakemap.use-amplitude-from-finder-output'.
        """
        use_finder_amplitudes = self.configuration.get("shakemap", {}).get("use-amplitude-from-finder-output", False)
        
        print(self.configuration["shakemap"])
        print("use_raw:", not use_finder_amplitudes)
        if not use_finder_amplitudes:
            if self.finder_solution.input_solution is not None:
                self.logger.info("Returning raw amplitudes from the input_solution.")
                # print("Returning raw amplitudes from the input_solution.")
                return self.finder_solution.input_solution
            else:
                self.logger.warning("Raw amplitudes were requested, but no input_solution is available. Using FinDer-derived solution.")
                # print("Raw amplitudes were requested, but no input_solution is available. Using FinDer-derived solution.")
                return self.finder_solution
        else:
            print("Returning FinDer-derived solution. ELSE branch.")
            return self.finder_solution
    
    def get_finder_event_id(self):
        """ Get the event id used by the FinDer executable. """
        return self.finder_event_id
    
    def get_configured_root_folder(self):
        """ Get the root output folder from the configuration. """
        return self.configuration["finder-executable"]["output-root-folder"]
            
    def get_working_directory(self):
        """ Get the working directory. Once set, it is the same 
        as combine_event_output_folder() method. """
        return self.working_directory
    
    def get_finder_used_channels(self) -> FinderChannelList:
        """ Get the channels used by the FinDer executable. """
        return self.finder_used_channels
    
    def _prepare_workspace(self, augmented_event_id):
        """ 
        Parepares the working environment for running the FinDer executable.
        FinDer needs a working directory to write its output and a specific
        config file configured for this event.
        """
        output_root_folder = self.get_configured_root_folder()
        workspace = select_workspace_path(
            output_root_folder,
            augmented_event_id,
        )
        # Creating the configured directory is the authoritative permission
        # check. Filesystem errors propagate; execution never selects a CWD or
        # home-directory substitute.
        os.makedirs(output_root_folder, exist_ok=True)

        # Reusing an event-and-delay workspace retains every existing FinDer
        # file. PyFinder only rewrites the two invocation files it owns.
        self.working_directory = str(workspace)
        os.makedirs(self.working_directory, exist_ok=True)

    @contextmanager
    def _workspace_lock(self):
        """Exclusively lock this augmented workspace for one invocation."""
        lock_file_path = os.path.join(
            self.working_directory,
            ".pyfinder.lock",
        )
        with open(lock_file_path, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _workspace_phase(self, augmented_event_id):
        """Use the event log only while this process owns the workspace."""
        process_logger = self.logger
        process_logger_name = getattr(process_logger, "name", None)
        if not isinstance(process_logger_name, str) or not process_logger_name:
            process_logger_name = type(process_logger).__name__
        workspace_log_path = os.path.join(
            self.working_directory,
            "pyfinder.log",
        )

        process_logger.info(
            "Calculation %s is waiting to enter the locked FinDer workspace "
            "phase; workspace=%s; pyfinder_log=%s",
            augmented_event_id,
            self.working_directory,
            workspace_log_path,
        )

        try:
            try:
                with self._workspace_lock():
                    with customlogger.transient_file_logger(
                        workspace_log_path
                    ) as workspace_logger:
                        self.logger = workspace_logger
                        workspace_logger.info(
                            "Entered locked FinDer workspace phase for %s; "
                            "originating_process_logger=%s",
                            augmented_event_id,
                            process_logger_name,
                        )
                        workspace_logger.info(
                            "START... Initiated with '%s'",
                            self.options["command_line_args"],
                        )
                        workspace_logger.info(
                            "Augmented event ID: %s",
                            augmented_event_id,
                        )
                        workspace_logger.info(
                            "FinDer executable path: %s",
                            self.executable_path,
                        )
                        workspace_logger.info(
                            "Event output folder: %s",
                            self.working_directory,
                        )
                        workspace_logger.debug(
                            "Self configuration: %s",
                            json.dumps(self.configuration, indent=4),
                        )
                        try:
                            yield
                        except BaseException as exc:
                            workspace_logger.error(
                                "Leaving locked FinDer workspace phase for %s "
                                "with failure: %s",
                                augmented_event_id,
                                exc,
                                exc_info=True,
                            )
                            raise
                        else:
                            workspace_logger.info(
                                "Leaving locked FinDer workspace phase "
                                "successfully for %s",
                                augmented_event_id,
                            )
            finally:
                # The transient handler closes before the lock is released; the
                # stable process logger is restored after both lifetimes end.
                self.logger = process_logger
        except BaseException as exc:
            process_logger.error(
                "Calculation %s failed in the locked FinDer workspace phase; "
                "pyfinder_log=%s; error=%s",
                augmented_event_id,
                workspace_log_path,
                exc,
            )
            raise
        else:
            process_logger.info(
                "Calculation %s completed the locked FinDer workspace phase; "
                "pyfinder_log=%s",
                augmented_event_id,
                workspace_log_path,
            )
    
    
    def _write_finder_configuration(self):
        """ Write the FinDer configuration file under the working directory. """
        self.logger.info("Writing the FinDer configuration file...")
        self.logger.info(
            "Selected FinDer configuration: %s",
            self.finder_configuration_name,
        )

        try:
            # Materialization works from another execution-local copy. Replacing
            # DATA_FOLDER for this run must not alter the mapping retained by the
            # executable or any upstream selector/manager-owned dictionary.
            finder_file_config = deepcopy(self.finder_configuration)

            # Change the data folder to the working directory. This is where FinDer
            # will create 'temp' and 'temp_data' directories to dump its output.
            finder_file_config["DATA_FOLDER"] = self.working_directory

            # Write the configuration to the working directory
            config_file_path = os.path.join(self.working_directory, "finder_file.config")

            with open(config_file_path, "w", encoding="utf-8") as config_file:
                for key, value in finder_file_config.items():
                    config_file.write("{} {}\n".format(key, value))

            self.finder_file_config_path = config_file_path

            # Log the configuration file path
            self.logger.info("FinDer configuration file: {}".format(config_file_path))

            rendered_config = "\n".join(
                "{} {}".format(key, value)
                for key, value in finder_file_config.items()
            )
            self.logger.debug("FinDer configuration:\n%s", rendered_config)
            self.logger.ok("FinDer configuration file is written.")

        except Exception as e:
            self.logger.error("Error writing the FinDer configuration file: {}".format(e))
            raise e

    def _check_finder_executable(self):
        # Check if the executable exists
        if not os.path.exists(self.executable_path):
            raise FileNotFoundError("Could not find the FinDer executable at: {}".format(self.executable_path))

        # Check if the executable is a file
        if not os.path.isfile(self.executable_path):
            raise FileNotFoundError("The FinDer executable path is not a file: {}".format(self.executable_path))

        # Check if the executable is executable
        if not os.access(self.executable_path, os.X_OK):
            raise PermissionError("The FinDer executable is not executable: {}".format(self.executable_path))
        
    def _write_data_for_finder(
        self,
        observations: list[RawStationMeasurement],
        event_data,
    ) -> tuple[str, FinderChannelList]:
        """
        Assemble merged observations and write the serialized ``data_0``.

        Service normalization and StationMerger have already selected the real
        observations and their order. This boundary assembles those decisions
        into linear channels, adds the invocation's artificial point, and
        returns the completed list alongside the written path.
        """
        data_file_path = os.path.join(self.working_directory, "data_0")

        if not isinstance(observations, list):
            raise TypeError(
                "FinDerExecutable requires merged normalized observations "
                "as a list"
            )

        self.logger.info(
            f"Preparing {len(observations)} merged real observations for FinDer."
        )
        finder_channels = self._build_real_finder_channels(observations)
        self.logger.info(
            f"Assembled {len(finder_channels)} real FinDer channels in merger "
            "order."
        )

        # The artificial point is built here so data_0 and the later companion
        # can use the same completed membership. Keep this call visible: a
        # developer may comment it out for a controlled experiment, while the
        # committed production path always leaves it enabled.
        finder_channels = self.add_artificial_observation_point(
            finder_channels,
            event_data,
        )

        mode_name = "live" if self.is_live_mode else "non-live"
        self.logger.info(
            f"Serializing {len(finder_channels)} completed FinDer channels to "
            f"data_0 in {mode_name} mode."
        )
        data_0_bytes = FinDerInputFormatter.format(
            finder_channels=finder_channels,
            event_time_epoch=get_epoch_time(event_data.get_origin_time()),
            is_live_mode=self.is_live_mode,
        )

        # Write the data to the working directory
        with open(data_file_path, "wb") as data_file:
            data_file.write(data_0_bytes)

        self.logger.info(f"data_0 written: {data_file_path}")
        return data_file_path, finder_channels

    @staticmethod
    def _build_real_finder_channels(
        observations: list[RawStationMeasurement],
    ) -> FinderChannelList:
        """Copy the merger-selected observations into linear channels."""
        if not isinstance(observations, list):
            raise TypeError(
                "FinDerExecutable requires merged normalized observations "
                "as a list"
            )
        if not observations:
            raise ValueError(
                "FinDerExecutable requires at least one merged normalized "
                "observation"
            )

        finder_channels = FinderChannelList()
        for index, observation in enumerate(observations):
            if not isinstance(observation, Mapping):
                raise TypeError(
                    f"Merged normalized observation {index} must be a mapping"
                )
            try:
                finder_channels.append(FinderChannel(
                    latitude=observation["latitude"],
                    longitude=observation["longitude"],
                    network_code=observation["network"],
                    station_code=observation["station"],
                    location_code=observation["location"],
                    channel_code=observation["channel"],
                    pga=observation["pga"],
                    is_artificial=False,
                ))
            except KeyError as error:
                raise ValueError(
                    f"Merged normalized observation {index} is missing "
                    f"required field {error.args[0]!r}"
                ) from error

        return finder_channels

    def _calculate_artificial_linear_pga(
        self,
        finder_channels: FinderChannelList,
        event_magnitude: float,
        event_depth: float,
    ) -> float:
        """Select the stabilizing PGA from linear event and observed values."""
        predicted_event_linear_pga = Calculator.predict_PGA_from_magnitude(
            event_magnitude,
            event_depth,
            log_scale=False,
        )
        if (
            isinstance(predicted_event_linear_pga, bool)
            or not isinstance(predicted_event_linear_pga, Real)
        ):
            raise ValueError("predicted artificial PGA must be numerical")
        try:
            predicted_event_linear_pga = float(predicted_event_linear_pga)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError(
                "predicted artificial PGA must be finite"
            ) from error
        if (
            not math.isfinite(predicted_event_linear_pga)
            or predicted_event_linear_pga <= 0
        ):
            raise ValueError(
                "predicted artificial PGA must be finite and greater than zero"
            )

        maximum_real_linear_pga = max(
            float(channel.pga) for channel in finder_channels
        )
        observed_margin_pga = maximum_real_linear_pga * (
            1 + self.artificial_point_margin_percent / 100
        )
        if not math.isfinite(observed_margin_pga):
            raise ValueError("observed-margin artificial PGA must be finite")

        artificial_linear_pga = max(
            predicted_event_linear_pga,
            observed_margin_pga,
        )
        if (
            not math.isfinite(artificial_linear_pga)
            or artificial_linear_pga <= 0
        ):
            raise ValueError(
                "selected artificial PGA must be finite and greater than zero"
            )

        self.logger.info("Adding artificial PGA:")
        self.logger.info(
            "Maximum observed linear PGA: "
            f"{maximum_real_linear_pga:.5f} cm/s^2"
        )
        self.logger.info(
            "Event-predicted linear PGA: "
            f"{predicted_event_linear_pga:.5f} cm/s^2"
        )
        self.logger.info(
            "Configured artificial-point margin: "
            f"{self.artificial_point_margin_percent}%"
        )
        self.logger.info(
            "Observed PGA with artificial margin: "
            f"{observed_margin_pga:.5f} cm/s^2"
        )
        self.logger.info(
            f"Selected artificial PGA: {artificial_linear_pga:.5f} cm/s^2"
        )

        return artificial_linear_pga

    def add_artificial_observation_point(
        self,
        finder_channels: FinderChannelList,
        event_data,
    ) -> FinderChannelList:
        """Prepend the configured linear stabilizing observation."""
        event_latitude = event_data.get_latitude()
        event_longitude = event_data.get_longitude()
        artificial_linear_pga = self._calculate_artificial_linear_pga(
            finder_channels=finder_channels,
            event_magnitude=event_data.get_magnitude(),
            event_depth=event_data.get_depth(),
        )

        # This is a stabilizing observation created by PyFinder, not provider
        # data, so it carries the fixed artificial SNCL and no provenance.
        artificial_channel = FinderChannel(
            latitude=event_latitude, longitude=event_longitude,
            sncl="XX.NONE.00.HNZ", pga=artificial_linear_pga,
            is_artificial=True,
        )
        self.logger.info(
            "Added artificial channel XX.NONE.00.HNZ at event coordinates "
            f"({event_latitude}, {event_longitude}) with PGA "
            f"{artificial_linear_pga:.5f} cm/s^2."
        )

        return FinderChannelList([artificial_channel, *finder_channels])


    def _is_live_mode_on(self):
        """Report the live-mode decision resolved for this invocation."""
        if self.is_live_mode:
            self.logger.info("FinDer live mode is enabled.")
        else:
            self.logger.info("FinDer live mode is disabled.")
    
        return self.is_live_mode
    
    def _process_finder_output(self, stdout, stderr):
        """ Process the FinDer output for the Event_ID and log everything. """
        self.logger.info(">>>> Start of FinDer::STDOUT")
        
        for line in stdout.decode().splitlines():
            self.logger.finder(line)

            # Get the Event ID from the FinDer output. FinDer used 
            # a time stamp as the event ID in the output.
            if "Event_ID" in line:
                event_id = line.split("=")[-1].strip()
                self.finder_event_id = event_id

        self.logger.info("<<<< End of FinDer::STDOUT")

        # Log the event ID used by the FinDer executable
        self.logger.debug(f"-"*80)
        self.logger.debug(f"FinDer Event ID used for output: {self.finder_event_id}")
        if self.finder_event_id is None:
            self.logger.error("FinDer did not return an event ID. Check the output for errors.")
        self.logger.debug(f"-"*80)

        # Log the stderr if there are any
        if stderr:
            self.logger.warning(">>>> FinDer produced errors/warnings! See FinDer::STDERR below.")
            for line in stderr.decode().splitlines():
                self.logger.error(line)
            self.logger.info("<<<< End of FinDer::STDERR")
        else:
            self.logger.info(">>>> FinDer::STDERR: Empty")
        

    def _run_finder(self):
        """ Run the FinDer executable. """
        self.logger.info("Executing the FinDer executable...")

        # Check if the live mode is enabled
        is_live_mode = self._is_live_mode_on()

        # Command line options for FinDer 
        cmd_line_opt = [self.finder_file_config_path, 
                        self.working_directory, '0', '0', 
                        'yes' if is_live_mode else 'no']
            
        process = subprocess.Popen(
            [self.executable_path] + cmd_line_opt, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE)
            
        stdout, stderr = process.communicate()

        # Handle the output to log and learn the event ID
        # that FinDer assinged for the output. This event ID
        # will be used to combine a path to the event folder.
        self._process_finder_output(stdout, stderr)

        # Check if the process is successful
        if process.returncode != 0:
            self.logger.error(f"FinDer execution failed with return code: {process.returncode}")
            # self.logger.error(f"Check the log file for more details: {self.logger.log_file}")
            sys.exit(1)

        # Return the stdout, stderr, and the return code, although we don't need them
        return stdout, stderr, process.returncode
        
    def _collect_finder_output(self, event_id):
        """ Collect the FinDer output. """
        self.logger.info("Collecting the FinDer output...")

        # Check if the event ID is set
        if self.finder_event_id is None:
            self.logger.error("FinDer did not return an event ID. Check the output for errors.")
            return
        if event_id is None:
            self.logger.error("Event ID is not set. Check the output for errors.")
            return
        
        # Folder where the FinDer output is stored:
        # <output_root_folder>/<event_id>/temp_data/<finder_event_id>
        event_output_folder = os.path.join(
            self.working_directory, "temp_data", self.finder_event_id)
         
        # Create a FinderSolution object to store the FinDer output
        self.finder_solution = FinderSolution()
        self.finder_solution.set_finder_event_id(self.get_finder_event_id())
        self.finder_solution.set_event_id(event_id)

        # Read the FinDer output files
        event_file = os.path.join(event_output_folder, "core_info_0")
        event_solution = read_event_solution_from_file(event_file)

        rupture_file = os.path.join(event_output_folder, "finder_rupture_list_0")
        rupture_polygon = read_rupture_polygon_from_file(rupture_file)
        
        finder_channels_file = os.path.join(event_output_folder, "data_0")
        finder_channels = read_finder_channels_from_file(finder_channels_file)

        # Store the FinDer solution
        self.finder_solution.set_event(event_solution)
        self.finder_solution.set_rupture(rupture_polygon)
        self.finder_solution.set_channels(finder_channels)
        self.finder_solution.set_description("Solution with processed amplitudes")

        # Attach the input solution (raw input channels) to the finder_solution.
        raw_solution = FinderSolution()
        raw_solution.set_finder_event_id(self.get_finder_event_id())
        raw_solution.set_event_id(event_id)
        raw_solution.set_channels(self.get_finder_used_channels())
        raw_solution.set_event(event_solution)
        raw_solution.set_rupture(rupture_polygon)
        raw_solution.set_description("Solution with raw amplitudes")
        # Set the input solution to the finder_solution
        self.finder_solution.input_solution = raw_solution
        self.logger.info("A FinDer solution with raw amplitudes are stored in the FinderSolution object.")

        # Log the FinDer solution
        self.logger.info("FinDer solution is collected.")
        self.logger.info(f"{self.finder_solution}")
        self.logger.info("The actual FinDer solution is stored in the FinderSolution object.")

    def materialize_inputs(
        self,
        amplitudes,
        event_data,
        *,
        augmented_event_id,
    ):
        """Write the existing FinDer configuration and input data files."""
        self._prepare_workspace(augmented_event_id)
        with self._workspace_phase(augmented_event_id):
            return self._materialize_inputs(amplitudes, event_data)

    def _materialize_inputs(self, amplitudes, event_data):
        """Write invocation inputs while the caller owns the workspace lock."""
        self._write_finder_configuration()
        data_path, self.finder_used_channels = self._write_data_for_finder(
            amplitudes,
            event_data,
        )
        return self.finder_file_config_path, data_path

    def execute(self, amplitudes, event_data, *, augmented_event_id):
        """ Runs the FinDer executable. Entry point for the class. """
        # The start time of the execution
        _exec_start = datetime.now()

        # Workspace identity is supplied by the manager. Validate it before
        # checking resources or preparing any invocation-owned files.
        select_workspace_path(
            self.get_configured_root_folder(),
            augmented_event_id,
        )

        # Check if the executable exists
        self._check_finder_executable()

        # Get the event id to create the working directory
        event_id = event_data.get_event_id()

        self._prepare_workspace(augmented_event_id)
        # FinDer reads these shared workspace inputs throughout its invocation,
        # so another process must not rewrite them until output collection ends.
        with self._workspace_phase(augmented_event_id):
            self._materialize_inputs(amplitudes, event_data)

            try:
                # Execute the FinDer executable
                self._run_finder()

                # Log the success and collect the output
                self.logger.info(f"FinDer execution completed. Now collecting the output...")

                # Collect the output of the executable
                self._collect_finder_output(event_id=event_id)

            except Exception as e:
                # Log the error and exit
                self.logger.error(f"Error executing FinDer:")
                self.logger.error(f"{e}")
                sys.exit(1)

            finally:
                # The end of the execution
                _exec_end = datetime.now()
                _exec_time = _exec_end - _exec_start
                _exec_time_min = round(_exec_time.total_seconds() / 60, 2)

                self.logger.info(f"execute() FINISHED... Elapsed time is {_exec_time} ({_exec_time_min} minutes)")

        # Return the self object for further use
        return self
