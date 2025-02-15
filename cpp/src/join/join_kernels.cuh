/*
 * Copyright (c) 2018-2020, NVIDIA CORPORATION.
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

#include <join/join_common_utils.cuh>
#include <join/join_common_utils.hpp>

#include <cudf/table/table_device_view.cuh>

#include <thrust/pair.h>
#include <cub/cub.cuh>

namespace cudf {
namespace detail {
/**
 * @brief Remaps a hash value to a new value if it is equal to the specified sentinel value.
 *
 * @param hash The hash value to potentially remap
 * @param sentinel The reserved value
 */
template <typename H, typename S>
constexpr auto remap_sentinel_hash(H hash, S sentinel)
{
  // Arbitrarily choose hash - 1
  return (hash == sentinel) ? (hash - 1) : hash;
}

/**
 * @brief Builds a hash table from a row hasher that maps the hash
 * values of each row to its respective row index.
 *
 * @tparam multimap_type The type of the hash table
 *
 * @param[in,out] multi_map The hash table to be built to insert rows into
 * @param[in] hash_build Row hasher for the build table
 * @param[in] build_table_num_rows The number of rows in the build table
 * @param[in] row_bitmask Bitmask where bit `i` indicates the presence of a null
 * value in row `i` of input keys. This is nullptr if nulls are equal.
 * @param[out] error Pointer used to set an error code if the insert fails
 */
template <typename multimap_type>
__global__ void build_hash_table(multimap_type multi_map,
                                 row_hash hash_build,
                                 const cudf::size_type build_table_num_rows,
                                 bitmask_type const* row_bitmask,
                                 int* error)
{
  cudf::size_type i = threadIdx.x + blockIdx.x * blockDim.x;

  while (i < build_table_num_rows) {
    if (!row_bitmask || cudf::bit_is_set(row_bitmask, i)) {
      // Compute the hash value of this row
      auto const row_hash_value = remap_sentinel_hash(hash_build(i), multi_map.get_unused_key());

      // Insert the (row hash value, row index) into the map
      // using the row hash value to determine the location in the
      // hash map where the new pair should be inserted
      auto const insert_location =
        multi_map.insert(thrust::make_pair(row_hash_value, i), true, row_hash_value);

      // If the insert failed, set the error code accordingly
      if (multi_map.end() == insert_location) { *error = 1; }
    }
    i += blockDim.x * gridDim.x;
  }
}

/**
 * @brief Computes the output size of joining the probe table to the build table
 * by probing the hash map with the probe table and counting the number of matches.
 *
 * @tparam JoinKind The type of join to be performed
 * @tparam multimap_type The datatype of the hash table
 * @tparam block_size The number of threads per block for this kernel
 *
 * @param[in] multi_map The hash table built on the build table
 * @param[in] build_table The build table
 * @param[in] probe_table The probe table
 * @param[in] hash_probe Row hasher for the probe table
 * @param[in] check_row_equality The row equality comparator
 * @param[in] probe_table_num_rows The number of rows in the probe table
 * @param[out] output_size The resulting output size
 */
