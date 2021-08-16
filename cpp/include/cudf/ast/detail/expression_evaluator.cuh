/*
 * Copyright (c) 2021, NVIDIA CORPORATION.
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

#include <cudf/ast/detail/expression_parser.hpp>
#include <cudf/ast/detail/operators.hpp>
#include <cudf/ast/nodes.hpp>
#include <cudf/ast/operators.hpp>
#include <cudf/column/column_device_view.cuh>
#include <cudf/column/column_factories.hpp>
#include <cudf/detail/utilities/assert.cuh>
#include <cudf/scalar/scalar_device_view.cuh>
#include <cudf/table/table_device_view.cuh>
#include <cudf/table/table_view.hpp>
#include <cudf/types.hpp>
#include <cudf/utilities/span.hpp>
#include <cudf/utilities/traits.hpp>

#include <rmm/cuda_stream_view.hpp>

#include <thrust/optional.h>

namespace cudf {

namespace ast {

namespace detail {

/**
 * @brief A container for capturing the output of an evaluated expression.
 *
 * This class is designed to be passed by reference as the first argument to
 * expression_evaluator::evaluate. The API is designed such that template
 * specializations for specific output types will be able to customize setting
 * behavior if necessary. The class leverages CRTP to define a suitable interface
 * for the `expression_evaluator` at compile-time and enforce this API on its
 * subclasses to get around the lack of device-side polymorphism.
 *
 * @tparam Subclass The subclass to dispatch methods to.
 * @tparam T The underlying data type.
 * @tparam has_nulls Whether or not the result data is nullable.
 */
template <typename Subclass, typename T, bool has_nulls>
struct expression_result {
  /**
   * Helper function to get the subclass type to dispatch methods to.
   */
  Subclass& subclass() { return static_cast<Subclass&>(*this); }
  Subclass const& subclass() const { return static_cast<Subclass const&>(*this); }

  // TODO: The index is ignored by the value subclass, but is included in this
  // signature because it is required by the implementation in the template
  // specialization for column views. It would be nice to clean this up, see
  // the related TODO below. Note that storing the index in the class on
  // construction (which would result in a cleaner delineation of the API for
  // the derived types) results in a significant performance penalty because
  // the index is pushed down the memory hierarchy by the time it needs to be
  // used, whereas passing it as a parameter keeps it in registers for fast
  // access at the point where indexing occurs.
  template <typename Element>
  __device__ void set_value(cudf::size_type index, possibly_null_value_t<Element, has_nulls> result)
  {
    subclass()->set_value();
  }

  __device__ bool is_valid() const { subclass()->is_valid(); }

  __device__ T value() const { subclass()->value(); }
};

/**
 * @brief A container for capturing the output of an evaluated expression in a scalar.
 *
 * This subclass of `expression_result` functions as an owning container of a
 * (possibly nullable) scalar type that can be written to by the
 * expression_evaluator. The data (and its validity) can then be accessed.
 *
 * @tparam T The underlying data type.
 * @tparam has_nulls Whether or not the result data is nullable.
 */
template <typename T, bool has_nulls>
struct value_expression_result
  : public expression_result<value_expression_result<T, has_nulls>, T, has_nulls> {
  __device__ value_expression_result() {}

  template <typename Element>
  __device__ void set_value(cudf::size_type index, possibly_null_value_t<Element, has_nulls> result)
  {
    if constexpr (std::is_same_v<Element, T>) {
      _obj = result;
    } else {
      cudf_assert(false && "Output type does not match container type.");
    }
  }

  /**
   * @brief Returns true if the underlying data is valid and false otherwise.
   */
  __device__ bool is_valid() const
  {
    if constexpr (has_nulls) { return _obj.has_value(); }
    return true;
  }

  /**
   * @brief Returns the underlying data.
   *
   * If the underlying data is not valid, behavior is undefined. Callers should
   * use is_valid to check for validity before accessing the value.
   */
  __device__ T value() const
  {
    // Using two separate constexprs silences compiler warnings, whereas an
    // if/else does not. An unconditional return is not ignored by the compiler
    // when has_nulls is true and therefore raises a compiler error.
    if constexpr (has_nulls) { return *_obj; }
    if constexpr (!has_nulls) { return _obj; }
  }

  possibly_null_value_t<T, has_nulls>
    _obj;  ///< The underlying data value, or a nullable version of it.
};

