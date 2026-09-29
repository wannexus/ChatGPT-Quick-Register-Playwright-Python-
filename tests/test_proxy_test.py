from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core import proxy_test


class IpEchoParsingTests(unittest.TestCase):
    def test_parses_ipify_and_ip_api_shapes(self):
        self.assertEqual(proxy_test.parse_ip_echo({"ip": "1.2.3.4"}), "1.2.3.4")
        self.assertEqual(proxy_test.parse_ip_echo({"query": "5.6.7.8"}), "5.6.7.8")
        self.assertEqual(proxy_test.parse_ip_echo({"origin": "9.9.9.9"}), "9.9.9.9")
        self.assertEqual(proxy_test.parse_ip_echo({"ip": "1.2.3.4", "success": True}), "1.2.3.4")

    def test_ignores_junk_and_non_ip_payloads(self):
        self.assertIsNone(proxy_test.parse_ip_echo({}))
        self.assertIsNone(proxy_test.parse_ip_echo({"ip": ""}))
        self.assertIsNone(proxy_test.parse_ip_echo({"ip": "not-an-ip"}))
        self.assertIsNone(proxy_test.parse_ip_echo(None))

    def test_accepts_a_raw_body(self):
        self.assertEqual(proxy_test.parse_ip_echo("203.0.113.7\n"), "203.0.113.7")
        self.assertEqual(proxy_test.parse_ip_echo('{"ip":"203.0.113.7"}'), "203.0.113.7")


class LookupNormalisationTests(unittest.TestCase):
    def test_normalises_the_ip_api_shape(self):
        info = proxy_test.parse_lookup({
            "status": "success",
            "query": "1.2.3.4",
            "country": "United States",
            "regionName": "California",
            "city": "Mountain View",
            "isp": "Comcast Cable Communications, LLC",
            "org": "Comcast",
            "as": "AS7922 Comcast Cable Communications, LLC",
            "hosting": False,
            "proxy": False,
            "mobile": False,
        }, source="ip-api")

        self.assertEqual(info["ip"], "1.2.3.4")
        self.assertEqual(info["isp"], "Comcast Cable Communications, LLC")
        self.assertEqual(info["asn"], "AS7922 Comcast Cable Communications, LLC")
        self.assertFalse(info["hosting"])
        self.assertEqual(info["source"], "ip-api")

    def test_normalises_the_ipwho_is_shape(self):
        info = proxy_test.parse_lookup({
            "ip": "1.2.3.4",
            "success": True,
            "country": "United States",
            "region": "California",
            "city": "Mountain View",
            "connection": {"asn": 7922, "org": "Comcast", "isp": "Comcast Cable", "domain": "comcast.net"},
        }, source="ipwho.is")

        self.assertEqual(info["isp"], "Comcast Cable")
        self.assertEqual(info["asn"], "AS7922 Comcast")
        self.assertIsNone(info["hosting"], "this source has no datacenter flag")

    def test_reports_a_failed_lookup(self):
        info = proxy_test.parse_lookup({"status": "fail", "message": "reserved range"}, source="ip-api")
        self.assertFalse(info["ok"])
        self.assertIn("reserved", info["error"])


