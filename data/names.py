"""Random name and birthday helpers."""

from __future__ import annotations

import random
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
