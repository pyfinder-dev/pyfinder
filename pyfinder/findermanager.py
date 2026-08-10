#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" 
Main module for running the FinDer library wrapper. The FinDerManager 
class is designed to call either FinDer executable directly or the 
library via the bindings. The bindings are in test phase and not yet 
fully implemented.

The FinDerManager class is designed to be used as a command line
utility as well as a runtime library. 
"""
import os
import sys
import logging
from collections.abc import Mapping

from pyfinder.eventcontext import (
    EventContext,
    EventContextError,
    ProviderModelAccessError,
)
from pyfinder.finderconfigs import (
    GlobalFinderConfigError,
    build_default_selector,
)
from pyfinder.pyfinderconfig import (
    EMSC_FELT_REPORT_SERVICE,
    ESM_SHAKEMAP_SERVICE,
    RRSM_PEAK_MOTION_SERVICE,
    pyfinderconfig,
)
from pyfinder.service_priority import resolve_service_priority
from pyfinder.utils import customlogger
from paramws.clients import (
    EMSCFeltReportClient,
    ESMShakeMapClient,
    RRSMPeakMotionClient,
)
from pyfinder.finderutils import (FinderChannelList, FinderSolution)
from pyfinder.utils.dataformatter import (
    EMSCFeltReportDataFormatter,
    ESMShakeMapDataFormatter,
    FinDerFormatterFromRawList,
    RRSMPeakMotionDataFormatter,
    get_epoch_time,
)
from pyfinder.utils.station_merger import StationMerger


_DEPENDENCY_MODEL_ERRORS = (
    AttributeError,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
)

class FinDerManager:
    """ Class for managing the FinDer library and executable wrappers"""

    ALERT_BACKED = "alert-backed"
    ON_DEMAND = "on-demand"

    @classmethod
    def for_alert_context(
        cls,
        *,
        event_context,
        context_diagnostic=None,
        **kwargs,
    ):
        """Construct a manager for persisted-alert execution."""
        return cls(
            entry_kind=cls.ALERT_BACKED,
            event_context=event_context,
            context_diagnostic=context_diagnostic,
            **kwargs,
        )

    @classmethod
    def for_on_demand(cls, **kwargs):
        """Construct a manager for explicit event-ID-only execution."""
        return cls(entry_kind=cls.ON_DEMAND, **kwargs)

    def __init__(
        self,
        options,
        configuration=None,
        metadata=None,
        finder_configuration_name=None,
        finder_configuration=None,
        logger=None,
        *,
        entry_kind,
        event_context=None,
        context_diagnostic=None,
    ):
        if entry_kind not in (self.ALERT_BACKED, self.ON_DEMAND):
            raise ValueError(
                "FinDerManager requires an explicit alert-backed or "
                "on-demand entry"
            )
        if entry_kind == self.ON_DEMAND and event_context is not None:
            raise ValueError(
                "On-demand construction cannot receive alert-backed context"
            )

        self.entry_kind = entry_kind
        self.event_context = event_context
        self.context_diagnostic = context_diagnostic

        # Options from the command line arguments
        self.options = options

        if configuration is None:
            # Use the default configuration
            self.configuration = pyfinderconfig
        else:
            # Use the user-defined configuration
            self.configuration = configuration

        # Solution metadata mainly for information purposes
        self.metadata = metadata or {}

        # FinDer data directories
        self.finder_temp_data_dir = self.configuration["finder-executable"]["finder-temp-data-dir"]
        self.finder_temp_dir = self.configuration["finder-executable"]["finder-temp-dir"]

        # Working directory
        self.working_dir = None

        # Composition owns file destinations. Direct library construction uses
        # a standard non-file logger and never opens a shared manager log.
        self.logger = logger or logging.getLogger(__name__)

        if isinstance(self.event_context, EventContext):
            self._populate_metadata_from_event_context(self.event_context)

        self._resolve_finder_configuration(
            finder_configuration_name=finder_configuration_name,
            finder_configuration=finder_configuration,
        )

    def _resolve_finder_configuration(
        self,
        finder_configuration_name,
        finder_configuration,
    ):
        """Retain a complete decision or resolve one for direct manager use."""
        name_supplied = finder_configuration_name is not None
        configuration_supplied = finder_configuration is not None
        if name_supplied and configuration_supplied:
            self.finder_configuration_name = finder_configuration_name
            self.finder_configuration = finder_configuration
            return

        if self.entry_kind == self.ON_DEMAND:
            if name_supplied != configuration_supplied:
                self.logger.critical(
                    "Incomplete FinDer configuration handoff was ignored; "
                    "on-demand selection will use its provider context"
                )
            self.finder_configuration_name = None
            self.finder_configuration = None
            return

        if (
            self.entry_kind == self.ALERT_BACKED
            and not isinstance(self.event_context, EventContext)
        ):
            # The run boundary reports the unusable alert context. Avoiding
            # selector construction here prevents that failure from becoming a
            # misleading computational-profile global fallback.
            self.finder_configuration_name = None
            self.finder_configuration = None
            return

        if name_supplied != configuration_supplied:
            self.logger.critical(
                "Incomplete FinDer configuration handoff was ignored; using "
                "the authoritative alert epicenter"
            )
        self._resolve_finder_configuration_from_context(self.event_context)

    def _resolve_finder_configuration_from_context(self, event_context):
        """Resolve one complete FinDer profile from a usable event context."""
        try:
            selector = build_default_selector(logger=self.logger)
        except GlobalFinderConfigError:
            self.logger.critical(
                "Global FinDer configuration validation failed; "
                "manager construction cannot continue",
                exc_info=True,
            )
            raise

        decision = selector.resolve(
            latitude=event_context.get_latitude(),
            longitude=event_context.get_longitude(),
        )
        self.finder_configuration_name = decision.configuration_name
        self.finder_configuration = decision.configuration

    def _populate_metadata_from_event_context(self, event_context):
        """Copy authoritative earthquake values into solution metadata."""
        self.metadata["origin_time"] = event_context.get_origin_time()
        self.metadata["longitude"] = event_context.get_longitude()
        self.metadata["latitude"] = event_context.get_latitude()
        self.metadata["magnitude"] = event_context.get_magnitude()
        self.metadata["depth"] = event_context.get_depth()
        self.metadata["magnitude_type"] = event_context.get_magnitude_type()
        
    def set_finder_data_dirs(self, working_dir, finder_event_id):
        """ Set the FinDer data directories using the event id from FinDer run """
        self.finder_temp_data_dir = self.finder_temp_data_dir.replace(
            "{FINDER_RUN_DIR}", working_dir)
        self.finder_temp_dir = self.finder_temp_dir.replace(
            "{FINDER_RUN_DIR}", working_dir)
        
        # Combine the event id with the working directory
        self.finder_temp_data_dir = os.path.join(
            self.finder_temp_data_dir, finder_event_id)
        
        self.logger.info(f"FinDer temp data directory: {self.finder_temp_data_dir}")
        self.logger.info(f"FinDer temp directory: {self.finder_temp_dir}")

    def run(self, event_id=None, file_path=None) -> FinderSolution:
        """ 
        Run the FinDer library based on an event_id or from a file
        event_id has the priority over file_path. The file_path maybe
        more useful for repeated processing of the same data without
        a need to query webservices.

        Returns a FinderSolution object from one of the process_event or 
        process_file methods.
        """
        if event_id:
            # Query the event_id from the web service
            return self.process_event(event_id)
        elif file_path:
            # Use a pre-existing file to execute FinDer. 
            # Useful for offline processing; not yet implemented.
            return self.process_file(file_path)
        else:
            raise ValueError("An event_id or file_path must be provided")

    def process_file(self, file_path) -> FinderSolution:
        """ Read data from a file and process it """
        raise NotImplementedError(
            "FinDerManager.process_file() method is not implemented yet")

    def _rename_channel_codes(self, finder_used_channels: FinderChannelList):
        """ 
        Rename the channel codes with the real ones in the FinDer output 
        by matching the coordinates. This is performed when live_mode is
        False, where FinDer assigns channel/station codes itself. 
        """
        if not finder_used_channels or len(finder_used_channels) == 0:
            self.logger.error("No FinDer channel codes to rename. List is empty.")
            return
        
        self.logger.info("Renaming the channel codes in the FinDer output.")        
        
        # Get FinDer's version of data_0 file
        finder_data_0 = os.path.join(self.finder_temp_data_dir, "data_0")
        
        # Check if the file exists
        if not os.path.exists(finder_data_0):
            self.logger.error(f"File {finder_data_0} does not exist. Cannot rename the channel codes.")

        # Read the FinDer data_0 file
        stations = {
            "lat": [],
            "lon": [],
            "sncl": [],
            "timestamp": [],
            "pga": []
        }
        header = "# "
        with open(finder_data_0, 'r') as f:
            lines = f.readlines()

            # Read the header
            header = lines[0]

            # And the data
            lines = lines[1:]
    
            for line in lines:
                _line = line.strip().split()
                stations['lat'].append(float(_line[0]))
                stations['lon'].append(float(_line[1]))
                
                # Find this station in the used channels
                for _channel in finder_used_channels:
                    if _channel.get_latitude() == float(_line[0]) and \
                        _channel.get_longitude() == float(_line[1]):
                        # Replace the channel code
                        _line[2] = _channel.get_sncl()
                        break

                stations['sncl'].append(_line[2])
                stations['timestamp'].append(_line[3])
                stations['pga'].append(float(_line[4]))

        # Write the new data_0 file
        renamed_data_0 = os.path.join(self.finder_temp_data_dir, "data_0_renamed")
        with open(renamed_data_0, 'w') as f:
            f.write(header)
            for i in range(len(stations['lat'])):
                f.write(f"{stations['lat'][i]}  {stations['lon'][i]}  {stations['sncl'][i]}  " + \
                        f"{stations['timestamp'][i]} {stations['pga'][i]}\n")

        self.logger.info(f"Channel codes have been renamed in the FinDer output. New file: {renamed_data_0}")

    def _send_failure_email(self, event_id, attachment=None):
        try:
            from services.alert import send_email_with_attachment
            subject = f"pyFinder Alert - event {event_id}"
            body = f"pyFinder attempted a shakemap calculation for {event_id},\n"
            body += f"but FinDer executable failed to produce a solution for the event.\n"
            body += f"Check the FinDer logs for more details.\n"
            
            send_email_with_attachment(
                subject=subject,
                body=body,
                attachments=[attachment],
                event_id=event_id,
                finder_solution=None,
                metadata=self.metadata
            )
            self.logger.info(f"Failure notification sent.")

        except Exception as e:
            self.logger.error(f"Failed to send failure notification: {e}")


    def _build_augmented_event_id(self, event_id, delay_minutes):
        self.logger.info(f"Building augmented event id for {event_id} with delay {delay_minutes} minutes.")

        if delay_minutes is not None:
            # e.g., "t00010" for 10 min
            appendix = f"t{int(delay_minutes):05d}"  
        else:
            # fallback if delay is undefined
            appendix = "t00000"  
        
        # e.g., "20230101_013045_t00010"
        return f"{event_id}_{appendix}"

    def _configured_enabled_services(self):
        """Return unique recognized enabled services in configured order."""
        configured = self.configuration.get("general", {}).get(
            "services-enabled",
            [],
        )
        if not isinstance(configured, list):
            self.logger.critical(
                "The configured services-enabled value must be a list; no "
                "observation provider can be queried"
            )
            return []

        recognized = []
        for service_name in configured:
            if service_name not in (
                ESM_SHAKEMAP_SERVICE,
                RRSM_PEAK_MOTION_SERVICE,
                EMSC_FELT_REPORT_SERVICE,
            ):
                self.logger.critical(
                    "Unsupported enabled observation service %r was skipped",
                    service_name,
                )
                continue
            if service_name not in recognized:
                recognized.append(service_name)
        return recognized

    @staticmethod
    def _new_provider_outcome():
        """Create the stable diagnostic fields for one provider attempt."""
        return {
            "status_code": None,
            "normalized_count": 0,
            "failure_kind": None,
            "diagnostic": None,
            "event_context_usable": False,
            "context_diagnostic": None,
        }

    @staticmethod
    def _append_diagnostic(outcome, diagnostic):
        """Retain multiple useful provider diagnostics without extra fields."""
        if outcome["diagnostic"]:
            outcome["diagnostic"] += f"; {diagnostic}"
        else:
            outcome["diagnostic"] = diagnostic

    def _record_provider_failure(
        self,
        service_name,
        outcome,
        failure_kind,
        diagnostic,
        *,
        exc_info=False,
    ):
        """Record and report one contained provider observation failure."""
        outcome["failure_kind"] = failure_kind
        self._append_diagnostic(outcome, diagnostic)
        self.logger.error(
            "%s observation acquisition failed: %s",
            service_name,
            diagnostic,
            exc_info=exc_info,
        )

    def _acquire_provider(self, service_name, event_id):
        """Query one recognized provider and adapt its two public results."""
        outcome = self._new_provider_outcome()
        acquired = {
            "event_context": None,
            "scientific_value": None,
            "outcome": outcome,
        }

        try:
            if service_name == ESM_SHAKEMAP_SERVICE:
                client = ESMShakeMapClient()
            elif service_name == RRSM_PEAK_MOTION_SERVICE:
                client = RRSMPeakMotionClient()
            else:
                client = EMSCFeltReportClient()
            query_result = client.query(event_id=event_id)
        except Exception as error:
            self._record_provider_failure(
                service_name,
                outcome,
                "exception",
                f"client construction or query raised {error!r}",
                exc_info=True,
            )
            return acquired

        if not isinstance(query_result, tuple) or len(query_result) != 3:
            self._record_provider_failure(
                service_name,
                outcome,
                "invalid-result",
                "query did not return the expected three-item tuple",
            )
            return acquired

        status_code, event_candidate, datasets = query_result
        outcome["status_code"] = status_code
        if status_code != 200:
            self._append_diagnostic(
                outcome,
                f"provider returned aggregate status {status_code!r}",
            )

        try:
            acquired["event_context"] = EventContext.from_provider_model(
                event_candidate,
                requested_event_id=event_id,
            )
        except (EventContextError, ProviderModelAccessError) as error:
            outcome["context_diagnostic"] = str(error)
            self.logger.error(
                "%s event context candidate is unusable: %s",
                service_name,
                error,
            )
        else:
            outcome["event_context_usable"] = True

        if service_name == EMSC_FELT_REPORT_SERVICE:
            try:
                felt_reports = client.get_feltreports()
            except Exception as error:
                self._record_provider_failure(
                    service_name,
                    outcome,
                    "exception",
                    f"single-event felt view raised {error!r}",
                    exc_info=True,
                )
                return acquired
            if felt_reports is None:
                self._record_provider_failure(
                    service_name,
                    outcome,
                    "invalid-result",
                    "single-event felt view is missing",
                )
                return acquired
            acquired["scientific_value"] = felt_reports
            return acquired

        if not isinstance(datasets, Mapping):
            self._record_provider_failure(
                service_name,
                outcome,
                "invalid-result",
                "query datasets value is not a mapping",
            )
            return acquired

        dataset_key = (
            "station_amplitudes"
            if service_name == ESM_SHAKEMAP_SERVICE
            else "peak_motion"
        )
        try:
            if dataset_key not in datasets:
                self._record_provider_failure(
                    service_name,
                    outcome,
                    "invalid-result",
                    f"query datasets are missing {dataset_key!r}",
                )
                return acquired
            scientific_value = datasets[dataset_key]
        except _DEPENDENCY_MODEL_ERRORS as error:
            self._record_provider_failure(
                service_name,
                outcome,
                "exception",
                f"query dataset access raised {error!r}",
                exc_info=True,
            )
            return acquired

        if scientific_value is None:
            self._record_provider_failure(
                service_name,
                outcome,
                "invalid-result",
                f"query dataset {dataset_key!r} is missing",
            )
            return acquired

        acquired["scientific_value"] = scientific_value
        return acquired

    def _acquire_enabled_providers(self, event_id):
        """Attempt every recognized enabled provider exactly once."""
        acquired = {}
        for service_name in self._configured_enabled_services():
            acquired[service_name] = self._acquire_provider(
                service_name,
                event_id,
            )
        return acquired

    def _select_on_demand_context(self, acquired, service_priority):
        """Select the first queried usable candidate in scientific priority."""
        for service_name in service_priority:
            provider_result = acquired.get(service_name)
            if (
                provider_result is not None
                and provider_result["event_context"] is not None
            ):
                return provider_result["event_context"]
        return None

    def _normalize_provider(self, service_name, event_context, value):
        """Normalize one provider value with the common event context."""
        if service_name == ESM_SHAKEMAP_SERVICE:
            formatter = ESMShakeMapDataFormatter(
                logger=self.logger,
                configuration=self.configuration,
            )
            return formatter.extract_raw_stations(
                event_data=event_context,
                amplitudes=value,
            )
        if service_name == RRSM_PEAK_MOTION_SERVICE:
            formatter = RRSMPeakMotionDataFormatter(
                logger=self.logger,
                configuration=self.configuration,
            )
            return formatter.extract_raw_stations(
                event_data=event_context,
                amplitudes=value,
            )

        formatter = EMSCFeltReportDataFormatter(logger=self.logger)
        return formatter.extract_raw_stations(
            event_data=event_context,
            felt_reports=value,
        )

    def _normalize_acquired_providers(self, acquired, event_context, event_id):
        """Build normalized available results and complete provider outcomes."""
        available_results = {}
        for service_name, provider_result in acquired.items():
            outcome = provider_result["outcome"]
            normalized = []
            value = provider_result["scientific_value"]
            if value is not None:
                try:
                    normalized = self._normalize_provider(
                        service_name,
                        event_context,
                        value,
                    )
                except ProviderModelAccessError as error:
                    self._record_provider_failure(
                        service_name,
                        outcome,
                        "exception",
                        f"provider model access failed: {error}",
                        exc_info=True,
                    )
                    normalized = []

                if not isinstance(normalized, list):
                    raise TypeError(
                        f"{service_name} normalizer must return a list"
                    )

            if not normalized and outcome["failure_kind"] is None:
                diagnostic = (
                    f"{service_name} produced zero usable normalized "
                    f"observations for event {event_id}"
                )
                self._append_diagnostic(outcome, diagnostic)
                self.logger.error(diagnostic)

            outcome["normalized_count"] = len(normalized)
            available_results[service_name] = normalized
        return available_results

    def _store_provider_outcomes(self, acquired):
        """Expose provider outcomes and retain legacy display status fields."""
        outcomes = {
            service_name: provider_result["outcome"]
            for service_name, provider_result in acquired.items()
        }
        self.metadata["provider_outcomes"] = outcomes

        for service_name, metadata_key in (
            (ESM_SHAKEMAP_SERVICE, "ESM_status"),
            (RRSM_PEAK_MOTION_SERVICE, "RRSM_status"),
        ):
            if service_name not in outcomes:
                continue
            status_code = outcomes[service_name]["status_code"]
            self.metadata[metadata_key] = (
                "Success"
                if status_code == 200
                else "Failed with HTTP " + str(status_code)
            )

    def _merge_available_results(self, available_results, service_priority):
        """Hand the exact normalized service mapping to the merger."""
        merger = StationMerger(
            service_priority=service_priority,
            logger=self.logger,
        )
        return merger.merge(available_results)

    def process_event(self, event_id) -> FinderSolution:
        """ Process data associated with an event_id """
        # Check if the event_id is not None
        if not event_id:
            raise ValueError("An event_id must be provided intead of None")

        if self.entry_kind == self.ALERT_BACKED:
            if not isinstance(self.event_context, EventContext):
                diagnostic = self.context_diagnostic or (
                    "the persisted EMSC alert context is missing or unusable"
                )
                self.logger.critical(
                    "Cannot process alert-backed event %s because its "
                    "authoritative EMSC alert context is unusable: %s. "
                    "Provider acquisition was not started.",
                    event_id,
                    diagnostic,
                )
                return None
            if self.event_context.get_event_id() != event_id:
                self.logger.critical(
                    "Cannot process alert-backed event %s because its "
                    "authoritative context belongs to %s. Provider acquisition "
                    "was not started.",
                    event_id,
                    self.event_context.get_event_id(),
                )
                return None
            authoritative_event = self.event_context
        else:
            authoritative_event = None

        configured_priority = self.configuration.get("general", {}).get(
            "services-priority"
        )
        service_priority = resolve_service_priority(
            configured_priority,
            logger=self.logger,
        )
        
        self.logger.info(
            "Querying configured observation services for event %s",
            event_id,
        )
        acquired = self._acquire_enabled_providers(event_id)
        self._store_provider_outcomes(acquired)
        self.available_results = {
            service_name: [] for service_name in acquired
        }

        if authoritative_event is None:
            authoritative_event = self._select_on_demand_context(
                acquired,
                service_priority,
            )
            if authoritative_event is None:
                diagnostic = (
                    "normalization was not attempted because no enabled "
                    "provider supplied a usable event context"
                )
                for provider_result in acquired.values():
                    self._append_diagnostic(
                        provider_result["outcome"],
                        diagnostic,
                    )
                self.logger.error(
                    "FinDer cannot process on-demand event %s because no "
                    "usable provider event context was returned",
                    event_id,
                )
                return None

            self.event_context = authoritative_event
            self._populate_metadata_from_event_context(authoritative_event)
            if (
                self.finder_configuration_name is None
                or self.finder_configuration is None
            ):
                self._resolve_finder_configuration_from_context(
                    authoritative_event
                )

        self.logger.info("Extracting normalized observations ...")
        self.available_results = self._normalize_acquired_providers(
            acquired,
            authoritative_event,
            event_id,
        )
        self.logger.info("Normalized observations extracted.")

        _event_data = authoritative_event

        self._populate_metadata_from_event_context(_event_data)
        self.logger.info(f"Calculation metadata: {self.metadata}")

        if not any(self.available_results.values()):
            self.logger.error(
                "All normalized observation sources produced zero usable "
                "records for event %s. FinDer cannot be run.",
                event_id,
            )
            return None

        # The downstream handoff is always the merged normalized list. An
        # empty normalized source must never cause its raw provider model to
        # bypass this boundary and reach a direct provider formatter.
        self.logger.info("Merging normalized observation data")
        _amplitude_data = self._merge_available_results(
            self.available_results,
            service_priority,
        )
        self.logger.info("Merge completed")


        # A final check before running FinDer
        if not _event_data or not _amplitude_data:
            self.logger.warning("FinDer cannot be run.")
            if not _event_data:
                self.logger.warning(
                    "|- Reason: No usable common event metadata was available.")
            if not _amplitude_data:
                self.logger.error(
                    "|- Reason: The merged normalized observation list is "
                    "empty.")
            self.logger.warning(f"|- event_id: {event_id}")
            self.logger.warning(
                "|- Normalized observations by service: %s",
                {
                    service_name: len(records)
                    for service_name, records in self.available_results.items()
                },
            )
            self.logger.warning(
                "|- Provider outcomes: %s",
                self.metadata["provider_outcomes"],
            )

            return None

        if self.options["use_library"]:
            # Call the FinDer library wrapper
            from finderlib import FinderLibrary
            library = FinderLibrary(
                options=self.options, configuration=self.configuration).execute(
                    event_data=_event_data, amplitudes=_amplitude_data)
            
            # Return None for the library wrapper. Once implemented, it should return 
            # a FinderSolution object: return True for a valid FinderLibrary.get_finder_solution()
            return None
        
        else:
            # Call the FinDer executable
            self.logger.info("Starting FinDer executable")
            from pyfinder.finderexec import FinDerExecutable
            finder_executable = FinDerExecutable(
                options=self.options,
                configuration=self.configuration,
                finder_configuration_name=self.finder_configuration_name,
                finder_configuration=self.finder_configuration,
                logger=self.logger,
            )
            executable = finder_executable.execute(
                event_data=_event_data,
                amplitudes=_amplitude_data,
            )
            
            # Check if the executable was successful
            if not executable or not executable.get_finder_solution_object():
                self.logger.error("FinDer executable failed to run or returned no solution.")
                self.logger.error("Check the FinDer ouput in the pyfinder logs for more details.")
                self.logger.warning("Returning to caller with no solution.")

                self._send_failure_email(
                    event_id=event_id, 
                    attachment=os.path.join(
                        executable.get_working_directory(), "pyfinder.log")
                )
                    
                # Return None for no solution
                return None
            self.logger.info("FinDer executable completed successfully")
            

            # Set the FinDer data directories
            self.working_dir = executable.get_working_directory()
            self.set_finder_data_dirs(
                working_dir=executable.get_working_directory(), 
                finder_event_id=executable.get_finder_event_id())
            
            # Build a new eventid with the scheduled delay time and export the data for shakemap 
            from utils.shakemap import ShakeMapExporter
            augmented_event_id = self._build_augmented_event_id(
                event_id=event_id, delay_minutes=self.metadata['current_delay'])
            self.logger.info(f"Augmented event id for shakemap is {augmented_event_id}")
            
            # Check if we are passing the amplitudes from FinDer output
            use_finder_amplitudes = self.configuration.get("shakemap", {}).get("use-amplitude-from-finder-output", False)
            self.logger.info(f"To ShakeMap :: Are you passing the amplitudes from FinDer output? {use_finder_amplitudes}")

            smap_exporter = ShakeMapExporter(
                solution=executable.get_finder_solution_object(),
                augmented_id=augmented_event_id,
                logger=self.logger)
            shakemapexp = smap_exporter.export_all()
            self.logger.info(f"ShakeMap files exported to: {shakemapexp['output_dir']}")

            # Trigger ShakeMap using exported files
            from utils.shakemap import ShakeMapTrigger
            # Create the products directory
            products_dir = os.path.join(shakemapexp["output_dir"], "products")
            os.makedirs(products_dir, exist_ok=True)
            # Copy the ShakeMap files to the products directory
            trigger = ShakeMapTrigger(
                event_id=augmented_event_id,#event_id,
                event_xml=shakemapexp["event.xml"],
                stationlist_path=shakemapexp["stationlist.json"],
                rupture_path=shakemapexp["rupture.json"]  
            )
            trigger.run()

            # Archive the products via ShakeMap exporter under the temp_data directory
            smap_exporter.archive_products(target_base_dir=self.finder_temp_data_dir)

            from services.alert import send_email_with_attachment
            products_dir = os.path.join(shakemapexp["output_dir"], "products")
            attachment = f"{products_dir}/intensity.jpg"
            subject = f"pyFinder Alert - event {event_id}"
            body = f"A new ShakeMap has been produced for event {event_id}.\n"
            send_email_with_attachment(
                subject=subject,
                body=body,
                attachments=[attachment],
                event_id=event_id,
                finder_solution=executable.get_finder_solution_object(),
                metadata=self.metadata
            )
     
            # Rename the channel codes if live mode is False. When live mode is False,
            # we pass FinDer only the coordinates and it assigns the channel codes itself.
            # We rename them back to the real ones for debugging purposes.
            if not self.configuration["finder-executable"]["finder-live-mode"]:
                self._rename_channel_codes(executable.get_finder_used_channels())

            # Return the FinderSolution object
            return executable.get_finder_solution_object()
            
    
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
