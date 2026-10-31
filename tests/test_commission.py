import copy
import unittest

import commission
from commission import calculate_service_commission


class ServiceCommissionTests(unittest.TestCase):
    def test_100_sgd_at_20_percent(self):
        self.assertEqual(calculate_service_commission(10000, 2000, True), 2000)

    def test_60_sgd_at_explicit_30_percent(self):
        self.assertEqual(calculate_service_commission(6000, 3000, True), 1800)

    def test_zero_rate(self):
        self.assertEqual(calculate_service_commission(10000, 0, True), 0)

    def test_uncompleted_service_returns_zero(self):
        for price, rate in ((10000, 2000), (6000, 3000), (1, 1)):
            with self.subTest(price=price, rate=rate):
                self.assertEqual(calculate_service_commission(price, rate, False), 0)

    def test_different_explicit_rates(self):
        for rate, expected in ((1, 1), (500, 500), (1250, 1250), (10000, 10000)):
            with self.subTest(rate=rate):
                result = calculate_service_commission(10000, rate, True)
                self.assertEqual(result, expected)
                self.assertIs(type(result), int)

    def test_invalid_amount_types(self):
        for price in (None, "10000", 10000.0, [], {}):
            with self.subTest(price=price):
                with self.assertRaisesRegex(TypeError, "original_price_cents"):
                    calculate_service_commission(price, 2000, True)

    def test_nonpositive_amounts(self):
        for price in (0, -1, -10000):
            with self.subTest(price=price):
                with self.assertRaisesRegex(ValueError, "original_price_cents"):
                    calculate_service_commission(price, 2000, True)

    def test_invalid_rate_types(self):
        for rate in (None, "2000", 2000.0, [], {}):
            with self.subTest(rate=rate):
                with self.assertRaisesRegex(TypeError, "rate_bps"):
                    calculate_service_commission(10000, rate, True)

    def test_out_of_range_rates(self):
        for rate in (-1, 10001):
            with self.subTest(rate=rate):
                with self.assertRaisesRegex(ValueError, "rate_bps"):
                    calculate_service_commission(10000, rate, True)

    def test_invalid_completed_types(self):
        for completed in (None, 0, 1, "True", "False", 1.0, [], {}):
            with self.subTest(completed=completed):
                with self.assertRaisesRegex(TypeError, "completed"):
                    calculate_service_commission(10000, 2000, completed)

    def test_bool_amount_and_rate_rejected(self):
        for value in (True, False):
            with self.subTest(field="amount", value=value):
                with self.assertRaisesRegex(TypeError, "original_price_cents"):
                    calculate_service_commission(value, 2000, True)
            with self.subTest(field="rate", value=value):
                with self.assertRaisesRegex(TypeError, "rate_bps"):
                    calculate_service_commission(10000, value, True)

    def test_fractional_cent_rejected(self):
        for price, rate in ((1, 1), (1, 5000), (999, 2000)):
            with self.subTest(price=price, rate=rate):
                with self.assertRaisesRegex(ValueError, "不足一分"):
                    calculate_service_commission(price, rate, True)

    def test_uncompleted_service_still_validates_inputs(self):
        for price, rate, error in (
            (True, 2000, TypeError),
            (0, 2000, ValueError),
            (10000, False, TypeError),
            (10000, 10001, ValueError),
        ):
            with self.subTest(price=price, rate=rate):
                with self.assertRaises(error):
                    calculate_service_commission(price, rate, False)

    def test_rate_has_no_default(self):
        with self.assertRaises(TypeError):
            calculate_service_commission(10000, completed=True)


