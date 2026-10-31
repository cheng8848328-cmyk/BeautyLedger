import re
from uuid import uuid4

import commission_storage
from commission import (
    calculate_service_commission,
    calculate_topup_commission,
    create_full_commission_reversal,
    split_commission_equally,
    summarize_employee_commissions,
)
from commission_storage import COMMISSIONS_FILE
from ledger import calculate_summary
from storage import RECORDS_FILE, load_records, save_records


def format_sgd(amount_cents):
    """只使用整数运算，将分显示为固定两位小数的 SGD。"""
    sign = "-" if amount_cents < 0 else ""
    dollars, cents = divmod(abs(amount_cents), 100)
    return f"{sign}{dollars}.{cents:02d} SGD"


class CommissionCancelled(Exception):
    """用户主动返回提成菜单。"""


def commission_input(prompt):
    value = input(prompt)
    if value.strip().lower() == "q":
        raise CommissionCancelled
    return value


def parse_commission_amount(value):
    """SGD 正整数或恰好两位小数，严格解析为整数分。"""
    if re.fullmatch(r"[0-9]+(?:\.[0-9]{2})?", value) is None:
        raise ValueError("金额请输入正整数或两位小数，例如 60、60.00、60.50")
    dollars, _, cents = value.partition(".")
    amount = int(dollars) * 100 + int(cents or "0")
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    return amount


def read_employee_id(prompt):
    employee_id = commission_input(prompt).strip()
    if not employee_id:
        raise ValueError("员工 ID 不能为空")
    return employee_id


def read_commission_month():
    month = commission_input("归属月份（YYYY-MM）：")
    summarize_employee_commissions([], month)
    return month


def read_service_commission():
    price = parse_commission_amount(commission_input("服务原价（SGD）："))
    rate = commission_input("本笔服务提成比例（%，例如 30 或 12.5，不含 %）：")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", rate) is None:
        raise ValueError("提成比例必须明确输入 0 至 100 的百分数，最多两位小数")
    whole, _, fraction = rate.partition(".")
    rate_bps = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if not 0 <= rate_bps <= 10000:
        raise ValueError("提成比例必须在 0% 至 100% 之间")
    completed = commission_input("服务是否完成（y/n）：").lower()
    if completed not in ("y", "n"):
        raise ValueError("服务完成状态请输入 y 或 n")
    basis = {"original_price_cents": price, "rate_bps": rate_bps,
             "completed": completed == "y"}
    return calculate_service_commission(price, rate_bps, basis["completed"]), basis


def record_commission(choice):
    """单进程串行录入；双人服务在同一次完整快照保存中提交。"""
    try:
        existing = commission_storage.load_commission_records(COMMISSIONS_FILE)
    except (OSError, ValueError) as error:
        print(f"无法读取提成记录：{error}；本次未录入，返回菜单。")
        return
    print("仅录入员工提成，不确认收款或会员余额变化。任一提示输入 q 返回菜单。")
    try:
        employees = [read_employee_id("员工 ID：")]
        if choice == "7":
            employees.append(read_employee_id("第二名员工 ID："))
            if employees[0] == employees[1]:
                raise ValueError("双人服务必须提供两个不同的员工 ID")
        month = read_commission_month()
        if choice == "6":
            amount = parse_commission_amount(commission_input("充值金额（SGD，提成固定 5%）："))
            shares = (calculate_topup_commission(amount),)
            entry_type = "topup"
            basis = {"topup_amount_cents": amount}
        else:
            total, basis = read_service_commission()
            shares = split_commission_equally(total) if choice == "7" else (total,)
            entry_type = "service"
            basis["participant_count"] = len(employees)
        transaction_id = uuid4().hex
        if any(entry.get("transaction_id") == transaction_id for entry in existing):
            raise ValueError("生成的交易 ID 冲突，请重新录入")
        new_entries = [
            {"entry_id": uuid4().hex, "employee_id": employee, "month": month,
             "commission_cents": share, "entry_type": entry_type,
             "transaction_id": transaction_id, "audit_version": 1, **basis}
            for employee, share in zip(employees, shares)
        ]
        commission_storage.validate_commission_records(new_entries, require_audit=True)
        updated = existing + new_entries
        commission_storage.validate_commission_records(updated)
    except CommissionCancelled:
        print("已取消，返回菜单。")
        return
    except ValueError as error:
        print(f"输入无效：{error}；本次未录入，返回菜单。")
        return
    try:
        commission_storage.save_commission_records(updated, COMMISSIONS_FILE)
    except (OSError, ValueError) as error:
        print(f"提成保存失败，本次未录入：{error}")
        return
    print("提成录入成功！")
    for entry in new_entries:
        print(f"{entry['employee_id']}：{format_sgd(entry['commission_cents'])}")


