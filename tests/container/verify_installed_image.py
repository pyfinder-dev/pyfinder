"""Verify the installed PyFinder image without invoking external services."""

import bz2
import builtins
from copy import deepcopy
from datetime import datetime, timezone
import ctypes
import fcntl
import importlib.metadata as metadata
import json
import logging
import lzma
import os
from pathlib import Path
import platform
import re
import shutil
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
from unittest import mock


EXPECTED_BASE_IMAGE = "ghcr.io/sceylan/finder-base:gmt5"
SERVICE_ROOT = Path("/home/sysop/runtime/pyfinder")
IDENTITY = "controlled-event_t00010"


def require(condition, message):
    """Raise one visible verification failure instead of silently skipping it."""
    if not condition:
        raise AssertionError(message)


def installed_root(distribution_name, module):
    """Return and validate normal installed distribution and module roots."""
    distribution = metadata.distribution(distribution_name)
    distribution_root = Path(distribution.locate_file("")).resolve()
    module_root = Path(module.__file__).resolve().parent
    for path in (distribution_root, module_root):
        require("site-packages" in path.parts, "not installed in site-packages: {0}".format(path))
        require("build" not in path.parts, "import came from /build: {0}".format(path))
        require(
            "paramws-clients" not in path.parts,
            "import came from a ParamWS checkout: {0}".format(path),
        )
    return distribution, distribution_root, module_root


def assert_cli_help(pyfinder_command):
    """Exercise installed CLI help without dispatching a workflow."""
    commands = ((), ("continuous",), ("playback",), ("on-demand",))
    for command in commands:
        completed = subprocess.run(
            [pyfinder_command, *command, "--help"],
            cwd="/home/sysop",
            text=True,
            capture_output=True,
            check=False,
        )
        require(completed.returncode == 0, completed.stderr)
        require("usage:" in completed.stdout.lower(), "CLI help was not rendered")


class ControlledEvent:
    """Supply deterministic event values through the existing formatter seam."""

    def get_latitude(self):
        return 0.0

    def get_longitude(self):
        return 0.0

    def get_depth(self):
        return 10.0

    def get_magnitude(self):
        return 5.6

    def get_origin_time(self):
        return "2026-08-10T08:15:30.250000Z"


def forbidden_call(operation):
    """Fail immediately if materialization attempts an external operation."""
    def reject(*_args, **_kwargs):
        raise AssertionError("controlled materialization attempted {0}".format(operation))

    return reject


