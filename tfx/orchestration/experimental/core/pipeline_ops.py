# Copyright 2020 Google LLC. All Rights Reserved.
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
"""Pipeline-level operations."""

import copy
import functools
import threading
import time
from typing import List, Sequence

from absl import logging
from tfx.orchestration import metadata
from tfx.orchestration.experimental.core import async_pipeline_task_gen
from tfx.orchestration.experimental.core import pipeline_state as pstate
from tfx.orchestration.experimental.core import status as status_lib
from tfx.orchestration.experimental.core import sync_pipeline_task_gen
from tfx.orchestration.experimental.core import task as task_lib
from tfx.orchestration.experimental.core import task_gen_utils
from tfx.orchestration.experimental.core import task_queue as tq
from tfx.orchestration.portable.mlmd import execution_lib
from tfx.proto.orchestration import pipeline_pb2

from ml_metadata.proto import metadata_store_pb2

# A coarse grained lock is used to ensure serialization of pipeline operations
# since there isn't a suitable MLMD transaction API.
_PIPELINE_OPS_LOCK = threading.RLock()


def _pipeline_ops_lock(fn):
  """Decorator to run `fn` within `_PIPELINE_OPS_LOCK` context."""

  @functools.wraps(fn)
  def _wrapper(*args, **kwargs):
    with _PIPELINE_OPS_LOCK:
      return fn(*args, **kwargs)

  return _wrapper


def _to_status_not_ok_error(fn):
  """Decorator to catch exceptions and re-raise a `status_lib.StatusNotOkError`."""

  @functools.wraps(fn)
  def _wrapper(*args, **kwargs):
    try:
      return fn(*args, **kwargs)
    except Exception as e:  # pylint: disable=broad-except
      logging.exception('Error raised by `%s`:', fn.__name__)
      if isinstance(e, status_lib.StatusNotOkError):
        raise
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.UNKNOWN,
          message=f'`{fn.__name__}` error: {str(e)}')

  return _wrapper


@_to_status_not_ok_error
@_pipeline_ops_lock
def initiate_pipeline_start(
    mlmd_handle: metadata.Metadata,
    pipeline: pipeline_pb2.Pipeline) -> pstate.PipelineState:
  """Initiates a pipeline start operation.

  Upon success, MLMD is updated to signal that the given pipeline must be
  started.

  Args:
    mlmd_handle: A handle to the MLMD db.
    pipeline: IR of the pipeline to start.

  Returns:
    The `PipelineState` object upon success.

  Raises:
    status_lib.StatusNotOkError: Failure to initiate pipeline start or if
      execution is not inactive after waiting `timeout_secs`.
  """
  with pstate.PipelineState.new(mlmd_handle, pipeline) as pipeline_state:
    pass
  return pipeline_state


DEFAULT_WAIT_FOR_INACTIVATION_TIMEOUT_SECS = 120.0


@_to_status_not_ok_error
def stop_pipeline(
    mlmd_handle: metadata.Metadata,
    pipeline_uid: task_lib.PipelineUid,
    timeout_secs: float = DEFAULT_WAIT_FOR_INACTIVATION_TIMEOUT_SECS) -> None:
  """Stops a pipeline.

  Initiates a pipeline stop operation and waits for the pipeline execution to be
  gracefully stopped in the orchestration loop.

  Args:
    mlmd_handle: A handle to the MLMD db.
    pipeline_uid: Uid of the pipeline to be stopped.
    timeout_secs: Amount of time in seconds to wait for pipeline to stop.

  Raises:
    status_lib.StatusNotOkError: Failure to initiate pipeline stop.
  """
  with _PIPELINE_OPS_LOCK:
    with pstate.PipelineState.load(mlmd_handle, pipeline_uid) as pipeline_state:
      pipeline_state.initiate_stop()
  _wait_for_inactivation(
      mlmd_handle, pipeline_state.execution, timeout_secs=timeout_secs)


@_to_status_not_ok_error
@_pipeline_ops_lock
def initiate_node_start(mlmd_handle: metadata.Metadata,
                        node_uid: task_lib.NodeUid) -> pstate.PipelineState:
  with pstate.PipelineState.load(mlmd_handle,
                                 node_uid.pipeline_uid) as pipeline_state:
    pipeline_state.initiate_node_start(node_uid)
  return pipeline_state


