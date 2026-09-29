"""Random name and birthday helpers."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date

FIRST_NAMES = [
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda",
    "William", "Elizabeth", "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Christopher", "Nancy", "Daniel", "Lisa",
    "Matthew", "Margaret", "Anthony", "Betty", "Mark", "Sandra", "Donald", "Ashley",
    "Steven", "Kimberly", "Paul", "Emily", "Andrew", "Donna", "Joshua", "Michelle",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill",
]


@dataclass
class Birthday:
    year: int
    month: int
    day: int

    def iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"

    def age(self) -> int:
        today = date.today()
        years = today.year - self.year
        if (today.month, today.day) < (self.month, self.day):
            years -= 1
        return years


def random_first_name() -> str:
    return random.choice(FIRST_NAMES)


def random_last_name() -> str:
    return random.choice(LAST_NAMES)


def random_birthday() -> Birthday:
    year = 1985 + random.randint(0, 17)
    month = 1 + random.randint(0, 11)
    day = 1 + random.randint(0, 26)
    return Birthday(year=year, month=month, day=day)


def generate_password(length: int = 14) -> str:
    upper = "ABCDEFGHJKMNPQRSTUVWXYZ"
    lower = "abcdefghjkmnpqrstuvwxyz"
    digits = "23456789"
    symbols = "!@#$%^&*"
    pool = upper + lower + digits + symbols
    chars = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
        random.choice(symbols),
    ]
    chars += [random.choice(pool) for _ in range(max(0, length - len(chars)))]
    random.shuffle(chars)
    return "".join(chars)


# ---------------------------------------------------------------------------
# 邮箱用户名（本地部分）
# ---------------------------------------------------------------------------
# MHJC 的规则：3–32 位，只能小写字母、数字、点、下划线、连字符
# （服务端原文：Username must be 3-32 characters, lowercase letters, numbers,
#   dots, underscores, hyphens only）。
# 服务端「随机生成」会给出 temp_<hex> 这种一眼临时邮箱的地址，容易被风控盯上，
# 因此默认在本地按真人姓名生成，如 emma.wilson / emmawilson92 / e_wilson7。

MAILBOX_USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,32}$")
MAILBOX_USERNAME_MAX = 32

# 姓名式用户名排版：点缀号/下划线占多数，纯连写和首字母缩写偶尔出现
_NAME_STYLES = (
    "dotted", "dotted", "dotted", "dotted_digits",
    "solid", "solid_digits", "underscore", "initial_last",
)


def sanitize_mailbox_username(value: str) -> str:
    """把任意输入规整成 MHJC 合法用户名；无法规整时返回空串。"""
    lowered = str(value or "").strip().lower()
    if "@" in lowered:
        lowered = lowered.split("@", 1)[0]
    cleaned = re.sub(r"[^a-z0-9._-]", "", lowered).strip("._-")
    if not cleaned:
        return ""
    return cleaned[:MAILBOX_USERNAME_MAX].strip("._-")


def _name_part(value: str, fallback: str) -> str:
    part = re.sub(r"[^a-z]", "", str(value or "").strip().lower())
    return part or fallback.lower()


def random_mailbox_username(first_name: str = "", last_name: str = "") -> str:
    """按真人姓名生成邮箱用户名（不含 @域名）。

    传入了注册档案的姓名时优先用同名，让邮箱地址与账号姓名保持一致；
    否则从姓名池里随机取。长度、字符集都满足 MHJC 的校验规则，
    不会产出 temp_ 之类一眼临时的前缀。
    """
    first = _name_part(first_name, random_first_name())
    last = _name_part(last_name, random_last_name())
    style = random.choice(_NAME_STYLES)
    initial = first[:1]

    if style == "dotted":
        local = f"{first}.{last}"
    elif style == "dotted_digits":
        local = f"{first}.{last}{random.randint(1, 9999)}"
    elif style == "solid":
        local = f"{first}{last}"
    elif style == "solid_digits":
        local = f"{first}{last}{random.randint(10, 999)}"
    elif style == "underscore":
        local = f"{first}_{last}{random.randint(1, 99)}"
    else:  # initial_last
        local = f"{initial}{last}{random.randint(10, 999)}"

    local = sanitize_mailbox_username(local)
    if MAILBOX_USERNAME_PATTERN.match(local):
        return local

    # 极端情况（名字被截断到 3 位以下）兜底：首名 + 数字
    fallback = sanitize_mailbox_username(f"{first}{last}")
    if MAILBOX_USERNAME_PATTERN.match(fallback):
        return fallback
    return sanitize_mailbox_username(f"{first or last}{random.randint(100, 9999)}")