class TopupCommissionTests(unittest.TestCase):
    def test_1000_sgd(self):
        self.assertEqual(commission.calculate_topup_commission(100000), 5000)

    def test_200_sgd(self):
        self.assertEqual(commission.calculate_topup_commission(20000), 1000)

    def test_50_sgd(self):
        self.assertEqual(commission.calculate_topup_commission(5000), 250)

    def test_1_sgd(self):
        self.assertEqual(commission.calculate_topup_commission(100), 5)

    def test_nonpositive_amounts(self):
        for amount in (0, -1, -10000):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    commission.calculate_topup_commission(amount)

    def test_invalid_amount_types(self):
        for amount in (100.0, "100", None, True, False, [], {}):
            with self.subTest(amount=amount):
                with self.assertRaises(TypeError):
                    commission.calculate_topup_commission(amount)

    def test_fractional_cent_rejected(self):
        for amount in (1, 19, 21, 99, 100001):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    commission.calculate_topup_commission(amount)

    def test_return_type_is_int(self):
        for amount in (20, 100, 5000, 20000, 100000):
            with self.subTest(amount=amount):
                self.assertIs(type(commission.calculate_topup_commission(amount)), int)

    def test_smallest_exact_commission(self):
        self.assertEqual(commission.calculate_topup_commission(20), 1)

    def test_large_amount_remains_exact(self):
        self.assertEqual(commission.calculate_topup_commission(10 ** 100), 5 * 10 ** 98)


class EqualCommissionSplitTests(unittest.TestCase):
    def test_3000_cents(self):
        self.assertEqual(commission.split_commission_equally(3000), (1500, 1500))

    def test_2000_cents(self):
        self.assertEqual(commission.split_commission_equally(2000), (1000, 1000))

    def test_zero_commission(self):
        self.assertEqual(commission.split_commission_equally(0), (0, 0))

    def test_odd_cents_rejected(self):
        for total in (1, 1501):
            with self.subTest(total=total):
                with self.assertRaisesRegex(ValueError, "平分舍入规则尚未确认"):
                    commission.split_commission_equally(total)

    def test_negative_amounts_rejected(self):
        for total in (-1, -2, -3000):
            with self.subTest(total=total):
                with self.assertRaises(ValueError):
                    commission.split_commission_equally(total)

    def test_invalid_amount_types(self):
        for total in (True, False, 3000.0, "3000", None, [], {}):
            with self.subTest(total=total):
                with self.assertRaises(TypeError):
                    commission.split_commission_equally(total)

    def test_returns_tuple_of_two_integers(self):
        for total in (0, 2, 3000):
            with self.subTest(total=total):
                result = commission.split_commission_equally(total)
                self.assertIs(type(result), tuple)
                self.assertEqual(len(result), 2)
                for share in result:
                    self.assertIs(type(share), int)

    def test_equal_shares_preserve_total(self):
        for total in range(0, 10002, 2):
            with self.subTest(total=total):
                first, second = commission.split_commission_equally(total)
                self.assertEqual(first, second)
                self.assertEqual(first + second, total)

    def test_large_amount_remains_exact(self):
        total = 10 ** 100 + 2
        result = commission.split_commission_equally(total)
        self.assertEqual(result, (5 * 10 ** 99 + 1, 5 * 10 ** 99 + 1))
        self.assertEqual(sum(result), total)

    def test_split_calculated_service_commission(self):
        total = calculate_service_commission(10000, 3000, True)
        self.assertEqual(total, 3000)
        result = commission.split_commission_equally(total)
        self.assertEqual(result, (1500, 1500))
        self.assertEqual(sum(result), total)


