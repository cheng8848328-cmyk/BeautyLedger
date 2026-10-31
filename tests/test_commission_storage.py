"""仅使用虚构提成和临时目录，所有存储调用显式传入测试路径。"""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import commission_storage as store


def sample_records():
    return [
        dict(entry_id="A", employee_id="Alice", month="2026-10", commission_cents=1800),
        dict(entry_id="B", employee_id="Bob", month="2026-11", commission_cents=1500),
        dict(entry_id="Z", employee_id="Alice", month="2026-11", commission_cents=0),
        dict(entry_id="RA", employee_id="Alice", month="2026-10",
             commission_cents=-1800, reversal_of_entry_id="A"),
        dict(entry_id="RB", employee_id="Bob", month="2026-12",
             commission_cents=-1500, reversal_of_entry_id="B"),
    ]


class CommissionStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="commission-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "data" / "commissions.json"

    def write_bytes(self, contents):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(contents)

    def assert_invalid(self, records):
        with self.assertRaises(ValueError):
            store.validate_commission_records(records)
        self.write_bytes(b"[]\n")
        with self.assertRaises(ValueError):
            store.save_commission_records(records, self.path)
        self.assertEqual(self.path.read_bytes(), b"[]\n")
        contents = json.dumps(records).encode("utf-8")
        self.write_bytes(contents)
        with self.assertRaises(ValueError):
            store.load_commission_records(self.path)
        self.assertEqual(self.path.read_bytes(), contents)
        self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_missing_file_returns_empty_without_creating(self):
        self.assertEqual(store.load_commission_records(self.path), [])
        self.assertFalse(self.path.parent.exists())

    def test_round_trip_multiple_employees_months_and_reversals(self):
        for index, records in enumerate(([], sample_records(), list(reversed(sample_records())))):
            with self.subTest(records=records):
                self.path = self.path.parent / f"round-trip-{index}.json"
                store.save_commission_records(records, self.path)
                self.assertEqual(store.load_commission_records(str(self.path)), records)
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), records)
                self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_append_and_full_reversal_preserve_history(self):
        from commission import create_full_commission_reversal
        records = sample_records()[:2]
        store.save_commission_records(records[:1], self.path)
        store.save_commission_records(records, self.path)
        records.append(create_full_commission_reversal(records[0], "NEW-R", "2026-12", records))
        store.save_commission_records(records, self.path)
        store.save_commission_records(records, self.path)
        self.assertEqual(store.load_commission_records(self.path), records)

    def test_reject_empty_stale_deleted_replaced_and_reordered_history(self):
        records = sample_records()[:3]
        store.save_commission_records(records, self.path)
        before = self.path.read_bytes()
        candidates = [[], records[:2], records[1:], list(reversed(records)),
                      [dict(records[0], entry_id="replacement")] + records[1:],
                      records[:1] + [dict(records[0], entry_id="new")]]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                store.validate_commission_records(candidate)
                with self.assertRaises(ValueError):
                    store.save_commission_records(candidate, self.path)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_reject_changes_to_valid_historical_fields_and_metadata_types(self):
        original = dict(sample_records()[0], metadata={"flag": True})
        store.save_commission_records([original], self.path)
        before = self.path.read_bytes()
        for changes in ({"commission_cents": 1900}, {"employee_id": "OTHER"},
                        {"month": "2026-11"}, {"metadata": {"flag": 1}},
                        {"metadata": {"flag": 1.0}}):
            with self.subTest(changes=changes):
                candidate = [dict(original, **changes)]
                store.validate_commission_records(candidate)
                with self.assertRaises(ValueError):
                    store.save_commission_records(candidate, self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def test_large_integer_cents_remain_exact(self):
        records = [dict(sample_records()[0], commission_cents=10 ** 100 + 1)]
        store.save_commission_records(records, self.path)
        loaded = store.load_commission_records(self.path)
        self.assertEqual(loaded, records)
        self.assertIs(type(loaded[0]["commission_cents"]), int)

    def test_invalid_structure_fields_types_and_months(self):
        base = sample_records()[0]
        invalid = [None, {}, "text", [None], [[]], [{}]]
        invalid += [[{k: v for k, v in base.items() if k != field}] for field in base]
        for field in ("entry_id", "employee_id"):
            invalid += [[dict(base, **{field: value})] for value in (None, True, 1, "", " ")]
        invalid += [[dict(base, month=value)] for value in
                    (None, True, 202610, "2026-1", "2026-00", "2026-13", "0000-01")]
        invalid += [[dict(base, commission_cents=value)] for value in
                    (True, False, 1.5, "100", None)]
        for records in invalid:
            with self.subTest(records=records):
                self.assert_invalid(records)

    def test_duplicate_entry_id(self):
        self.assert_invalid([sample_records()[0], sample_records()[0].copy()])

    def test_duplicate_reversal(self):
        records = sample_records()
        records.append(dict(records[3], entry_id="RA2", month="2026-12"))
        self.assert_invalid(records)

    def test_missing_original_reference(self):
        self.assert_invalid([sample_records()[3]])

    def test_negative_requires_valid_reference(self):
        original, reversal = sample_records()[0], sample_records()[3]
        missing = reversal.copy()
        del missing["reversal_of_entry_id"]
        self.assert_invalid([original, missing])
        for value in (None, True, 1, [], "", " ", "missing", "RA"):
            with self.subTest(reference=value):
                self.assert_invalid([original, dict(reversal, reversal_of_entry_id=value)])

    def test_reversal_amount_and_employee_must_match(self):
        for changes in ({"commission_cents": -1799}, {"employee_id": "Bob"}):
            with self.subTest(changes=changes):
                self.assert_invalid([sample_records()[0], dict(sample_records()[3], **changes)])

    def test_references_on_nonnegative_records_rejected(self):
        for amount in (0, 1800):
            with self.subTest(amount=amount):
                self.assert_invalid([dict(sample_records()[0], commission_cents=amount,
                                          reversal_of_entry_id="A")])

    def test_cannot_reference_zero_or_another_reversal(self):
        for reference in ("Z", "RA"):
            records = sample_records()
            records.append(dict(entry_id="RX", employee_id="Alice", month="2026-12",
                                commission_cents=-1800, reversal_of_entry_id=reference))
            with self.subTest(reference=reference):
                self.assert_invalid(records)

    def test_corrupt_files_rejected_on_load_and_save(self):
        for contents in (b"", b"{broken", b"\xff\xfe", b"[{}]",
                         b'[{"entry_id":"A","entry_id":"B"}]', b"[NaN]"):
            with self.subTest(contents=contents):
                self.write_bytes(contents)
                with self.assertRaises(ValueError):
                    store.load_commission_records(self.path)
                with self.assertRaises(ValueError):
                    store.save_commission_records(sample_records(), self.path)
                self.assertEqual(self.path.read_bytes(), contents)
                self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_directory_and_permission_errors_propagate(self):
        self.path.mkdir(parents=True)
        with self.assertRaises(OSError):
            store.load_commission_records(self.path)
        with patch.object(Path, "open", side_effect=PermissionError("拒绝读取")):
            with self.assertRaises(PermissionError):
                store.load_commission_records(self.path)
            with self.assertRaises(PermissionError):
                store.save_commission_records([], self.path)

    def test_validation_before_creating_directories(self):
        with self.assertRaises(ValueError):
            store.save_commission_records([{}], self.path)
        self.assertFalse(self.path.parent.exists())

    def test_save_failures_preserve_old_file_and_unrelated_temp(self):
        self.write_bytes(b"[]\n")
        unrelated = self.path.parent / "commissions-existing.tmp"
        unrelated.write_bytes(b"keep")
        for target, attribute in ((store.Path, "mkdir"), (store, "NamedTemporaryFile"),
                                  (store.json, "dump"), (store.os, "fsync"),
                                  (store.os, "replace")):
            with self.subTest(failure=attribute):
                with patch.object(target, attribute, side_effect=OSError("保存失败")):
                    with self.assertRaises(OSError):
                        store.save_commission_records(sample_records(), self.path)
                self.assertEqual(self.path.read_bytes(), b"[]\n")
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [unrelated])

    def test_partial_write_failure_cleans_temp(self):
        self.write_bytes(b"[]")

        def fail_after_write(records, file, **kwargs):
            file.write('[{"partial":')
            raise OSError("写入中断")

        with patch.object(store.json, "dump", side_effect=fail_after_write):
            with self.assertRaises(OSError):
                store.save_commission_records(sample_records(), self.path)
        self.assertEqual(self.path.read_bytes(), b"[]")
        self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_cleanup_failure_does_not_hide_original_error(self):
        self.write_bytes(b"[]")
        with patch.object(store.os, "replace", side_effect=OSError("替换失败")):
            with patch.object(Path, "unlink", side_effect=PermissionError("清理失败")):
                with self.assertRaisesRegex(OSError, "替换失败"):
                    store.save_commission_records(sample_records(), self.path)
        self.assertEqual(self.path.read_bytes(), b"[]")

    def test_failed_first_save_leaves_no_formal_file(self):
        with patch.object(store.os, "replace", side_effect=OSError("替换失败")):
            with self.assertRaises(OSError):
                store.save_commission_records(sample_records(), self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_atomic_replace_uses_same_directory_and_synced_file(self):
        with patch.object(store, "NamedTemporaryFile", wraps=store.NamedTemporaryFile) as create:
            with patch.object(store.os, "replace", wraps=store.os.replace) as replace:
                with patch.object(store.os, "fsync", wraps=store.os.fsync) as sync:
                    store.save_commission_records(sample_records(), self.path)
        self.assertEqual(create.call_args.kwargs["dir"], self.path.parent)
        source, destination = replace.call_args.args
        self.assertEqual(source.parent, self.path.parent)
        self.assertEqual(destination, self.path)
        sync.assert_called_once()

    def test_input_not_modified_and_metadata_preserved(self):
        records = sample_records()
        records[0]["note"] = {"text": ["模拟提成"]}
        before = copy.deepcopy(records)
        store.validate_commission_records(records)
        store.save_commission_records(records, self.path)
        self.assertEqual(store.load_commission_records(self.path), before)
        with patch.object(store.os, "replace", side_effect=OSError("失败")):
            with self.assertRaises(OSError):
                store.save_commission_records(records, self.path)
        self.assertEqual(records, before)

    def test_nonserializable_metadata_rejected_before_io(self):
        for value in (object(), ("tuple",), {1: "integer key"}, {1: "a", "1": "b"}):
            with self.subTest(value=value):
                records = sample_records()
                records[0]["note"] = value
                with self.assertRaises(ValueError):
                    store.save_commission_records(records, self.path)
                self.assertFalse(self.path.parent.exists())


class AuditFieldsTests(unittest.TestCase):
    def test_reject_valid_recalculated_audit_history_and_removed_basis(self):
        original = self.service()
        store.save_commission_records([original], self.path)
        before = self.path.read_bytes()
        candidates = [self.service(original_price_cents=9000, rate_bps=2000),
                      self.service(transaction_id="OTHER"),
                      {k: original[k] for k in ("entry_id", "employee_id", "month", "commission_cents")}]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                store.validate_commission_records([candidate])
                with self.assertRaises(ValueError):
                    store.save_commission_records([candidate], self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="commission-audit-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "commissions.json"

    def service(self, **changes):
        return dict(dict(entry_id="S1", employee_id="Alice", month="2026-10",
                         commission_cents=1800, entry_type="service", transaction_id="T1",
                         audit_version=1, participant_count=1, original_price_cents=6000,
                         rate_bps=3000, completed=True), **changes)

    def topup(self, **changes):
        return dict(dict(entry_id="P1", employee_id="Bob", month="2026-10",
                         commission_cents=5000, entry_type="topup", transaction_id="T2",
                         audit_version=1, topup_amount_cents=100000), **changes)

    def pair(self):
        return [self.service(participant_count=2, commission_cents=900),
                self.service(entry_id="S2", employee_id="Bob", participant_count=2,
                             commission_cents=900)]

    def assert_rejected(self, records):
        self.path.write_bytes(b"[]\n")
        before = copy.deepcopy(records)
        with self.assertRaises(ValueError):
            store.save_commission_records(records, self.path)
        self.assertEqual(self.path.read_bytes(), b"[]\n")
        self.assertEqual(records, before)
        self.path.write_text(json.dumps(records), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.load_commission_records(self.path)

    def test_audit_valid_round_trip_without_mutation(self):
        records = self.pair() + [self.topup()]
        before = copy.deepcopy(records)
        store.save_commission_records(records, self.path)
        self.assertEqual(store.load_commission_records(self.path), before)
        self.assertEqual(records, before)

    def test_audit_legacy_formats_remain_unchanged(self):
        records = sample_records() + [dict(entry_id="old-cli", employee_id="Bob",
            month="2026-10", commission_cents=999, entry_type="service", transaction_id="old")]
        self.path.write_text(json.dumps(records), encoding="utf-8")
        self.assertEqual(store.load_commission_records(self.path), records)
        store.save_commission_records(records + [self.topup()], self.path)
        self.assertEqual(store.load_commission_records(self.path)[:-1], records)

    def test_audit_missing_fields_rejected(self):
        for original in (self.service(), self.topup()):
            for field in (set(original) - {"entry_id", "employee_id", "month", "commission_cents"}):
                record = original.copy()
                del record[field]
                with self.subTest(entry_type=original["entry_type"], missing=field):
                    self.assert_rejected([record])

    def test_audit_new_record_mode_rejects_legacy(self):
        with self.assertRaises(ValueError):
            store.validate_commission_records(sample_records(), require_audit=True)
        store.validate_commission_records([self.service(), self.topup()], require_audit=True)

    def test_audit_invalid_types_and_values(self):
        cases = {
            "audit_version": (True, "1", 2, None),
            "original_price_cents": (True, 6000.0, "6000", None, 0, -1),
            "rate_bps": (True, 3000.0, "3000", None, -1, 10001),
            "completed": (1, 0, "True", None),
            "participant_count": (True, "1", 1.0, 0, 3),
            "transaction_id": (True, None, [], "", " "),
            "entry_type": (None, [], "unknown"),
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_rejected([self.service(**{field: value})])
        for value in (True, 100000.0, "100000", None, 0, -1):
            with self.subTest(topup=value):
                self.assert_rejected([self.topup(topup_amount_cents=value)])

    def test_audit_contradictory_amounts_and_no_rounding(self):
        for record in (self.service(commission_cents=1801),
                       self.service(completed=False),
                       self.service(original_price_cents=1),
                       self.topup(commission_cents=4999), self.topup(topup_amount_cents=1)):
            with self.subTest(record=record):
                self.assert_rejected([record])
        pair = self.pair()
        for entry in pair:
            entry.update(original_price_cents=10, commission_cents=1)
        self.assert_rejected(pair)

    def test_audit_incomplete_or_inconsistent_shared_service(self):
        self.assert_rejected(self.pair()[:1])
        for changes in ({"employee_id": "Alice"}, {"month": "2026-11"},
                        {"original_price_cents": 10000}, {"rate_bps": 2000},
                        {"completed": False}, {"commission_cents": 1800},
                        {"participant_count": 1}, {"transaction_id": "other"}):
            pair = self.pair()
            pair[1].update(changes)
            with self.subTest(changes=changes):
                self.assert_rejected(pair)

    def test_audit_transaction_cannot_mix_types_or_legacy(self):
        self.assert_rejected([self.service(), self.topup(transaction_id="T1")])
        self.assert_rejected([self.topup(), self.topup(entry_id="P2")])
        legacy = dict(entry_id="old", employee_id="Bob", month="2026-10",
                      commission_cents=900, transaction_id="T1", entry_type="service")
        self.assert_rejected([self.service(), legacy])

    def test_audit_wrong_basis_for_entry_type_rejected(self):
        self.assert_rejected([self.service(topup_amount_cents=100000)])
        for field, value in (("original_price_cents", 6000), ("rate_bps", 500),
                             ("completed", True), ("participant_count", 1)):
            with self.subTest(field=field):
                self.assert_rejected([self.topup(**{field: value})])

    def test_audit_zero_commission_and_uncompleted_are_valid(self):
        records = [self.service(completed=False, commission_cents=0),
                   self.service(entry_id="S2", transaction_id="T2", rate_bps=0,
                                commission_cents=0)]
        store.save_commission_records(records, self.path)
        self.assertEqual(store.load_commission_records(self.path), records)

    def test_audit_reversal_retains_original_basis(self):
        from commission import create_full_commission_reversal
        records = self.pair()
        before = copy.deepcopy(records)
        records.append(create_full_commission_reversal(records[0], "R1", "2026-11", records))
        store.save_commission_records(records, self.path)
        loaded = store.load_commission_records(self.path)
        self.assertEqual(loaded[:2], before)
        self.assertEqual(loaded[2]["reversal_of_entry_id"], "S1")
        self.assertEqual(loaded[2]["commission_cents"], -900)


if __name__ == "__main__":
    unittest.main()
