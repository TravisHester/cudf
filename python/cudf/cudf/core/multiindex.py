# Copyright (c) 2019-2020, NVIDIA CORPORATION.
from __future__ import annotations

import itertools
import numbers
import pickle
import warnings
from collections.abc import Sequence
from typing import Any, List, Mapping, Tuple, Union

import cupy
import numpy as np
import pandas as pd
from pandas._config import get_option

import cudf
from cudf import _lib as libcudf
from cudf._typing import DataFrameOrSeries
from cudf.core._compat import PANDAS_GE_120
from cudf.core.column import as_column, column
from cudf.core.frame import SingleColumnFrame
from cudf.core.index import BaseIndex, as_index
from cudf.utils.utils import _maybe_indices_to_slice


class MultiIndex(BaseIndex):
    """A multi-level or hierarchical index.

    Provides N-Dimensional indexing into Series and DataFrame objects.

    Parameters
    ----------
    levels : sequence of arrays
        The unique labels for each level.
    labels : sequence of arrays
        labels is depreciated, please use levels
    codes: sequence of arrays
        Integers for each level designating which label at each location.
    sortorder : optional int
        Not yet supported
    names: optional sequence of objects
        Names for each of the index levels.
    copy : bool, default False
        Copy the levels and codes.
    verify_integrity : bool, default True
        Check that the levels/codes are consistent and valid.
        Not yet supported

    Returns
    -------
    MultiIndex

    Examples
    --------
    >>> import cudf
    >>> cudf.MultiIndex(
    ... levels=[[1, 2], ['blue', 'red']], codes=[[0, 0, 1, 1], [1, 0, 1, 0]])
    MultiIndex([(1,  'red'),
                (1, 'blue'),
                (2,  'red'),
                (2, 'blue')],
               )
    """

    def __init__(
        self,
        levels=None,
        codes=None,
        sortorder=None,
        labels=None,
        names=None,
        dtype=None,
        copy=False,
        name=None,
        **kwargs,
    ):

        if sortorder is not None:
            raise NotImplementedError("sortorder is not yet supported")

        if name is not None:
            raise NotImplementedError(
                "Use `names`, `name` is not yet supported"
            )

        super().__init__()

        if copy:
            if isinstance(codes, cudf.DataFrame):
                codes = codes.copy(deep=True)
            if len(levels) > 0 and isinstance(levels[0], cudf.Series):
                levels = [level.copy(deep=True) for level in levels]

        self._name = None

        column_names = []
        if labels:
            warnings.warn(
                "the 'labels' keyword is deprecated, use 'codes' " "instead",
                FutureWarning,
            )
        if labels and not codes:
            codes = labels

        # early termination enables lazy evaluation of codes
        if "source_data" in kwargs:
            source_data = kwargs["source_data"].copy(deep=False)
            source_data.reset_index(drop=True, inplace=True)

            if isinstance(source_data, pd.DataFrame):
                nan_as_null = kwargs.get("nan_as_null", None)
                source_data = cudf.DataFrame.from_pandas(
                    source_data, nan_as_null=nan_as_null
                )
            names = names if names is not None else source_data._data.names
            # if names are unique
            # try using those as the source_data column names:
            if len(dict.fromkeys(names)) == len(names):
                source_data.columns = names
            self._data = source_data._data
            self.names = names
            self._codes = codes
            self._levels = levels
            return

        # name setup
        if isinstance(names, (Sequence, pd.core.indexes.frozen.FrozenList,),):
            if sum(x is None for x in names) > 1:
                column_names = list(range(len(codes)))
            else:
                column_names = names
        elif names is None:
            column_names = list(range(len(codes)))
        else:
            column_names = names

        if len(levels) == 0:
            raise ValueError("Must pass non-zero number of levels/codes")

        if not isinstance(codes, cudf.DataFrame) and not isinstance(
            codes[0], (Sequence, np.ndarray)
        ):
            raise TypeError("Codes is not a Sequence of sequences")

        if isinstance(codes, cudf.DataFrame):
            self._codes = codes
        elif len(levels) == len(codes):
            self._codes = cudf.DataFrame()
            for i, codes in enumerate(codes):
                name = column_names[i] or i
                codes = column.as_column(codes)
                self._codes[name] = codes.astype(np.int64)
        else:
            raise ValueError(
                "MultiIndex has unequal number of levels and "
                "codes and is inconsistent!"
            )

        self._levels = [cudf.Series(level) for level in levels]
        self._validate_levels_and_codes(self._levels, self._codes)

        source_data = cudf.DataFrame()
        for i, name in enumerate(self._codes.columns):
            codes = as_index(self._codes[name]._column)
            if -1 in self._codes[name].values:
                # Must account for null(s) in _source_data column
                level = cudf.DataFrame(
                    {name: [None] + list(self._levels[i])},
                    index=range(-1, len(self._levels[i])),
                )
            else:
                level = cudf.DataFrame({name: self._levels[i]})

            source_data[name] = libcudf.copying.gather(
                level, codes._data.columns[0]
            )[0][name]

        self._data = source_data._data
        self.names = names

    @property
    def names(self):
        return self._names

    @names.setter
    def names(self, value):
        value = [None] * self.nlevels if value is None else value
        assert len(value) == self.nlevels

        if len(value) == len(set(value)):
            # IMPORTANT: if the provided names are unique,
            # we reconstruct self._data with the names as keys.
            # If they are not unique, the keys of self._data
            # and self._names will be different, which can lead
            # to unexpected behaviour in some cases. This is
            # definitely buggy, but we can't disallow non-unique
            # names either...
            self._data = self._data.__class__._create_unsafe(
                dict(zip(value, self._data.values())),
                level_names=self._data.level_names,
            )
        self._names = pd.core.indexes.frozen.FrozenList(value)

    @property
    def _num_columns(self):
        # MultiIndex is not a single-columned frame.
        return super(SingleColumnFrame, self)._num_columns

    def rename(self, names, inplace=False):
        """
        Alter MultiIndex level names

        Parameters
        ----------
        names : list of label
            Names to set, length must be the same as number of levels
        inplace : bool, default False
            If True, modifies objects directly, otherwise returns a new
            ``MultiIndex`` instance

        Returns
        --------
        None or MultiIndex

        Examples
        --------
        Renaming each levels of a MultiIndex to specified name:

        >>> midx = cudf.MultiIndex.from_product(
                [('A', 'B'), (2020, 2021)], names=['c1', 'c2'])
        >>> midx.rename(['lv1', 'lv2'])
        MultiIndex([('A', 2020),
                    ('A', 2021),
                    ('B', 2020),
                    ('B', 2021)],
                names=['lv1', 'lv2'])
        >>> midx.rename(['lv1', 'lv2'], inplace=True)
        >>> midx
        MultiIndex([('A', 2020),
                    ('A', 2021),
                    ('B', 2020),
                    ('B', 2021)],
                names=['lv1', 'lv2'])

        ``names`` argument must be a list, and must have same length as
        ``MultiIndex.levels``:

        >>> midx.rename(['lv0'])
        Traceback (most recent call last):
        ValueError: Length of names must match number of levels in MultiIndex.

        """
        return self.set_names(names, level=None, inplace=inplace)

    def set_names(self, names, level=None, inplace=False):
        if (
            level is not None
            and not cudf.utils.dtypes.is_list_like(level)
            and cudf.utils.dtypes.is_list_like(names)
        ):
            raise TypeError(
                "Names must be a string when a single level is provided."
            )

        if (
            not cudf.utils.dtypes.is_list_like(names)
            and level is None
            and self.nlevels > 1
        ):
            raise TypeError("Must pass list-like as `names`.")

        if not cudf.utils.dtypes.is_list_like(names):
            names = [names]
        if level is not None and not cudf.utils.dtypes.is_list_like(level):
            level = [level]

        if level is not None and len(names) != len(level):
            raise ValueError("Length of names must match length of level.")
        if level is None and len(names) != self.nlevels:
            raise ValueError(
                "Length of names must match number of levels in MultiIndex."
            )

        if level is None:
            level = range(self.nlevels)
        else:
            level = [self._level_index_from_level(lev) for lev in level]

        existing_names = list(self.names)
        for i, l in enumerate(level):
            existing_names[l] = names[i]
        names = existing_names

        return self._set_names(names=names, inplace=inplace)

    # TODO: This type ignore is indicating a real problem, which is that
    # MultiIndex should not be inheriting from SingleColumnFrame, but fixing
    # that will have to wait until we reshuffle the Index hierarchy.
    @classmethod
    def _from_data(  # type: ignore
        cls, data: Mapping, index=None
    ) -> MultiIndex:
        return cls.from_frame(cudf.DataFrame._from_data(data))

    @property
    def shape(self):
        return (self._data.nrows, len(self._data.names))

    @property
    def _source_data(self):
        return cudf.DataFrame._from_data(data=self._data)

    @_source_data.setter
    def _source_data(self, value):
        self._data = value._data
        self._compute_levels_and_codes()

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    def _validate_levels_and_codes(self, levels, codes):
        if len(levels) != len(codes.columns):
            raise ValueError(
                "MultiIndex has unequal number of levels and "
                "codes and is inconsistent!"
            )
        code_length = len(codes[codes.columns[0]])
        for index, code in enumerate(codes):
            if code_length != len(codes[code]):
                raise ValueError(
                    "MultiIndex length of codes does not match "
                    "and is inconsistent!"
                )
        for index, code in enumerate(codes):
            if codes[code].max() > len(levels[index]) - 1:
                raise ValueError(
                    "MultiIndex code %d contains value %d larger "
                    "than maximum level size at this position"
                )

    def copy(
        self,
        names=None,
        dtype=None,
        levels=None,
        codes=None,
        deep=False,
        name=None,
    ):
        """Returns copy of MultiIndex object.

        Returns a copy of `MultiIndex`. The `levels` and `codes` value can be
        set to the provided parameters. When they are provided, the returned
        MultiIndex is always newly constructed.

        Parameters
        ----------
        names : sequence of objects, optional (default None)
            Names for each of the index levels.
        dtype : object, optional (default None)
            MultiIndex dtype, only supports None or object type
        levels : sequence of arrays, optional (default None)
            The unique labels for each level. Original values used if None.
        codes : sequence of arrays, optional (default None)
            Integers for each level designating which label at each location.
            Original values used if None.
        deep : Bool (default False)
            If True, `._data`, `._levels`, `._codes` will be copied. Ignored if
            `levels` or `codes` are specified.
        name : object, optional (defulat None)
            To keep consistent with `Index.copy`, should not be used.

        Returns
        -------
        Copy of MultiIndex Instance

        Examples
        --------
        >>> df = cudf.DataFrame({'Close': [3400.00, 226.58, 3401.80, 228.91]})
        >>> idx1 = cudf.MultiIndex(
        ... levels=[['2020-08-27', '2020-08-28'], ['AMZN', 'MSFT']],
        ... codes=[[0, 0, 1, 1], [0, 1, 0, 1]],
        ... names=['Date', 'Symbol'])
        >>> idx2 = idx1.copy(
        ... levels=[['day1', 'day2'], ['com1', 'com2']],
        ... codes=[[0, 0, 1, 1], [0, 1, 0, 1]],
        ... names=['col1', 'col2'])

        >>> df.index = idx1
        >>> df
                             Close
        Date       Symbol
        2020-08-27 AMZN    3400.00
                   MSFT     226.58
        2020-08-28 AMZN    3401.80
                   MSFT     228.91

        >>> df.index = idx2
        >>> df
                     Close
        col1 col2
        day1 com1  3400.00
             com2   226.58
        day2 com1  3401.80
             com2   228.91

        """

        dtype = object if dtype is None else dtype
        if not pd.core.dtypes.common.is_object_dtype(dtype):
            raise TypeError("Dtype for MultiIndex only supports object type.")

        # ._data needs to be rebuilt
        if levels is not None or codes is not None:
            if self._levels is None or self._codes is None:
                self._compute_levels_and_codes()
            levels = self._levels if levels is None else levels
            codes = self._codes if codes is None else codes
            names = self.names if names is None else names

            mi = MultiIndex(levels=levels, codes=codes, names=names, copy=deep)
            return mi

        mi = MultiIndex(source_data=self._source_data.copy(deep=deep))
        if self._levels is not None:
            mi._levels = [s.copy(deep) for s in self._levels]
        if self._codes is not None:
            mi._codes = self._codes.copy(deep)
        if names is not None:
            mi.names = names
        elif self.names is not None:
            mi.names = self.names.copy()

        return mi

    def deepcopy(self):
        return self.copy(deep=True)

    def __copy__(self):
        return self.copy(deep=True)

    def _popn(self, n):
        """ Returns a copy of this index without the left-most n values.

        Removes n names, labels, and codes in order to build a new index
        for results.
        """
        result = MultiIndex(source_data=self._source_data.iloc[:, n:])
        if self.names is not None:
            result.names = self.names[n:]
        return result

    def __repr__(self):
        max_seq_items = get_option("display.max_seq_items") or len(self)

        if len(self) > max_seq_items:
            n = int(max_seq_items / 2) + 1
            # TODO: Update the following two arange calls to
            # a single arange call once arange has support for
            # a vector start/end points.
            indices = cudf.core.column.arange(start=0, stop=n, step=1)
            indices = indices.append(
                cudf.core.column.arange(
                    start=len(self) - n, stop=len(self), step=1
                )
            )
            preprocess = self.take(indices)
        else:
            preprocess = self.copy(deep=False)

        cols_nulls = [
            preprocess._source_data._data[col].has_nulls
            for col in preprocess._source_data._data
        ]
        if any(cols_nulls):
            preprocess_df = preprocess._source_data
            for name, col in preprocess_df._data.items():
                if isinstance(
                    col,
                    (
                        cudf.core.column.datetime.DatetimeColumn,
                        cudf.core.column.timedelta.TimeDeltaColumn,
                    ),
                ):
                    preprocess_df[name] = col.astype("str").fillna(
                        cudf._NA_REP
                    )
                else:
                    preprocess_df[name] = col

            tuples_list = list(
                zip(
                    *list(
                        map(lambda val: pd.NA if val is None else val, col)
                        for col in preprocess_df.to_arrow()
                        .to_pydict()
                        .values()
                    )
                )
            )

            if PANDAS_GE_120:
                # TODO: Remove this whole `if` block,
                # this is a workaround for the following issue:
                # https://github.com/pandas-dev/pandas/issues/39984
                temp_df = preprocess._source_data

                preprocess_pdf = pd.DataFrame()
                for col in temp_df.columns:
                    if temp_df[col].dtype.kind == "f":
                        preprocess_pdf[col] = temp_df[col].to_pandas(
                            nullable=False
                        )
                    else:
                        preprocess_pdf[col] = temp_df[col].to_pandas(
                            nullable=True
                        )

                preprocess_pdf.columns = preprocess.names
                preprocess = pd.MultiIndex.from_frame(preprocess_pdf)
            else:
                preprocess = preprocess.to_pandas(nullable=True)
            preprocess.values[:] = tuples_list
        else:
            preprocess = preprocess.to_pandas(nullable=True)

        output = preprocess.__repr__()
        output_prefix = self.__class__.__name__ + "("
        output = output.lstrip(output_prefix)
        lines = output.split("\n")

        if len(lines) > 1:
            if "length=" in lines[-1] and len(self) != len(preprocess):
                last_line = lines[-1]
                length_index = last_line.index("length=")
                last_line = last_line[:length_index] + f"length={len(self)})"
                lines = lines[:-1]
                lines.append(last_line)

        data_output = "\n".join(lines)
        return output_prefix + data_output

    @classmethod
    def from_arrow(cls, table):
        """
        Convert PyArrow Table to MultiIndex

        Parameters
        ----------
        table : PyArrow Table
            PyArrow Object which has to be converted to MultiIndex

        Returns
        -------
        cudf MultiIndex

        Examples
        --------
        >>> import cudf
        >>> import pyarrow as pa
        >>> tbl = pa.table({"a":[1, 2, 3], "b":["a", "b", "c"]})
        >>> cudf.MultiIndex.from_arrow(tbl)
        MultiIndex([(1, 'a'),
                    (2, 'b'),
                    (3, 'c')],
                   names=['a', 'b'])
        """

        return super(SingleColumnFrame, cls).from_arrow(table)

    def to_arrow(self):
        """Convert MultiIndex to PyArrow Table

        Returns
        -------
        PyArrow Table

        Examples
        --------
        >>> import cudf
        >>> df = cudf.DataFrame({"a":[1, 2, 3], "b":[2, 3, 4]})
        >>> mindex = cudf.Index(df)
        >>> mindex
        MultiIndex([(1, 2),
                    (2, 3),
                    (3, 4)],
                   names=['a', 'b'])
        >>> mindex.to_arrow()
        pyarrow.Table
        a: int64
        b: int64
        >>> mindex.to_arrow()['a']
        <pyarrow.lib.ChunkedArray object at 0x7f5c6b71fad0>
        [
            [
                1,
                2,
                3
            ]
        ]
        """

        return super(SingleColumnFrame, self).to_arrow()

    @property
    def codes(self):
        """
        Returns the codes of the underlying MultiIndex.

        Examples
        --------
        >>> import cudf
        >>> df = cudf.DataFrame({'a':[1, 2, 3], 'b':[10, 11, 12]})
        >>> cudf.MultiIndex.from_frame(df)
        MultiIndex([(1, 10),
                    (2, 11),
                    (3, 12)],
                names=['a', 'b'])
        >>> midx = cudf.MultiIndex.from_frame(df)
        >>> midx
        MultiIndex([(1, 10),
                    (2, 11),
                    (3, 12)],
                names=['a', 'b'])
        >>> midx.codes
           a  b
        0  0  0
        1  1  1
        2  2  2
        """
        if self._codes is None:
            self._compute_levels_and_codes()
        return self._codes

    @property
    def nlevels(self):
        """
        Integer number of levels in this MultiIndex.
        """
        return self._source_data.shape[1]

    @property
    def levels(self):
        """
        Returns list of levels in the MultiIndex

        Returns
        -------
        List of Series objects

        Examples
        --------
        >>> import cudf
        >>> df = cudf.DataFrame({'a':[1, 2, 3], 'b':[10, 11, 12]})
        >>> cudf.MultiIndex.from_frame(df)
        MultiIndex([(1, 10),
                    (2, 11),
                    (3, 12)],
                names=['a', 'b'])
        >>> midx = cudf.MultiIndex.from_frame(df)
        >>> midx
        MultiIndex([(1, 10),
                    (2, 11),
                    (3, 12)],
                names=['a', 'b'])
        >>> midx.levels
        [0    1
        1    2
        2    3
        dtype: int64, 0    10
        1    11
        2    12
        dtype: int64]
        """
        if self._levels is None:
            self._compute_levels_and_codes()
        return self._levels

    @property
    def labels(self):
        warnings.warn(
            "This feature is deprecated in pandas and will be"
            "dropped from cudf as well.",
            FutureWarning,
        )
        return self.codes

    @property
    def ndim(self):
        """Dimension of the data. For MultiIndex ndim is always 2.
        """
        return 2

    def _get_level_label(self, level):
        """ Get name of the level.

        Parameters
        ----------
        level : int or level name
            if level is name, it will be returned as it is
            else if level is index of the level, then level
            label will be returned as per the index.
        """

        if level in self._data.names:
            return level
        else:
            return self._data.names[level]

    def isin(self, values, level=None):
        """Return a boolean array where the index values are in values.

        Compute boolean array of whether each index value is found in
        the passed set of values. The length of the returned boolean
        array matches the length of the index.

        Parameters
        ----------
        values : set, list-like, Index or Multi-Index
            Sought values.
        level : str or int, optional
            Name or position of the index level to use (if the index
            is a MultiIndex).

        Returns
        -------
        is_contained : cupy array
            CuPy array of boolean values.

        Notes
        -------
        When `level` is None, `values` can only be MultiIndex, or a
        set/list-like tuples.
        When `level` is provided, `values` can be Index or MultiIndex,
        or a set/list-like tuples.

        Examples
        --------
        >>> import cudf
        >>> import pandas as pd
        >>> midx = cudf.from_pandas(pd.MultiIndex.from_arrays([[1,2,3],
        ...                                  ['red', 'blue', 'green']],
        ...                                  names=('number', 'color')))
        >>> midx
        MultiIndex([(1,   'red'),
                    (2,  'blue'),
                    (3, 'green')],
                   names=['number', 'color'])

        Check whether the strings in the 'color' level of the MultiIndex
        are in a list of colors.

        >>> midx.isin(['red', 'orange', 'yellow'], level='color')
        array([ True, False, False])

        To check across the levels of a MultiIndex, pass a list of tuples:

        >>> midx.isin([(1, 'red'), (3, 'red')])
        array([ True, False, False])
        """
        from cudf.utils.dtypes import is_list_like

        if level is None:
            if isinstance(values, cudf.MultiIndex):
                values_idx = values
            elif (
                (
                    isinstance(
                        values,
                        (
                            cudf.Series,
                            cudf.Index,
                            cudf.DataFrame,
                            column.ColumnBase,
                        ),
                    )
                )
                or (not is_list_like(values))
                or (
                    is_list_like(values)
                    and len(values) > 0
                    and not isinstance(values[0], tuple)
                )
            ):
                raise TypeError(
                    "values need to be a Multi-Index or set/list-like tuple "
                    "squences  when `level=None`."
                )
            else:
                values_idx = cudf.MultiIndex.from_tuples(
                    values, names=self.names
                )

            res = []
            for name in self.names:
                level_idx = self.get_level_values(name)
                value_idx = values_idx.get_level_values(name)

                existence = level_idx.isin(value_idx)
                res.append(existence)

            result = res[0]
            for i in res[1:]:
                result = result & i
        else:
            level_series = self.get_level_values(level)
            result = level_series.isin(values)

        return result

    def mask(self, cond, other=None, inplace=False):
        raise NotImplementedError(
            ".mask is not supported for MultiIndex operations"
        )

    def where(self, cond, other=None, inplace=False):
        raise NotImplementedError(
            ".where is not supported for MultiIndex operations"
        )

    def _compute_levels_and_codes(self):
        levels = []

        codes = cudf.DataFrame()
        for name in self._source_data.columns:
            code, cats = self._source_data[name].factorize()
            codes[name] = code.astype(np.int64)
            cats = cudf.Series(cats, name=None)
            levels.append(cats)

        self._levels = levels
        self._codes = codes

    def _compute_validity_mask(self, index, row_tuple, max_length):
        """ Computes the valid set of indices of values in the lookup
        """
        lookup = cudf.DataFrame()
        for idx, row in enumerate(row_tuple):
            if isinstance(row, slice) and row == slice(None):
                continue
            lookup[index._source_data.columns[idx]] = cudf.Series(row)
        data_table = cudf.concat(
            [
                index._source_data,
                cudf.DataFrame(
                    {
                        "idx": cudf.Series(
                            column.arange(len(index._source_data))
                        )
                    }
                ),
            ],
            axis=1,
        )
        result = lookup.merge(data_table)["idx"]
        # Avoid computing levels unless the result of the merge is empty,
        # which suggests that a KeyError should be raised.
        if len(result) == 0:
            for idx, row in enumerate(row_tuple):
                if row == slice(None):
                    continue
                if row not in index.levels[idx]._column:
                    raise KeyError(row)
        return result

    def _get_valid_indices_by_tuple(self, index, row_tuple, max_length):
        # Instructions for Slicing
        # if tuple, get first and last elements of tuple
        # if open beginning tuple, get 0 to highest valid_index
        # if open ending tuple, get highest valid_index to len()
        # if not open end or beginning, get range lowest beginning index
        # to highest ending index
        if isinstance(row_tuple, slice):
            if (
                isinstance(row_tuple.start, numbers.Number)
                or isinstance(row_tuple.stop, numbers.Number)
                or row_tuple == slice(None)
            ):
                stop = row_tuple.stop or max_length
                start, stop, step = row_tuple.indices(stop)
                return column.arange(start, stop, step)
            start_values = self._compute_validity_mask(
                index, row_tuple.start, max_length
            )
            stop_values = self._compute_validity_mask(
                index, row_tuple.stop, max_length
            )
            return column.arange(start_values.min(), stop_values.max() + 1)
        elif isinstance(row_tuple, numbers.Number):
            return row_tuple
        return self._compute_validity_mask(index, row_tuple, max_length)

    def _index_and_downcast(self, result, index, index_key):

        if isinstance(index_key, (numbers.Number, slice)):
            index_key = [index_key]
        if (
            len(index_key) > 0 and not isinstance(index_key, tuple)
        ) or isinstance(index_key[0], slice):
            index_key = index_key[0]

        slice_access = False
        if isinstance(index_key, slice):
            slice_access = True
        out_index = cudf.DataFrame()
        # Select the last n-k columns where n is the number of _source_data
        # columns and k is the length of the indexing tuple
        size = 0
        if not isinstance(index_key, (numbers.Number, slice)):
            size = len(index_key)
        for k in range(size, len(index._source_data.columns)):
            if index.names is None:
                name = k
            else:
                name = index.names[k]
            out_index.insert(
                len(out_index.columns),
                name,
                index._source_data[index._source_data.columns[k]],
            )

        if len(result) == 1 and size == 0 and slice_access is False:
            # If the final result is one row and it was not mapped into
            # directly, return a Series with a tuple as name.
            result = result.T
            result = result[result._data.names[0]]
        elif len(result) == 0 and slice_access is False:
            # Pandas returns an empty Series with a tuple as name
            # the one expected result column
            series_name = []
            for code in index._source_data.columns:
                series_name.append(index._source_data[code][0])
            result = cudf.Series([])
            result.name = tuple(series_name)
        elif len(out_index.columns) == 1:
            # If there's only one column remaining in the output index, convert
            # it into an Index and name the final index values according
            # to the _source_data column names
            last_column = index._source_data.columns[-1]
            out_index = index._source_data[last_column]
            out_index = as_index(out_index)
            out_index.name = index.names[len(index.names) - 1]
            index = out_index
        elif len(out_index.columns) > 1:
            # Otherwise pop the leftmost levels, names, and codes from the
            # source index until it has the correct number of columns (n-k)
            result.reset_index(drop=True)
            index = index._popn(size)
        if isinstance(index_key, tuple):
            result = result.set_index(index)
        return result

    def _get_row_major(
        self,
        df: DataFrameOrSeries,
        row_tuple: Union[
            numbers.Number, slice, Tuple[Any, ...], List[Tuple[Any, ...]]
        ],
    ) -> DataFrameOrSeries:
        if pd.api.types.is_bool_dtype(
            list(row_tuple) if isinstance(row_tuple, tuple) else row_tuple
        ):
            return df[row_tuple]
        if isinstance(row_tuple, slice):
            if row_tuple.start is None:
                row_tuple = slice(self[0], row_tuple.stop, row_tuple.step)
            if row_tuple.stop is None:
                row_tuple = slice(row_tuple.start, self[-1], row_tuple.step)
        self._validate_indexer(row_tuple)
        valid_indices = self._get_valid_indices_by_tuple(
            df.index, row_tuple, len(df.index)
        )
        indices = cudf.Series(valid_indices)
        result = df.take(indices)
        final = self._index_and_downcast(result, result.index, row_tuple)
        return final

    def _validate_indexer(
        self,
        indexer: Union[
            numbers.Number, slice, Tuple[Any, ...], List[Tuple[Any, ...]]
        ],
    ):
        if isinstance(indexer, numbers.Number):
            return
        if isinstance(indexer, tuple):
            # drop any slice(None) from the end:
            indexer = tuple(
                itertools.dropwhile(
                    lambda x: x == slice(None), reversed(indexer)
                )
            )[::-1]

            # now check for size
            if len(indexer) > self.nlevels:
                raise IndexError("Indexer size exceeds number of levels")
        elif isinstance(indexer, slice):
            self._validate_indexer(indexer.start)
            self._validate_indexer(indexer.stop)
        else:
            for i in indexer:
                self._validate_indexer(i)

    def _split_tuples(self, tuples):
        if len(tuples) == 1:
            return tuples, slice(None)
        elif isinstance(tuples[0], tuple):
            row = tuples[0]
            if len(tuples) == 1:
                column = slice(None)
            else:
                column = tuples[1]
            return row, column
        elif isinstance(tuples[0], slice):
            return tuples
        else:
            return tuples, slice(None)

    def __len__(self):
        return self._data.nrows

    def __eq__(self, other):
        if not hasattr(other, "_levels"):
            return False
        # Lazy comparison
        if isinstance(other, MultiIndex) or hasattr(other, "_source_data"):
            for self_col, other_col in zip(
                self._source_data._data.values(),
                other._source_data._data.values(),
            ):
                if not self_col.equals(other_col):
                    return False
            return self.names == other.names
        else:
            # Lazy comparison isn't possible - MI was created manually.
            # Actually compare the MI, not its source data (it doesn't have
            # any).
            equal_levels = self.levels == other.levels
            if isinstance(equal_levels, np.ndarray):
                equal_levels = equal_levels.all()
            return (
                equal_levels
                and self.codes.equals(other.codes)
                and self.names == other.names
            )

    @property
    def is_contiguous(self):
        return True

    @property
    def size(self):
        return len(self)

    def take(self, indices):
        from collections.abc import Sequence
        from numbers import Integral

        if isinstance(indices, (Integral, Sequence)):
            indices = np.array(indices)
        elif isinstance(indices, cudf.Series):
            if indices.has_nulls:
                raise ValueError("Column must have no nulls.")
            indices = indices
        elif isinstance(indices, slice):
            start, stop, step = indices.indices(len(self))
            indices = column.arange(start, stop, step)
        result = MultiIndex(source_data=self._source_data.take(indices))
        if self._codes is not None:
            result._codes = self._codes.take(indices)
        if self._levels is not None:
            result._levels = self._levels
        result.names = self.names
        return result

    def serialize(self):
        header = {}
        header["type-serialized"] = pickle.dumps(type(self))
        header["names"] = pickle.dumps(self.names)

        header["source_data"], frames = self._source_data.serialize()

        return header, frames

    @classmethod
    def deserialize(cls, header, frames):
        names = pickle.loads(header["names"])

        source_data_typ = pickle.loads(
            header["source_data"]["type-serialized"]
        )
        source_data = source_data_typ.deserialize(
            header["source_data"], frames
        )

        names = pickle.loads(header["names"])
        return MultiIndex(names=names, source_data=source_data)

    def __getitem__(self, index):
        # TODO: This should be a take of the _source_data only
        match = self.take(index)
        if isinstance(index, slice):
            return match
        result = []
        for level, item in enumerate(match.codes):
            result.append(match.levels[level][match.codes[item].iloc[0]])
        return tuple(result)

    def to_frame(self, index=True, name=None):
        df = self._source_data
        if index:
            df = df.set_index(self)
        if name is not None:
            if len(name) != len(self.levels):
                raise ValueError(
                    "'name' should have th same length as "
                    "number of levels on index."
                )
            df.columns = name
        return df

    def get_level_values(self, level):
        """
        Return the values at the requested level

        Parameters
        ----------
        level : int or label

        Returns
        -------
        An Index containing the values at the requested level.
        """
        colnames = list(self._source_data.columns)
        if level not in colnames:
            if isinstance(level, int):
                if level < 0:
                    level = level + len(colnames)
                if level < 0 or level >= len(colnames):
                    raise IndexError(f"Invalid level number: '{level}'")
                level_idx = level
                level = colnames[level_idx]
            elif level in self.names:
                level_idx = list(self.names).index(level)
                level = colnames[level_idx]
            else:
                raise KeyError(f"Level not found: '{level}'")
        else:
            level_idx = colnames.index(level)
        level_values = as_index(
            self._source_data._data[level], name=self.names[level_idx]
        )
        return level_values

    @classmethod
    def _concat(cls, objs):

        source_data = [o._source_data for o in objs]

        if len(source_data) > 1:
            for index, obj in enumerate(source_data[1:]):
                obj.columns = source_data[0].columns
                source_data[index + 1] = obj

        source_data = cudf.DataFrame._concat(source_data)
        names = [None for x in source_data.columns]
        objs = list(filter(lambda o: o.names is not None, objs))
        for o in range(len(objs)):
            for i, name in enumerate(objs[o].names):
                names[i] = names[i] or name
        return cudf.MultiIndex(names=names, source_data=source_data)

    @classmethod
    def from_tuples(cls, tuples, names=None):
        """
        Convert list of tuples to MultiIndex.

        Parameters
        ----------
        tuples : list / sequence of tuple-likes
            Each tuple is the index of one row/column.
        names : list / sequence of str, optional
            Names for the levels in the index.

        Returns
        -------
        MultiIndex

        See Also
        --------
        MultiIndex.from_product : Make a MultiIndex from cartesian product
                                  of iterables.
        MultiIndex.from_frame : Make a MultiIndex from a DataFrame.

        Examples
        --------
        >>> tuples = [(1, 'red'), (1, 'blue'),
        ...           (2, 'red'), (2, 'blue')]
        >>> cudf.MultiIndex.from_tuples(tuples, names=('number', 'color'))
        MultiIndex([(1,  'red'),
                    (1, 'blue'),
                    (2,  'red'),
                    (2, 'blue')],
                   names=['number', 'color'])
        """
        # Use Pandas for handling Python host objects
        pdi = pd.MultiIndex.from_tuples(tuples, names=names)
        result = cls.from_pandas(pdi)
        return result

    @property
    def values_host(self):
        """
        Return a numpy representation of the MultiIndex.

        Only the values in the MultiIndex will be returned.

        Returns
        -------
        out : numpy.ndarray
            The values of the MultiIndex.

        Examples
        --------
        >>> import cudf
        >>> midx = cudf.MultiIndex(
        ...         levels=[[1, 3, 4, 5], [1, 2, 5]],
        ...         codes=[[0, 0, 1, 2, 3], [0, 2, 1, 1, 0]],
        ...         names=["x", "y"],
        ...     )
        >>> midx.values_host
        array([(1, 1), (1, 5), (3, 2), (4, 2), (5, 1)], dtype=object)
        >>> type(midx.values_host)
        <class 'numpy.ndarray'>
        """
        return self.to_pandas().values

    @property
    def values(self):
        """
        Return a CuPy representation of the MultiIndex.

        Only the values in the MultiIndex will be returned.

        Returns
        -------
        out: cupy.ndarray
            The values of the MultiIndex.

        Examples
        --------
        >>> import cudf
        >>> midx = cudf.MultiIndex(
        ...         levels=[[1, 3, 4, 5], [1, 2, 5]],
        ...         codes=[[0, 0, 1, 2, 3], [0, 2, 1, 1, 0]],
        ...         names=["x", "y"],
        ...     )
        >>> midx.values
        array([[1, 1],
            [1, 5],
            [3, 2],
            [4, 2],
            [5, 1]])
        >>> type(midx.values)
        <class 'cupy.core.core.ndarray'>
        """
        return self._source_data.values

    @classmethod
    def from_frame(cls, df, names=None):
        """
        Make a MultiIndex from a DataFrame.

        Parameters
        ----------
        df : DataFrame
            DataFrame to be converted to MultiIndex.
        names : list-like, optional
            If no names are provided, use the column names, or tuple of column
            names if the columns is a MultiIndex. If a sequence, overwrite
            names with the given sequence.

        Returns
        -------
        MultiIndex
            The MultiIndex representation of the given DataFrame.

        See Also
        --------
        MultiIndex.from_tuples : Convert list of tuples to MultiIndex.
        MultiIndex.from_product : Make a MultiIndex from cartesian product
                                  of iterables.

        Examples
        --------
        >>> import cudf
        >>> df = cudf.DataFrame([['HI', 'Temp'], ['HI', 'Precip'],
        ...                    ['NJ', 'Temp'], ['NJ', 'Precip']],
        ...                   columns=['a', 'b'])
        >>> df
              a       b
        0    HI    Temp
        1    HI  Precip
        2    NJ    Temp
        3    NJ  Precip
        >>> cudf.MultiIndex.from_frame(df)
        MultiIndex([('HI',   'Temp'),
                    ('HI', 'Precip'),
                    ('NJ',   'Temp'),
                    ('NJ', 'Precip')],
                   names=['a', 'b'])

        Using explicit names, instead of the column names

        >>> cudf.MultiIndex.from_frame(df, names=['state', 'observation'])
        MultiIndex([('HI',   'Temp'),
                    ('HI', 'Precip'),
                    ('NJ',   'Temp'),
                    ('NJ', 'Precip')],
                   names=['state', 'observation'])
        """
        return cls(source_data=df, names=names)

    @classmethod
    def from_product(cls, arrays, names=None):
        """
        Make a MultiIndex from the cartesian product of multiple iterables.

        Parameters
        ----------
        iterables : list / sequence of iterables
            Each iterable has unique labels for each level of the index.
        names : list / sequence of str, optional
            Names for the levels in the index.
            If not explicitly provided, names will be inferred from the
            elements of iterables if an element has a name attribute

        Returns
        -------
        MultiIndex

        See Also
        --------
        MultiIndex.from_tuples : Convert list of tuples to MultiIndex.
        MultiIndex.from_frame : Make a MultiIndex from a DataFrame.

        Examples
        --------
        >>> numbers = [0, 1, 2]
        >>> colors = ['green', 'purple']
        >>> cudf.MultiIndex.from_product([numbers, colors],
        ...                            names=['number', 'color'])
        MultiIndex([(0,  'green'),
                    (0, 'purple'),
                    (1,  'green'),
                    (1, 'purple'),
                    (2,  'green'),
                    (2, 'purple')],
                   names=['number', 'color'])
        """
        # Use Pandas for handling Python host objects
        pdi = pd.MultiIndex.from_product(arrays, names=names)
        result = cls.from_pandas(pdi)
        return result

    def _poplevels(self, level):
        """
        Remove and return the specified levels from self.

        Parameters
        ----------
        level : level name or index, list
            One or more levels to remove

        Returns
        -------
        Index composed of the removed levels. If only a single level
        is removed, a flat index is returned. If no levels are specified
        (empty list), None is returned.
        """
        if not pd.api.types.is_list_like(level):
            level = (level,)

        ilevels = sorted([self._level_index_from_level(lev) for lev in level])

        if not ilevels:
            return None

        popped_data = {}
        popped_names = []
        names = list(self.names)

        # build the popped data and names
        for i in ilevels:
            n = self._data.names[i]
            popped_data[n] = self._data[n]
            popped_names.append(self.names[i])

        # pop the levels out from self
        # this must be done iterating backwards
        for i in reversed(ilevels):
            n = self._data.names[i]
            names.pop(i)
            popped_data[n] = self._data.pop(n)

        # construct the popped result
        popped = cudf.Index._from_data(popped_data)
        popped.names = popped_names

        # update self
        self.names = names
        self._compute_levels_and_codes()

        return popped

    def droplevel(self, level=-1):
        """
        Removes the specified levels from the MultiIndex.

        Parameters
        ----------
        level : level name or index, list-like
            Integer, name or list of such, specifying one or more
            levels to drop from the MultiIndex

        Returns
        -------
        A MultiIndex or Index object, depending on the number of remaining
        levels.

        Examples
        --------
        >>> import cudf
        >>> idx = cudf.MultiIndex.from_frame(
        ...     cudf.DataFrame(
        ...         {
        ...             "first": ["a", "a", "a", "b", "b", "b"],
        ...             "second": [1, 1, 2, 2, 3, 3],
        ...             "third": [0, 1, 2, 0, 1, 2],
        ...         }
        ...     )
        ... )

        Dropping level by index:

        >>> idx.droplevel(0)
        MultiIndex([(1, 0),
                    (1, 1),
                    (2, 2),
                    (2, 0),
                    (3, 1),
                    (3, 2)],
                   names=['second', 'third'])

        Dropping level by name:

        >>> idx.droplevel("first")
        MultiIndex([(1, 0),
                    (1, 1),
                    (2, 2),
                    (2, 0),
                    (3, 1),
                    (3, 2)],
                   names=['second', 'third'])

        Dropping multiple levels:

        >>> idx.droplevel(["first", "second"])
        Int64Index([0, 1, 2, 0, 1, 2], dtype='int64', name='third')
        """
        mi = self.copy(deep=False)
        mi._poplevels(level)
        if mi.nlevels == 1:
            return mi.get_level_values(mi.names[0])
        else:
            return mi

    def to_pandas(self, nullable=False, **kwargs):
        if hasattr(self, "_source_data"):
            result = self._source_data.to_pandas(nullable=nullable)
            result.columns = self.names
            return pd.MultiIndex.from_frame(result)

        pandas_codes = []
        for code in self.codes.columns:
            pandas_codes.append(self.codes[code].to_array())

        # We do two things here to mimic Pandas behavior:
        # 1. as_index() on each level, so DatetimeColumn becomes DatetimeIndex
        # 2. convert levels to numpy array so empty levels become Float64Index
        levels = np.array(
            [as_index(level).to_pandas() for level in self.levels]
        )

        # Backwards compatibility:
        # Construct a dummy MultiIndex and check for the codes attr.
        # This indicates that it is pandas >= 0.24
        # If no codes attr is present it is pandas <= 0.23
        if hasattr(pd.MultiIndex([[]], [[]]), "codes"):
            pandas_mi = pd.MultiIndex(levels=levels, codes=pandas_codes)
        else:
            pandas_mi = pd.MultiIndex(levels=levels, labels=pandas_codes)
        if self.names is not None:
            pandas_mi.names = self.names
        return pandas_mi

    @classmethod
    def from_pandas(cls, multiindex, nan_as_null=None):
        """
        Convert from a Pandas MultiIndex

        Raises
        ------
        TypeError for invalid input type.

        Examples
        --------
        >>> import cudf
        >>> import pandas as pd
        >>> pmi = pd.MultiIndex(levels=[['a', 'b'], ['c', 'd']],
        ...                     codes=[[0, 1], [1, 1]])
        >>> cudf.from_pandas(pmi)
        MultiIndex([('a', 'd'),
                    ('b', 'd')],
                   )
        """
        if not isinstance(multiindex, pd.MultiIndex):
            raise TypeError("not a pandas.MultiIndex")

        mi = cls(
            names=multiindex.names,
            source_data=multiindex.to_frame(),
            nan_as_null=nan_as_null,
        )

        return mi

    @property
    def is_unique(self):
        if not hasattr(self, "_is_unique"):
            self._is_unique = len(self._source_data) == len(
                self._source_data.drop_duplicates(ignore_index=True)
            )
        return self._is_unique

    @property
    def is_monotonic_increasing(self):
        """
        Return if the index is monotonic increasing
        (only equal or increasing) values.
        """
        return self._is_sorted(ascending=None, null_position=None)

    @property
    def is_monotonic_decreasing(self):
        """
        Return if the index is monotonic decreasing
        (only equal or decreasing) values.
        """
        return self._is_sorted(
            ascending=[False] * len(self.levels), null_position=None
        )

    def argsort(self, ascending=True, **kwargs):
        indices = self._source_data.argsort(ascending=ascending, **kwargs)
        return cupy.asarray(indices)

    def sort_values(self, return_indexer=False, ascending=True, key=None):
        if key is not None:
            raise NotImplementedError("key parameter is not yet implemented.")

        indices = self._source_data.argsort(ascending=ascending)
        index_sorted = as_index(self.take(indices), name=self.names)

        if return_indexer:
            return index_sorted, cupy.asarray(indices)
        else:
            return index_sorted

    def fillna(self, value):
        """
        Fill null values with the specified value.

        Parameters
        ----------
        value : scalar
            Scalar value to use to fill nulls. This value cannot be a
            list-likes.

        Returns
        -------
        filled : MultiIndex

        Examples
        --------
        >>> import cudf
        >>> index = cudf.MultiIndex(
        ...         levels=[["a", "b", "c", None], ["1", None, "5"]],
        ...         codes=[[0, 0, 1, 2, 3], [0, 2, 1, 1, 0]],
        ...         names=["x", "y"],
        ...       )
        >>> index
        MultiIndex([( 'a',  '1'),
                    ( 'a',  '5'),
                    ( 'b', <NA>),
                    ( 'c', <NA>),
                    (<NA>,  '1')],
                   names=['x', 'y'])
        >>> index.fillna('hello')
        MultiIndex([(    'a',     '1'),
                    (    'a',     '5'),
                    (    'b', 'hello'),
                    (    'c', 'hello'),
                    ('hello',     '1')],
                   names=['x', 'y'])
        """

        return super().fillna(value=value)

    def unique(self):
        return MultiIndex.from_frame(self._source_data.drop_duplicates())

    def _clean_nulls_from_index(self):
        """
        Convert all na values(if any) in MultiIndex object
        to `<NA>` as a preprocessing step to `__repr__` methods.
        """
        index_df = self._source_data
        return MultiIndex.from_frame(
            index_df._clean_nulls_from_dataframe(index_df), names=self.names
        )

    def memory_usage(self, deep=False):
        n = 0
        for col in self._source_data._columns:
            n += col._memory_usage(deep=deep)
        if self._levels:
            for level in self._levels:
                n += level.memory_usage(deep=deep)
        if self._codes:
            for col in self._codes._columns:
                n += col._memory_usage(deep=deep)
        return n

    def difference(self, other, sort=None):
        temp_self = self
        temp_other = other
        if hasattr(self, "to_pandas"):
            temp_self = self.to_pandas()
        if hasattr(other, "to_pandas"):
            temp_other = self.to_pandas()
        return temp_self.difference(temp_other, sort)

    def append(self, other):
        """
        Append a collection of MultiIndex objects together

        Parameters
        ----------
        other : MultiIndex or list/tuple of MultiIndex objects

        Returns
        -------
        appended : Index

        Examples
        --------
        >>> import cudf
        >>> idx1 = cudf.MultiIndex(
        ...     levels=[[1, 2], ['blue', 'red']],
        ...     codes=[[0, 0, 1, 1], [1, 0, 1, 0]]
        ... )
        >>> idx2 = cudf.MultiIndex(
        ...     levels=[[3, 4], ['blue', 'red']],
        ...     codes=[[0, 0, 1, 1], [1, 0, 1, 0]]
        ... )
        >>> idx1
        MultiIndex([(1,  'red'),
                    (1, 'blue'),
                    (2,  'red'),
                    (2, 'blue')],
                   )
        >>> idx2
        MultiIndex([(3,  'red'),
                    (3, 'blue'),
                    (4,  'red'),
                    (4, 'blue')],
                   )
        >>> idx1.append(idx2)
        MultiIndex([(1,  'red'),
                    (1, 'blue'),
                    (2,  'red'),
                    (2, 'blue'),
                    (3,  'red'),
                    (3, 'blue'),
                    (4,  'red'),
                    (4, 'blue')],
                   )
        """
        if isinstance(other, (list, tuple)):
            to_concat = [self]
            to_concat.extend(other)
        else:
            to_concat = [self, other]

        for obj in to_concat:
            if not isinstance(obj, MultiIndex):
                raise TypeError(
                    f"all objects should be of type "
                    f"MultiIndex for MultiIndex.append, "
                    f"found object of type: {type(obj)}"
                )

        return MultiIndex._concat(to_concat)

    def nan_to_num(*args, **kwargs):
        return args[0]

    def array_equal(*args, **kwargs):
        return args[0] == args[1]

    def __array_function__(self, func, types, args, kwargs):
        cudf_df_module = MultiIndex

        for submodule in func.__module__.split(".")[1:]:
            # point cudf to the correct submodule
            if hasattr(cudf_df_module, submodule):
                cudf_df_module = getattr(cudf_df_module, submodule)
            else:
                return NotImplemented

        fname = func.__name__

        handled_types = [cudf_df_module, np.ndarray]

        for t in types:
            if t not in handled_types:
                return NotImplemented

        if hasattr(cudf_df_module, fname):
            cudf_func = getattr(cudf_df_module, fname)
            # Handle case if cudf_func is same as numpy function
            if cudf_func is func:
                return NotImplemented
            else:
                return cudf_func(*args, **kwargs)
        else:
            return NotImplemented

    def _level_index_from_level(self, level):
        """
        Return level index from given level name or index
        """
        try:
            return self.names.index(level)
        except ValueError:
            if not pd.api.types.is_integer(level):
                raise KeyError(f"Level {level} not found") from None
            if level < 0:
                level += self.nlevels
            if level >= self.nlevels:
                raise IndexError(
                    f"Level {level} out of bounds. "
                    f"Index has {self.nlevels} levels."
                ) from None
            return level

    def _level_name_from_level(self, level):
        return self.names[self._level_index_from_level(level)]

    def get_loc(self, key, method=None, tolerance=None):
        """
        Get location for a label or a tuple of labels.

        The location is returned as an integer/slice or boolean mask.

        Parameters
        ----------
        key : label or tuple of labels (one for each level)
        method : None

        Returns
        -------
        loc : int, slice object or boolean mask
            - If index is unique, search result is unique, return a single int.
            - If index is monotonic, index is returned as a slice object.
            - Otherwise, cudf attempts a best effort to convert the search
              result into a slice object, and will return a boolean mask if
              failed to do so. Notice this can deviate from Pandas behavior
              in some situations.

        Examples
        --------
        >>> import cudf
        >>> mi = cudf.MultiIndex.from_tuples(
            [('a', 'd'), ('b', 'e'), ('b', 'f')])
        >>> mi.get_loc('b')
        slice(1, 3, None)
        >>> mi.get_loc(('b', 'e'))
        1
        >>> non_monotonic_non_unique_idx = cudf.MultiIndex.from_tuples(
            [('c', 'd'), ('b', 'e'), ('a', 'f'), ('b', 'e')])
        >>> non_monotonic_non_unique_idx.get_loc('b') # differ from pandas
        slice(1, 4, 2)

        .. pandas-compat::
            **MultiIndex.get_loc**

            The return types of this function may deviates from the
            method provided by Pandas. If the index is neither
            lexicographically sorted nor unique, a best effort attempt is made
            to coerce the found indices into a slice. For example:

            .. code-block::

                >>> import pandas as pd
                >>> import cudf
                >>> x = pd.MultiIndex.from_tuples(
                            [(2, 1, 1), (1, 2, 3), (1, 2, 1),
                                (1, 1, 1), (1, 1, 1), (2, 2, 1)]
                        )
                >>> x.get_loc(1)
                array([False,  True,  True,  True,  True, False])
                >>> cudf.from_pandas(x).get_loc(1)
                slice(1, 5, 1)
        """
        if tolerance is not None:
            raise NotImplementedError(
                "Parameter tolerance is unsupported yet."
            )
        if method is not None:
            raise NotImplementedError(
                "only the default get_loc method is currently supported for"
                " MultiIndex"
            )

        is_sorted = (
            self.is_monotonic_increasing or self.is_monotonic_decreasing
        )
        is_unique = self.is_unique
        key = (key,) if not isinstance(key, tuple) else key

        # Handle partial key search. If length of `key` is less than `nlevels`,
        # Only search levels up to `len(key)` level.
        key_as_table = libcudf.table.Table(
            {i: as_column(k, length=1) for i, k in enumerate(key)}
        )
        partial_index = self.__class__._from_data(
            data=self._data.select_by_index(slice(key_as_table._num_columns))
        )
        (
            lower_bound,
            upper_bound,
            sort_inds,
        ) = partial_index._lexsorted_equal_range(key_as_table, is_sorted)

        if lower_bound == upper_bound:
            raise KeyError(key)

        if is_unique and lower_bound + 1 == upper_bound:
            # Indices are unique (Pandas constraint), search result is unique,
            # return int.
            return (
                lower_bound
                if is_sorted
                else sort_inds.element_indexing(lower_bound)
            )

        if is_sorted:
            # In monotonic index, lex search result is continuous. A slice for
            # the range is returned.
            return slice(lower_bound, upper_bound)

        true_inds = cupy.array(
            sort_inds.slice(lower_bound, upper_bound).to_gpu_array()
        )
        true_inds = _maybe_indices_to_slice(true_inds)
        if isinstance(true_inds, slice):
            return true_inds

        # Not sorted and not unique. Return a boolean mask
        mask = cupy.full(self._data.nrows, False)
        mask[true_inds] = True
        return mask