def main():
    require(sys.platform == "linux", "container operating system is not Linux")
    require(
        platform.machine().lower() in {"amd64", "x86_64"},
        "container architecture is not amd64: {0}".format(platform.machine()),
    )
    require((os.geteuid(), os.getegid()) == (1000, 1000), "runtime identity is not 1000:1000")
    supplied_python_version = os.environ["PYFINDER_IMAGE_PYTHON_VERSION"]
    supplied_base_digest = os.environ["PYFINDER_IMAGE_BASE_DIGEST"]
    actual_python_version = platform.python_version()
    require(sys.version_info[:2] == (3, 12), "Python is not from the 3.12 series")
    require(
        re.fullmatch(r"3\.12\.\d+", supplied_python_version) is not None,
        "image Python version context is invalid",
    )
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", supplied_base_digest) is not None,
        "image base digest context is invalid",
    )
    require(
        actual_python_version == supplied_python_version,
        "actual Python version differs from the image label",
    )
    require(shutil.which("python3.9") is None, "legacy python3.9 executable is present")
    require(all((bz2, ctypes, lzma, sqlite3, ssl)), "native standard-library imports failed")
    require(Path.cwd() == Path("/home/sysop"), "verification did not run outside source checkouts")

    # ParamWS installs its file handler at import time. Bootstrap a supported
    # non-continuous process first so that handler can only select the mounted
    # process-owned log directory.
    from pyfinder import runtime

    require(
        not any(name == "paramws" or name.startswith("paramws.") for name in sys.modules),
        "ParamWS was imported before runtime bootstrap",
    )
    trigger_time = datetime(2026, 8, 11, 12, 34, 56, 789012, tzinfo=timezone.utc)
    runtime_context = runtime.bootstrap_process(
        "playback",
        service_root=SERVICE_ROOT,
        clock=lambda: trigger_time,
    )

    import geopandas
    import numpy
    import paramws
    from paramws.clients import (
        EMSCFeltReportClient,
        ESMShakeMapClient,
        FeltReportEventData,
        FeltReportIntensityData,
        PeakMotionData,
        RRSMPeakMotionClient,
        ShakeMapEventData,
        ShakeMapStationAmplitudes,
    )
    from paramws.clients.services.baseconnector import BaseWebServiceConnector
    from paramws.utils import customlogger as paramws_customlogger
    import shapely
    import tornado

    import pyfinder
    from pyfinder import cli, finderexec, findermanager
    from pyfinder.finderconfigs import profiles
    from pyfinder.pyfinderconfig import pyfinderconfig
    from pyfinder.utils import customlogger

    require(
        all(
            (
                geopandas,
                numpy,
                shapely,
                tornado,
                cli,
                finderexec,
                findermanager,
                runtime,
                EMSCFeltReportClient,
                ESMShakeMapClient,
                FeltReportEventData,
                FeltReportIntensityData,
                PeakMotionData,
                RRSMPeakMotionClient,
                ShakeMapEventData,
                ShakeMapStationAmplitudes,
            )
        ),
        "required runtime imports failed",
    )

    pyfinder_distribution, pyfinder_distribution_root, pyfinder_root = installed_root(
        "pyfinder", pyfinder
    )
    paramws_distribution, paramws_distribution_root, paramws_root = installed_root(
        "paramws-clients", paramws
    )
    pyfinder_command = shutil.which("pyfinder")
    require(pyfinder_command is not None, "installed pyfinder command is absent")
    assert_cli_help(pyfinder_command)

    wkt_root = pyfinder_root / "extern/finder_regional_wkt"
    country_root = pyfinder_root / "extern/ne_110m_admin_0_countries"
    require(wkt_root.is_dir(), "installed WKT resource directory is absent")
    require(country_root.is_dir(), "installed country resource directory is absent")
    registered_wkt_files = tuple(
        profile.wkt_filename for profile in profiles.REGIONAL_PROFILES
    )
    require(
        registered_wkt_files and all(registered_wkt_files),
        "installed profile catalog has no usable WKT registrations",
    )
    wkt_files = {
        path.name
        for path in wkt_root.glob("*.wkt")
        if path.is_file() and path.stat().st_size > 0
    }
    require(
        set(registered_wkt_files).issubset(wkt_files),
        "a registered WKT resource is absent or empty",
    )
    unregistered_wkt_files = wkt_files - set(registered_wkt_files)
    require(
        unregistered_wkt_files,
        "the packaged WKT pool has no resource outside the profile catalog",
    )
    country_stem = "ne_110m_admin_0_countries"
    required_country_files = tuple(
        country_root / "{0}.{1}".format(country_stem, suffix)
        for suffix in ("shp", "shx", "dbf", "prj")
    )
    require(
        all(path.is_file() and path.stat().st_size > 0 for path in required_country_files),
        "a required country shapefile component is absent or empty",
    )

    finder_paths = (
        Path("/usr/local/src/FinDer/finder_run"),
        Path("/usr/local/src/FinDer/finder_create_mask"),
    )
    for finder_path in finder_paths:
        require(finder_path.is_file(), "FinDer executable is absent: {0}".format(finder_path))
        require(os.access(finder_path, os.X_OK), "FinDer path is not executable: {0}".format(finder_path))

    installed_names = {distribution.metadata["Name"].lower() for distribution in metadata.distributions()}
    require("shakemap" not in installed_names, "ShakeMap distribution is installed")
    require(shutil.which("shake") is None, "ShakeMap command is installed")
    require(not (pyfinder_root / "extern/shakemap-conf-eu").exists(), "ShakeMap resources are packaged")
    for forbidden_path in (
        Path("/build"),
        Path("/tmp/wheelhouse"),
        Path("/tmp/pyfinder-build-paramws.log"),
        Path("/home/sysop/.pyfinder_alert_config"),
        Path("/home/sysop/.pyfinder_alert_config.json"),
        Path("/home/sysop/paramws.log"),
        Path("/home/sysop/shakemap_profiles"),
    ):
        require(not forbidden_path.exists(), "forbidden image artifact exists: {0}".format(forbidden_path))
    for path in pyfinder_root.rglob("*"):
        require(path.name not in {".pyfinder_alert_config", ".pyfinder_alert_config.json", "gmt.conf", "gmt.history"}, "secret/runtime artifact is packaged: {0}".format(path))
        require(path.suffix.lower() not in {".db", ".log", ".sqlite", ".sqlite3"}, "persistent runtime artifact is packaged: {0}".format(path))

    build_information_path = Path("/usr/local/share/pyfinder/build-info.json")
    build_information = json.loads(build_information_path.read_text(encoding="utf-8"))
    require(build_information["base_image"] == EXPECTED_BASE_IMAGE, "base image record differs")
    require(build_information["base_digest"] == supplied_base_digest, "base digest record differs from the image label")
    require(build_information["python_version"] == supplied_python_version, "Python record differs from the image label")
    require(build_information["python_version"] == actual_python_version, "Python record differs from the interpreter")
    require(build_information["pyfinder"]["version"] == pyfinder_distribution.version, "PyFinder version record differs")
    require(Path(build_information["pyfinder"]["distribution_origin"]) == pyfinder_distribution_root, "PyFinder distribution origin record differs")
    require(Path(build_information["pyfinder"]["module_origin"]) == pyfinder_root, "PyFinder origin record differs")
    require(build_information["paramws"]["version"] == paramws_distribution.version, "ParamWS version record differs")
    require(Path(build_information["paramws"]["distribution_origin"]) == paramws_distribution_root, "ParamWS distribution origin record differs")
    require(Path(build_information["paramws"]["module_origin"]) == paramws_root, "ParamWS origin record differs")
    require(
        re.fullmatch(r"[0-9a-f]{40}", build_information["paramws"]["commit"])
        is not None,
        "ParamWS commit record is not a full Git commit",
    )

    paramws_file_handlers = [
        handler
        for handler in paramws_customlogger.logger.handlers
        if getattr(handler, "baseFilename", None)
    ]
    require(len(paramws_file_handlers) == 1, "ParamWS does not own exactly one file handler")
    paramws_handler_path = Path(paramws_file_handlers[0].baseFilename).resolve()
    require(paramws_handler_path == runtime_context.paramws_log_path, "ParamWS handler escaped its process log directory")
    require(paramws_handler_path.is_file(), "ParamWS log destination was not created")
    require(not Path("/home/sysop/paramws.log").exists(), "ParamWS wrote to the container home")
    require(not (Path.cwd() / "paramws.log").exists(), "relative ./paramws.log was created")
    home_paramws_logs = {
        path.resolve() for path in Path("/home/sysop").rglob("paramws.log")
    }
    require(
        home_paramws_logs == {paramws_handler_path},
        "ParamWS created a log outside its mounted process directory",
    )

    event_data = ControlledEvent()
    observations = [
        {
            "latitude": 0.0,
            "longitude": 1.0,
            "network": "CH",
            "station": "IMAGECHECK",
            "location": "00",
            "channel": "HNZ",
            "pga": 12.5,
            "timestamp": 1786349730.25,
            "source": "RRSM",
            "provider_value": 12.5,
            "provider_unit": "cm/s^2",
        }
    ]
    expected_data = (
        b"# 1786349730 0\n"
        b"0.0 0.0 1.101231386790699\n"
        b"0.0 1.0 1.0969100130080565"
    )
    expected_companion = (
        b"# SNCL PGA_CM_S2 EPI_DISTANCE_KM\n"
        b"XX.NONE.00.HNZ 12.625 0.0\n"
        b"CH.IMAGECHECK.00.HNZ 12.5 111.2"
    )
    process_logger = customlogger.file_logger(
        runtime_context.process_log_path,
        module_name="image-verification.materialization",
        overwrite=False,
        rotate=False,
    )
    configuration = runtime_context.isolated_configuration(pyfinderconfig)
    executable = finderexec.FinDerExecutable(
        options={"command_line_args": "controlled installed image materialization"},
        configuration=configuration,
        finder_configuration_name=profiles.GLOBAL_PROFILE.name,
        finder_configuration=deepcopy(profiles.GLOBAL_PROFILE.configuration),
        logger=process_logger,
    )
    workspace = runtime_context.playbacks_directory / IDENTITY
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"pyfinder.utils.shakemap", "pyfinder.services.alert"}:
            raise AssertionError("controlled materialization imported external-service code: {0}".format(name))
        return original_import(name, *args, **kwargs)

    # Materialization ends before FinDer starts. The mocks make accidental
    # FinDer, provider, network, ShakeMap, or email activity fail immediately.
    with mock.patch.object(executable, "_check_finder_executable", side_effect=forbidden_call("FinDer validation")) as finder_check, mock.patch.object(executable, "_run_finder", side_effect=forbidden_call("FinDer execution")) as finder_run, mock.patch.object(finderexec.subprocess, "Popen", side_effect=forbidden_call("FinDer subprocess")) as process_constructor, mock.patch.object(finderexec, "read_event_solution_from_file", side_effect=forbidden_call("FinDer output")) as read_event, mock.patch.object(finderexec, "read_rupture_polygon_from_file", side_effect=forbidden_call("FinDer output")) as read_rupture, mock.patch.object(finderexec, "read_finder_channels_from_file", side_effect=forbidden_call("FinDer output")) as read_channels, mock.patch.object(finderexec, "get_epoch_time", return_value=1786349730.25), mock.patch.object(finderexec.Calculator, "predict_PGA_from_magnitude", return_value=1.0), mock.patch.object(BaseWebServiceConnector, "query", side_effect=forbidden_call("provider query")) as provider_query, mock.patch.object(BaseWebServiceConnector, "open_url", side_effect=forbidden_call("provider transport")) as provider_transport, mock.patch.object(socket, "socket", side_effect=forbidden_call("network socket")) as socket_constructor, mock.patch.object(smtplib, "SMTP", side_effect=forbidden_call("SMTP")) as smtp_constructor, mock.patch.object(smtplib, "SMTP_SSL", side_effect=forbidden_call("SMTP SSL")) as smtp_ssl_constructor, mock.patch("builtins.__import__", side_effect=guarded_import):
        config_path, data_path = executable.materialize_inputs(
            observations,
            event_data,
            augmented_event_id=IDENTITY,
        )

    for guarded_mock in (
        finder_check,
        finder_run,
        process_constructor,
        read_event,
        read_rupture,
        read_channels,
        provider_query,
        provider_transport,
        socket_constructor,
        smtp_constructor,
        smtp_ssl_constructor,
    ):
        require(guarded_mock.call_count == 0, "materialization attempted an external action")

    config_path = Path(config_path)
    data_path = Path(data_path)
    companion_path = workspace / "pyfinder_amplitudes_to_Finder.txt"
    workspace_log_path = workspace / "pyfinder.log"
    require(workspace == Path(executable.get_working_directory()), "augmented workspace differs")
    require(config_path == workspace / "finder_file.config", "configuration path differs")
    require(data_path == workspace / "data_0", "data path differs")
    require(companion_path.is_file(), "amplitude companion is absent")
    require(workspace_log_path.is_file(), "workspace log is absent")
    require(data_path.read_bytes() == expected_data, "installed formatter output differs")
    require(
        companion_path.read_bytes() == expected_companion,
        "installed amplitude companion output differs",
    )
    configuration_lines = config_path.read_text(encoding="utf-8").splitlines()
    require("DATA_FOLDER {0}".format(workspace) in configuration_lines, "DATA_FOLDER does not select the workspace")

    workspace_log = workspace_log_path.read_text(encoding="utf-8")
    require(IDENTITY in workspace_log, "workspace log lacks augmented identity")
    require("FinDer configuration file" in workspace_log, "workspace log lacks configuration diagnostics")
    require("data_0 written" in workspace_log, "workspace log lacks data_0 diagnostics")
    require(
        "FinDer amplitude companion written" in workspace_log,
        "workspace log lacks amplitude companion diagnostics",
    )
    process_log = runtime_context.process_log_path.read_text(encoding="utf-8")
    require("waiting to enter" in process_log, "process log lacks workspace handover")
    require("completed the locked" in process_log, "process log lacks completion handover")
    require(str(workspace) in process_log, "process log lacks workspace path")
    require(str(workspace_log_path) in process_log, "process log lacks event-log path")

    lock_path = workspace / ".pyfinder.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    result = {
        "base_digest": build_information["base_digest"],
        "finder_paths": [str(path) for path in finder_paths],
        "owned_workspace_files": [
            str(config_path),
            str(data_path),
            str(companion_path),
            str(workspace_log_path),
        ],
        "paramws_commit": build_information["paramws"]["commit"],
        "paramws_handler": str(paramws_handler_path),
        "paramws_origin": str(paramws_root),
        "paramws_version": paramws_distribution.version,
        "platform": "linux/amd64",
        "pyfinder_origin": str(pyfinder_root),
        "pyfinder_version": pyfinder_distribution.version,
        "python_version": platform.python_version(),
        "runtime_identity": "{0}:{1}".format(os.geteuid(), os.getegid()),
        "workspace": str(workspace),
    }
    print("PYFINDER_IMAGE_RESULT=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
