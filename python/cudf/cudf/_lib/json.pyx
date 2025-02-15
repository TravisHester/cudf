# Copyright (c) 2019-2020, NVIDIA CORPORATION.

# cython: boundscheck = False


import collections.abc as abc
import io
import os

import cudf

from libcpp cimport bool
from libcpp.string cimport string
from libcpp.utility cimport move
from libcpp.vector cimport vector

cimport cudf._lib.cpp.io.types as cudf_io_types
from cudf._lib.cpp.io.json cimport (
    json_reader_options,
    read_json as libcudf_read_json,
)
from cudf._lib.cpp.types cimport size_type
from cudf._lib.io.utils cimport make_source_info
from cudf._lib.table cimport Table
from cudf._lib.utils cimport data_from_unique_ptr


cpdef read_json(object filepaths_or_buffers,
                object dtype,
                bool lines,
                object compression,
                object byte_range):
    """
    Cython function to call into libcudf API, see `read_json`.

    See Also
    --------
    cudf.io.json.read_json
    cudf.io.json.to_json
    """

    # If input data is a JSON string (or StringIO), hold a reference to
    # the encoded memoryview externally to ensure the encoded buffer
    # isn't destroyed before calling libcudf++ `read_json()`
    for idx in range(len(filepaths_or_buffers)):
        if isinstance(filepaths_or_buffers[idx], io.StringIO):
            filepaths_or_buffers[idx] = \
                filepaths_or_buffers[idx].read().encode()
        elif isinstance(filepaths_or_buffers[idx], str) and \
                not os.path.isfile(filepaths_or_buffers[idx]):
            filepaths_or_buffers[idx] = filepaths_or_buffers[idx].encode()

    # Setup arguments
    cdef vector[string] c_dtypes
    cdef cudf_io_types.compression_type c_compression
    # Determine byte read offsets if applicable
    cdef size_type c_range_offset = (
        byte_range[0] if byte_range is not None else 0
    )
    cdef size_type c_range_size = (
        byte_range[1] if byte_range is not None else 0
    )
    cdef bool c_lines = lines

    if compression is not None:
        if compression == 'gzip':
            c_compression = cudf_io_types.compression_type.GZIP
        elif compression == 'bz2':
            c_compression = cudf_io_types.compression_type.BZIP2
        else:
            c_compression = cudf_io_types.compression_type.AUTO
    else:
        c_compression = cudf_io_types.compression_type.NONE

    if dtype is False:
        raise ValueError("False value is unsupported for `dtype`")
    elif dtype is not True:
        if isinstance(dtype, abc.Mapping):
            c_dtypes.reserve(len(dtype))
            for k, v in dtype.items():
                if cudf.utils.dtypes.is_categorical_dtype(v):
                    raise NotImplementedError(
                        "CategoricalDtype as dtype is not yet "
                        "supported in JSON reader"
                    )
                c_dtypes.push_back(str(str(k) + ":" + str(v)).encode())
        elif not isinstance(dtype, abc.Iterable):
            raise TypeError("`dtype` must be 'list like' or 'dict'")
        else:
            c_dtypes.reserve(len(dtype))
            for col_dtype in dtype:
                if cudf.utils.dtypes.is_categorical_dtype(col_dtype):
                    raise NotImplementedError(
                        "CategoricalDtype as dtype is not yet "
                        "supported in JSON reader"
                    )
                c_dtypes.push_back(str(col_dtype).encode())

    cdef json_reader_options opts = move(
        json_reader_options.builder(make_source_info(filepaths_or_buffers))
        .dtypes(c_dtypes)
        .compression(c_compression)
        .lines(c_lines)
        .byte_range_offset(c_range_offset)
        .byte_range_size(c_range_size)
        .build()
    )

    # Read JSON
    cdef cudf_io_types.table_with_metadata c_out_table

    with nogil:
        c_out_table = move(libcudf_read_json(opts))

    column_names = [x.decode() for x in c_out_table.metadata.column_names]
    return data_from_unique_ptr(move(c_out_table.tbl),
                                column_names=column_names)