// TODO: The below implementation significantly differs from the default
// implementation above due to the non-owning nature of the container and the
// usage of the index. It would be ideal to unify these further if possible.

/**
 * @brief A container for capturing the output of an evaluated expression in a column.
 *
 * This subclass of `expression_result` functions as a non-owning container
 * that transparently passes calls through to an underlying mutable view to a
 * column. Not all methods are implemented
 *
 * @tparam has_nulls Whether or not the result data is nullable.
 */
template <bool has_nulls>
struct mutable_column_expression_result
  : public expression_result<mutable_column_expression_result<has_nulls>,
                             mutable_column_device_view,
                             has_nulls> {
  __device__ mutable_column_expression_result(mutable_column_device_view& obj) : _obj(obj) {}

  template <typename Element>
  __device__ void set_value(cudf::size_type index, possibly_null_value_t<Element, has_nulls> result)
  {
    if constexpr (has_nulls) {
      if (result.has_value()) {
        _obj.template element<Element>(index) = *result;
        _obj.set_valid(index);
      } else {
        _obj.set_null(index);
      }
    } else {
      _obj.template element<Element>(index) = result;
    }
  }

  /**
   * @brief Not implemented for this specialization.
   */
  __device__ bool is_valid() const
  {
    // Not implemented since it would require modifying the API in the parent class to accept an
    // index.
    cudf_assert(false && "This method is not implemented.");
  }

  /**
   * @brief Not implemented for this specialization.
   */
  __device__ mutable_column_device_view value() const
  {
    // Not implemented since it would require modifying the API in the parent class to accept an
    // index.
    cudf_assert(false && "This method is not implemented.");
  }

  mutable_column_device_view& _obj;  ///< The column to which the data is written.
};

/**
 * @brief Dispatch to a binary operator based on a single data type.
 *
 * This functor is a dispatcher for binary operations that assumes that both
 * operands are of the same type. This assumption is encoded in the
 * non-deducible template parameter LHS, the type of the left-hand operand,
 * which is then used as the template parameter for both the left and right
 * operands to the binary operator f.
 */
struct single_dispatch_binary_operator {
  /**
   * @brief Single-type dispatch to a binary operation.
   *
   * @tparam LHS Left input type.
   * @tparam F Type of forwarded binary operator functor.
   * @tparam Ts Parameter pack of forwarded arguments.
   *
   * @param f Binary operator functor.
   * @param args Forwarded arguments to `operator()` of `f`.
   */
  template <typename LHS, typename F, typename... Ts>
  CUDA_DEVICE_CALLABLE auto operator()(F&& f, Ts&&... args)
  {
    f.template operator()<LHS, LHS>(std::forward<Ts>(args)...);
  }
};

/**
 * @brief The principal object for evaluating AST expressions on device.
 *
 * This class is designed for n-ary transform evaluation. It operates on two
 * tables.
 */
template <bool has_nulls>
struct expression_evaluator {
 public:
  /**
   * @brief Construct an expression evaluator acting on two tables.
   *
   * @param left View of the left table view used for evaluation.
   * @param right View of the right table view used for evaluation.
   * @param plan The collection of device references representing the expression to evaluate.
   * @param thread_intermediate_storage Pointer to this thread's portion of shared memory for
   * storing intermediates.
   * @param compare_nulls Whether the equality operator returns true or false for two nulls.

   */
  __device__ expression_evaluator(table_device_view const& left,
                                  table_device_view const& right,
                                  expression_device_view const& plan,
                                  IntermediateDataType<has_nulls>* thread_intermediate_storage,
                                  null_equality compare_nulls = null_equality::EQUAL)
    : left(left),
      right(right),
      plan(plan),
      thread_intermediate_storage(thread_intermediate_storage),
      compare_nulls(compare_nulls)
  {
  }

  /**
   * @brief Construct an expression evaluator acting on one table.
   *
   * @param table View of the table view used for evaluation.
   * @param plan The collection of device references representing the expression to evaluate.
   * @param thread_intermediate_storage Pointer to this thread's portion of shared memory for
   * storing intermediates.
   * @param compare_nulls Whether the equality operator returns true or false for two nulls.
   */
  __device__ expression_evaluator(table_device_view const& table,
                                  expression_device_view const& plan,
                                  IntermediateDataType<has_nulls>* thread_intermediate_storage,
                                  null_equality compare_nulls = null_equality::EQUAL)
    : left(table),
      right(table),
      plan(plan),
      thread_intermediate_storage(thread_intermediate_storage),
      compare_nulls(compare_nulls)
  {
  }

