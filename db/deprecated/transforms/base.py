from abc import ABC, abstractmethod
from copy import deepcopy
import itertools

import sqlalchemy
from sqlalchemy import select

from db.deprecated.functions.operations.apply import apply_db_function_by_id, apply_db_function_spec_as_filter
from db.deprecated.functions.packed import DistinctArrayAgg
from db.deprecated import sort as rec_sort


class UniqueConstraintMapping:
    """
    A unique constraint mapping describes how a transform in a query maps a given input alias to an
    output alias, in the context of unique constraints.

    When a unique constraint on the input alias would be carried over to the output alias, both
    `input_alias` and `output_alias` are set. Otherwise, only the `output_alias` is set and
    `input_alias` is None. This signifies that `output_alias` cannot inherit a unique constraint.

    You can start with an initial column (initial alias) that is unique constrained. As it flows
    through the query's transformation pipeline it might be transformed and result in a new alias.
    Some of these new aliases will preserve/inherit that unique constraint, while others will not.

    For example, an output alias generated by Summarize transform's aggregation couldn't inherit a
    unique constraint.

    All UniqueConstraintMappings for all transforms in a query describe following things:
        - tells you what the input and output aliases are for a given transform;
        - whether a given alias is linked to a given initial column (initial-column-linked);
        - whether a given initial-column-linked alias is unique-constrained when that initial column
        is unique-constrained.
    """

    def __init__(self, input_alias, output_alias):
        self.input_alias = input_alias
        self.output_alias = output_alias


class Transform(ABC):
    type = None
    spec = None

    def __init__(
        self,
        spec,
    ):
        if self.type is None:
            raise ValueError(
                'Transform subclasses must define a type.'
            )
        if spec is None:
            raise ValueError(
                'A spec must be passed when instantiating a Transform subclass.'
            )
        self.spec = spec

    @abstractmethod
    def apply_to_relation(self, relation):
        return None

    def __eq__(self, other):
        return (
            type(self) is type(other)
            and self.__dict__ == other.__dict__
        )

    @property
    def map_of_output_alias_to_input_alias(self):
        """
        Expected to return a mapping of output alias to input alias.

        Useful when looking for ancestor aliases of a given alias.

        Note, the reverse mapping (from input aliases to output aliases) would be
        significantly different, because a single input alias can map to multiple output aliases.

        Note, this presumes that a single output alias maps to no more than a single input alias,
        but that's not true at least in the case of multi-column aggregation functions [0].

        [0] http://www.postgresonline.com/journal/archives/105-How-to-create-multi-column-aggregates.html
        """
        return dict()

    def get_output_aliases(self, input_aliases):
        uc_mappings = self.get_unique_constraint_mappings(input_aliases)
        return [
            uc_mapping.output_alias
            for uc_mapping
            in uc_mappings
        ]

    def get_unique_constraint_mappings(self, input_aliases):
        """
        By default, each input alias maps to an identically named output alias, and its unique
        constraint, if any, is carried over.
        """
        return [
            UniqueConstraintMapping(
                input_alias,
                input_alias,
            )
            for input_alias
            in input_aliases
        ]


class Filter(Transform):
    type = "filter"

    def apply_to_relation(self, relation):
        filter = self.spec
        enforce_relation_type_expectations(relation)
        executable = _to_executable(relation)
        if filter is not None:
            executable = apply_db_function_spec_as_filter(executable, filter)
        return _to_non_executable(executable)


class Order(Transform):
    type = "order"

    def apply_to_relation(self, relation):
        order_by = self.spec
        enforce_relation_type_expectations(relation)
        order_by = rec_sort.make_order_by_deterministic(relation, order_by)
        if order_by is not None:
            executable = rec_sort.apply_relation_sorting(relation, order_by)
        else:
            executable = _to_executable(relation)
        return _to_non_executable(executable)


class Limit(Transform):
    type = "limit"

    def apply_to_relation(self, relation):
        limit = self.spec
        executable = _to_executable(relation)
        executable = executable.limit(limit)
        return _to_non_executable(executable)


class Offset(Transform):
    type = "offset"

    def apply_to_relation(self, relation):
        offset = self.spec
        executable = _to_executable(relation)
        executable = executable.offset(offset)
        return _to_non_executable(executable)


