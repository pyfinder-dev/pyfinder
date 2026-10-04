"""Offline playback tests with real SQLite and controlled downstream boundaries."""

from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from pyfinder import playback
from pyfinder.eventcontext import EventContext
from pyfinder.services.eventtracker import EventTracker
from pyfinder.services.querypolicy import RRSMQueryPolicy
from pyfinder.services.scheduler import FollowUpScheduler
from tests.unit import test_scheduler_shakemap as fixtures
from tests.unit.test_shakemap_client import acknowledgement, detail, row


class PlaybackScheduleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tracker = EventTracker(str(Path(self.directory.name) / "playback.sqlite"))
        self.addCleanup(self.tracker.close)
        self.now = datetime(2032, 1, 2, 3, 4, tzinfo=timezone.utc)

    def rows(self):
        return self.tracker._db.conn.execute(
            "SELECT event_id, current_delay_time, next_query_time, origin_time, "
            "emsc_alert_json FROM event_tracker ORDER BY event_id, current_delay_time"
        ).fetchall()

    def test_default_registers_one_immediate_step_per_unique_arbitrary_id(self):
        playback.register_playback(
            self.tracker, ["custom", "other", "custom"], RRSMQueryPolicy(),
            registered_at=self.now,
        )
        self.assertEqual(self.rows(), [
            ("custom", 0, self.now.isoformat(timespec="seconds"), None, None),
            ("other", 0, self.now.isoformat(timespec="seconds"), None, None),
        ])
        self.assertEqual(self.tracker.status_counts(), {"pending": 2})

    def test_full_schedule_keeps_normal_delays_and_no_fabricated_alert(self):
        playback.register_playback(
            self.tracker, ["custom"], RRSMQueryPolicy(),
            full_schedule=True, registered_at=self.now,
        )
        rows = self.rows()
        self.assertEqual(
            [item[1] for item in rows], [0, 5, 15, 60, 180, 360, 1440, 2880],
        )
        for _event, delay, due, origin, alert in rows:
            self.assertEqual(due, (self.now + timedelta(minutes=delay)).isoformat(timespec="seconds"))
            self.assertIsNone(origin)
            self.assertIsNone(alert)

    def test_fast_spaces_each_events_steps_but_keeps_nominal_identity(self):
        playback.register_playback(
            self.tracker, ["a", "b"], RRSMQueryPolicy(),
            full_schedule=True, fast=True, registered_at=self.now,
        )
        for event_id in ("a", "b"):
            rows = [item for item in self.rows() if item[0] == event_id]
            self.assertEqual(
                [item[1] for item in rows], [0, 5, 15, 60, 180, 360, 1440, 2880],
            )
            self.assertEqual([item[2] for item in rows], [
                (self.now + timedelta(minutes=offset)).isoformat(timespec="seconds")
                for offset in (0, 2, 4, 6, 8, 10, 12, 14)
            ])

    def test_one_failed_insert_does_not_skip_remaining_events_or_stages(self):
        original = self.tracker.register_new_schedule
        def insert(**values):
            if values["event_id"] == "a" and values["current_delay_time"] == 5:
                raise OSError("one stage could not be saved")
            return original(**values)
        with mock.patch.object(self.tracker, "register_new_schedule", side_effect=insert):
            with self.assertRaises(playback.ScheduleRegistrationError):
                playback.register_playback(self.tracker, ["a", "b"], RRSMQueryPolicy(),
                                           full_schedule=True, registered_at=self.now)
        self.assertEqual(len(self.rows()), 15)
        self.assertEqual(sum(row[0] == "b" for row in self.rows()), 8)

    def test_fast_single_step_remains_immediate(self):
        playback.register_playback(
            self.tracker, ["a"], RRSMQueryPolicy(),
            fast=True, registered_at=self.now,
        )
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][2], self.now.isoformat(timespec="seconds"))