  /**
   * @brief Resolves an input data reference into a value.
   *
   * Only input columns (COLUMN), literal values (LITERAL), and intermediates (INTERMEDIATE) are
   * supported as input data references. Intermediates must be of fixed width less than or equal to
   * sizeof(std::int64_t). This requirement on intermediates is enforced by the linearizer.
   *
   * @tparam Element Type of element to return.
   * @tparam has_nulls Whether or not the result data is nullable.
   * @param device_data_reference Data reference to resolve.
   * @param row_index Row index of data column.
   * @return Element The type- and null-resolved data.
   */
  template <typename Element, CUDF_ENABLE_IF(column_device_view::has_element_accessor<Element>())>
  __device__ possibly_null_value_t<Element, has_nulls> resolve_input(
    detail::device_data_reference device_data_reference,
    cudf::size_type left_row_index,
    thrust::optional<cudf::size_type> right_row_index = {}) const
  {
    auto const data_index = device_data_reference.data_index;
    auto const ref_type   = device_data_reference.reference_type;
    // TODO: Everywhere in the code assumes that the table reference is either
    // left or right. Should we error-check somewhere to prevent
    // table_reference::OUTPUT from being specified?
    using ReturnType = possibly_null_value_t<Element, has_nulls>;
    if (ref_type == detail::device_data_reference_type::COLUMN) {
      // If we have nullable data, return an empty nullable type with no value if the data is null.
      auto const& table =
        (device_data_reference.table_source == table_reference::LEFT) ? left : right;
      // Note that the code below assumes that a right index has been passed in
      // any case where device_data_reference.table_source == table_reference::RIGHT.
      // Otherwise, behavior is undefined.
      auto const row_index = (device_data_reference.table_source == table_reference::LEFT)
                               ? left_row_index
                               : *right_row_index;
      if constexpr (has_nulls) {
        return table.column(data_index).is_valid(row_index)
                 ? ReturnType(table.column(data_index).element<Element>(row_index))
                 : ReturnType();

      } else {
        return ReturnType(table.column(data_index).element<Element>(row_index));
      }
    } else if (ref_type == detail::device_data_reference_type::LITERAL) {
      return ReturnType(plan.literals[data_index].value<Element>());
    } else {  // Assumes ref_type == detail::device_data_reference_type::INTERMEDIATE
      // Using memcpy instead of reinterpret_cast<Element*> for safe type aliasing
      // Using a temporary variable ensures that the compiler knows the result is aligned
      IntermediateDataType<has_nulls> intermediate = thread_intermediate_storage[data_index];
      ReturnType tmp;
      memcpy(&tmp, &intermediate, sizeof(ReturnType));
      return tmp;
    }
    // Unreachable return used to silence compiler warnings.
    return {};
  }

  template <typename Element,
            CUDF_ENABLE_IF(not column_device_view::has_element_accessor<Element>())>
  __device__ possibly_null_value_t<Element, has_nulls> resolve_input(
    detail::device_data_reference device_data_reference,
    cudf::size_type left_row_index,
    thrust::optional<cudf::size_type> right_row_index = {}) const
  {
    cudf_assert(false && "Unsupported type in resolve_input.");
    // Unreachable return used to silence compiler warnings.
    return {};
  }

  /**
   * @brief Callable to perform a unary operation.
   *
   * @tparam Input Type of input value.
   * @tparam OutputType The container type that data will be inserted into.
   *
   * @param output_object The container that data will be inserted into.
   * @param input_row_index The row to pull the data from the input table.
   * @param input Input data reference.
   * @param output Output data reference.
   * @param output_row_index The row in the output to insert the result.
   * @param op The operator to act with.
   */
  template <typename Input, typename OutputType>
  __device__ void operator()(OutputType& output_object,
                             cudf::size_type const input_row_index,
                             detail::device_data_reference const input,
                             detail::device_data_reference const output,
                             cudf::size_type const output_row_index,
                             ast_operator const op) const
  {
    auto const typed_input = resolve_input<Input>(input, input_row_index);
    ast_operator_dispatcher(op,
                            unary_expression_output_handler<Input>(*this),
                            output_object,
                            output_row_index,
                            typed_input,
                            output);
  }

