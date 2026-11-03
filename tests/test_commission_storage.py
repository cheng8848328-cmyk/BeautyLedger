"""仅使用虚构提成和临时目录，所有存储调用显式传入测试路径。"""

import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import sys
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


def audited_records():
    """合法的新交易夹具；旧格式夹具只用于模拟已有磁盘历史。"""
    records = sample_records()
    for record in records[:3]:
        record.update(audit_version=1, entry_type="service",
                      transaction_id="TX-" + record["entry_id"],
                      original_price_cents=max(record["commission_cents"], 1),
                      rate_bps=10000 if record["commission_cents"] else 0,
                      completed=True, participant_count=1)
    return records


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
                # 布置升级前已存在的文件，不通过新保存接口新增旧格式记录。
                self.write_bytes(json.dumps(records).encode("utf-8"))
                store.save_commission_records(records, self.path)
                self.assertEqual(store.load_commission_records(str(self.path)), records)
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), records)
                self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_append_and_full_reversal_preserve_history(self):
        from commission import create_full_commission_reversal
        records = audited_records()[:2]
        store.save_commission_records(records[:1], self.path)
        store.save_commission_records(records, self.path)
        records.append(create_full_commission_reversal(records[0], "NEW-R", "2026-12", records))
        store.save_commission_records(records, self.path)
        store.save_commission_records(records, self.path)
        self.assertEqual(store.load_commission_records(self.path), records)

    def test_reject_empty_stale_deleted_replaced_and_reordered_history(self):
        records = sample_records()[:3]
        self.write_bytes(json.dumps(records).encode("utf-8"))
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
        self.write_bytes(json.dumps([original]).encode("utf-8"))
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
        records = [dict(audited_records()[0], commission_cents=10 ** 100 + 1,
                        original_price_cents=10 ** 100 + 1)]
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
                    store.save_commission_records(audited_records(), self.path)
                self.assertEqual(self.path.read_bytes(), contents)
                self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_directory_and_permission_errors_propagate(self):
        self.path.mkdir(parents=True)
        with self.assertRaises(OSError):
            store.load_commission_records(self.path)
        with patch.object(Path, "open", side_effect=PermissionError("拒绝读取")) as opened:
            with self.assertRaises(PermissionError):
                store.load_commission_records(self.path)
            with self.assertRaises(PermissionError):
                store.save_commission_records([], self.path)
        self.assertEqual(opened.call_count, 2)

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
                with patch.object(target, attribute, side_effect=OSError("保存失败")) as failure:
                    with self.assertRaises(OSError):
                        store.save_commission_records(audited_records(), self.path)
                failure.assert_called_once()
                self.assertEqual(self.path.read_bytes(), b"[]\n")
                self.assertEqual(unrelated.read_bytes(), b"keep")
                self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [unrelated])

    def test_partial_write_failure_cleans_temp(self):
        self.write_bytes(b"[]")

        def fail_after_write(records, file, **kwargs):
            file.write('[{"partial":')
            raise OSError("写入中断")

        with patch.object(store.json, "dump", side_effect=fail_after_write) as failure:
            with self.assertRaises(OSError):
                store.save_commission_records(audited_records(), self.path)
        failure.assert_called_once()
        self.assertEqual(self.path.read_bytes(), b"[]")
        self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_cleanup_failure_does_not_hide_original_error(self):
        self.write_bytes(b"[]")
        with patch.object(store.os, "replace", side_effect=OSError("替换失败")) as replace:
            with patch.object(Path, "unlink", side_effect=PermissionError("清理失败")) as unlink:
                with self.assertRaisesRegex(OSError, "替换失败"):
                    store.save_commission_records(audited_records(), self.path)
        replace.assert_called_once()
        unlink.assert_called_once()
        self.assertEqual(self.path.read_bytes(), b"[]")

    def test_failed_first_save_leaves_no_formal_file(self):
        with patch.object(store.os, "replace", side_effect=OSError("替换失败")) as replace:
            with self.assertRaises(OSError):
                store.save_commission_records(audited_records(), self.path)
        replace.assert_called_once()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_atomic_replace_uses_same_directory_and_synced_file(self):
        with patch.object(store, "NamedTemporaryFile", wraps=store.NamedTemporaryFile) as create:
            with patch.object(store.os, "replace", wraps=store.os.replace) as replace:
                with patch.object(store.os, "fsync", wraps=store.os.fsync) as sync:
                    store.save_commission_records(audited_records(), self.path)
        create.assert_called_once()
        replace.assert_called_once()
        self.assertEqual(create.call_args.kwargs["dir"], self.path.parent)
        source, destination = replace.call_args.args
        self.assertEqual(source.parent, self.path.parent)
        self.assertEqual(destination, self.path)
        sync.assert_called_once()

    def test_input_not_modified_and_metadata_preserved(self):
        records = audited_records()
        records[0]["note"] = {"text": ["模拟提成"]}
        before = copy.deepcopy(records)
        store.validate_commission_records(records)
        store.save_commission_records(records, self.path)
        self.assertEqual(store.load_commission_records(self.path), before)
        with patch.object(store.os, "replace", side_effect=OSError("失败")) as replace:
            with self.assertRaises(OSError):
                store.save_commission_records(records, self.path)
        replace.assert_called_once()
        self.assertEqual(records, before)

    def test_nonserializable_metadata_rejected_before_io(self):
        for value in (object(), ("tuple",), {1: "integer key"}, {1: "a", "1": "b"}):
            with self.subTest(value=value):
                records = audited_records()
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

    def test_shared_basis_mismatch_rejected_even_when_each_amount_is_correct(self):
        pair = self.pair()
        pair[1].update(original_price_cents=9000, rate_bps=2000)
        for entry in pair:
            total = store.calculate_service_commission(
                entry["original_price_cents"], entry["rate_bps"], entry["completed"])
            self.assertEqual(store.split_commission_equally(total), (900, 900))
            self.assertEqual(entry["commission_cents"], 900)
        # Both complete, uniform-basis alternatives must pass the real save API.
        for price, rate in ((6000, 3000), (9000, 2000)):
            legal = [dict(entry, original_price_cents=price, rate_bps=rate) for entry in pair]
            legal_path = self.path.parent / ("legal-" + str(price) + ".json")
            store.save_commission_records(legal, legal_path)
            self.assertEqual(store.load_commission_records(legal_path), legal)
        self.path.write_bytes(b"[]\n")
        before = copy.deepcopy(pair)
        with patch.object(store, "NamedTemporaryFile") as create, \
                patch.object(store.os, "replace") as replace:
            with self.assertRaisesRegex(ValueError, "计算依据不一致"):
                store.save_commission_records(pair, self.path)
        create.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"[]\n")
        self.assertEqual(pair, before)

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


class StorageHardeningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="commission-hardening-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "data" / "commissions.json"

    def new_record(self, **changes):
        return dict(dict(audited_records()[0], entry_id="NEW", transaction_id="NEW-TX"),
                    **changes)

    def seed_bytes(self, contents):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(contents)

    def seed_legacy(self, records):
        self.seed_bytes(json.dumps(records, ensure_ascii=False).encode("utf-8"))

    def assert_memory_rejected(self, records):
        # 先独立检查全量验证，防止因新增缺少依据而意外通过安全测试。
        with self.assertRaises(ValueError):
            store.validate_commission_records(records)
        with patch.object(store.Path, "mkdir") as mkdir:
            with patch.object(store, "NamedTemporaryFile") as temporary:
                with patch.object(store.os, "replace") as replace:
                    with self.assertRaises(ValueError):
                        store.save_commission_records(records, self.path)
        mkdir.assert_not_called()
        temporary.assert_not_called()
        replace.assert_not_called()

    def assert_file_rejected(self, contents):
        self.seed_bytes(contents)
        with self.assertRaises(ValueError):
            store.load_commission_records(self.path)
        self.assertEqual(self.path.read_bytes(), contents)
        with patch.object(store, "load_commission_records",
                          wraps=store.load_commission_records) as load:
            with patch.object(store, "NamedTemporaryFile") as temporary:
                with patch.object(store.os, "replace") as replace:
                    with self.assertRaises(ValueError):
                        store.save_commission_records([self.new_record()], self.path)
        load.assert_called_once()
        temporary.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), contents)
        self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def test_new_file_rejects_legacy_positive_and_zero_before_creating_directory(self):
        for amount in (1800, 0):
            records = [dict(sample_records()[0], commission_cents=amount)]
            with self.subTest(amount=amount):
                # 这仍是可读取的合法旧格式；拒绝原因只能是新增规则。
                store.validate_commission_records(records)
                with patch.object(store.Path, "mkdir") as mkdir:
                    with patch.object(store, "NamedTemporaryFile") as temporary:
                        with self.assertRaises(ValueError):
                            store.save_commission_records(records, self.path)
                mkdir.assert_not_called()
                temporary.assert_not_called()
                self.assertFalse(self.path.parent.exists())

    def test_existing_legacy_rejects_new_legacy_positive_and_zero_preserving_bytes(self):
        history = sample_records()
        self.seed_legacy(history)
        before = self.path.read_bytes()
        for amount in (100, 0):
            updated = history + [dict(sample_records()[0], entry_id="NEW", commission_cents=amount)]
            with self.subTest(amount=amount):
                store.validate_commission_records(updated)
                with patch.object(store, "NamedTemporaryFile") as temporary:
                    with self.assertRaises(ValueError):
                        store.save_commission_records(updated, self.path)
                temporary.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_history_resave_and_new_reversal_do_not_add_audit_fields(self):
        from commission import create_full_commission_reversal, summarize_employee_commissions
        history = sample_records()[:3]
        self.seed_legacy(history)
        store.save_commission_records(history, self.path)
        reversal = create_full_commission_reversal(history[0], "NEW-R", "2026-12", history)
        store.save_commission_records(history + [reversal], self.path)
        loaded = store.load_commission_records(self.path)
        self.assertEqual(loaded, history + [reversal])
        self.assertTrue(all("audit_version" not in row for row in loaded))
        self.assertEqual(summarize_employee_commissions(loaded, "2026-12"), {"Alice": -1800})

    def test_legacy_history_accepts_complete_new_service_topup_pair_and_zero(self):
        history = sample_records()
        self.seed_legacy(history)
        pair = [self.new_record(entry_id="PAIR-A", transaction_id="PAIR", participant_count=2,
                                original_price_cents=3000, commission_cents=1500),
                self.new_record(entry_id="PAIR-B", transaction_id="PAIR", participant_count=2,
                                original_price_cents=3000, commission_cents=1500, employee_id="Bob")]
        topup = dict(entry_id="TOPUP", transaction_id="TOPUP-TX", employee_id="Bob",
                     month="2026-10", commission_cents=500, entry_type="topup",
                     audit_version=1, topup_amount_cents=10000)
        zero = self.new_record(entry_id="ZERO", transaction_id="ZERO-TX", completed=False,
                               commission_cents=0)
        updated = history + [self.new_record(), topup, zero] + pair
        store.save_commission_records(updated, self.path)
        loaded = store.load_commission_records(self.path)
        self.assertEqual(loaded, updated)
        self.assertEqual(loaded[:len(history)], history)

    def test_new_record_and_reversal_same_batch_in_either_order(self):
        from commission import create_full_commission_reversal
        original = self.new_record()
        reversal = create_full_commission_reversal(original, "R-NEW", "2026-11", [original])
        for index, rows in enumerate(([original, reversal], [reversal, original])):
            with self.subTest(order=index):
                path = self.path.parent / f"batch-{index}.json"
                store.save_commission_records(rows, path)
                self.assertEqual(store.load_commission_records(path), rows)
                self.assertNotIn("audit_version", reversal)

    def test_missing_new_pair_member_preserves_existing_history(self):
        history = sample_records()
        self.seed_legacy(history)
        before = self.path.read_bytes()
        incomplete = self.new_record(participant_count=2, original_price_cents=3000,
                                     commission_cents=1500)
        with patch.object(store, "NamedTemporaryFile") as temporary:
            with self.assertRaises(ValueError):
                store.save_commission_records(history + [incomplete], self.path)
        temporary.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_json_keys_at_all_levels_and_escaped_equivalence(self):
        base = json.dumps([self.new_record()])
        cases = [base.replace('"entry_id": "NEW"', '"entry_id": "NEW", "entry_id": "NEW"'),
                 base.replace('"commission_cents": 1800',
                              '"commission_cents": 999, "commission_cents": 1800'),
                 base[:-2] + ', "metadata": {"x": 1, "x": 2}}]',
                 base[:-2] + r', "metadata": {"a": 1, "\u0061": 2}}]']
        for index, text in enumerate(cases):
            with self.subTest(case=index):
                self.assert_file_rejected(text.encode("utf-8"))

    def test_nonfinite_json_constants_and_exponent_overflow(self):
        base = json.dumps([self.new_record()])
        for number in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400"):
            with self.subTest(number=number):
                text = base[:-2] + ', "metadata": {"nested": [' + number + ']}}]'
                self.assert_file_rejected(text.encode("utf-8"))

    def test_nonfinite_python_metadata_is_rejected_before_io(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=str(value)):
                self.assert_memory_rejected([self.new_record(metadata={"nested": [value]})])
        self.assertFalse(self.path.parent.exists())

    def test_surrogate_json_values_and_keys_are_rejected(self):
        for surrogate in ("\ud800", "\udfff"):
            for location in ("employee", "value", "key"):
                record = self.new_record()
                if location == "employee":
                    record["employee_id"] = surrogate
                elif location == "key":
                    record["metadata"] = {surrogate: "value"}
                else:
                    record["metadata"] = {"nested": [surrogate]}
                with self.subTest(codepoint=ord(surrogate), location=location):
                    self.assert_file_rejected(json.dumps([record]).encode("utf-8"))

    def test_surrogate_python_values_and_keys_rejected_without_mutating_input(self):
        for value in ("\ud800", "\udfff", {"nested": ["\ud800"]}, {"\udfff": "value"}):
            rows = [self.new_record(metadata=value)]
            before = copy.deepcopy(rows)
            self.assert_memory_rejected(rows)
            self.assertEqual(rows, before)
        self.assertFalse(self.path.parent.exists())

    def test_unsupported_python_types_and_key_collisions_rejected_before_io(self):
        values = [{1: "number"}, {True: "bool"}, {None: "null"},
                  {1: "first", "1": "second"}, (1, 2), {"nested": [(1, 2)]},
                  {1, 2}, frozenset({1}), object()]
        for index, value in enumerate(values):
            with self.subTest(case=index):
                self.assert_memory_rejected([self.new_record(metadata=value)])
        self.assertFalse(self.path.parent.exists())

    def test_cyclic_objects_are_rejected_but_shared_objects_are_allowed(self):
        circular_list = []
        circular_list.append(circular_list)
        circular_dict = {}
        circular_dict["self"] = circular_dict
        for cycle in (circular_list, circular_dict):
            self.assert_memory_rejected([self.new_record(metadata=cycle)])
        self.assertIs(circular_list[0], circular_list)
        self.assertIs(circular_dict["self"], circular_dict)
        shared = {"value": ["共享"]}
        rows = [self.new_record(metadata=[shared, shared])]
        store.save_commission_records(rows, self.path)
        self.assertEqual(store.load_commission_records(self.path), rows)

    def test_deep_json_and_python_metadata_raise_value_error_preserving_file(self):
        base = json.dumps([self.new_record()])
        text = base[:-2] + ', "metadata": ' + '[' * 5000 + '0' + ']' * 5000 + '}]'
        self.assert_file_rejected(text.encode("utf-8"))
        before = self.path.read_bytes()
        nested = 0
        for _ in range(5000):
            nested = [nested]
        self.assert_memory_rejected([self.new_record(metadata=nested)])
        self.assertEqual(self.path.read_bytes(), before)

    def test_legal_unicode_big_integers_and_finite_metadata_round_trip(self):
        huge = 10 ** 100 + 1
        record = self.new_record(employee_id="模拟员工😀", commission_cents=huge,
                                 original_price_cents=huge,
                                 metadata={"中文😀": [None, True, False, huge, 1.25, -0.0,
                                                    {"array": [1, "中文", "😀"]}]})
        before = copy.deepcopy(record)
        store.save_commission_records([record], self.path)
        loaded = store.load_commission_records(self.path)
        self.assertEqual(loaded, [record])
        self.assertEqual(record, before)
        self.assertIs(type(loaded[0]["commission_cents"]), int)
        self.assertIs(type(loaded[0]["metadata"]["中文😀"][4]), float)
        # JSON 中合法的 surrogate pair 应解析为正常 emoji，而非被误拒绝。
        self.seed_bytes(json.dumps([record], ensure_ascii=True).encode("utf-8"))
        self.assertEqual(store.load_commission_records(self.path), [record])
        store.save_commission_records([record], self.path)

    def test_recursion_during_partial_write_preserves_bytes_and_unrelated_temp(self):
        history = audited_records()[:1]
        store.save_commission_records(history, self.path)
        before = self.path.read_bytes()
        unrelated = self.path.parent / "commissions-unrelated.tmp"
        unrelated.write_bytes(b"keep")

        def interrupted_dump(records, stream, **kwargs):
            stream.write('[{"partial":')
            raise RecursionError("simulated encoding recursion")

        with patch.object(store.json, "dump", side_effect=interrupted_dump) as fault:
            with self.assertRaises(ValueError) as error:
                store.save_commission_records(history + [self.new_record()], self.path)
        fault.assert_called_once()
        self.assertIsInstance(error.exception.__cause__, RecursionError)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(unrelated.read_bytes(), b"keep")
        self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [unrelated])

    def test_history_comparison_distinguishes_nested_types_and_preserves_order(self):
        for original_value, replacement in ((True, 1), (1, 1.0), (False, 0)):
            history = [dict(sample_records()[0], metadata={"nested": [{"value": original_value}]})]
            self.seed_legacy(history)
            before = self.path.read_bytes()
            candidate = copy.deepcopy(history)
            candidate[0]["metadata"]["nested"][0]["value"] = replacement
            store.validate_commission_records(candidate)
            with self.subTest(original_type=type(original_value).__name__,
                              replacement_type=type(replacement).__name__):
                with self.assertRaises(ValueError):
                    store.save_commission_records(candidate, self.path)
                self.assertEqual(self.path.read_bytes(), before)
        history = [dict(sample_records()[0], metadata={"a": 1, "b": [1, 2]})]
        self.seed_legacy(history)
        reordered_keys = [dict(reversed(list(history[0].items())))]
        reordered_keys[0]["metadata"] = {"b": [1, 2], "a": 1}
        store.save_commission_records(reordered_keys, self.path)
        self.assertEqual(store.load_commission_records(self.path), history)
        before = self.path.read_bytes()
        for changes in ({"a": 1, "b": [2, 1]}, {"a": 1}, {"a": 1, "b": [1, 2], "c": None}):
            with self.assertRaises(ValueError):
                store.save_commission_records([dict(history[0], metadata=changes)], self.path)
            self.assertEqual(self.path.read_bytes(), before)


class CommissionFinalRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="commission-final-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "data" / "commissions.json"
        self.history = audited_records()[:1]
        self.new_record = dict(self.history[0], entry_id="NEXT", transaction_id="NEXT-TX")
        store.save_commission_records(self.history, self.path)

    def test_recursion_in_each_history_encoding_preserves_file_before_io(self):
        before = self.path.read_bytes()
        candidate = self.history + [self.new_record]
        original_input = copy.deepcopy(candidate)
        real_dumps = json.dumps
        for failing_call in (1, 2):
            with self.subTest(history_encoding=failing_call):
                fault = RecursionError("history encoding fault")
                history_calls = []

                def encode(value, **kwargs):
                    # Do not fail the full-data preflight or the disk reload.
                    if kwargs.get("sort_keys"):
                        history_calls.append(copy.deepcopy(value))
                        if len(history_calls) == failing_call:
                            raise fault
                    return real_dumps(value, **kwargs)

                with patch.object(json, "dumps", side_effect=encode), \
                        patch.object(store, "load_commission_records", wraps=store.load_commission_records) as load, \
                        patch.object(store.Path, "mkdir") as mkdir, \
                        patch.object(store, "NamedTemporaryFile") as create, \
                        patch.object(store.os, "replace") as replace:
                    with self.assertRaises(ValueError) as error:
                        store.save_commission_records(candidate, self.path)
                self.assertIs(error.exception.__cause__, fault)
                self.assertEqual(history_calls, [self.history] * failing_call)
                load.assert_called_once_with(self.path)
                mkdir.assert_not_called()
                create.assert_not_called()
                replace.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(candidate, original_input)
                self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [])

    def assert_stream_failure(self, stage, *, cleanup_failure=False):
        before = self.path.read_bytes()
        candidate = self.history + [self.new_record]
        original_input = copy.deepcopy(candidate)
        unrelated = self.path.parent / "commissions-unrelated.tmp"
        unrelated.write_bytes(b"keep")
        fault = KeyboardInterrupt("write interrupted") if stage == "write" else OSError(stage + " failed")
        real_temporary = store.NamedTemporaryFile
        created = []
        streams = []
        injected = []
        positions = []
        with ExitStack() as patches:
            def create_file(**kwargs):
                stream = real_temporary(**kwargs)
                raw = stream.file
                created.append(Path(stream.name))
                streams.append(raw)
                real_operation = getattr(raw, stage)
                calls = 0

                def fail_operation(*args, **options):
                    nonlocal calls
                    calls += 1
                    # After the explicit flush fails, close must still release the handle.
                    if calls > 1:
                        return real_operation(*args, **options)
                    if stage == "write":
                        real_operation(*args, **options)
                    positions.append(raw.tell())
                    if stage == "close":
                        real_operation(*args, **options)
                    raise fault

                injected.append(patches.enter_context(
                    patch.object(raw, stage, side_effect=fail_operation)))
                return stream

            create = patches.enter_context(patch.object(store, "NamedTemporaryFile", side_effect=create_file))
            dump = patches.enter_context(patch.object(store.json, "dump", wraps=store.json.dump))
            sync = patches.enter_context(patch.object(store.os, "fsync", wraps=store.os.fsync))
            replace = patches.enter_context(patch.object(store.os, "replace"))
            if cleanup_failure:
                cleanup = patches.enter_context(patch.object(
                    Path, "unlink", autospec=True, side_effect=PermissionError("cleanup denied")))
            with self.assertRaises(type(fault)) as error:
                store.save_commission_records(candidate, self.path)

        self.assertIs(error.exception, fault)
        create.assert_called_once()
        dump.assert_called_once()
        replace.assert_not_called()
        if stage == "close":
            sync.assert_called_once()
        else:
            sync.assert_not_called()
        self.assertEqual(len(injected), 1)
        injected[0].assert_called()
        self.assertEqual(len(positions), 1)
        self.assertGreater(positions[0], 0)
        self.assertTrue(streams[0].closed)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(candidate, original_input)
        self.assertEqual(unrelated.read_bytes(), b"keep")
        if cleanup_failure:
            cleanup.assert_called_once_with(created[0])
            self.assertEqual(set(self.path.parent.glob("commissions-*.tmp")), {unrelated, created[0]})
            created[0].unlink()
        self.assertEqual(list(self.path.parent.glob("commissions-*.tmp")), [unrelated])

    def test_explicit_flush_failure_preserves_original_and_cleans_temp(self):
        self.assert_stream_failure("flush")

    def test_close_failure_preserves_original_and_cleans_temp(self):
        self.assert_stream_failure("close")

    def test_keyboard_interrupt_during_write_propagates_and_cleans_temp(self):
        self.assert_stream_failure("write")

    def test_cleanup_failure_preserves_original_io_error_or_control_signal(self):
        for stage in ("flush", "close", "write"):
            with self.subTest(stage=stage):
                self.assert_stream_failure(stage, cleanup_failure=True)

    def test_nested_builtin_subclasses_are_rejected_before_temporary_creation(self):
        before = self.path.read_bytes()
        for builtin, value in ((int, 1), (float, 1.0), (str, "DEMO"), (list, [1]), (dict, {"x": 1})):
            with self.subTest(builtin=builtin.__name__):
                subclass = type("Custom" + builtin.__name__, (builtin,), {})
                custom = subclass(value)
                normal = self.history + [dict(self.new_record, metadata={"nested": [value]})]
                store.validate_commission_records(normal, require_audit=True)
                candidate = self.history + [dict(self.new_record, metadata={"nested": [custom]})]
                with patch.object(store, "NamedTemporaryFile") as create:
                    with self.assertRaises(ValueError):
                        store.save_commission_records(candidate, self.path)
                create.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertIs(candidate[-1]["metadata"]["nested"][0], custom)

    def test_integer_conversion_limit_rejects_memory_and_disk_without_overwrite(self):
        before = self.path.read_bytes()
        previous = sys.get_int_max_str_digits()
        self.addCleanup(sys.set_int_max_str_digits, previous)
        limit = sys.int_info.str_digits_check_threshold
        sys.set_int_max_str_digits(limit)
        candidate = self.history + [dict(self.new_record, metadata=10 ** limit)]
        with patch.object(store, "NamedTemporaryFile") as create:
            with self.assertRaises(ValueError):
                store.save_commission_records(candidate, self.path)
        create.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        disk = json.dumps([dict(self.new_record, metadata="HUGE")]).encode("utf-8")
        disk = disk.replace(b'"HUGE"', b"1" + b"0" * limit)
        self.path.write_bytes(disk)
        with patch.object(store, "NamedTemporaryFile") as create:
            with self.assertRaises(ValueError):
                store.load_commission_records(self.path)
            with self.assertRaises(ValueError):
                store.save_commission_records(self.history + [self.new_record], self.path)
        create.assert_not_called()
        self.assertEqual(self.path.read_bytes(), disk)


if __name__ == "__main__":
    unittest.main()