class PlaybackCompletionTests(unittest.TestCase):
    def scheduler(self, counts, *, unresolved=(), links=(), states=("SUPPRESSED",)):
        scheduler = object.__new__(FollowUpScheduler)
        scheduler.logger = logging.getLogger("test.playback.completion")
        scheduler.run_once = mock.Mock()
        scheduler._monitor_future = None
        scheduler._future_condition = threading.Condition()
        scheduler._submitted_futures = {}
        scheduler._futures_being_observed = set()
        scheduler.tracker = mock.Mock()
        scheduler.tracker.status_counts.return_value = counts
        scheduler.shakemap_workflow = mock.Mock()
        scheduler.shakemap_workflow.unresolved.return_value = list(unresolved)
        scheduler.shakemap_workflow.pending_scheduled_attempts.return_value = list(links)
        scheduler.shakemap_notifier = mock.Mock()
        scheduler.shakemap_notifier.delivery_states.return_value = list(states)
        return scheduler

    def test_completed_chain_with_explicitly_suppressed_mail_succeeds(self):
        scheduler = self.scheduler({"completed": 1})
        self.assertEqual(scheduler.run_until_complete(shutdown_event=threading.Event()), 0)

    def test_future_steps_do_not_look_complete_while_workers_are_idle(self):
        scheduler = self.scheduler({"pending": 7, "completed": 1})
        stop = threading.Event()
        with mock.patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            self.assertEqual(scheduler.run_until_complete(shutdown_event=stop), 130)
        scheduler.shakemap_notifier.delivery_states.assert_not_called()

    def test_accepted_native_job_is_observed_until_terminal(self):
        scheduler = self.scheduler({"processing": 1}, unresolved=[{
            "submission_state": "ACCEPTED", "last_error": None,
        }])
        stop = threading.Event()
        with mock.patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            self.assertEqual(scheduler.run_until_complete(shutdown_event=stop), 130)

    def test_ambiguous_acceptance_and_evidence_errors_exit_without_replay(self):
        for state, error in (
            ("UNCERTAIN", None), ("SUBMITTING", None),
            ("ACCEPTED", "evidence failed"),
        ):
            with self.subTest(state=state, error=error):
                scheduler = self.scheduler({"processing": 1}, unresolved=[{
                    "submission_state": state, "last_error": error,
                }])
                self.assertEqual(scheduler.run_until_complete(shutdown_event=threading.Event()), 1)
                scheduler.shakemap_workflow.submit.assert_not_called()

    def test_failed_or_stranded_local_rows_are_not_success(self):
        for counts in ({"failed": 1}, {"processing": 1}, {}, {"unexpected": 1}):
            with self.subTest(counts=counts):
                scheduler = self.scheduler(counts)
                self.assertEqual(scheduler.run_until_complete(shutdown_event=threading.Event()), 1)

    def test_unsuccessful_mail_does_not_change_scientific_result(self):
        for state in ("UNKNOWN", "PARTIAL", "FAILED", "BLOCKED"):
            with self.subTest(state=state):
                scheduler = self.scheduler({"completed": 1}, states=[state])
                self.assertEqual(scheduler.run_until_complete(shutdown_event=threading.Event()), 1)
                scheduler.tracker.mark_failed.assert_not_called()

    def test_mail_enqueued_after_monitor_pass_is_drained_before_exit(self):
        scheduler = self.scheduler({"completed": 1})
        scheduler.shakemap_notifier.delivery_states.side_effect = [["PENDING"], ["SENT"]]
        stop = threading.Event()
        with mock.patch.object(stop, "wait", return_value=False):
            self.assertEqual(scheduler.run_until_complete(shutdown_event=stop), 0)
        self.assertEqual(scheduler.run_once.call_count, 2)

    def test_monitor_failure_is_observed(self):
        scheduler = self.scheduler({"completed": 1})
        scheduler._monitor_future = Future()
        scheduler._monitor_future.set_exception(RuntimeError("bounded observation failed"))
        self.assertEqual(scheduler.run_until_complete(shutdown_event=threading.Event()), 1)


class PlaybackProviderSchedulerTests(unittest.TestCase):
    """Exercise the real scheduler/ledger/export with fake acquisition and HTTP."""

    def test_provider_entry_retains_nominal_identity_and_requires_native_completion(self):
        fixture = fixtures.SchedulerShakeMapTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        calculation_id = "event-1_t00005"
        scheduler, transport = fixture.scheduler(
            (202, acknowledgement(event_id=calculation_id)),
            (200, detail(row(status="SUCCESS"), event_id=calculation_id)),
        )
        scheduler.provider_backed = True

        class ProviderManager(fixtures.Manager):
            def for_on_demand(self, **arguments):
                self.metadata = arguments["metadata"]
                self.event_context = EventContext(
                    "event-1", 46, 7, 5.5, 8, "2026-08-06T09:55:00Z",
                )
                self.entry_kind = self.ON_DEMAND
                self.arguments = arguments
                return self

            def for_alert_context(self, **arguments):
                raise AssertionError("playback must not fabricate or require an alert")

        manager = ProviderManager()
        scheduler._finder_manager_class = manager
        scheduler.tracker.register_new_schedule(
            event_id="event-1", service="RRSM", origin_time=None,
            last_update_time="2026-01-01T00:00:00Z", current_delay_time=5,
            next_query_time="2000-01-01T00:00:00+00:00",
        )
        scheduler.run_once()
        self.assertEqual(scheduler.tracker.status_counts(), {"processing": 1})
        self.assertEqual(manager.metadata["current_delay"], 5)
        self.assertNotIn("event_context", manager.arguments)
        self.assertEqual(manager.prepare_shakemap(manager.solution)[0], calculation_id)
        self.assertEqual(scheduler.run_until_complete(shutdown_event=threading.Event()), 0)
        self.assertEqual(manager.calls, 1)
        self.assertEqual([request[0] for request in transport.calls], ["POST", "GET"])
