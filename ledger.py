def calculate_summary(records):
    income_cents = 0
    expense_cents = 0

    for record in records:
        if record["type"] == "income":
            income_cents += record["amount_cents"]

        elif record["type"] == "expense":
            expense_cents += record["amount_cents"]

    balance_cents = income_cents - expense_cents

    return income_cents, expense_cents, balance_cents