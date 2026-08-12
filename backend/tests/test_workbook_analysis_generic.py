"""Synthetic, domain-neutral planner tests for structured workbook analysis."""

from decimal import Decimal
from unittest import TestCase

from app.services.workbook_analysis import (
    RowRecord,
    WorkbookScope,
    _operation,
    _numeric_condition,
    _numeric_matches,
    _number,
    _answer_from_rows,
    _tokens,
    _plan_for_scope,
    _row_filters,
)


def _scope(rows: list[dict[str, object]], schema: dict[str, str]) -> WorkbookScope:
    """Create a fixture-independent workbook scope with schema supplied by the test."""
    return WorkbookScope(
        document_id=1,
        version_id=1,
        filename="neutral-data.xlsx",
        rows=[RowRecord("Data", index, values) for index, values in enumerate(rows, start=2)],
        sheet_names=["Data"],
        schema={header: {"type": value, "sheet": "Data"} for header, value in schema.items()},
    )


class GenericWorkbookPlanningTests(TestCase):
    """Classify common table questions from tokens, values, and schema only."""

    def test_operation_categories_are_domain_neutral(self) -> None:
        """Intent words, not business nouns, select deterministic plan families."""
        cases = {
            "What is the total measure?": "total",
            "What is the average measure?": "average",
            "How many records?": "count",
            "What is the percentage rate?": "average",
            "Show values between 3 and 9": "list",
            "Compare actual versus planned": "comparison",
            "List the labels": "list",
            "Group measure by category": "group",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(_operation(question), expected)

    def test_plural_column_words_match_schema_evidence(self) -> None:
        """Generic plural forms such as categories resolve to their column singular."""
        self.assertIn("category", _tokens("How many distinct categories?"))

    def test_lookup_and_filter_use_matching_schema_values(self) -> None:
        """A label/value lookup needs no HR-specific field names."""
        scope = _scope(
            [{"Person": "Riya", "Assignment": "Delta"}],
            {"Person": "identifier", "Assignment": "text"},
        )

        plan = _plan_for_scope(scope, "Show assignment for Riya", explicit_scope=True)

        self.assertEqual(plan.intent, "records")
        self.assertEqual(plan.value_column, "Assignment")
        self.assertEqual(plan.filters, {"Person": {"riya"}})

    def test_numeric_column_is_selected_from_values_not_business_nouns(self) -> None:
        """A numeric custom field remains aggregatable despite a text-like schema label."""
        scope = _scope(
            [{"Segment": "A", "Measure": 4}, {"Segment": "B", "Measure": "8"}],
            {"Segment": "category", "Measure": "text"},
        )

        plan = _plan_for_scope(scope, "What is the total measure?", explicit_scope=True)

        self.assertEqual(plan.intent, "total")
        self.assertEqual(plan.value_column, "Measure")

    def test_range_uses_numeric_values_for_any_column_name(self) -> None:
        """Range classification applies to observed numeric values without a domain lexicon."""
        scope = _scope(
            [{"Code": "A", "Level": 2}, {"Code": "B", "Level": 6}],
            {"Code": "identifier", "Level": "number"},
        )

        plan = _plan_for_scope(scope, "Show level between 3 and 9", explicit_scope=True)

        self.assertEqual(plan.intent, "records")
        self.assertEqual(plan.numeric_filter[0:2] if plan.numeric_filter else None, ("Level", "between"))

    def test_range_phrases_have_explicit_boundary_semantics(self) -> None:
        """Each supported phrase maps to a deterministic Decimal comparison operator."""
        cases = {
            "Show metric between 2.5 and 4.5": ("between", Decimal("2.5"), Decimal("4.5")),
            "Show metric from 2.5 to 4.5": ("between", Decimal("2.5"), Decimal("4.5")),
            "Show metric greater than 2.5": ("gt", Decimal("2.5"), None),
            "Show metric less than 2.5": ("lt", Decimal("2.5"), None),
            "Show metric at least 2.5": ("ge", Decimal("2.5"), None),
            "Show metric at most 2.5": ("le", Decimal("2.5"), None),
            "Show metric above 2.5": ("gt", Decimal("2.5"), None),
            "Show metric below 2.5": ("lt", Decimal("2.5"), None),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(_numeric_condition(question), expected)

        self.assertTrue(_numeric_matches("2.5", ("between", Decimal("2.5"), Decimal("4.5"))))
        self.assertFalse(_numeric_matches("2.5", ("gt", Decimal("2.5"), None)))
        self.assertTrue(_numeric_matches("2.5", ("ge", Decimal("2.5"), None)))

    def test_percentage_fraction_values_use_percentage_point_bounds(self) -> None:
        """A 15 percent query matches stored Decimal-style fraction values deterministically."""
        scope = _scope(
            [{"Completion": Decimal("0.10")}, {"Completion": Decimal("0.15")}, {"Completion": Decimal("0.20")}],
            {"Completion": "number"},
        )

        plan = _plan_for_scope(scope, "Show completion from 10% to 20%", explicit_scope=True)

        self.assertEqual(plan.intent, "records")
        self.assertEqual(
            plan.numeric_filter,
            ("Completion", "between", Decimal("0.1"), Decimal("0.2")),
        )

    def test_currency_and_numeric_strings_keep_decimal_precision(self) -> None:
        """Currency-formatted strings are parsed without floating-point conversion."""
        self.assertEqual(_number("$1,234.50"), Decimal("1234.50"))
        self.assertEqual(_number("(₹1,234.50)"), Decimal("-1234.50"))
        self.assertTrue(_numeric_matches("$1,234.50", ("gt", Decimal("1234.49"), None)))

    def test_comparison_returns_record_plan_from_compared_columns(self) -> None:
        """Comparison remains a record-style plan rather than an arbitrary list column."""
        scope = _scope(
            [{"Actual": 9, "Planned": 10}],
            {"Actual": "number", "Planned": "number"},
        )

        plan = _plan_for_scope(scope, "Compare actual versus planned", explicit_scope=True)

        self.assertEqual(plan.intent, "comparison")

    def test_grouping_uses_numeric_and_category_schema_evidence(self) -> None:
        """Generic grouping works across arbitrary measure/category labels."""
        scope = _scope(
            [{"Category": "North", "Measure": 3}, {"Category": "South", "Measure": 5}],
            {"Category": "category", "Measure": "number"},
        )

        plan = _plan_for_scope(scope, "Group measure by category", explicit_scope=True)

        self.assertEqual((plan.intent, plan.value_column, plan.group_column), ("group", "Measure", "Category"))

    def test_filtered_aggregate_retains_explicit_entity_projection_for_follow_up(self) -> None:
        """A follow-up can replay matched entities without treating a numeric measure as one."""
        scope = _scope(
            [{"Asset": "A-1", "Cost": 20}, {"Asset": "A-2", "Cost": 30}],
            {"Asset": "text", "Cost": "number"},
        )
        plan = _plan_for_scope(scope, "How many assets cost between 10 and 40?", explicit_scope=True)

        result = _answer_from_rows(scope, scope.rows, plan, "How many assets cost between 10 and 40?")
        context = result["_context"]

        self.assertEqual(context["display_column"], "Asset")
        self.assertEqual(context["contributing_values"], ["A-1", "A-2"])

    def test_in_out_filter_uses_unique_matching_values_not_header_nouns(self) -> None:
        """A unique IN/OUT column is usable even when it is not called attendance/direction."""
        rows = [
            RowRecord("Data", 2, {"Gate Flag": "IN", "Recorded Time": "08:00"}),
            RowRecord("Data", 3, {"Gate Flag": "OUT", "Recorded Time": "17:00"}),
        ]

        self.assertEqual(_row_filters(rows, "Show time in"), {"Gate Flag": {"in"}})
