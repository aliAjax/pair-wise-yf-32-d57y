import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class OrganFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, expires_days=2):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East", "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=expires_days)), "clinical_match": 8})

    def candidate(self, name="患者甲", hospital="H2", urgency=5, wait=500):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital, "region": "East", "urgency": urgency, "wait_days": wait, "willing": True, "clinical_match": 9})

    def test_complete_allocation_and_cold_chain_flow(self):
        donor, candidate = self.donor(), self.candidate()
        rank = self.svc.ranking(donor["id"], "allocation_officer", "")
        self.assertEqual(rank["candidates"][0]["id"], candidate["id"])
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.assertEqual(transit["status"], "in_transit")
        handoff = self.svc.initiate_handoff(allocation["id"], "hospital-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.assertEqual(handoff["handoff"]["status"], "initiated")
        received = self.svc.accept_handoff(allocation["id"], "hospital-h2", "hospital", "H2", {})
        self.assertEqual(received["status"], "handed_off")
        implanted = self.svc.implant(allocation["id"], "allocator", "allocation_officer", {})
        self.assertEqual(implanted["status"], "implanted")
        audit = self.svc.audit(allocation["id"], "auditor")
        self.assertEqual([item["action"] for item in audit], ["allocation_proposed", "allocation_accepted", "transfer_started", "handoff_initiated", "handoff_accepted", "organ_implanted"])

    def test_expiry_privacy_and_single_allocation(self):
        expired = self.donor(expires_days=-1); candidate = self.candidate()
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": expired["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "organ_expired")
        donor2 = self.donor(); allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "wrong", "hospital", "H1", {"expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)
        masked = self.svc.get_allocation(allocation["id"], "hospital", "H1")
        self.assertEqual(masked["patient_name"], "***")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "donor_unavailable")
        other = self.candidate("患者乙", "H2", 4, 300)
        self.assertNotEqual(other["id"], candidate["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 12})
        self.assertEqual(ctx.exception.code, "cold_chain_violation")


class DonorBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def batch_body(self, overrides=None):
        body = {
            "blood_type": "O", "hospital": "H1", "region": "East",
            "available_at": iso(self.now - timedelta(days=3)),
            "organs": [
                {"organ": "kidney", "expires_at": iso(self.now + timedelta(hours=24))},
                {"organ": "liver", "expires_at": iso(self.now + timedelta(hours=12))},
                {"organ": "cornea", "expires_at": iso(self.now + timedelta(days=7))},
            ],
        }
        if overrides: body.update(overrides)
        return body

    def test_batch_registers_shared_info_once_with_per_organ_limits(self):
        result = self.svc.register_donor_batch("coord", "coordinator", self.batch_body())
        self.assertEqual(len(result["donors"]), 3)
        batch_id = result["batch"]["id"]
        by_organ = {d["organ"]: d for d in result["donors"]}
        self.assertEqual([d["organ"] for d in result["donors"]], ["kidney", "liver", "cornea"])
        for organ, donor in by_organ.items():
            self.assertEqual(donor["batch_id"], batch_id)
            self.assertEqual(donor["blood_type"], "O")
            self.assertEqual(donor["hospital"], "H1")
        self.assertLess(by_organ["liver"]["expires_at"], by_organ["kidney"]["expires_at"])
        self.assertGreater(by_organ["cornea"]["expires_at"], by_organ["kidney"]["expires_at"])

    def _assert_empty(self):
        state = self.svc.state("coordinator", "")
        self.assertEqual(state["donors"], [])
        self.assertEqual([b for b in state["batches"] if b.get("id") is not None], [])

    def test_missing_organ_limit_rejects_whole_batch(self):
        body = self.batch_body()
        del body["organs"][1]["expires_at"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("coord", "coordinator", body)
        self.assertEqual(ctx.exception.status, 400); self.assertEqual(ctx.exception.code, "invalid_batch")
        self.assertIn(1, ctx.exception.details["organs"])
        self._assert_empty()

    def test_duplicate_organ_and_missing_shared_field_reject_whole_batch(self):
        body = self.batch_body(); body["organs"][2]["organ"] = "kidney"
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("coord", "coordinator", body)
        self.assertEqual(ctx.exception.details["organs"][2]["organ"], "批次内器官重复")
        self._assert_empty()
        body = self.batch_body(); del body["blood_type"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("coord", "coordinator", body)
        self.assertIn("blood_type", ctx.exception.details["missing_fields"])
        self._assert_empty()

    def test_window_not_after_available_rejects_batch(self):
        body = self.batch_body()
        body["organs"][0]["expires_at"] = iso(self.now - timedelta(days=5))
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("coord", "coordinator", body)
        self.assertIn(0, ctx.exception.details["organs"])
        self._assert_empty()

    def _batch_with_candidates(self):
        batch = self.svc.register_donor_batch("coord", "coordinator", self.batch_body())
        donors = {d["organ"]: d for d in batch["donors"]}

        def cand(name, organ, hospital="H2"):
            return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": organ,
                                                                        "hospital": hospital, "region": "East", "urgency": 5, "wait_days": 100})
        return donors, cand

    def test_same_donor_organs_cannot_go_to_same_patient(self):
        donors, cand = self._batch_with_candidates()
        kidney_patient = cand("患者甲", "kidney"); liver_same = cand("患者甲", "liver"); liver_other = cand("患者乙", "liver")
        kidney_alloc = self.svc.propose("allocator", "allocation_officer", {"donor_id": donors["kidney"]["id"], "candidate_id": kidney_patient["id"]})
        self.assertEqual(kidney_alloc["status"], "proposed")
        # 排序仍按具体器官办理：肝只在肝候选中排序，且兼容者都在
        liver_rank = self.svc.ranking(donors["liver"]["id"], "allocation_officer", "")
        self.assertEqual({c["patient_name"] for c in liver_rank["candidates"]}, {"患者甲", "患者乙"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donors["liver"]["id"], "candidate_id": liver_same["id"]})
        self.assertEqual(ctx.exception.code, "same_recipient")
        # 肝仍待分配，可以分给另一位患者
        liver_alloc = self.svc.propose("allocator", "allocation_officer", {"donor_id": donors["liver"]["id"], "candidate_id": liver_other["id"]})
        self.assertEqual(liver_alloc["status"], "proposed")

    def test_withdraw_or_expiry_of_one_organ_is_scoped_to_that_organ(self):
        # 肾的窗口已结束，肝仍新鲜：肾过期不应拖累肝
        body = self.batch_body()
        body["organs"][0]["expires_at"] = iso(self.now - timedelta(days=1))  # 肾在 3 天前可用、1 天前到期
        batch = self.svc.register_donor_batch("coord", "coordinator", body)
        donors = {d["organ"]: d for d in batch["donors"]}
        kidney_patient = self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者甲", "blood_type": "B", "organ": "kidney",
                                                                              "hospital": "H2", "region": "East", "urgency": 5, "wait_days": 100})
        liver_patient = self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者乙", "blood_type": "B", "organ": "liver",
                                                                             "hospital": "H2", "region": "East", "urgency": 4, "wait_days": 80})
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donors["kidney"]["id"], "candidate_id": kidney_patient["id"]})
        self.assertEqual(ctx.exception.code, "organ_expired")
        liver_alloc = self.svc.propose("allocator", "allocation_officer", {"donor_id": donors["liver"]["id"], "candidate_id": liver_patient["id"]})
        # 撤回肝：肝回到待分配，角膜仍待分配，肾保持过期
        self.svc.withdraw(liver_alloc["id"], "hospital-h2", "hospital", "H2", {"reason": "患者临时状况"})
        view = self.svc.get_batch(batch["batch"]["id"], "coordinator")
        stages = {item["organ"]: item["stage"] for item in view["organs"]}
        self.assertEqual(stages["kidney"], "expired")
        self.assertEqual(stages["liver"], "pending")
        self.assertEqual(stages["cornea"], "pending")
        self.assertEqual((view["allocated_count"], view["pending_count"], view["expired_count"]), (0, 2, 1))
        # 撤回只影响肝这一件：肝、角膜的原始器官记录仍可独立分配
        state = self.svc.state("coordinator", "")
        raw = {d["organ"]: d["status"] for d in state["donors"]}
        self.assertEqual(raw["liver"], "available")
        self.assertEqual(raw["cornea"], "available")

    def test_coordinator_console_groups_batch_allocated_and_pending(self):
        donors, cand = self._batch_with_candidates()
        kidney_patient = cand("患者甲", "kidney")
        self.svc.propose("allocator", "allocation_officer", {"donor_id": donors["kidney"]["id"], "candidate_id": kidney_patient["id"]})
        view = self.svc.get_batch(donors["kidney"]["batch_id"], "coordinator")
        self.assertEqual(view["organ_count"], 3); self.assertEqual(view["allocated_count"], 1); self.assertEqual(view["pending_count"], 2)
        stages = {item["organ"]: item["stage"] for item in view["organs"]}
        self.assertEqual(stages, {"kidney": "allocated", "liver": "pending", "cornea": "pending"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.batches("hospital")
        self.assertEqual(ctx.exception.status, 403)

    def test_legacy_single_organ_records_still_listed(self):
        donor = self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "heart", "hospital": "H1", "region": "East",
                                                                 "available_at": iso(self.now - timedelta(days=1)),
                                                                 "expires_at": iso(self.now + timedelta(days=1))})
        self.assertIsNone(donor["batch_id"])
        batches = self.svc.batches("auditor")
        legacy = [b for b in batches if b.get("legacy_donor_id") == donor["id"]]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["organ_count"], 1); self.assertEqual(legacy[0]["pending_count"], 1)
        self.assertEqual(legacy[0]["organs"][0]["organ"], "heart")


if __name__ == "__main__": unittest.main()