  /**
   * @brief Callable to perform a unary operation.
   *
   * @tparam LHS Type of the left input value.
   * @tparam RHS Type of the right input value.
   * @tparam OutputType The container type that data will be inserted into.
   *
   * @param output_object The container that data will be inserted into.
   * @param left_row_index The row to pull the data from the left table.
   * @param right_row_index The row to pull the data from the right table.
   * @param lhs Left input data reference.
   * @param rhs Right input data reference.
   * @param output Output data reference.
   * @param output_row_index The row in the output to insert the result.
   * @param op The operator to act with.
   */
  template <typename LHS, typename RHS, typename OutputType>
  __device__ void operator()(OutputType& output_object,
                             cudf::size_type const left_row_index,
                             cudf::size_type const right_row_index,
                             detail::device_data_reference const lhs,
                             detail::device_data_reference const rhs,
                             detail::device_data_reference const output,
                             cudf::size_type const output_row_index,
                             ast_operator const op) const
  {
    auto const typed_lhs = resolve_input<LHS>(lhs, left_row_index, right_row_index);
    auto const typed_rhs = resolve_input<RHS>(rhs, left_row_index, right_row_index);
    ast_operator_dispatcher(op,
                            binary_expression_output_handler<LHS, RHS>(*this),
                            output_object,
                            output_row_index,
                            typed_lhs,
                            typed_rhs,
                            output);
  }

  template <typename OperatorFunctor,
            typename LHS,
            typename RHS,
            typename OutputType,
            std::enable_if_t<!detail::is_valid_binary_op<OperatorFunctor, LHS, RHS>>* = nullptr>
  __device__ void operator()(OutputType& output_object,
                             cudf::size_type left_row_index,
                             cudf::size_type right_row_index,
                             detail::device_data_reference const lhs,
                             detail::device_data_reference const rhs,
                             detail::device_data_reference const output,
                             cudf::size_type output_row_index,
                             ast_operator const op) const
  {
    cudf_assert(false && "Invalid binary dispatch operator for the provided input.");
  }

  /**
   * @brief Evaluate an expression applied to a row.
   *
   * This function performs an n-ary transform for one row on one thread.
   *
   * @tparam OutputType The container type that data will be inserted into.
   *
   * @param output_object The container that data will be inserted into.
   * @param row_index Row index of all input and output data column(s).
   */
  template <typename OutputType>
  __device__ void evaluate(OutputType& output_object, cudf::size_type const row_index)
  {
    evaluate(output_object, row_index, row_index, row_index);
  }

  /**
   * @brief Evaluate an expression applied to a row.
   *
   * This function performs an n-ary transform for one row on one thread.
   *
   * @tparam OutputType The container type that data will be inserted into.
   *
   * @param output_object The container that data will be inserted into.
   * @param left_row_index The row to pull the data from the left table.
   * @param right_row_index The row to pull the data from the right table.
   * @param output_row_index The row in the output to insert the result.
   */
  template <typename OutputType>
  __device__ void evaluate(OutputType& output_object,
                           cudf::size_type const left_row_index,
                           cudf::size_type const right_row_index,
                           cudf::size_type const output_row_index)
  {
    cudf::size_type operator_source_index{0};
    for (cudf::size_type operator_index = 0; operator_index < plan.operators.size();
         ++operator_index) {
      // Execute operator
      auto const op    = plan.operators[operator_index];
      auto const arity = ast_operator_arity(op);
      if (arity == 1) {
        // Unary operator
        auto const input =
          plan.data_references[plan.operator_source_indices[operator_source_index++]];
        auto const output =
          plan.data_references[plan.operator_source_indices[operator_source_index++]];
        auto input_row_index =
          input.table_source == table_reference::LEFT ? left_row_index : right_row_index;
        type_dispatcher(input.data_type,
                        *this,
                        output_object,
                        input_row_index,
                        input,
                        output,
                        output_row_index,
                        op);
      } else if (arity == 2) {
        // Binary operator
        auto const lhs =
          plan.data_references[plan.operator_source_indices[operator_source_index++]];
        auto const rhs =
          plan.data_references[plan.operator_source_indices[operator_source_index++]];
        auto const output =
          plan.data_references[plan.operator_source_indices[operator_source_index++]];
        type_dispatcher(lhs.data_type,
                        detail::single_dispatch_binary_operator{},
                        *this,
                        output_object,
                        left_row_index,
                        right_row_index,
                        lhs,
                        rhs,
                        output,
                        output_row_index,
                        op);
      } else {
        cudf_assert(false && "Invalid operator arity.");
      }
    }
  }