class Summarize(Transform):
    """
    "spec": {
        "base_grouping_column": "col1",
        "grouping_expressions": [
            {
                "input_alias": "col1",
                "output_alias": "col1_alias",
                "preproc": None  # optional for grouping cols
            },
            {
                "input_alias": "col2",
                "output_alias": None,  # optional for grouping cols
                "preproc": "truncate_to_month"  # optional DBFunction id
            },
        ],
        "aggregation_expressions": [
            {
                "input_alias": "col3",
                "output_alias": "col3_alias",  # required for aggregation cols
                "function": "distinct_aggregate_to_array"  # required DBFunction id
            }
        ]
    }
    """
    type = "summarize"

    # When generating specs, largely in testing, we want predictable output aliases.
    default_group_output_alias_suffix = "_grouped"
    default_agg_output_alias_suffix = "_agged"

    @property
    def map_of_output_alias_to_input_alias(self):
        m = dict()
        grouping_expressions = self.spec['grouping_expressions']
        aggregation_expressions = self.spec['aggregation_expressions']
        all_expressions = grouping_expressions + aggregation_expressions
        for expression in all_expressions:
            expr_output_alias = expression.get('output_alias', None)
            expr_input_alias = expression.get('input_alias', None)
            m[expr_output_alias] = expr_input_alias
        return m

    def apply_to_relation(self, relation):

        def _get_grouping_column(col_spec):
            preproc_db_function_subclass_id = col_spec.get('preproc')
            input_alias = col_spec['input_alias']
            output_alias = col_spec['output_alias']
            sa_expression = relation.columns[input_alias]
            if preproc_db_function_subclass_id is not None:
                sa_expression = apply_db_function_by_id(
                    preproc_db_function_subclass_id,
                    [sa_expression],
                )
            sa_expression = sa_expression.label(output_alias)
            return sa_expression

        def _get_aggregation_column(relation, col_spec):
            input_alias = col_spec['input_alias']
            output_alias = col_spec['output_alias']
            agg_db_function_subclass_id = col_spec['function']
            column_to_aggregate = relation.columns[input_alias]
            sa_expression = apply_db_function_by_id(
                agg_db_function_subclass_id,
                [column_to_aggregate],
            )
            return sa_expression.label(output_alias)

        grouping_expressions = [
            _get_grouping_column(col_spec)
            for col_spec
            in self._grouping_col_specs
        ]
        aggregation_expressions = [
            _get_aggregation_column(relation, col_spec)
            for col_spec
            in self.aggregation_col_specs
        ]
        executable = (
            select(*grouping_expressions, *aggregation_expressions)
            .group_by(*grouping_expressions)
        )
        return _to_non_executable(executable)

    def get_unique_constraint_mappings(self, _):
        mappings_that_carry_uniqueness_over = [
            UniqueConstraintMapping(
                input_alias=col_spec['input_alias'],
                output_alias=col_spec['output_alias'],
            )
            for col_spec
            in self._grouping_col_specs
        ]
        mappings_that_dont_carry_uniqueness_over = [
            UniqueConstraintMapping(
                input_alias=None,
                output_alias=col_spec['output_alias'],
            )
            for col_spec
            in self.aggregation_col_specs
        ]
        return (
            mappings_that_carry_uniqueness_over
            + mappings_that_dont_carry_uniqueness_over
        )

    def get_new_with_aliases_added_to_group_by(self, aliases):
        def get_col_spec_from_alias(alias):
            return dict(
                input_alias=alias,
                output_alias=alias + default_suffix,
            )
        spec_field = 'grouping_expressions'
        default_suffix = self.default_group_output_alias_suffix
        return _add_aliases_to_summarization_expr_field(
            summarization=self,
            spec_field=spec_field,
            aliases=aliases,
            get_col_spec_from_alias=get_col_spec_from_alias,
        )

    def get_new_with_aliases_added_to_agg_on(self, aliases):
        def get_col_spec_from_alias(alias):
            return dict(
                input_alias=alias,
                output_alias=alias + default_suffix,
                function=default_aggregation_fn
            )
        spec_field = 'aggregation_expressions'
        default_suffix = self.default_agg_output_alias_suffix
        default_aggregation_fn = DistinctArrayAgg.id
        return _add_aliases_to_summarization_expr_field(
            summarization=self,
            spec_field=spec_field,
            aliases=aliases,
            get_col_spec_from_alias=get_col_spec_from_alias,
        )

    @property
    def base_grouping_column(self):
        return self.spec['base_grouping_column']

    @property
    def aggregation_output_aliases(self):
        return [
            col_spec['output_alias']
            for col_spec
            in self.aggregation_col_specs
        ]

    @property
    def grouping_output_aliases(self):
        return [
            col_spec['output_alias']
            for col_spec
            in self._grouping_col_specs
        ]

    @property
    def grouping_input_aliases(self):
        return [
            col_spec['input_alias']
            for col_spec
            in self._grouping_col_specs
        ]

    @property
    def aggregation_input_aliases(self):
        return [
            col_spec['input_alias']
            for col_spec
            in self.aggregation_col_specs
        ]

    @property
    def _grouping_col_specs(self):
        return self.spec.get("grouping_expressions", [])

    @property
    def aggregation_col_specs(self):
        return self.spec.get("aggregation_expressions", [])