@_to_status_not_ok_error
def stop_node(
    mlmd_handle: metadata.Metadata,
    node_uid: task_lib.NodeUid,
    timeout_secs: float = DEFAULT_WAIT_FOR_INACTIVATION_TIMEOUT_SECS) -> None:
  """Stops a node in a pipeline.

  Initiates a node stop operation and waits for the node execution to become
  inactive.

  Args:
    mlmd_handle: A handle to the MLMD db.
    node_uid: Uid of the node to be stopped.
    timeout_secs: Amount of time in seconds to wait for node to stop.

  Raises:
    status_lib.StatusNotOkError: Failure to stop the node.
  """
  with _PIPELINE_OPS_LOCK:
    with pstate.PipelineState.load(mlmd_handle,
                                   node_uid.pipeline_uid) as pipeline_state:
      nodes = pstate.get_all_pipeline_nodes(pipeline_state.pipeline)
      filtered_nodes = [n for n in nodes if n.node_info.id == node_uid.node_id]
      if len(filtered_nodes) != 1:
        raise status_lib.StatusNotOkError(
            code=status_lib.Code.INTERNAL,
            message=(
                f'`stop_node` operation failed, unable to find node to stop: '
                f'{node_uid}'))
      node = filtered_nodes[0]
      pipeline_state.initiate_node_stop(node_uid)

    executions = task_gen_utils.get_executions(mlmd_handle, node)
    active_executions = [
        e for e in executions if execution_lib.is_execution_active(e)
    ]
    if not active_executions:
      # If there are no active executions, we're done.
      return
    if len(active_executions) > 1:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.INTERNAL,
          message=(
              f'Unexpected multiple active executions for node: {node_uid}'))
  _wait_for_inactivation(
      mlmd_handle, active_executions[0], timeout_secs=timeout_secs)


@_to_status_not_ok_error
def _wait_for_inactivation(
    mlmd_handle: metadata.Metadata,
    execution: metadata_store_pb2.Execution,
    timeout_secs: float = DEFAULT_WAIT_FOR_INACTIVATION_TIMEOUT_SECS) -> None:
  """Waits for the given execution to become inactive.

  Args:
    mlmd_handle: A handle to the MLMD db.
    execution: Execution whose inactivation is waited.
    timeout_secs: Amount of time in seconds to wait.

  Raises:
    StatusNotOkError: With error code `DEADLINE_EXCEEDED` if execution is not
      inactive after waiting approx. `timeout_secs`.
  """
  polling_interval_secs = min(10.0, timeout_secs / 4)
  end_time = time.time() + timeout_secs
  while end_time - time.time() > 0:
    updated_executions = mlmd_handle.store.get_executions_by_id([execution.id])
    if not execution_lib.is_execution_active(updated_executions[0]):
      return
    time.sleep(max(0, min(polling_interval_secs, end_time - time.time())))
  raise status_lib.StatusNotOkError(
      code=status_lib.Code.DEADLINE_EXCEEDED,
      message=(f'Timed out ({timeout_secs} secs) waiting for execution '
               f'inactivation.'))


@_to_status_not_ok_error
@_pipeline_ops_lock
def generate_tasks(mlmd_handle: metadata.Metadata,
                   task_queue: tq.TaskQueue) -> None:
  """Generates and enqueues tasks to be performed.

  Embodies the core functionality of the main orchestration loop that scans MLMD
  pipeline execution states, generates and enqueues the tasks to be performed.

  Args:
    mlmd_handle: A handle to the MLMD db.
    task_queue: A `TaskQueue` instance into which any tasks will be enqueued.

  Raises:
    status_lib.StatusNotOkError: If error generating tasks.
  """
  pipeline_states = _get_pipeline_states(mlmd_handle)
  if not pipeline_states:
    logging.info('No active pipelines to run.')
    return

  active_pipeline_states = []
  stop_initiated_pipeline_states = []
  for pipeline_state in pipeline_states:
    if pipeline_state.is_stop_initiated():
      stop_initiated_pipeline_states.append(pipeline_state)
    elif execution_lib.is_execution_active(pipeline_state.execution):
      active_pipeline_states.append(pipeline_state)
    else:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.INTERNAL,
          message=(f'Found pipeline (uid: {pipeline_state.pipeline_uid}) which '
                   f'is neither active nor stop-initiated.'))

  if stop_initiated_pipeline_states:
    logging.info(
        'Stop-initiated pipeline uids:\n%s', '\n'.join(
            str(pipeline_state.pipeline_uid)
            for pipeline_state in stop_initiated_pipeline_states))
    _process_stop_initiated_pipelines(mlmd_handle, task_queue,
                                      stop_initiated_pipeline_states)
  if active_pipeline_states:
    logging.info(
        'Active (excluding stop-initiated) pipeline uids:\n%s', '\n'.join(
            str(pipeline_state.pipeline_uid)
            for pipeline_state in active_pipeline_states))
    _process_active_pipelines(mlmd_handle, task_queue, active_pipeline_states)


def _get_pipeline_states(
    mlmd_handle: metadata.Metadata) -> List[pstate.PipelineState]:
  """Scans MLMD and returns pipeline states."""
  contexts = pstate.get_orchestrator_contexts(mlmd_handle)
  result = []
  for context in contexts:
    try:
      pipeline_state = pstate.PipelineState.load_from_orchestrator_context(
          mlmd_handle, context)
    except status_lib.StatusNotOkError as e:
      if e.code == status_lib.Code.NOT_FOUND:
        # Ignore any old contexts with no associated active pipelines.
        logging.info(e.message)
        continue
      else:
        raise
    result.append(pipeline_state)
  return result