 private:
  /**
   * @brief Helper struct for type dispatch on the result of an expression.
   *
   * Evaluating an expression requires multiple levels of type dispatch to
   * determine the input types, the operation type, and the output type. This
   * helper class is a functor that handles the operator dispatch, invokes the
   * operator, and dispatches output writing based on the resulting data type.
   */
  struct expression_output_handler {
   public:
    __device__ expression_output_handler(expression_evaluator<has_nulls> const& evaluator)
      : evaluator(evaluator)
    {
    }

    /**
     * @brief Resolves an output data reference and assigns result value.
     *
     * Only output columns (COLUMN) and intermediates (INTERMEDIATE) are supported as output
     * reference types. Intermediates must be of fixed width less than or equal to
     * sizeof(std::int64_t). This requirement on intermediates is enforced by the linearizer.
     *
     * @tparam Element Type of result element.
     * @tparam OutputType The container type that data will be inserted into.
     *
     * @param output_object The container that data will be inserted into.
     * @param device_data_reference Data reference to resolve.
     * @param row_index Row index of data column.
     * @param result Value to assign to output.
     */
    template <typename Element,
              typename OutputType,
              CUDF_ENABLE_IF(is_rep_layout_compatible<Element>())>
    __device__ void resolve_output(OutputType& output_object,
                                   detail::device_data_reference const device_data_reference,
                                   cudf::size_type const row_index,
                                   possibly_null_value_t<Element, has_nulls> const result) const
    {
      auto const ref_type = device_data_reference.reference_type;
      if (ref_type == detail::device_data_reference_type::COLUMN) {
        output_object.template set_value<Element>(row_index, result);
      } else {  // Assumes ref_type == detail::device_data_reference_type::INTERMEDIATE
        // Using memcpy instead of reinterpret_cast<Element*> for safe type aliasing.
        // Using a temporary variable ensures that the compiler knows the result is aligned.
        IntermediateDataType<has_nulls> tmp;
        memcpy(&tmp, &result, sizeof(possibly_null_value_t<Element, has_nulls>));
        evaluator.thread_intermediate_storage[device_data_reference.data_index] = tmp;
      }
    }

    template <typename Element,
              typename OutputType,
              CUDF_ENABLE_IF(not is_rep_layout_compatible<Element>())>
    __device__ void resolve_output(OutputType& output_object,
                                   detail::device_data_reference const device_data_reference,
                                   cudf::size_type const row_index,
                                   possibly_null_value_t<Element, has_nulls> const result) const
    {
      cudf_assert(false && "Invalid type in resolve_output.");
    }

   protected:
    expression_evaluator<has_nulls> const& evaluator;
  };

  /**
   * @brief Subclass of the expression output handler for unary operations.
   *
   * This functor's call operator is specialized to handle unary operations,
   * which only require a single operand.
   */
  template <typename Input>
  struct unary_expression_output_handler : public expression_output_handler {
    __device__ unary_expression_output_handler(expression_evaluator<has_nulls> const& evaluator)
      : expression_output_handler(evaluator)
    {
    }

    /**
     * @brief Callable to perform a unary operation.
     *
     * @tparam op The operation to perform.
     * @tparam OutputType The container type that data will be inserted into.
     *
     * @param output_object The container that data will be inserted into.
     * @param outputrow_index The row in the output object to insert the data.
     * @param input Input to the operation.
     * @param output Output data reference.
     */
    template <
      ast_operator op,
      typename OutputType,
      std::enable_if_t<detail::is_valid_unary_op<detail::operator_functor<op>, Input>>* = nullptr>
    __device__ void operator()(OutputType& output_object,
                               cudf::size_type const output_row_index,
                               possibly_null_value_t<Input, has_nulls> const input,
                               detail::device_data_reference const output) const
    {
      using OperatorFunctor = detail::operator_functor<op>;
      using Out             = cuda::std::invoke_result_t<OperatorFunctor, Input>;
      if constexpr (has_nulls) {
        auto const result = input.has_value()
                              ? possibly_null_value_t<Out, has_nulls>(OperatorFunctor{}(*input))
                              : possibly_null_value_t<Out, has_nulls>();
        this->template resolve_output<Out>(output_object, output, output_row_index, result);
      } else {
        this->template resolve_output<Out>(
          output_object, output, output_row_index, OperatorFunctor{}(input));
      }
    }

