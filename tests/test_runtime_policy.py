"""Dependency-free tests for storage reservations and trusted-proxy IP parsing."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from client_address import resolve_client_ip
from storage_policy import StorageBudget, StorageBudgetError


class StorageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.free_bytes = 10_000
        self.budget = StorageBudget(
            self.downloads,
            max_total_bytes=500,
            min_free_bytes=100,
            max_file_bytes=100,
            disk_usage=lambda _: SimpleNamespace(free=self.free_bytes),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_reservation_prevents_concurrent_jobs_overcommitting_quota(self):
        self.budget.reserve("job-a")
        self.budget.reserve("job-b")
        with self.assertRaises(StorageBudgetError):
            self.budget.reserve("job-c")
        self.assertEqual(self.budget.reserved_bytes, 400)

    def test_concurrent_reservations_are_atomic(self):
        barrier = threading.Barrier(3)
        outcomes = []
        outcomes_lock = threading.Lock()

        def reserve(job_id):
            barrier.wait()
            try:
                self.budget.reserve(job_id)
                result = "reserved"
            except StorageBudgetError:
                result = "rejected"
            with outcomes_lock:
                outcomes.append(result)

        workers = [threading.Thread(target=reserve, args=(f"job-{index}",)) for index in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(outcomes.count("reserved"), 2)
        self.assertEqual(outcomes.count("rejected"), 1)
        self.assertEqual(self.budget.reserved_bytes, 400)

    def test_reservation_tracks_existing_job_files_without_double_counting(self):
        self.budget.reserve("job-a")
        job_dir = self.downloads / "job-a"
        job_dir.mkdir()
        (job_dir / "part.mp4.part").write_bytes(b"x" * 50)
        self.budget.refresh("job-a")
        self.assertEqual(self.budget.reserved_bytes, 150)
        total = self.budget._directory_size(self.downloads) + self.budget.reserved_bytes
        self.assertEqual(total, 200)

    def test_reservation_fails_when_minimum_free_disk_would_be_violated(self):
        self.free_bytes = 250
        with self.assertRaises(StorageBudgetError):
            self.budget.reserve("job-a")
        self.assertEqual(self.budget.reserved_bytes, 0)

    def test_refresh_aborts_a_job_that_exceeds_working_set_cap(self):
        self.budget.reserve("job-a")
        job_dir = self.downloads / "job-a"
        job_dir.mkdir()
        (job_dir / "oversized.part").write_bytes(b"x" * 201)
        with self.assertRaisesRegex(StorageBudgetError, "storage cap"):
            self.budget.refresh("job-a")

    def test_release_is_idempotent(self):
        self.budget.reserve("job-a")
        self.budget.release("job-a")
        self.budget.release("job-a")
        self.assertEqual(self.budget.reserved_bytes, 0)


class TrustedProxyAddressTests(unittest.TestCase):
    def test_ignores_forwarded_header_from_untrusted_peer(self):
        result = resolve_client_ip("198.51.100.10", "203.0.113.5", "10.0.0.0/8")
        self.assertEqual(result, "198.51.100.10")

    def test_uses_forwarded_client_only_when_socket_peer_is_trusted(self):
        result = resolve_client_ip("10.0.0.4", "203.0.113.5", "10.0.0.0/8")
        self.assertEqual(result, "203.0.113.5")

    def test_walks_trusted_proxy_chain_from_right_to_left(self):
        result = resolve_client_ip(
            "10.0.0.4", "203.0.113.5, 10.0.0.3", "10.0.0.0/8"
        )
        self.assertEqual(result, "203.0.113.5")

    def test_bad_forwarded_chain_fails_closed_to_the_peer(self):
        result = resolve_client_ip("10.0.0.4", "not-an-ip, 203.0.113.5", "10.0.0.0/8")
        self.assertEqual(result, "10.0.0.4")

    def test_ipv6_addresses_are_normalized(self):
        result = resolve_client_ip("2001:4860:4860::8888", "127.0.0.1", "10.0.0.0/8")
        self.assertEqual(result, "2001:4860:4860::8888")

    def test_ipv4_mapped_ipv6_peer_matches_ipv4_trust_network(self):
        result = resolve_client_ip("::ffff:10.0.0.4", "203.0.113.5", "10.0.0.0/8")
        self.assertEqual(result, "203.0.113.5")

    def test_invalid_proxy_cidr_does_not_enable_forwarded_header_trust(self):
        result = resolve_client_ip("10.0.0.4", "203.0.113.5", "not-a-cidr")
        self.assertEqual(result, "10.0.0.4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
