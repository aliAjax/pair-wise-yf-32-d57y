import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class DonorBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db")
        self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def batch_body(self, **overrides):
        body = {
            "blood_type": "O", "hospital": "H1", "region": "East",
            "available_at": iso(self.now - timedelta(hours=1)),
            "organs": [
                {"organ": "kidney", "expires_at": iso(self.now + timedelta(hours=24))},
                {"organ": "liver", "expires_at": iso(self.now + timedelta(hours=8))},
                {"organ": "cornea", "expires_at": iso(self.now + timedelta(days=7))},
            ],
        }
        body.update(overrides)
        return body

    def register_batch(self, **overrides):
        return self.svc.register_donor_batch("coord", "coordinator", self.batch_body(**overrides))

    def candidate(self, name, hospital, organ, urgency=4, blood="O"):
        return self.svc.register_candidate("coord", "coordinator", {
            "patient_name": name, "blood_type": blood, "organ": organ, "hospital": hospital,
            "region": "East", "urgency": urgency, "wait_days": 100,
        })

    def donor_id(self, batch, organ):
        return next(o["id"] for o in batch["organs"]["allocated"] + batch["organs"]["pending"] + batch["organs"]["expired"] if o["organ"] == organ)

    def test_batch_registers_each_organ_with_own_window(self):
        batch = self.register_batch()
        self.assertEqual(batch["summary"], {"allocated": 0, "pending": 3, "expired": 0})
        organs = {o["organ"]: o for group in batch["organs"].values() for o in group}
        self.assertEqual({o["blood_type"] for o in self.svc.state("coordinator", "")["donors"]}, {"O"})
        self.assertTrue(organs["liver"]["expires_at"] < organs["kidney"]["expires_at"] < organs["cornea"]["expires_at"])
        self.assertTrue(all(o["batch_id"] == batch["id"] for o in organs.values()))
        stored = self.svc.get_batch(batch["id"], "auditor")
        self.assertEqual(stored["hospital"], "H1")

    def test_missing_fields_abort_whole_batch(self):
        with self.assertRaises(ApiError) as ctx:
            self.register_batch(hospital="")
        self.assertEqual(ctx.exception.code, "missing_fields")
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("coord", "coordinator", self.batch_body(organs=[]))
        self.assertEqual(ctx.exception.code, "missing_fields")
        bad = self.batch_body()
        del bad["organs"][1]["expires_at"]
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("coord", "coordinator", bad)
        self.assertEqual(ctx.exception.code, "expires_required")
        self.assertEqual(self.svc.list_batches("coordinator")["batches"], [])
        self.assertEqual(self.svc.state("coordinator", "")["donors"], [])

    def test_duplicate_organ_aborts_whole_batch(self):
        body = self.batch_body()
        body["organs"][2]["organ"] = "kidney"
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("coord", "coordinator", body)
        self.assertEqual(ctx.exception.code, "duplicate_organ")
        self.assertEqual(self.svc.list_batches("coordinator")["batches"], [])

    def test_invalid_window_aborts_whole_batch(self):
        body = self.batch_body()
        body["organs"][0]["expires_at"] = iso(self.now - timedelta(days=1))
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("coord", "coordinator", body)
        self.assertEqual(ctx.exception.code, "invalid_window")
        self.assertEqual(self.svc.state("coordinator", "")["donors"], [])

    def test_ranking_and_allocation_stay_per_organ(self):
        batch = self.register_batch()
        kidney_id, liver_id = self.donor_id(batch, "kidney"), self.donor_id(batch, "liver")
        ck = self.candidate("肾病患者", "H2", "kidney")
        cl = self.candidate("肝病患者", "H3", "liver")
        kidney_rank = self.svc.ranking(kidney_id, "allocation_officer", "")
        liver_rank = self.svc.ranking(liver_id, "allocation_officer", "")
        self.assertEqual([c["id"] for c in kidney_rank["candidates"]], [ck["id"]])
        self.assertEqual([c["id"] for c in liver_rank["candidates"]], [cl["id"]])
        self.svc.propose("allocator", "allocation_officer", {"donor_id": kidney_id, "candidate_id": ck["id"]})
        self.svc.propose("allocator", "allocation_officer", {"donor_id": liver_id, "candidate_id": cl["id"]})
        view = self.svc.get_batch(batch["id"], "coordinator")
        self.assertEqual(view["summary"], {"allocated": 2, "pending": 1, "expired": 0})
        allocated = {o["organ"]: o for o in view["organs"]["allocated"]}
        self.assertEqual(allocated["kidney"]["patient_name"], "肾病患者")
        self.assertEqual(allocated["liver"]["patient_name"], "肝病患者")

    def test_same_donor_organs_cannot_go_to_same_patient(self):
        batch = self.register_batch()
        kidney_id, liver_id = self.donor_id(batch, "kidney"), self.donor_id(batch, "liver")
        same_kidney = self.candidate("同一患者", "H2", "kidney")
        same_liver = self.candidate("同一患者", "H2", "liver")
        other_liver = self.candidate("另一患者", "H3", "liver")
        self.svc.propose("allocator", "allocation_officer", {"donor_id": kidney_id, "candidate_id": same_kidney["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": liver_id, "candidate_id": same_liver["id"]})
        self.assertEqual(ctx.exception.code, "same_patient_conflict")
        self.svc.propose("allocator", "allocation_officer", {"donor_id": liver_id, "candidate_id": other_liver["id"]})

    def test_withdraw_or_expiry_only_affects_that_organ(self):
        batch = self.register_batch()
        kidney_id, liver_id, cornea_id = (self.donor_id(batch, o) for o in ("kidney", "liver", "cornea"))
        ck = self.candidate("肾病患者", "H2", "kidney")
        cl = self.candidate("肝病患者", "H3", "liver")
        kidney_alloc = self.svc.propose("allocator", "allocation_officer", {"donor_id": kidney_id, "candidate_id": ck["id"]})
        self.svc.propose("allocator", "allocation_officer", {"donor_id": liver_id, "candidate_id": cl["id"]})
        self.svc.withdraw(kidney_alloc["id"], "hospital-h2", "hospital", "H2", {"reason": "患者临时状况"})
        # 角膜已过期：只影响角膜一件，肝肾不受影响
        self.svc.repo.conn.execute("UPDATE donors SET expires_at=? WHERE id=?", (iso(self.now - timedelta(minutes=1)), cornea_id))
        cc = self.candidate("角膜患者", "H4", "cornea")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": cornea_id, "candidate_id": cc["id"]})
        self.assertEqual(ctx.exception.code, "organ_expired")
        view = self.svc.get_batch(batch["id"], "coordinator")
        self.assertEqual(view["summary"], {"allocated": 1, "pending": 1, "expired": 1})
        self.assertEqual([o["organ"] for o in view["organs"]["allocated"]], ["liver"])
        self.assertEqual([o["organ"] for o in view["organs"]["pending"]], ["kidney"])
        self.assertEqual([o["organ"] for o in view["organs"]["expired"]], ["cornea"])
        # 撤回后该件可分给其他患者（同一患者限制只看有效分配）
        other = self.candidate("另一肾病患者", "H4", "kidney")
        self.svc.propose("allocator", "allocation_officer", {"donor_id": kidney_id, "candidate_id": other["id"]})

    def test_legacy_single_organ_records_still_work(self):
        single = self.svc.register_donor("coord", "coordinator", {
            "blood_type": "A", "organ": "kidney", "hospital": "H9", "region": "West",
            "available_at": iso(self.now - timedelta(hours=1)),
            "expires_at": iso(self.now + timedelta(days=1)),
        })
        self.assertIsNone(single["batch_id"])
        listing = self.svc.list_batches("coordinator")
        self.assertEqual([o["id"] for o in listing["single_organs"]], [single["id"]])
        cand = self.candidate("旧流程患者", "H9", "kidney", blood="A")
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": single["id"], "candidate_id": cand["id"]})
        self.assertEqual(allocation["status"], "proposed")

    def test_batch_requires_privileged_role(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_donor_batch("v", "viewer", self.batch_body())
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.list_batches("hospital")
        self.assertEqual(ctx.exception.code, "batch_forbidden")


if __name__ == "__main__": unittest.main()