    template <
      ast_operator op,
      typename OutputType,
      std::enable_if_t<!detail::is_valid_unary_op<detail::operator_functor<op>, Input>>* = nullptr>
    __device__ void operator()(OutputType& output_object,
                               cudf::size_type const output_row_index,
                               possibly_null_value_t<Input, has_nulls> const input,
                               detail::device_data_reference const output) const
    {
      cudf_assert(false && "Invalid unary dispatch operator for the provided input.");
    }
  };

  /**
   * @brief Subclass of the expression output handler for binary operations.
   *
   * This functor's call operator is specialized to handle binary operations,
   * which require two operands.
   */
  template <typename LHS, typename RHS>
  struct binary_expression_output_handler : public expression_output_handler {
    __device__ binary_expression_output_handler(expression_evaluator<has_nulls> const& evaluator)
      : expression_output_handler(evaluator)
    {
    }

    /**
     * @brief Callable to perform a binary operation.
     *
     * @tparam op The operation to perform.
     * @tparam OutputType The container type that data will be inserted into.
     *
     * @param output_object The container that data will be inserted into.
     * @param output_row_index The row in the output to insert the result.
     * @param lhs Left input to the operation.
     * @param rhs Right input to the operation.
     * @param output Output data reference.
     */
    template <ast_operator op,
              typename OutputType,
              std::enable_if_t<
                detail::is_valid_binary_op<detail::operator_functor<op>, LHS, RHS>>* = nullptr>
    __device__ void operator()(OutputType& output_object,
                               cudf::size_type const output_row_index,
                               possibly_null_value_t<LHS, has_nulls> const lhs,
                               possibly_null_value_t<RHS, has_nulls> const rhs,
                               detail::device_data_reference const output) const
    {
      using OperatorFunctor = detail::operator_functor<op>;
      using Out             = cuda::std::invoke_result_t<OperatorFunctor, LHS, RHS>;
      if constexpr (has_nulls) {
        if constexpr (op == ast_operator::EQUAL) {
          // Special handling of the equality operator based on what kind
          // of null handling was requested.
          possibly_null_value_t<Out, has_nulls> result;
          if (!lhs.has_value() && !rhs.has_value()) {
            // Case 1: Both null, so the output is based on compare_nulls.
            result = possibly_null_value_t<Out, has_nulls>(this->evaluator.compare_nulls ==
                                                           null_equality::EQUAL);
          } else if (lhs.has_value() && rhs.has_value()) {
            // Case 2: Neither is null, so the output is given by the operation.
            result = possibly_null_value_t<Out, has_nulls>(OperatorFunctor{}(*lhs, *rhs));
          } else {
            // Case 3: One value is null, while the other is not, so we simply propagate nulls.
            result = possibly_null_value_t<Out, has_nulls>();
          }
          this->template resolve_output<Out>(output_object, output, output_row_index, result);
        } else {
          // Default behavior for all other operators is to propagate nulls.
          auto result = (lhs.has_value() && rhs.has_value())
                          ? possibly_null_value_t<Out, has_nulls>(OperatorFunctor{}(*lhs, *rhs))
                          : possibly_null_value_t<Out, has_nulls>();
          this->template resolve_output<Out>(output_object, output, output_row_index, result);
        }
      } else {
        this->template resolve_output<Out>(
          output_object, output, output_row_index, OperatorFunctor{}(lhs, rhs));
      }
    }

    template <ast_operator op,
              typename OutputType,
              std::enable_if_t<
                !detail::is_valid_binary_op<detail::operator_functor<op>, LHS, RHS>>* = nullptr>
    __device__ void operator()(OutputType& output_object,
                               cudf::size_type const output_row_index,
                               possibly_null_value_t<LHS, has_nulls> const lhs,
                               possibly_null_value_t<RHS, has_nulls> const rhs,
                               detail::device_data_reference output) const
    {
      cudf_assert(false && "Invalid binary dispatch operator for the provided input.");
    }
  };

  table_device_view const& left;   ///< The left table to operate on.
  table_device_view const& right;  ///< The right table to operate on.
  expression_device_view const&
    plan;  ///< The container of device data representing the expression to evaluate.
  IntermediateDataType<has_nulls>*
    thread_intermediate_storage;  ///< The shared memory store of intermediates produced during
                                  ///< evaluation.
  null_equality
    compare_nulls;  ///< Whether the equality operator returns true or false for two nulls.
};

}  // namespace detail

}  // namespace ast

}  // namespace cudf
