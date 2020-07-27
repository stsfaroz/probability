# Copyright 2020 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Drivers for streaming reductions framework."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import warnings

# Dependency imports
import tensorflow.compat.v2 as tf
from tensorflow_probability.python.experimental.mcmc import sample
from tensorflow_probability.python.experimental.mcmc import sample_discarding_kernel
from tensorflow_probability.python.experimental.mcmc import with_reductions
from tensorflow_probability.python.experimental import mcmc
from tensorflow_probability.python.mcmc.internal import util as mcmc_util
from tensorflow.python.util import nest  # pylint: disable=g-direct-tensorflow-import


__all__ = [
    'sample_chain',
    'sample_fold',
]


def sample_fold(
    num_steps,
    current_state,
    previous_kernel_results=None,
    kernel=None,
    reducer=None,
    num_burnin_steps=0,
    num_steps_between_results=0,
    parallel_iterations=10,
    seed=None,
    name=None,
):
  """Computes the requested reductions over the `kernel`'s samples.

  To wit, runs the given `kernel` for `num_steps` steps, and consumes
  the stream of samples with the given `Reducer`s' `one_step` method(s).
  This runs in constant memory (unless a given `Reducer` builds a
  large structure).

  The driver internally composes the correct onion of `WithReductions`
  and `SampleDiscardingKernel` to implement the requested optionally
  thinned reduction; however, the kernel results of those applied
  Transition Kernels will not be returned. Hence, if warm-restarting
  reductions is desired, one should manually build the Transition Kernel
  onion and use `tfp.experimental.mcmc.step_kernel`.

  An arbitrary collection of `reducer` can be provided, and the resulting
  finalized statistic(s) will be returned in an identical structure.

  Args:
    num_steps: Integer or scalar `Tensor` representing the number of `Reducer`
      steps.
    current_state: `Tensor` or Python `list` of `Tensor`s representing the
      current state(s) of the Markov chain(s).
    previous_kernel_results: A `Tensor` or a nested collection of `Tensor`s.
      Warm-start for the auxiliary state needed by the given `kernel`.
      If not supplied, `sample_fold` will cold-start with
      `kernel.bootstrap_results`.
    kernel: An instance of `tfp.mcmc.TransitionKernel` which implements one step
      of the Markov chain.
    reducer: A (possibly nested) structure of `Reducer`s to be evaluated
      on the `kernel`'s samples. If no reducers are given (`reducer=None`),
      then `None` will be returned in place of streaming calculations.
    num_burnin_steps: Integer or scalar `Tensor` representing the number
        of chain steps to take before starting to collect results.
        Defaults to 0 (i.e., no burn-in).
    num_steps_between_results: Integer or scalar `Tensor` representing
      the number of chain steps between collecting a result. Only one out
      of every `num_steps_between_samples + 1` steps is included in the
      returned results. Defaults to 0 (i.e., no thinning).
    parallel_iterations: The number of iterations allowed to run in parallel. It
      must be a positive integer. See `tf.while_loop` for more details.
    seed: Optional seed for reproducible sampling.
    name: Python `str` name prefixed to Ops created by this function.
      Default value: `None` (i.e., 'mcmc_sample_fold').

  Returns:
    reduction_results: A (possibly nested) structure of finalized reducer
      statistics. The structure identically mimics that of `reducer`.
    end_state: The final state of the Markov chain(s).
    final_kernel_results: `collections.namedtuple` of internal calculations
      used to advance the supplied `kernel`. These results do not include
      the kernel results of `WithReductions` or `SampleDiscardingKernel`.
  """
  with tf.name_scope(name or 'mcmc_sample_fold'):
    num_steps = tf.convert_to_tensor(
        num_steps, dtype=tf.int32, name='num_steps')
    current_state = tf.nest.map_structure(
        lambda x: tf.convert_to_tensor(x, name='current_state'),
        current_state)
    reducer_was_none = False
    if reducer is None:
      reducer = []
      reducer_was_none = True
    reduction_kernel = with_reductions.WithReductions(
        inner_kernel=sample_discarding_kernel.SampleDiscardingKernel(
            inner_kernel=kernel,
            num_burnin_steps=num_burnin_steps,
            num_steps_between_results=num_steps_between_results),
        reducer=reducer,
    )
    end_state, final_kernel_results = sample.step_kernel(
        num_steps=num_steps,
        current_state=current_state,
        previous_kernel_results=previous_kernel_results,
        kernel=reduction_kernel,
        return_final_kernel_results=True,
        parallel_iterations=parallel_iterations,
        seed=seed,
        name=name,
    )
    reduction_results = nest.map_structure_up_to(
        reducer,
        lambda r, s: r.finalize(s),
        reducer,
        final_kernel_results.streaming_calculations,
        check_types=False)
    if reducer_was_none:
      reduction_results = None
    return (reduction_results,
            end_state,
            final_kernel_results.inner_results.inner_results)


