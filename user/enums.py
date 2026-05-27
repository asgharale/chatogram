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

# Anonymous chat partner-gender preference
ANON_CHAT_PREF_ANY   = "any"
ANON_CHAT_PREF_BOYS  = "boys"   # callback: anon_pref_boys
ANON_CHAT_PREF_GIRLS = "girls"  # callback: anon_pref_girls