template <join_kind JoinKind, typename multimap_type, int block_size>
__global__ void compute_join_output_size(multimap_type multi_map,
                                         table_device_view build_table,
                                         table_device_view probe_table,
                                         row_hash hash_probe,
                                         row_equality check_row_equality,
                                         const cudf::size_type probe_table_num_rows,
                                         std::size_t* output_size)
{
  // This kernel probes multiple elements in the probe_table and store the number of matches found
  // inside a register. A block reduction is used at the end to calculate the matches per thread
  // block, and atomically add to the global 'output_size'. Compared to probing one element per
  // thread, this implementation improves performance by reducing atomic adds to the shared memory
  // counter.

  cudf::size_type thread_counter{0};
  const cudf::size_type start_idx = threadIdx.x + blockIdx.x * blockDim.x;
  const cudf::size_type stride    = blockDim.x * gridDim.x;
  const auto unused_key           = multi_map.get_unused_key();
  const auto end                  = multi_map.end();

  for (cudf::size_type probe_row_index = start_idx; probe_row_index < probe_table_num_rows;
       probe_row_index += stride) {
    // Search the hash map for the hash value of the probe row using the row's
    // hash value to determine the location where to search for the row in the hash map
    auto const probe_row_hash_value = remap_sentinel_hash(hash_probe(probe_row_index), unused_key);

    auto found = multi_map.find(probe_row_hash_value, true, probe_row_hash_value);

    // for left-joins we always need to add an output
    bool running     = (JoinKind == join_kind::LEFT_JOIN) || (end != found);
    bool found_match = false;

    while (running) {
      // TODO Simplify this logic...

      // Left joins always have an entry in the output
      if (JoinKind == join_kind::LEFT_JOIN && (end == found)) {
        running = false;
      }
      // Stop searching after encountering an empty hash table entry
      else if (unused_key == found->first) {
        running = false;
      }
      // First check that the hash values of the two rows match
      else if (found->first == probe_row_hash_value) {
        // If the hash values are equal, check that the rows are equal
        if (check_row_equality(probe_row_index, found->second)) {
          // If the rows are equal, then we have found a true match
          found_match = true;
          ++thread_counter;
        }
        // Continue searching for matching rows until you hit an empty hash map entry
        ++found;
        // If you hit the end of the hash map, wrap around to the beginning
        if (end == found) found = multi_map.begin();
        // Next entry is empty, stop searching
        if (unused_key == found->first) running = false;
      } else {
        // Continue searching for matching rows until you hit an empty hash table entry
        ++found;
        // If you hit the end of the hash map, wrap around to the beginning
        if (end == found) found = multi_map.begin();
        // Next entry is empty, stop searching
        if (unused_key == found->first) running = false;
      }

      if ((JoinKind == join_kind::LEFT_JOIN) && (!running) && (!found_match)) { ++thread_counter; }
    }
  }

  using BlockReduce = cub::BlockReduce<std::size_t, block_size>;
  __shared__ typename BlockReduce::TempStorage temp_storage;
  std::size_t block_counter = BlockReduce(temp_storage).Sum(thread_counter);

  // Add block counter to global counter
  if (threadIdx.x == 0) atomicAdd(output_size, block_counter);
}

/**
 * @brief Probes the hash map with the probe table to find all matching rows
 * between the probe and hash table and generate the output for the desired
 * Join operation.
 *
 * @tparam JoinKind The type of join to be performed
 * @tparam multimap_type The type of the hash table
 * @tparam block_size The number of threads per block for this kernel
 * @tparam output_cache_size The side of the shared memory buffer to cache join output results
 *
 * @param[in] multi_map The hash table built from the build table
 * @param[in] build_table The build table
 * @param[in] probe_table The probe table
 * @param[in] hash_probe Row hasher for the probe table
 * @param[in] check_row_equality The row equality comparator
 * @param[out] join_output_l The left result of the join operation
 * @param[out] join_output_r The right result of the join operation
 * @param[in,out] current_idx A global counter used by threads to coordinate writes to the global
 output
 * @param[in] max_size The maximum size of the output
 */
template <join_kind JoinKind,
          typename multimap_type,
          cudf::size_type block_size,
          cudf::size_type output_cache_size>
