from ledger import calculate_summary
from storage import RECORDS_FILE, load_records, save_records


def format_sgd(amount_cents):
    """只使用整数运算，将分显示为固定两位小数的 SGD。"""
    sign = "-" if amount_cents < 0 else ""
    dollars, cents = divmod(abs(amount_cents), 100)
    return f"{sign}{dollars}.{cents:02d} SGD"


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
            print("====== BeautyLedger v0.1 ======")
            print("1. 录入收入")
            print("2. 录入支出")
            print("3. 查看收支记录")
            print("4. 查看财务汇总")
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
            else:
                print("无效选项，请输入 0、1、2、3 或 4")
    except (KeyboardInterrupt, EOFError):
        print("\n输入已结束，退出 BeautyLedger。已保存的账目不受影响。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
