/*
 * Copyright (c) 2018-2019, NVIDIA CORPORATION.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include <cudf/column/column_factories.hpp>
#include <cudf/unary.hpp>

namespace cudf {
namespace experimental {
namespace detail {
/**
 * @brief Creates a column of `BOOL8` elements by applying a predicate to every element between
 * [`begin, `end`) `true` indicates the value is satisfies the predicate and `false` indicates it
 * doesn't.
 *
 * @tparam InputIterator Iterator type for `begin` and `end`
 * @tparam Predicate A predicator type which will be evaludated
 * @param begin Begining of the sequence of elements
 * @param end End of the sequence of elements
 * @param p Predicate to be applied to each element in `[begin,end)`
 * @param mr Device memory resource used to allocate the returned column's device memory
 * @param stream CUDA stream used for device memory operations and kernel launches.
 *
 * @returns A column of type `BOOL8,` with `true` representing predicate is satisfied.
 */

template <typename InputIterator, typename Predicate>
std::unique_ptr<column> true_if(
  InputIterator begin,
  InputIterator end,
  size_type size,
  Predicate p,
  rmm::mr::device_memory_resource* mr = rmm::mr::get_default_resource(),
  cudaStream_t stream                 = 0)
{
  auto output = make_numeric_column(data_type(BOOL8), size, mask_state::UNALLOCATED, stream, mr);
  auto output_mutable_view = output->mutable_view();
  auto output_data         = output_mutable_view.data<bool>();

  thrust::transform(rmm::exec_policy(stream)->on(stream), begin, end, output_data, p);

  return output;
}

/**
 * @copydoc cudf::experimental::unary_operation
 *
 * @param stream CUDA stream used for device memory operations and kernel launches.
 */
std::unique_ptr<cudf::column> unary_operation(
  cudf::column_view const& input,
  cudf::experimental::unary_op op,
  rmm::mr::device_memory_resource* mr = rmm::mr::get_default_resource(),
  cudaStream_t stream                 = 0);

/**
 * @copydoc cudf::experimental::cast
 *
 * @param stream CUDA stream used for device memory operations and kernel launches.
 */
std::unique_ptr<column> cast(column_view const& input,
                             data_type type,
                             rmm::mr::device_memory_resource* mr = rmm::mr::get_default_resource(),
                             cudaStream_t stream                 = 0);

/**
 * @copydoc cudf::experimental::is_nan
 *
 * @param[in] stream Optional CUDA stream on which to execute kernels
 */
std::unique_ptr<column> is_nan(
  cudf::column_view const& input,
  rmm::mr::device_memory_resource* mr = rmm::mr::get_default_resource(),
  cudaStream_t stream                 = 0);

/**
 * @copydoc cudf::experimental::is_not_nan
 *
 * @param[in] stream Optional CUDA stream on which to execute kernels
 */
std::unique_ptr<column> is_not_nan(
  cudf::column_view const& input,
  rmm::mr::device_memory_resource* mr = rmm::mr::get_default_resource(),
  cudaStream_t stream                 = 0);

}  // namespace detail
}  // namespace experimental
}  // namespace cudf