def _add_aliases_to_summarization_expr_field(
    summarization, spec_field, aliases, get_col_spec_from_alias
):
    """
    Returns new summarization with aliases added to `spec_field`.

    This function will apply `get_col_spec_from_alias` to each column alias in `aliases`,
    and add the results to the chosen `spec_field` in a copy of `summarization`, returning the copy.
    """
    summarization = deepcopy(summarization)
    expressions_to_add = [
        get_col_spec_from_alias(alias)
        for alias
        in aliases
    ]
    existing_expressions = summarization.spec.get(spec_field, [])
    new_expressions = list(
        itertools.chain(
            existing_expressions, expressions_to_add
        )
    )
    summarization.spec[spec_field] = new_expressions
    return summarization


class HideColumns(Transform):
    """
    Selects every column in the transform, except for the columns specified in the spec.

    We're implementing this transform in terms of selecting columns (since Postgres doesn't really
    support "selecting everything except").
    """
    type = "hide"

    def apply_to_relation(self, relation):
        input_aliases = [
            col.name
            for col
            in relation.c
        ]
        columns_to_select = self.get_columns_to_select(input_aliases)
        select_transform = SelectSubsetOfColumns(columns_to_select)
        relation = select_transform.apply_to_relation(relation)
        return relation

    def get_unique_constraint_mappings(self, input_aliases):
        columns_to_select = self.get_columns_to_select(input_aliases)
        return [
            UniqueConstraintMapping(
                column_to_select,
                column_to_select,
            )
            for column_to_select
            in columns_to_select
        ]

    def get_columns_to_select(self, input_aliases):
        return [
            column
            for column
            in input_aliases
            if column not in self._columns_to_hide
        ]

    @property
    def _columns_to_hide(self):
        return self.spec


class SelectSubsetOfColumns(Transform):
    type = "select"

    def apply_to_relation(self, relation):
        sa_columns_to_select = self._get_sa_columns_to_select(relation)
        if sa_columns_to_select:
            executable = select(*sa_columns_to_select).select_from(relation)
            return _to_non_executable(executable)
        else:
            return relation

    def get_unique_constraint_mappings(self, _):
        # We presume that when we're looking at uc mappings, the raw spec will always be string
        # names.
        column_names_to_select = self._raw_columns_to_select
        return [
            UniqueConstraintMapping(
                column_names_to_select,
                column_names_to_select,
            )
            for column_names_to_select
            in column_names_to_select
        ]

    def _get_sa_columns_to_select(self, relation):
        return tuple(
            _make_sure_sa_col_expr(raw_col, relation)
            for raw_col
            in self._raw_columns_to_select
        )

    @property
    def _raw_columns_to_select(self):
        """
        The spec will be a list whose items will be SQLAlchemy column expressions and/or string
        names.

        This accepts SQLAlchemy column expressions so that we can count records by doing
        SelectSubsetOfColumns(count(1).label("_count")).
        """
        return self.spec or []


def _make_sure_sa_col_expr(raw_col, relation):
    if isinstance(raw_col, str):
        # If raw_col is a string, we consider it a column name and look up an SQL column using it
        col_name = raw_col
        sa_col = relation.c[col_name]
        return sa_col
    else:
        # If raw_col isn't a string, we presume it's an SA column expression
        sa_col = raw_col
        return sa_col


def _to_executable(relation):
    """
    Executables are a subset of Selectables.
    """
    assert isinstance(relation, sqlalchemy.sql.expression.Selectable)
    if isinstance(relation, sqlalchemy.sql.expression.Executable):
        return relation
    else:
        return select(relation)


def _to_non_executable(relation):
    """
    Non-executables are Selectables that are not Executables. Non-executables are more portable
    than Executables.
    """
    assert isinstance(relation, sqlalchemy.sql.expression.Selectable)
    if isinstance(relation, sqlalchemy.sql.expression.Executable):
        return relation.cte()
    else:
        return relation


def enforce_relation_type_expectations(relation):
    """
    The convention being enforced is to pass around instances of Selectables that are not
    Executables. We need to do it one way, for the sake of uniformity and compatibility.
    It's not the other way around, because if you pass around Executables, composition sometimes
    works differently.

    This method is a development tool mostly, probably shouldn't exist in actual production.
    """
    assert isinstance(relation, sqlalchemy.sql.expression.Selectable)
    assert not isinstance(relation, sqlalchemy.sql.expression.Executable)
