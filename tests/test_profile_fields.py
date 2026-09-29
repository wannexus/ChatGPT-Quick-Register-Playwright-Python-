from __future__ import annotations

import unittest

from core import flow
from data.names import Birthday


def _meta(**overrides) -> dict:
    base = {"name": "", "id": "", "placeholder": "", "type": "text",
            "autocomplete": "", "ariaLabel": "", "inputmode": "", "maxlength": ""}
    base.update(overrides)
    return base


BIRTHDAY = Birthday(year=1992, month=7, day=14)


class ClassifyInputTests(unittest.TestCase):
    """The /about-you page asks for a full name plus either age or birth date."""

    def test_full_name_field(self):
        for meta in (_meta(name="name"), _meta(placeholder="全名"), _meta(autocomplete="name")):
            self.assertEqual(flow._classify_input(meta), "name")

    def test_age_field_by_keyword(self):
        self.assertEqual(flow._classify_input(_meta(name="age")), "age")
        self.assertEqual(flow._classify_input(_meta(placeholder="年龄")), "age")
        self.assertEqual(flow._classify_input(_meta(ariaLabel="age", type="number")), "age")

    def test_numeric_field_labelled_like_age(self):
        meta = _meta(type="number", inputmode="numeric", maxlength="3", ariaLabel="your age")
        self.assertEqual(flow._classify_input(meta), "age")

    def test_native_date_input_is_a_birthday(self):
        self.assertEqual(flow._classify_input(_meta(type="date", name="birthday")), "birth")
        self.assertEqual(flow._classify_input(_meta(type="date")), "birth")

    def test_text_field_with_a_date_placeholder_is_a_birthday_not_a_day_part(self):
        for placeholder in ("yyyy-mm-dd", "mm/dd/yyyy", "dd/mm/yyyy", "yyyy/mm/dd", "生日", "出生日期", "Date of birth"):
            kind = flow._classify_input(_meta(placeholder=placeholder))
            self.assertEqual(kind, "birth", f"{placeholder!r} should be a whole birthday field")

    def test_labelled_birthday_wins_over_the_day_month_year_heuristics(self):
        self.assertEqual(flow._classify_input(_meta(name="date-of-birth")), "birth")
        self.assertEqual(flow._classify_input(_meta(placeholder="出生日期 yyyy-mm-dd")), "birth")

    def test_plain_day_month_year_fields_still_classify_separately(self):
        self.assertEqual(flow._classify_input(_meta(name="day", placeholder="日")), "day")
        self.assertEqual(flow._classify_input(_meta(name="month", placeholder="月")), "month")
        self.assertEqual(flow._classify_input(_meta(name="year", placeholder="年")), "year")

    def test_first_and_last_still_work(self):
        self.assertEqual(flow._classify_input(_meta(name="first-name")), "first")
        self.assertEqual(flow._classify_input(_meta(name="last_name")), "last")


class BirthdayStrategyTests(unittest.TestCase):
    def test_a_date_field_wins_when_both_exist(self):
        self.assertEqual(flow.choose_birthday_strategy({"birth", "age"}), "date")

    def test_age_only(self):
        self.assertEqual(flow.choose_birthday_strategy({"age", "name"}), "age")

    def test_split_day_month_year(self):
        self.assertEqual(flow.choose_birthday_strategy({"day", "month", "year"}), "split")

    def test_incomplete_split_is_not_used(self):
        self.assertEqual(flow.choose_birthday_strategy({"day", "month"}), "none")

    def test_nothing_recognised(self):
        self.assertEqual(flow.choose_birthday_strategy({"name", "unknown"}), "none")


class BirthdayValueTests(unittest.TestCase):
    def test_age_is_a_plain_integer(self):
        self.assertEqual(flow.birthday_age_value(BIRTHDAY), str(BIRTHDAY.age()))
        self.assertTrue(flow.birthday_age_value(BIRTHDAY).isdigit())

    def test_native_date_input_gets_the_iso_date(self):
        self.assertEqual(flow.birthday_text_candidates(BIRTHDAY, _meta(type="date"))[0], "1992-07-14")

    def test_placeholder_format_is_tried_first(self):
        self.assertEqual(
            flow.birthday_text_candidates(BIRTHDAY, _meta(placeholder="mm/dd/yyyy"))[0], "07/14/1992")
        self.assertEqual(
            flow.birthday_text_candidates(BIRTHDAY, _meta(placeholder="dd/mm/yyyy"))[0], "14/07/1992")
        self.assertEqual(
            flow.birthday_text_candidates(BIRTHDAY, _meta(placeholder="yyyy/mm/dd"))[0], "1992/07/14")

    def test_cjk_date_format(self):
        self.assertEqual(
            flow.birthday_text_candidates(BIRTHDAY, _meta(placeholder="年/月/日"))[0], "1992/07/14")
        self.assertEqual(
            flow.birthday_text_candidates(BIRTHDAY, _meta(placeholder="yyyy年mm月dd日"))[0], "1992年07月14日")

    def test_always_offers_the_common_fallbacks(self):
        candidates = flow.birthday_text_candidates(BIRTHDAY, _meta(placeholder="yyyy-mm-dd"))
        self.assertEqual(candidates[0], "1992-07-14")
        self.assertIn("1992-07-14", candidates)
        self.assertIn("07/14/1992", candidates)
        self.assertIn("14/07/1992", candidates)

    def test_candidates_are_unique(self):
        candidates = flow.birthday_text_candidates(BIRTHDAY, _meta(type="date"))
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_the_produced_date_is_a_real_calendar_date(self):
        candidates = flow.birthday_text_candidates(BIRTHDAY, _meta(placeholder="mm/dd/yyyy"))
        self.assertEqual(candidates[0], "07/14/1992")