__global__ void probe_hash_table(multimap_type multi_map,
                                 table_device_view build_table,
                                 table_device_view probe_table,
                                 row_hash hash_probe,
                                 row_equality check_row_equality,
                                 size_type* join_output_l,
                                 size_type* join_output_r,
                                 cudf::size_type* current_idx,
                                 const std::size_t max_size)
{
  constexpr int num_warps = block_size / detail::warp_size;
  __shared__ size_type current_idx_shared[num_warps];
  __shared__ size_type join_shared_l[num_warps][output_cache_size];
  __shared__ size_type join_shared_r[num_warps][output_cache_size];

  const int warp_id                          = threadIdx.x / detail::warp_size;
  const int lane_id                          = threadIdx.x % detail::warp_size;
  const cudf::size_type probe_table_num_rows = probe_table.num_rows();

  if (0 == lane_id) { current_idx_shared[warp_id] = 0; }

  __syncwarp();

  size_type probe_row_index = threadIdx.x + blockIdx.x * blockDim.x;

  const unsigned int activemask = __ballot_sync(0xffffffff, probe_row_index < probe_table_num_rows);
  if (probe_row_index < probe_table_num_rows) {
    const auto unused_key = multi_map.get_unused_key();
    const auto end        = multi_map.end();

    // Search the hash map for the hash value of the probe row using the row's
    // hash value to determine the location where to search for the row in the hash map
    auto const probe_row_hash_value = remap_sentinel_hash(hash_probe(probe_row_index), unused_key);

    auto found = multi_map.find(probe_row_hash_value, true, probe_row_hash_value);

    bool running = (JoinKind == join_kind::LEFT_JOIN) ||
                   (end != found);  // for left-joins we always need to add an output
    bool found_match = false;
    while (__any_sync(activemask, running)) {
      if (running) {
        // TODO Simplify this logic...

        // Left joins always have an entry in the output
        if ((JoinKind == join_kind::LEFT_JOIN) && (end == found)) {
          running = false;
        }
        // Stop searching after encountering an empty hash table entry
        else if (unused_key == found->first) {
          running = false;
        }
        // First check that the hash values of the two rows match
        else if (found->first == probe_row_hash_value) {
          // If the hash values are equal, check that the rows are equal
          // TODO : REMOVE : if(row_equal{probe_table, build_table}(probe_row_index, found->second))
          if (check_row_equality(probe_row_index, found->second)) {
            // If the rows are equal, then we have found a true match
            found_match = true;
            add_pair_to_cache(probe_row_index,
                              found->second,
                              current_idx_shared,
                              warp_id,
                              join_shared_l[warp_id],
                              join_shared_r[warp_id]);
          }
          // Continue searching for matching rows until you hit an empty hash map entry
          ++found;
          // If you hit the end of the hash map, wrap around to the beginning
          if (end == found) found = multi_map.begin();
          // Next entry is empty, stop searching
          if (unused_key == found->first) running = false;
        } else {
          // Continue searching for matching rows until you hit an empty hash table entry
          ++found;
          // If you hit the end of the hash map, wrap around to the beginning
          if (end == found) found = multi_map.begin();
          // Next entry is empty, stop searching
          if (unused_key == found->first) running = false;
        }

        // If performing a LEFT join and no match was found, insert a Null into the output
        if ((JoinKind == join_kind::LEFT_JOIN) && (!running) && (!found_match)) {
          add_pair_to_cache(probe_row_index,
                            static_cast<size_type>(JoinNoneValue),
                            current_idx_shared,
                            warp_id,
                            join_shared_l[warp_id],
                            join_shared_r[warp_id]);
        }
      }

      __syncwarp(activemask);
      // flush output cache if next iteration does not fit
      if (current_idx_shared[warp_id] + detail::warp_size >= output_cache_size) {
        flush_output_cache<num_warps, output_cache_size>(activemask,
                                                         max_size,
                                                         warp_id,
                                                         lane_id,
                                                         current_idx,
                                                         current_idx_shared,
                                                         join_shared_l,
                                                         join_shared_r,
                                                         join_output_l,
                                                         join_output_r);
        __syncwarp(activemask);
        if (0 == lane_id) { current_idx_shared[warp_id] = 0; }
        __syncwarp(activemask);
      }
    }

    // final flush of output cache
    if (current_idx_shared[warp_id] > 0) {
      flush_output_cache<num_warps, output_cache_size>(activemask,
                                                       max_size,
                                                       warp_id,
                                                       lane_id,
                                                       current_idx,
                                                       current_idx_shared,
                                                       join_shared_l,
                                                       join_shared_r,
                                                       join_output_l,
                                                       join_output_r);
    }
  }
}

}  // namespace detail

}  // namespace cudf
