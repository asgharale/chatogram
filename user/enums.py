GENDER = (
    (0, "آقا"),
    (1, "خانم"),
    (2, "ترجیح میدهم نگویم")
)

GENDER_MAP = {
    "man_gender": 0,
    "woman_gender": 1,
    "unknown_gender": 2,
}

GENDER_LABEL = {
    0: "آقا 🧑",
    1: "خانم 👩",
    2: "ترجیح نمی‌دهند",
}

TRANSACTION_TYPE = (
    (0, "شارژ"),
    (1, "کسر"),
)

DEPOSIT_STATUS = (
    (0, "در انتظار تأیید"),
    (1, "تأیید شده"),
    (2, "رد شده"),
)