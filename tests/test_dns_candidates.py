"""Large public DNS answers retain full validation and bounded connections."""
import socket
import unittest
from unittest.mock import patch

from wechat_dot_bridge import net_transport as net


def answers(count=18):
    result = []
    for index in range(count):
        if index % 2:
            endpoint = (f"2606:4700::{index + 1:x}", 443, 0, 0)
            family = socket.AF_INET6
        else:
            endpoint = (f"8.8.4.{index + 1}", 443)
            family = socket.AF_INET
        result.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", endpoint))
    return result


class DNSCandidateTests(unittest.TestCase):
    def test_system_answer_larger_than_connection_budget_succeeds(self):
        entries = answers()
        with patch.object(net.socket, "getaddrinfo", return_value=entries) as resolver, \
                patch.object(net.socket, "socket", side_effect=AssertionError("no network")):
            selected = net.resolve_addresses("cdn.example.com")
        resolver.assert_called_once_with("cdn.example.com", 443,
                                         type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        self.assertEqual(len(selected), net.MAX_CONNECTION_CANDIDATES)
        self.assertEqual([row[3][0] for row in selected], [row[4][0] for row in entries[:16]])
        self.assertEqual(net.checked_addresses(selected), selected)

    def test_late_unsafe_address_is_rejected_before_any_connection(self):
        unsafe = [(socket.AF_INET, ("10.0.0.1", 443)),
                  (socket.AF_INET6, ("::1", 443, 0, 0)),
                  (socket.AF_INET6, ("::ffff:8.8.8.8", 443, 0, 0)),
                  (socket.AF_INET6, ("2002:0808:0808::1", 443, 0, 0)),
                  (socket.AF_INET6, ("2001:0:4136:e378:8000:63bf:3fff:fdd2", 443, 0, 0))]
        for family, endpoint in unsafe:
            entries = answers() + [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", endpoint)]
            with self.subTest(family=family, address=endpoint[0]), \
                    patch.object(net.socket, "getaddrinfo", return_value=entries), \
                    patch.object(net.socket, "socket") as connection:
                with self.assertRaisesRegex(net.NetworkFailure, "^dns_blocked$"):
                    net.resolve_addresses("cdn.example.com")
                connection.assert_not_called()

    def test_late_malformed_endpoint_is_not_hidden_by_candidate_limit(self):
        malformed = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80)),
                     (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
                     (socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", ("8.8.8.8", 443))]
        for entry in malformed:
            with self.subTest(entry=entry), self.assertRaisesRegex(net.NetworkFailure, "^dns_failed$"):
                net.checked_addresses(answers() + [entry])

    def test_duplicates_are_normalized_before_candidate_selection(self):
        entries = answers()
        selected = net.checked_addresses([entries[0]] * 20 + entries)
        self.assertEqual(selected, net.checked_addresses(entries))
        self.assertEqual(len(selected), net.MAX_CONNECTION_CANDIDATES)

    def test_answer_size_has_a_separate_hard_resource_bound(self):
        public = answers(1)[0]
        self.assertEqual(len(net.checked_addresses([public] * net.MAX_DNS_ENTRIES)), 1)
        with self.assertRaisesRegex(net.NetworkFailure, "^dns_failed$"):
            net.checked_addresses([public] * (net.MAX_DNS_ENTRIES + 1))
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
        with self.assertRaisesRegex(net.NetworkFailure, "^dns_blocked$"):
            net.checked_addresses([public] * (net.MAX_DNS_ENTRIES - 1) + [private])


if __name__ == "__main__":
    unittest.main()