class StatesAndTrace(
    mcmc_util.PrettyNamedTupleMixin,
    collections.namedtuple('StatesAndTrace', ['all_states', 'trace'])):
  """States and auxiliary trace of an MCMC chain.

  The first dimension of all the `Tensor`s in this structure is the same and
  represents the chain length.

  Attributes:
    all_states: A `Tensor` or a nested collection of `Tensor`s representing the
      MCMC chain state.
    trace: A `Tensor` or a nested collection of `Tensor`s representing the
      auxiliary values traced alongside the chain.
  """
  __slots__ = ()


class CheckpointableStatesAndTrace(
    mcmc_util.PrettyNamedTupleMixin,
    collections.namedtuple('CheckpointableStatesAndTrace',
                           ['all_states', 'trace', 'final_kernel_results'])):
  """States and auxiliary trace of an MCMC chain.

  The first dimension of all the `Tensor`s in the `all_states` and `trace`
  attributes is the same and represents the chain length.

  Attributes:
    all_states: A `Tensor` or a nested collection of `Tensor`s representing the
      MCMC chain state.
    trace: A `Tensor` or a nested collection of `Tensor`s representing the
      auxiliary values traced alongside the chain.
    final_kernel_results: A `Tensor` or a nested collection of `Tensor`s
      representing the final value of the auxiliary state of the
      `TransitionKernel` that generated this chain.
  """
  __slots__ = ()


def sample_chain(
    num_results,
    current_state,
    previous_kernel_results=None,
    kernel=None,
    num_burnin_steps=0,
    num_steps_between_results=0,
    trace_fn=lambda current_state, kernel_results: kernel_results,
    return_final_kernel_results=False,
    parallel_iterations=10,
    seed=None,
    name=None,
):
  with tf.name_scope(name or 'mcmc_sample_chain'):
    if not kernel.is_calibrated:
      warnings.warn('supplied `TransitionKernel` is not calibrated. Markov '
                    'chain may not converge to intended target distribution.')

    if trace_fn is None:
      trace_fn = lambda *args: ()
      no_trace = True
    else:
      no_trace = False

    if trace_fn is sample_chain.__defaults__[4]:
      warnings.warn('Tracing all kernel results by default is deprecated. Set '
                    'the `trace_fn` argument to None (the future default '
                    'value) or an explicit callback that traces the values '
                    'you are interested in.')

    tracing_reducer = mcmc.TracingReducer(
        trace_fn=lambda curr_state, kr: (curr_state, trace_fn(curr_state, kr)),
        size=num_results
    )
    trace_results, _, final_kernel_results = sample_fold(
        num_steps=num_results,
        current_state=current_state,
        previous_kernel_results=previous_kernel_results,
        kernel=kernel,
        reducer=tracing_reducer,
        num_burnin_steps=num_burnin_steps,
        num_steps_between_results=num_steps_between_results,
        parallel_iterations=parallel_iterations,
        seed=seed,
        name=name,
    )

    all_states, trace = trace_results
    if return_final_kernel_results:
      return CheckpointableStatesAndTrace(
          all_states=all_states,
          trace=trace,
          final_kernel_results=final_kernel_results)
    else:
      if no_trace:
        return all_states
      else:
        return StatesAndTrace(all_states=all_states, trace=trace)
