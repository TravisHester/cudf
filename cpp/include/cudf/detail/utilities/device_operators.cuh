/*
 * Copyright (c) 2019-2021, NVIDIA CORPORATION.
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

/**
 * @brief definition of the device operators
 * @file device_operators.cuh
 */

#include <cudf/fixed_point/fixed_point.hpp>
#include <cudf/scalar/scalar.hpp>
#include <cudf/strings/string_view.cuh>
#include <cudf/types.hpp>
#include <cudf/utilities/error.hpp>
#include <cudf/utilities/traits.hpp>

#include <type_traits>

namespace cudf {
// ------------------------------------------------------------------------
// Binary operators
/* @brief binary `sum` operator */
struct DeviceSum {
  template <typename T, typename std::enable_if_t<!cudf::is_timestamp<T>()>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T& lhs, const T& rhs)
  {
    return lhs + rhs;
  }

  template <typename T, typename std::enable_if_t<cudf::is_timestamp<T>()>* = nullptr>
  static constexpr T identity()
  {
    return T{typename T::duration{0}};
  }

  template <
    typename T,
    typename std::enable_if_t<!cudf::is_timestamp<T>() && !cudf::is_fixed_point<T>()>* = nullptr>
  static constexpr T identity()
  {
    return T{0};
  }

  template <typename T, typename std::enable_if_t<cudf::is_fixed_point<T>()>* = nullptr>
  static constexpr T identity()
  {
    CUDF_FAIL("fixed_point does not yet support device operator identity");
    return T{};
  }
};

/* @brief `count` operator - used in rolling windows */
struct DeviceCount {
  template <typename T, typename std::enable_if_t<cudf::is_timestamp<T>()>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T& lhs, const T& rhs)
  {
    return T{DeviceCount{}(lhs.time_since_epoch(), rhs.time_since_epoch())};
  }

  template <typename T, typename std::enable_if_t<!cudf::is_timestamp<T>()>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T&, const T& rhs)
  {
    return rhs + T{1};
  }

  template <typename T>
  static constexpr T identity()
  {
    return T{};
  }
};

/* @brief binary `min` operator */
struct DeviceMin {
  template <typename T>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T& lhs, const T& rhs)
  {
    return std::min(lhs, rhs);
  }

  template <
    typename T,
    typename std::enable_if_t<!std::is_same_v<T, cudf::string_view> && !cudf::is_dictionary<T>() &&
                              !cudf::is_fixed_point<T>()>* = nullptr>
  static constexpr T identity()
  {
    return std::numeric_limits<T>::max();
  }

  template <typename T, typename std::enable_if_t<cudf::is_fixed_point<T>()>* = nullptr>
  static constexpr T identity()
  {
    CUDF_FAIL("fixed_point does not yet support DeviceMin identity");
    return std::numeric_limits<T>::max();
  }

  // @brief identity specialized for string_view
  template <typename T, typename std::enable_if_t<std::is_same_v<T, cudf::string_view>>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE static constexpr T identity()
  {
    return string_view::max();
  }

  template <typename T, typename std::enable_if_t<cudf::is_dictionary<T>()>* = nullptr>
  static constexpr T identity()
  {
    return static_cast<T>(T::max_value());
  }
};

/* @brief binary `max` operator */
struct DeviceMax {
  template <typename T>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T& lhs, const T& rhs)
  {
    return std::max(lhs, rhs);
  }

  template <
    typename T,
    typename std::enable_if_t<!std::is_same_v<T, cudf::string_view> && !cudf::is_dictionary<T>() &&
                              !cudf::is_fixed_point<T>()>* = nullptr>
  static constexpr T identity()
  {
    return std::numeric_limits<T>::lowest();
  }

  template <typename T, typename std::enable_if_t<cudf::is_fixed_point<T>()>* = nullptr>
  static constexpr T identity()
  {
    CUDF_FAIL("fixed_point does not yet support DeviceMax identity");
    return std::numeric_limits<T>::lowest();
  }

  template <typename T, typename std::enable_if_t<std::is_same_v<T, cudf::string_view>>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE static constexpr T identity()
  {
    return string_view::min();
  }

  template <typename T, typename std::enable_if_t<cudf::is_dictionary<T>()>* = nullptr>
  static constexpr T identity()
  {
    return static_cast<T>(T::lowest_value());
  }
};

/* @brief binary `product` operator */
struct DeviceProduct {
  template <typename T, typename std::enable_if_t<!cudf::is_timestamp<T>()>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T& lhs, const T& rhs)
  {
    return lhs * rhs;
  }

  template <typename T, typename std::enable_if_t<!cudf::is_fixed_point<T>()>* = nullptr>
  static constexpr T identity()
  {
    return T{1};
  }

  template <typename T, typename std::enable_if_t<cudf::is_fixed_point<T>()>* = nullptr>
  static constexpr T identity()
  {
    CUDF_FAIL("fixed_point does not yet support DeviceProduct identity");
    return T{1, numeric::scale_type{0}};
  }
};

/* @brief binary `and` operator */
struct DeviceAnd {
  template <typename T, typename std::enable_if_t<std::is_integral<T>::value>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T& lhs, const T& rhs)
  {
    return (lhs & rhs);
  }
};

/* @brief binary `or` operator */
struct DeviceOr {
  template <typename T, typename std::enable_if_t<std::is_integral<T>::value>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T& lhs, const T& rhs)
  {
    return (lhs | rhs);
  }
};

/* @brief binary `xor` operator */
struct DeviceXor {
  template <typename T, typename std::enable_if_t<std::is_integral<T>::value>* = nullptr>
  CUDA_HOST_DEVICE_CALLABLE T operator()(const T& lhs, const T& rhs)
  {
    return (lhs ^ rhs);
  }
};

/**
 * @brief Operator for calculating Lead/Lag window function.
 */
struct DeviceLeadLag {
  const size_type row_offset;

  explicit CUDA_HOST_DEVICE_CALLABLE DeviceLeadLag(size_type offset_) : row_offset(offset_) {}
};

}  // namespace cudf