class PageNeedsBirthdayTests(unittest.TestCase):
    """Deciding whether step 5 is still missing a required field."""

    def test_reports_a_missing_birthday_field(self):
        self.assertTrue(flow.profile_needs_birthday({"name": "filled"}, {"name", "birth"}))
        self.assertTrue(flow.profile_needs_birthday({}, {"name", "age"}))

    def test_not_needed_when_already_filled(self):
        self.assertFalse(flow.profile_needs_birthday({"name": "filled", "age": "33"}, {"name", "age"}))

    def test_not_needed_when_the_page_has_no_such_field(self):
        self.assertFalse(flow.profile_needs_birthday({}, {"name"}))

    def test_needed_when_the_split_fields_are_empty(self):
        self.assertTrue(flow.profile_needs_birthday({}, {"day", "month", "year"}))


class _FakeInput:
    """Minimal stand-in for a Playwright locator."""

    def __init__(self, accepts=None, *, keeps=True):
        self.value = ""
        self.accepts = accepts          # None = accepts anything
        self.keeps = keeps              # False = the page drops what we type
        self.fills: list[str] = []

    async def fill(self, value, timeout=None):
        self.fills.append(value)
        if self.keeps and (self.accepts is None or value in self.accepts):
            self.value = value
        else:
            self.value = ""

    async def input_value(self, timeout=None):
        return self.value


class FillProfileBirthdayTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, fields):
        classified: dict[str, list] = {}
        for kind, (loc, meta) in fields.items():
            classified[kind] = [(loc, meta)]

        async def safe_fill(loc, value, *, label=""):
            await loc.fill(value)
            return (await loc.input_value()).strip() == value.strip()

        filled: dict[str, str] = {}
        await flow.fill_profile_birthday(BIRTHDAY, classified, filled, safe_fill=safe_fill)
        return filled

    async def test_age_field_gets_the_computed_age(self):
        loc = _FakeInput()
        filled = await self._run({"age": (loc, _meta(name="age", type="number"))})
        self.assertEqual(filled, {"age": str(BIRTHDAY.age())})
        self.assertEqual(loc.value, str(BIRTHDAY.age()))

    async def test_date_field_uses_the_hinted_format(self):
        loc = _FakeInput()
        filled = await self._run({"birth": (loc, _meta(placeholder="mm/dd/yyyy"))})
        self.assertEqual(filled, {"birth": "07/14/1992"})
        self.assertEqual(loc.fills, ["07/14/1992"])

    async def test_falls_through_to_the_next_format_when_one_does_not_stick(self):
        # the field silently drops mm/dd/yyyy but keeps the ISO form
        loc = _FakeInput(accepts={"1992-07-14"})
        filled = await self._run({"birth": (loc, _meta(placeholder="mm/dd/yyyy"))})
        self.assertEqual(filled, {"birth": "1992-07-14"})
        self.assertEqual(loc.fills, ["07/14/1992", "1992-07-14"])

    async def test_native_date_input_gets_iso_immediately(self):
        loc = _FakeInput()
        filled = await self._run({"birth": (loc, _meta(type="date"))})
        self.assertEqual(filled, {"birth": "1992-07-14"})
        self.assertEqual(len(loc.fills), 1)

    async def test_split_fields_are_all_filled(self):
        day, month, year = _FakeInput(), _FakeInput(), _FakeInput()
        filled = await self._run({
            "day": (day, _meta(name="day")),
            "month": (month, _meta(name="month")),
            "year": (year, _meta(name="year")),
        })
        self.assertEqual(filled, {"day": "14", "month": "07", "year": "1992"})

    async def test_no_birthday_field_means_nothing_is_written(self):
        loc = _FakeInput()
        filled = await self._run({"name": (loc, _meta(name="name"))})
        self.assertEqual(filled, {})
        self.assertEqual(loc.fills, [])

    async def test_a_field_that_never_sticks_is_reported_as_unfilled(self):
        loc = _FakeInput(keeps=False)
        filled = await self._run({"birth": (loc, _meta(placeholder="yyyy-mm-dd"))})
        self.assertEqual(filled, {})
        self.assertGreater(len(loc.fills), 1, "every candidate format should be attempted")


if __name__ == "__main__":
    unittest.main()