class ClassifyIpTests(unittest.TestCase):
    def _classify(self, **info):
        base = {"ip": "1.2.3.4", "isp": "", "org": "", "asn": "", "hosting": None, "proxy": None, "mobile": None}
        base.update(info)
        return proxy_test.classify_ip(base)

    def test_hosting_flag_means_datacenter_not_home_broadband(self):
        verdict = self._classify(isp="DigitalOcean, LLC", asn="AS14061 DigitalOcean, LLC", hosting=True)
        self.assertEqual(verdict["kind"], "datacenter")
        self.assertFalse(verdict["isResidential"])
        self.assertEqual(verdict["confidence"], "high")

    def test_plain_isp_without_hosting_is_home_broadband(self):
        verdict = self._classify(isp="Comcast Cable Communications, LLC", asn="AS7922 Comcast", hosting=False)
        self.assertEqual(verdict["kind"], "residential")
        self.assertTrue(verdict["isResidential"])
        self.assertEqual(verdict["confidence"], "high")

    def test_mobile_networks_are_not_labelled_home_broadband(self):
        verdict = self._classify(isp="T-Mobile USA", hosting=False, mobile=True)
        self.assertEqual(verdict["kind"], "mobile")
        self.assertFalse(verdict["isResidential"])

    def test_falls_back_to_isp_name_heuristics_when_no_flag_exists(self):
        for name in ("DigitalOcean, LLC", "Amazon.com, Inc.", "OVH SAS", "Alibaba Cloud", "Hetzner Online GmbH"):
            verdict = self._classify(isp=name)
            self.assertEqual(verdict["kind"], "datacenter", f"{name} should look like a datacenter")
            self.assertFalse(verdict["isResidential"])
        verdict = self._classify(isp="China Telecom", asn="AS4134 CHINANET-BACKBONE")
        self.assertEqual(verdict["kind"], "residential")
        self.assertEqual(verdict["confidence"], "medium")

    def test_unknown_without_any_evidence(self):
        verdict = self._classify()
        self.assertEqual(verdict["kind"], "unknown")
        self.assertFalse(verdict["isResidential"])
        self.assertEqual(verdict["confidence"], "low")

    def test_known_proxy_flag_is_surfaced_as_a_risk_note(self):
        verdict = self._classify(isp="Comcast", hosting=False, proxy=True)
        self.assertEqual(verdict["kind"], "residential")
        self.assertTrue(verdict["flagged"])
        self.assertTrue(any("代理" in note or "VPN" in note for note in verdict["notes"]))

    def test_every_verdict_explains_itself(self):
        verdict = self._classify(isp="Comcast", hosting=False)
        self.assertTrue(verdict["label"])
        self.assertTrue(verdict["reasons"])


class TestProxyFlowTests(unittest.TestCase):
    def test_reports_the_exit_ip_and_compares_it_with_direct(self):
        def fake_echo(proxy, **_kwargs):
            return "203.0.113.9" if proxy else "198.51.100.5"

        with patch.object(proxy_test, "fetch_exit_ip", side_effect=fake_echo), \
             patch.object(proxy_test, "lookup_ip", return_value=proxy_test.parse_lookup({
                 "status": "success", "query": "203.0.113.9", "isp": "Comcast",
                 "as": "AS7922 Comcast", "hosting": False, "proxy": False, "mobile": False,
             }, source="ip-api")):
            result = proxy_test.test_proxy("http://user:pw@proxy.example:8080")

        self.assertTrue(result["ok"])
        self.assertEqual(result["exitIp"], "203.0.113.9")
        self.assertEqual(result["directIp"], "198.51.100.5")
        self.assertEqual(result["verdict"]["kind"], "residential")
        self.assertTrue(result["verdict"]["isResidential"])
        self.assertIn("Comcast", result["info"]["isp"])

    def test_flags_a_transparent_proxy_that_does_not_change_the_ip(self):
        with patch.object(proxy_test, "fetch_exit_ip", return_value="198.51.100.5"), \
             patch.object(proxy_test, "lookup_ip", return_value=proxy_test.parse_lookup(
                 {"status": "success", "query": "198.51.100.5"}, source="ip-api")):
            result = proxy_test.test_proxy("http://proxy.example:8080")

        self.assertTrue(result["ok"])
        self.assertFalse(result["changedIp"])
        self.assertTrue(any("没有改变" in note or "直连" in note for note in result["notes"]))

    def test_reports_a_dead_proxy_instead_of_raising(self):
        with patch.object(proxy_test, "fetch_exit_ip", side_effect=OSError("connection refused")):
            result = proxy_test.test_proxy("http://127.0.0.1:9")

        self.assertFalse(result["ok"])
        self.assertIn("refused", result["error"])

    def test_requires_a_proxy_url(self):
        result = proxy_test.test_proxy("")
        self.assertFalse(result["ok"])
        self.assertIn("代理", result["error"])

    def test_never_echoes_the_proxy_password(self):
        with patch.object(proxy_test, "fetch_exit_ip", side_effect=OSError("boom")):
            result = proxy_test.test_proxy("http://alice:sup3rsecret@proxy.example:8080")
        self.assertNotIn("sup3rsecret", json.dumps(result, ensure_ascii=False))


class LookupUrlTests(unittest.TestCase):
    def test_lookup_url_targets_the_requested_ip(self):
        url = proxy_test.lookup_url("1.2.3.4")
        self.assertIn("1.2.3.4", url)
        for field in ("hosting", "proxy", "mobile", "isp", "as"):
            self.assertIn(field, url)


if __name__ == "__main__":
    unittest.main()