def _process_stop_initiated_pipelines(
    mlmd_handle: metadata.Metadata, task_queue: tq.TaskQueue,
    pipeline_states: Sequence[pstate.PipelineState]) -> None:
  """Processes stop initiated pipelines."""
  for pipeline_state in pipeline_states:
    pipeline = pipeline_state.pipeline
    execution = pipeline_state.execution
    has_active_executions = False
    for node in pstate.get_all_pipeline_nodes(pipeline):
      if _maybe_enqueue_cancellation_task(mlmd_handle, pipeline, node,
                                          task_queue):
        has_active_executions = True
    if not has_active_executions:
      updated_execution = copy.deepcopy(execution)
      updated_execution.last_known_state = metadata_store_pb2.Execution.CANCELED
      mlmd_handle.store.put_executions([updated_execution])


def _process_active_pipelines(
    mlmd_handle: metadata.Metadata, task_queue: tq.TaskQueue,
    pipeline_states: Sequence[pstate.PipelineState]) -> None:
  """Processes active pipelines."""
  for pipeline_state in pipeline_states:
    pipeline = pipeline_state.pipeline
    execution = pipeline_state.execution
    assert execution.last_known_state in (metadata_store_pb2.Execution.NEW,
                                          metadata_store_pb2.Execution.RUNNING)
    if execution.last_known_state != metadata_store_pb2.Execution.RUNNING:
      updated_execution = copy.deepcopy(execution)
      updated_execution.last_known_state = metadata_store_pb2.Execution.RUNNING
      mlmd_handle.store.put_executions([updated_execution])

    # Create cancellation tasks for stop-initiated nodes if necessary.
    stop_initiated_nodes = _get_stop_initiated_nodes(pipeline_state)
    for node in stop_initiated_nodes:
      _maybe_enqueue_cancellation_task(mlmd_handle, pipeline, node, task_queue)

    # Initialize task generator for the pipeline.
    if pipeline.execution_mode == pipeline_pb2.Pipeline.SYNC:
      generator = sync_pipeline_task_gen.SyncPipelineTaskGenerator(
          mlmd_handle, pipeline, task_queue.contains_task_id)
    elif pipeline.execution_mode == pipeline_pb2.Pipeline.ASYNC:
      generator = async_pipeline_task_gen.AsyncPipelineTaskGenerator(
          mlmd_handle, pipeline, task_queue.contains_task_id,
          set(n.node_info.id for n in stop_initiated_nodes))
    else:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.FAILED_PRECONDITION,
          message=(
              f'Only SYNC and ASYNC pipeline execution modes supported; '
              f'found pipeline with execution mode: {pipeline.execution_mode}'))

    # TODO(goutham): Consider concurrent task generation.
    tasks = generator.generate()
    for task in tasks:
      task_queue.enqueue(task)


def _get_stop_initiated_nodes(
    pipeline_state: pstate.PipelineState) -> List[pipeline_pb2.PipelineNode]:
  """Returns list of all stop initiated nodes."""
  nodes = pstate.get_all_pipeline_nodes(pipeline_state.pipeline)
  result = []
  for node in nodes:
    node_uid = task_lib.NodeUid.from_pipeline_node(pipeline_state.pipeline,
                                                   node)
    if pipeline_state.is_node_stop_initiated(node_uid):
      result.append(node)
  return result


def _maybe_enqueue_cancellation_task(mlmd_handle: metadata.Metadata,
                                     pipeline: pipeline_pb2.Pipeline,
                                     node: pipeline_pb2.PipelineNode,
                                     task_queue: tq.TaskQueue) -> bool:
  """Enqueues a node cancellation task if not already stopped.

  If the node has an ExecNodeTask in the task queue, issue a cancellation.
  Otherwise, if the node has an active execution in MLMD but no ExecNodeTask
  enqueued, it may be due to orchestrator restart after stopping was initiated
  but before the schedulers could finish. So, enqueue an ExecNodeTask with
  is_cancelled set to give a chance for the scheduler to finish gracefully.

  Args:
    mlmd_handle: A handle to the MLMD db.
    pipeline: The pipeline containing the node to cancel.
    node: The node to cancel.
    task_queue: A `TaskQueue` instance into which any cancellation tasks will be
      enqueued.

  Returns:
    `True` if a cancellation task was enqueued. `False` if node is already
    stopped or no cancellation was required.
  """
  if not task_gen_utils.is_feasible_node(node):
    return False
  exec_node_task_id = task_lib.exec_node_task_id_from_pipeline_node(
      pipeline, node)
  if task_queue.contains_task_id(exec_node_task_id):
    task_queue.enqueue(
        task_lib.CancelNodeTask(
            node_uid=task_lib.NodeUid.from_pipeline_node(pipeline, node)))
    return True
  else:
    executions = task_gen_utils.get_executions(mlmd_handle, node)
    exec_node_task = task_gen_utils.generate_task_from_active_execution(
        mlmd_handle, pipeline, node, executions, is_cancelled=True)
    if exec_node_task:
      task_queue.enqueue(exec_node_task)
      return True
  return False