def query_commissions():
    print("查询员工月度提成，输入 q 返回菜单。")
    try:
        month = read_commission_month()
    except CommissionCancelled:
        print("已取消，返回菜单。")
        return
    except ValueError as error:
        print(f"输入无效：{error}；返回菜单。")
        return
    try:
        entries = commission_storage.load_commission_records(COMMISSIONS_FILE)
        totals = summarize_employee_commissions(entries, month)
    except (OSError, ValueError) as error:
        print(f"无法读取提成记录：{error}；返回菜单。")
        return
    print(f"{month} 员工提成汇总")
    if not totals:
        print("该月暂无提成记录")
    for employee in sorted(totals):
        print(f"{employee}：{format_sgd(totals[employee])}")


def reverse_commission():
    """人工确认单条全额冲回；确认后重读快照，不提供并发锁。"""
    try:
        entries = commission_storage.load_commission_records(COMMISSIONS_FILE)
    except (OSError, ValueError) as error:
        print(f"无法读取提成记录：{error}；冲回操作已停止。")
        return
    try:
        entry_id = commission_input("原始提成 entry_id（q 返回菜单）：")
        original = next((entry for entry in entries if entry["entry_id"] == entry_id), None)
        if original is None:
            raise ValueError("原始提成 entry_id 不存在")
        if original["commission_cents"] <= 0 or "reversal_of_entry_id" in original:
            raise ValueError("只能冲回金额大于零的正向提成")
        if any(entry.get("reversal_of_entry_id") == entry_id for entry in entries):
            raise ValueError("原明细已有冲回记录，不能重复冲回")
        print(f"原明细：{entry_id}")
        print(f"员工 ID：{original['employee_id']}")
        print(f"原提成：{format_sgd(original['commission_cents'])}")
        print(f"原始归属月份：{original['month']}")
        print(f"交易 ID：{original.get('transaction_id', '未记录')}")
        print(f"提成类型：{original.get('entry_type', '未记录')}")
        for field, label in (("original_price_cents", "服务原价"),
                             ("topup_amount_cents", "充值金额")):
            if field in original:
                print(f"{label}：{format_sgd(original[field])}")
        for field, label in (("rate_bps", "总提成比例（基点）"), ("completed", "服务已完成"),
                             ("participant_count", "服务人数"), ("audit_version", "审计版本")):
            if field in original:
                print(f"{label}：{original[field]}")
        transaction_id = original.get("transaction_id")
        related = [entry for entry in entries if transaction_id
                   and entry.get("transaction_id") == transaction_id
                   and entry["entry_id"] != entry_id and "reversal_of_entry_id" not in entry]
        if original.get("participant_count") == 2 or related:
            print("仅冲回当前员工的一条明细，不自动冲回另一名员工；请检查关联的其他员工明细。")
            for entry in related:
                print(f"关联明细：{entry['entry_id']}，员工：{entry['employee_id']}")
        print("请明确输入本次冲回月份，输入 q 取消。")
        month = read_commission_month()
        print(f"冲回预览：原提成 {format_sgd(original['commission_cents'])}，"
              f"拟冲回 {format_sgd(-original['commission_cents'])}，冲回月份 {month}。")
        print(f"仅减少 {original['employee_id']} 在 {month} 的净提成；原明细保留。")
        print("本操作仅生成提成冲回记录，不执行实际退款，也不自动扣回已发放工资。")
        print("本次菜单确认不代表真实退款已获授权。")
        if input("输入 YES 才执行，其他输入取消：") != "YES":
            raise CommissionCancelled
    except CommissionCancelled:
        print("已取消，未生成冲回记录。")
        return
    except ValueError as error:
        print(f"无法冲回：{error}；返回菜单。")
        return
    try:
        latest = commission_storage.load_commission_records(COMMISSIONS_FILE)
        current = next((entry for entry in latest if entry["entry_id"] == entry_id), None)
        if current != original:
            raise ValueError("原明细已变化或不存在，请重新查看并确认")
        reversal = create_full_commission_reversal(original, uuid4().hex, month, latest)
        updated = latest + [reversal]
        commission_storage.validate_commission_records(updated)
    except (OSError, ValueError) as error:
        print(f"提成冲回未保存：{error}")
        return
    try:
        commission_storage.save_commission_records(updated, COMMISSIONS_FILE)
    except (OSError, ValueError) as error:
        print(f"提成冲回保存失败：{error}")
        return
    print("提成冲回记录已保存")
    print(f"冲回明细：{reversal['entry_id']}，原明细：{entry_id}")


