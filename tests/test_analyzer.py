import unittest

from main import analyze, compact


class AnalyzerTests(unittest.TestCase):
    def test_time_origin_and_duration_are_normalized(self):
        report = analyze("fixture", [{"t": 12.5, "gyro.x": 1.0}, {"t": 13.0, "gyro.x": 2.0}], ["gyro.x"], {})
        self.assertEqual(report["series"][0]["t"], 0)
        self.assertEqual(report["summary"]["duration_s"], 0.5)

    def test_absent_position_and_rpm_are_not_fabricated(self):
        report = analyze("fixture", [{"t": 0, "motor.command[0]": 0.2}], ["motor.command[0]"], {})
        titles = {d["title"] for d in report["diagnoses"]}
        self.assertIn("GPS position unavailable", titles)
        self.assertIn("Motor RPM unavailable", titles)
        self.assertEqual(report["summary"]["gps_channels"], [])
        self.assertEqual(report["summary"]["motor_rpm_channels"], [])

    def test_channel_coverage_identifies_gaps(self):
        report = analyze("fixture", [{"t": 0, "voltage": 16}, {"t": 1}], ["voltage"], {})
        self.assertEqual(report["quality"]["voltage"], 50)
        self.assertIn("voltage", report["missing_channels"])

    def test_compaction_keeps_last_region_represented(self):
        rows = [{"t": n} for n in range(1001)]
        reduced = compact(rows, 100)
        self.assertLessEqual(len(reduced), 100)
        self.assertEqual(reduced[0]["t"], 0)
        self.assertGreater(reduced[-1]["t"], 900)

    def test_ardupilot_scaled_coordinates_convert_to_degrees_and_preserve_source(self):
        row = {"t": 0, "GPS.Lat": 377749000, "GPS.Lng": -1224194000}
        report = analyze("ArduPilot DataFlash BIN", [row], list(row)[1:], {})
        point = report["position"][0]
        self.assertAlmostEqual(point["lat"], 37.7749)
        self.assertAlmostEqual(point["lon"], -122.4194)
        self.assertEqual(report["series"][0]["GPS.Lat"], 377749000)

    def test_px4_global_position_radians_convert_to_degrees(self):
        row = {"t": 0, "vehicle_global_position.lat": 0.5, "vehicle_global_position.lon": -1.0}
        report = analyze("PX4 ULog", [row], list(row)[1:], {})
        self.assertAlmostEqual(report["position"][0]["lat"], 28.6478898)
        self.assertAlmostEqual(report["position"][0]["lon"], -57.2957795)

    def test_voltage_range_formula_and_control_evidence(self):
        rows = [
            {"t": 2, "BAT.voltage_v": 16.2, "setpoint.roll": 0.1, "mode": 2},
            {"t": 3, "BAT.voltage_v": 15.7, "setpoint.roll": 0.2, "mode": 3},
        ]
        report = analyze("PX4 ULog", rows, [], {})
        battery = next(d for d in report["diagnoses"] if d["title"] == "Recorded voltage range")
        self.assertIn("range = max − min = 0.5", battery["reason"])
        self.assertTrue(any(d["title"] == "Control reference channels recorded" for d in report["diagnoses"]))
        mode_events = [e for e in report["events"] if e["title"] == "Mode/arming field changed"]
        self.assertEqual(len(mode_events), 1)
        self.assertEqual(mode_events[0]["t"], 1)


if __name__ == "__main__":
    unittest.main()