class EmployeeCommissionSummaryTests(unittest.TestCase):
    def entry(self, entry_id="E1", employee_id="EMP001", month="2026-10", amount=100):
        return dict(entry_id=entry_id, employee_id=employee_id,
                    month=month, commission_cents=amount)

    def test_single_employee_multiple_entries(self):
        entries = [self.entry(amount=1500), self.entry("E2", amount=2000)]
        self.assertEqual(commission.summarize_employee_commissions(entries, "2026-10"),
                         {"EMP001": 3500})

    def test_multiple_employees(self):
        entries = [self.entry(), self.entry("E2", "EMP002", amount=1500)]
        self.assertEqual(commission.summarize_employee_commissions(entries, "2026-10"),
                         {"EMP001": 100, "EMP002": 1500})

    def test_month_isolation_and_no_matches(self):
        entries = [self.entry(), self.entry("E2", month="2026-11", amount=900)]
        self.assertEqual(commission.summarize_employee_commissions(entries, "2026-10"),
                         {"EMP001": 100})
        self.assertEqual(commission.summarize_employee_commissions(entries, "2025-10"), {})

    def test_empty_list(self):
        self.assertEqual(commission.summarize_employee_commissions([], "2026-10"), {})

    def test_duplicate_ids_rejected_across_employees_and_months(self):
        for employee, month in (("EMP001", "2026-10"), ("EMP002", "2026-10"),
                                ("EMP002", "2026-11")):
            with self.subTest(employee=employee, month=month):
                entries = [self.entry(), self.entry(employee_id=employee, month=month)]
                with self.assertRaisesRegex(ValueError, "entry_id"):
                    commission.summarize_employee_commissions(entries, "2026-10")

    def test_invalid_input_structure(self):
        for entries in (None, {}, (), "entries", True, [None], [[]], [1]):
            with self.subTest(entries=entries):
                with self.assertRaises(TypeError):
                    commission.summarize_employee_commissions(entries, "2026-10")

    def test_missing_required_fields(self):
        for field in self.entry():
            entry = self.entry()
            del entry[field]
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    commission.summarize_employee_commissions([entry], "2026-10")

    def test_invalid_ids(self):
        for field in ("entry_id", "employee_id"):
            for value in (None, True, 123, [], "", " \t"):
                entry = self.entry()
                entry[field] = value
                with self.subTest(field=field, value=value):
                    error = ValueError if isinstance(value, str) else TypeError
                    with self.assertRaises(error):
                        commission.summarize_employee_commissions([entry], "2026-10")

    def test_invalid_month_types(self):
        for month in (None, True, 202610, 2026.10, []):
            with self.subTest(month=month):
                with self.assertRaises(TypeError):
                    commission.summarize_employee_commissions([], month)
                with self.assertRaises(TypeError):
                    commission.summarize_employee_commissions(
                        [self.entry(month=month)], "2026-10")

    def test_invalid_month_values(self):
        for month in ("", "2026-1", "2026-00", "2026-13", "0000-01",
                      "10000-01", "2026/10", "2026-10-01", "2026-10\n", "２０２６-１０"):
            with self.subTest(month=month):
                with self.assertRaises(ValueError):
                    commission.summarize_employee_commissions([], month)
                with self.assertRaises(ValueError):
                    commission.summarize_employee_commissions(
                        [self.entry(month=month)], "2026-10")

    def test_month_boundaries(self):
        for month in ("0001-01", "9999-12"):
            with self.subTest(month=month):
                self.assertEqual(commission.summarize_employee_commissions(
                    [self.entry(month=month)], month), {"EMP001": 100})

    def test_invalid_amount_types_including_bool(self):
        for amount in (True, False, 100.0, "100", None, [], {}):
            with self.subTest(amount=amount):
                with self.assertRaises(TypeError):
                    commission.summarize_employee_commissions(
                        [self.entry(amount=amount)], "2026-10")

    def test_other_month_records_are_validated(self):
        with self.assertRaises(TypeError):
            commission.summarize_employee_commissions(
                [self.entry(month="2026-11", amount=True)], "2026-10")

    def test_input_unchanged_on_success_and_error(self):
        entries = [self.entry(), self.entry("E2", amount=-20)]
        entries[0]["metadata"] = {"notes": ["模拟"]}
        for invalid in (False, True):
            if invalid:
                entries.append(self.entry())
            before = copy.deepcopy(entries)
            with self.subTest(invalid=invalid):
                if invalid:
                    with self.assertRaises(ValueError):
                        commission.summarize_employee_commissions(entries, "2026-10")
                else:
                    result = commission.summarize_employee_commissions(entries, "2026-10")
                    self.assertEqual(result, {"EMP001": 80})
                    result["EMP001"] = 999
                self.assertEqual(entries, before)

    def test_confirmed_adjustments_and_zero_totals(self):
        entries = [self.entry(amount=1000), self.entry("E2", amount=-200),
                   self.entry("E3", "EMP002", amount=-300),
                   self.entry("E4", "EMP003", amount=0),
                   self.entry("E5", "EMP004", amount=100),
                   self.entry("E6", "EMP004", amount=-100)]
        result = commission.summarize_employee_commissions(entries, "2026-10")
        self.assertEqual(result, {"EMP001": 800, "EMP002": -300, "EMP003": 0, "EMP004": 0})
        self.assertIs(type(result), dict)
        for value in result.values():
            self.assertIs(type(value), int)

    def test_large_integer_precision(self):
        entries = [self.entry(amount=10 ** 100), self.entry("E2", amount=-1)]
        self.assertEqual(commission.summarize_employee_commissions(entries, "2026-10"),
                         {"EMP001": 10 ** 100 - 1})

    def test_alice_and_bob_with_existing_calculators(self):
        single = calculate_service_commission(6000, 3000, True)
        topup = commission.calculate_topup_commission(100000)
        shared = calculate_service_commission(10000, 3000, True)
        alice_share, bob_share = commission.split_commission_equally(shared)
        entries = [self.entry("S1", "Alice", amount=single),
                   self.entry("T1", "Alice", amount=topup),
                   self.entry("S2-A", "Alice", amount=alice_share),
                   self.entry("S2-B", "Bob", amount=bob_share)]
        self.assertEqual(commission.summarize_employee_commissions(entries, "2026-10"),
                         {"Alice": 8300, "Bob": 1500})


class FullCommissionReversalTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(entry_id="S1", employee_id="Alice",
                             month="2026-10", commission_cents=1800)
        self.entries = [self.original.copy()]

    def create(self, entry_id="R1", month="2026-10"):
        return commission.create_full_commission_reversal(
            self.original, entry_id, month, self.entries)

    def test_full_reversal_amount_employee_and_reference(self):
        self.assertEqual(self.create(), {
            "entry_id": "R1", "employee_id": "Alice", "month": "2026-10",
            "commission_cents": -1800, "reversal_of_entry_id": "S1",
        })
        self.assertIs(type(self.create()["commission_cents"]), int)

    def test_same_month_net_zero(self):
        entries = self.entries + [self.create()]
        self.assertEqual(commission.summarize_employee_commissions(entries, "2026-10"),
                         {"Alice": 0})

    def test_explicit_cross_month(self):
        reversal = self.create(month="2026-11")
        self.assertEqual(reversal["month"], "2026-11")
        entries = self.entries + [reversal]
        for month, expected in (("2026-10", 1800), ("2026-11", -1800)):
            with self.subTest(month=month):
                self.assertEqual(commission.summarize_employee_commissions(entries, month),
                                 {"Alice": expected})

    def test_duplicate_reversal_rejected_across_months(self):
        self.entries.append(self.create())
        with self.assertRaises(ValueError):
            self.create("R2", "2026-11")

    def test_reversal_id_collision(self):
        self.entries.append(dict(entry_id="OTHER", employee_id="Bob",
                                 month="2026-09", commission_cents=100))
        for entry_id in ("S1", "OTHER"):
            with self.subTest(entry_id=entry_id):
                with self.assertRaises(ValueError):
                    self.create(entry_id)

    def test_missing_original(self):
        self.entries = []
        with self.assertRaises(ValueError):
            self.create()

    def test_original_key_fields_must_match(self):
        for field, value in (("employee_id", "Bob"), ("month", "2026-09"),
                             ("commission_cents", 2000)):
            with self.subTest(field=field):
                original = dict(self.original, **{field: value})
                with self.assertRaises(ValueError):
                    commission.create_full_commission_reversal(
                        original, "R1", "2026-10", self.entries)

    def test_invalid_original_amounts(self):
        for amount in (0, -1800, True, False, 1800.0, "1800", None):
            original = dict(self.original, commission_cents=amount)
            with self.subTest(amount=amount):
                error = ValueError if type(amount) is int else TypeError
                with self.assertRaises(error):
                    commission.create_full_commission_reversal(
                        original, "R1", "2026-10", [original.copy()])

    def test_invalid_ids(self):
        for value in (None, True, 123, [], "", " \t"):
            error = ValueError if type(value) is str else TypeError
            with self.subTest(field="reversal_entry_id", value=value):
                with self.assertRaises(error):
                    self.create(value)
            for field in ("entry_id", "employee_id"):
                original = dict(self.original, **{field: value})
                with self.subTest(field=field, value=value):
                    with self.assertRaises(error):
                        commission.create_full_commission_reversal(
                            original, "R1", "2026-10", [original.copy()])

    def test_invalid_months(self):
        for value in (None, True, 202610, "", "2026-1", "2026-13", "0000-01"):
            error = ValueError if type(value) is str else TypeError
            with self.subTest(month=value):
                with self.assertRaises(error):
                    self.create(month=value)
                original = dict(self.original, month=value)
                with self.assertRaises(error):
                    commission.create_full_commission_reversal(
                        original, "R1", "2026-10", [original.copy()])

    def test_invalid_structures_and_missing_fields(self):
        for value in (None, [], "entry", True):
            with self.subTest(original=value):
                with self.assertRaises(TypeError):
                    commission.create_full_commission_reversal(
                        value, "R1", "2026-10", self.entries)
        for value in (None, {}, (), [None], [[]]):
            with self.subTest(entries=value):
                with self.assertRaises(TypeError):
                    commission.create_full_commission_reversal(
                        self.original, "R1", "2026-10", value)
        for field in self.original:
            original = self.original.copy()
            del original[field]
            with self.subTest(missing=field):
                with self.assertRaises(ValueError):
                    commission.create_full_commission_reversal(
                        original, "R1", "2026-10", self.entries)

    def test_invalid_existing_entries_and_duplicate_ids(self):
        for extra in (self.original.copy(), {},
                      dict(self.original, entry_id="E2", commission_cents=True)):
            with self.subTest(extra=extra):
                error = TypeError if extra.get("commission_cents") is True else ValueError
                with self.assertRaises(error):
                    commission.create_full_commission_reversal(
                        self.original, "R1", "2026-10", self.entries + [extra])

    def test_reversal_record_cannot_be_original(self):
        original = dict(self.original, reversal_of_entry_id="EARLIER")
        with self.assertRaises(ValueError):
            commission.create_full_commission_reversal(
                original, "R1", "2026-10", [original.copy()])

    def test_invalid_existing_reversal_reference(self):
        for value in (None, True, [], "", " "):
            extra = dict(entry_id="R0", employee_id="Bob", month="2026-09",
                         commission_cents=-100, reversal_of_entry_id=value)
            with self.subTest(value=value):
                error = ValueError if type(value) is str else TypeError
                with self.assertRaises(error):
                    commission.create_full_commission_reversal(
                        self.original, "R1", "2026-10", self.entries + [extra])

    def test_inputs_unchanged_on_success_and_failure(self):
        self.original["metadata"] = {"notes": ["模拟"]}
        self.entries[0]["metadata"] = {"notes": ["模拟"]}
        before = copy.deepcopy((self.original, self.entries))
        reversal = self.create()
        reversal["employee_id"] = "Changed"
        self.assertEqual((self.original, self.entries), before)
        with self.assertRaises(ValueError):
            self.create("S1")
        self.assertEqual((self.original, self.entries), before)

    def test_two_employees_reversed_individually(self):
        shares = commission.split_commission_equally(
            calculate_service_commission(10000, 3000, True))
        originals = [dict(entry_id=entry_id, employee_id=employee, month="2026-10",
                          commission_cents=amount)
                     for entry_id, employee, amount in
                     (("S-A", "Alice", shares[0]), ("S-B", "Bob", shares[1]))]
        entries = copy.deepcopy(originals)
        for index, original in enumerate(originals):
            entries.append(commission.create_full_commission_reversal(
                original, f"R{index}", "2026-10", entries))
            expected = {"Alice": 0, "Bob": 1500 if index == 0 else 0}
            self.assertEqual(commission.summarize_employee_commissions(entries, "2026-10"),
                             expected)
        for original in originals:
            with self.subTest(employee=original["employee_id"]):
                with self.assertRaises(ValueError):
                    commission.create_full_commission_reversal(
                        original, "R-new", "2026-11", entries)


if __name__ == "__main__":
    unittest.main()
