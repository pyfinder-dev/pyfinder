"""Offline tests for cross-source normalized observation merging."""

import unittest
from unittest.mock import Mock

from pyfinder.pyfinderconfig import (
    EMSC_FELT_REPORT_SERVICE,
    ESM_SHAKEMAP_SERVICE,
    RRSM_PEAK_MOTION_SERVICE,
)
from pyfinder.utils.station_merger import StationMerger


def measurement(
    *,
    source="ESM",
    network="NW",
    station="STA",
    location="",
    channel="HHE",
    latitude=45.0,
    longitude=12.0,
    pga=10.0,
    timestamp=1000.0,
    provider_value=1.0,
    provider_unit="%g",
):
    """Build one complete normalized record with controlled field values."""
    return {
        "latitude": latitude,
        "longitude": longitude,
        "network": network,
        "station": station,
        "location": location,
        "channel": channel,
        "pga": pga,
        "timestamp": timestamp,
        "source": source,
        "provider_value": provider_value,
        "provider_unit": provider_unit,
    }


class StationMergerTests(unittest.TestCase):

    priority = [
        ESM_SHAKEMAP_SERVICE,
        RRSM_PEAK_MOTION_SERVICE,
        EMSC_FELT_REPORT_SERVICE,
    ]

    def merge(self, available_results, *, priority=None, logger=None):
        logger = logger or Mock()
        merger = StationMerger(
            service_priority=self.priority if priority is None else priority,
            logger=logger,
        )
        return merger.merge(available_results), logger

    def test_empty_and_all_empty_mappings_return_empty_lists(self):
        merged, logger = self.merge({})
        self.assertEqual(merged, [])
        logger.warning.assert_not_called()

        merged, logger = self.merge({
            ESM_SHAKEMAP_SERVICE: [],
            RRSM_PEAK_MOTION_SERVICE: [],
            EMSC_FELT_REPORT_SERVICE: [],
        })
        self.assertEqual(merged, [])
        logger.warning.assert_not_called()

    def test_present_mapping_keys_define_membership_and_empty_is_retained(self):
        esm = measurement(station="ESM")
        rrsm = measurement(source="RRSM", station="RRSM")
        available_results = {
            RRSM_PEAK_MOTION_SERVICE: [rrsm],
            ESM_SHAKEMAP_SERVICE: [esm],
        }

        merged, _logger = self.merge(available_results)

        self.assertEqual(merged, [esm, rrsm])
        self.assertNotIn(EMSC_FELT_REPORT_SERVICE, available_results)

        merged, _logger = self.merge({
            ESM_SHAKEMAP_SERVICE: [esm],
            RRSM_PEAK_MOTION_SERVICE: [],
        })
        self.assertEqual(merged, [esm])

    def test_changed_priority_reverses_instrumental_winner(self):
        esm = measurement(source="ESM", pga=1.0)
        rrsm = measurement(
            source="RRSM",
            pga=100.0,
            provider_value=100.0,
            provider_unit="cm/s^2",
        )
        available_results = {
            ESM_SHAKEMAP_SERVICE: [esm],
            RRSM_PEAK_MOTION_SERVICE: [rrsm],
        }

        merged, _logger = self.merge(available_results)
        reversed_merged, _logger = self.merge(
            available_results,
            priority=[RRSM_PEAK_MOTION_SERVICE, ESM_SHAKEMAP_SERVICE],
        )

        self.assertEqual(merged, [esm])
        self.assertIs(merged[0], esm)
        self.assertEqual(reversed_merged, [rrsm])
        self.assertIs(reversed_merged[0], rrsm)

    def test_empty_location_identity_keeps_complete_higher_priority_object(self):
        esm = measurement(
            source="ESM",
            location="",
            latitude=45.1,
            longitude=12.1,
            pga=9.5,
            timestamp=1234.5,
            provider_value=0.97,
            provider_unit="%g",
        )
        esm_before = dict(esm)
        rrsm = measurement(
            source="RRSM",
            location="",
            latitude=46.2,
            longitude=13.2,
            pga=500.0,
            timestamp=9999.0,
            provider_value=500.0,
            provider_unit="cm/s^2",
        )

        merged, logger = self.merge({
            ESM_SHAKEMAP_SERVICE: [esm],
            RRSM_PEAK_MOTION_SERVICE: [rrsm],
        })

        self.assertEqual(merged, [esm])
        self.assertIs(merged[0], esm)
        self.assertEqual(esm, esm_before)
        self.assertTrue(any(
            "lower-priority instrumental observation" in call.args[0]
            and RRSM_PEAK_MOTION_SERVICE in call.args[1:]
            and ESM_SHAKEMAP_SERVICE in call.args[1:]
            for call in logger.warning.call_args_list
        ))

    def test_lower_priority_identity_survives_when_higher_source_lacks_it(self):
        higher_other = measurement(station="OTHER")
        lower = measurement(source="RRSM", station="TARGET")

        merged, _logger = self.merge({
            ESM_SHAKEMAP_SERVICE: [higher_other],
            RRSM_PEAK_MOTION_SERVICE: [lower],
        })

        self.assertEqual(merged, [higher_other, lower])
        self.assertIs(merged[1], lower)

    def test_same_service_duplicate_warns_and_keeps_first_record(self):
        first = measurement(source="RRSM", pga=1.0)
        second = measurement(source="RRSM", pga=100.0)
        identity = ("NW", "STA", "", "HHE")

        merged, logger = self.merge({
            RRSM_PEAK_MOTION_SERVICE: [first, second],
        })

        self.assertEqual(merged, [first])
        self.assertIs(merged[0], first)
        logger.warning.assert_called_once()
        warning = logger.warning.call_args
        self.assertIn("duplicate instrumental observation", warning.args[0])
        self.assertIn("used as its representative", warning.args[0])
        self.assertNotIn("retained", warning.args[0].lower())
        self.assertNotIn("surviv", warning.args[0].lower())
        self.assertEqual(warning.args[1:], (
            RRSM_PEAK_MOTION_SERVICE,
            identity,
        ))

    def test_same_service_duplicate_is_warned_after_global_loss(self):
        higher = measurement(source="ESM", pga=1.0)
        lower_first = measurement(source="RRSM", pga=50.0)
        lower_second = measurement(source="RRSM", pga=100.0)
        identity = ("NW", "STA", "", "HHE")

        merged, logger = self.merge({
            ESM_SHAKEMAP_SERVICE: [higher],
            RRSM_PEAK_MOTION_SERVICE: [lower_first, lower_second],
        })

        self.assertEqual(merged, [higher])
        self.assertIs(merged[0], higher)
        self.assertNotIn(lower_first, merged)
        self.assertNotIn(lower_second, merged)
        self.assertEqual(logger.warning.call_count, 2)

        cross_service = logger.warning.call_args_list[0]
        self.assertIn(
            "lower-priority instrumental observation",
            cross_service.args[0],
        )
        self.assertEqual(cross_service.args[1:], (
            RRSM_PEAK_MOTION_SERVICE,
            identity,
            ESM_SHAKEMAP_SERVICE,
        ))

        same_service = logger.warning.call_args_list[1]
        self.assertIn("duplicate instrumental observation", same_service.args[0])
        self.assertIn("used as its representative", same_service.args[0])
        self.assertNotIn("retained", same_service.args[0].lower())
        self.assertNotIn("surviv", same_service.args[0].lower())
        self.assertEqual(same_service.args[1:], (
            RRSM_PEAK_MOTION_SERVICE,
            identity,
        ))

    def test_output_is_not_selected_or_sorted_by_pga(self):
        esm_first = measurement(station="FIRST", pga=1.0)
        esm_second = measurement(station="SECOND", pga=50.0)
        rrsm_duplicate = measurement(
            source="RRSM",
            station="FIRST",
            pga=1000.0,
        )
        rrsm_unique = measurement(
            source="RRSM",
            station="THIRD",
            pga=500.0,
        )

        merged, _logger = self.merge({
            RRSM_PEAK_MOTION_SERVICE: [rrsm_duplicate, rrsm_unique],
            ESM_SHAKEMAP_SERVICE: [esm_first, esm_second],
        })

        self.assertEqual(merged, [esm_first, esm_second, rrsm_unique])
        self.assertEqual([record["pga"] for record in merged], [1.0, 50.0, 500.0])

    def test_missing_identity_fields_remain_independent(self):
        for field_name in ("network", "station", "channel"):
            with self.subTest(field=field_name):
                first = measurement(source="ESM", pga=1.0)
                second = measurement(source="ESM", pga=2.0)
                first[field_name] = ""
                second[field_name] = ""

                merged, logger = self.merge({
                    ESM_SHAKEMAP_SERVICE: [first, second],
                })

                self.assertEqual(merged, [first, second])
                self.assertIs(merged[0], first)
                self.assertIs(merged[1], second)
                logger.warning.assert_not_called()

    def test_coordinates_never_replace_exact_instrumental_identity(self):
        esm = measurement(
            source="ESM",
            network="N1",
            station="ONE",
            latitude=45.123456,
            longitude=12.123456,
        )
        rrsm = measurement(
            source="RRSM",
            network="N2",
            station="TWO",
            latitude=45.123456,
            longitude=12.123456,
        )

        merged, logger = self.merge({
            ESM_SHAKEMAP_SERVICE: [esm],
            RRSM_PEAK_MOTION_SERVICE: [rrsm],
        })

        self.assertEqual(merged, [esm, rrsm])
        logger.warning.assert_not_called()

    def test_colocated_felt_records_and_instrument_remain_independent(self):
        instrument = measurement(
            source="ESM",
            latitude=46.0,
            longitude=7.0,
        )
        felt_first = measurement(
            source="EMSC",
            network="",
            station="",
            location="",
            channel="",
            latitude=46.0,
            longitude=7.0,
            pga=2.0,
            provider_value=5.0,
            provider_unit="EMS-98",
        )
        felt_second = measurement(
            source="EMSC",
            network="",
            station="",
            location="",
            channel="",
            latitude=46.0,
            longitude=7.0,
            pga=3.0,
            provider_value=6.0,
            provider_unit="EMS-98",
        )

        merged, logger = self.merge({
            EMSC_FELT_REPORT_SERVICE: [felt_first, felt_second],
            ESM_SHAKEMAP_SERVICE: [instrument],
        })

        self.assertEqual(merged, [instrument, felt_first, felt_second])
        self.assertIs(merged[1], felt_first)
        self.assertIs(merged[2], felt_second)
        logger.warning.assert_not_called()

    def test_mixed_mapping_preserves_priority_provider_and_object_order(self):
        esm_first = measurement(source="ESM", station="E1", pga=100.0)
        esm_second = measurement(source="ESM", station="E2", pga=1.0)
        rrsm = measurement(source="RRSM", station="R1", pga=50.0)
        felt = measurement(
            source="EMSC",
            network="",
            station="",
            channel="",
            pga=500.0,
            provider_unit="EMS-98",
        )
        available_results = {
            EMSC_FELT_REPORT_SERVICE: [felt],
            ESM_SHAKEMAP_SERVICE: [esm_first, esm_second],
            RRSM_PEAK_MOTION_SERVICE: [rrsm],
        }
        priority = [
            RRSM_PEAK_MOTION_SERVICE,
            ESM_SHAKEMAP_SERVICE,
            EMSC_FELT_REPORT_SERVICE,
        ]

        merged, logger = self.merge(
            available_results,
            priority=priority,
        )

        expected = [rrsm, esm_first, esm_second, felt]
        self.assertEqual(merged, expected)
        for actual, original in zip(merged, expected):
            self.assertIs(actual, original)
        logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