def main():
    try:
        try:
            records = load_records()
        except (OSError, ValueError) as error:
            print(f"无法读取账本：{RECORDS_FILE}")
            print(f"原因：{error}")
            print("程序停止启动，请检查原文件；不会自动覆盖账本。")
            return 1

        while True:
            print("====== BeautyLedger v0.2 ======")
            print("1. 录入收入")
            print("2. 录入支出")
            print("3. 查看收支记录")
            print("4. 查看财务汇总")
            print("5. 录入单人服务提成")
            print("6. 录入会员充值销售提成")
            print("7. 录入双人服务提成（同一总提成平分）")
            print("8. 查询员工月度提成")
            print("9. 全额提成冲回")
            print("0. 退出系统")

            choice = input("请输入选项：")
            if choice == "0":
                print("感谢使用 BeautyLedger！")
                return 0

            if choice in ("1", "2"):
                record_type = "income" if choice == "1" else "expense"
                record_name = "收入" if choice == "1" else "支出"
                try:
                    money = int(input(f"请输入{record_name}金额（SGD整数）："))
                except ValueError:
                    print("请输入正确的整数金额")
                    continue

                if money <= 0:
                    print("金额必须大于0")
                    continue

                note = input("请输入备注：")
                record = {
                    "type": record_type,
                    "amount_cents": money * 100,
                    "note": note,
                }
                updated_records = records + [record]
                try:
                    save_records(updated_records)
                except (OSError, ValueError) as error:
                    print(f"保存失败，本笔账目未录入：{error}")
                    continue

                records = updated_records
                print(f"{record_name}录入成功！金额：{format_sgd(record['amount_cents'])}")

            elif choice == "3":
                if not records:
                    print("暂无收支记录")
                else:
                    for record in records:
                        print(record["type"], format_sgd(record["amount_cents"]), record["note"])
            elif choice == "4":
                income, expense, balance = calculate_summary(records)
                print(f"总收入：{format_sgd(income)}")
                print(f"总支出：{format_sgd(expense)}")
                print(f"当前余额：{format_sgd(balance)}")
            elif choice in ("5", "6", "7"):
                record_commission(choice)
            elif choice == "8":
                query_commissions()
            elif choice == "9":
                reverse_commission()
            else:
                print("无效选项，请输入 0 至 9")
    except (KeyboardInterrupt, EOFError):
        print("\n输入已结束，退出 BeautyLedger。已保存的账目不受影响。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
