# Copyright 2021 Google LLC. All Rights Reserved.
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
"""Tests for tfx.orchestration.experimental.core.pipeline_state."""

import os

import tensorflow as tf
from tfx.orchestration import metadata
from tfx.orchestration.experimental.core import pipeline_state as pstate
from tfx.orchestration.experimental.core import status as status_lib
from tfx.orchestration.experimental.core import task as task_lib
from tfx.proto.orchestration import pipeline_pb2
from tfx.utils import test_case_utils as tu

from ml_metadata.proto import metadata_store_pb2


def _test_pipeline(pipeline_id,
                   execution_mode: pipeline_pb2.Pipeline.ExecutionMode = (
                       pipeline_pb2.Pipeline.ASYNC)):
  pipeline = pipeline_pb2.Pipeline()
  pipeline.pipeline_info.id = pipeline_id
  pipeline.execution_mode = execution_mode
  pipeline.nodes.add().pipeline_node.node_info.id = 'Trainer'
  return pipeline


class PipelineStateTest(tu.TfxTest):

  def setUp(self):
    super(PipelineStateTest, self).setUp()
    pipeline_root = os.path.join(
        os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR', self.get_temp_dir()),
        self.id())

    # Makes sure multiple connections within a test always connect to the same
    # MLMD instance.
    metadata_path = os.path.join(pipeline_root, 'metadata', 'metadata.db')
    self._metadata_path = metadata_path
    connection_config = metadata.sqlite_metadata_connection_config(
        metadata_path)
    connection_config.sqlite.SetInParent()
    self._mlmd_connection = metadata.Metadata(
        connection_config=connection_config)

  def test_new_pipeline_state(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1')
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        pass

      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      self.assertLen(mlmd_contexts, 1)
      self.assertProtoPartiallyEquals(
          mlmd_contexts[0],
          pipeline_state.context,
          ignored_fields=[
              'create_time_since_epoch', 'last_update_time_since_epoch'
          ])

      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)
      self.assertProtoPartiallyEquals(
          mlmd_executions[0],
          pipeline_state.execution,
          ignored_fields=[
              'create_time_since_epoch', 'last_update_time_since_epoch'
          ])

      self.assertEqual(pipeline, pipeline_state.pipeline)
      self.assertEqual(
          task_lib.PipelineUid.from_pipeline(pipeline),
          pipeline_state.pipeline_uid)

  def test_load_pipeline_state(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1')
      with pstate.PipelineState.new(m, pipeline):
        pass

      pipeline_state = pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline))

      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      self.assertLen(mlmd_contexts, 1)
      self.assertProtoPartiallyEquals(mlmd_contexts[0], pipeline_state.context)

      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)
      self.assertProtoPartiallyEquals(mlmd_executions[0],
                                      pipeline_state.execution)

      self.assertEqual(pipeline, pipeline_state.pipeline)
      self.assertEqual(
          task_lib.PipelineUid.from_pipeline(pipeline),
          pipeline_state.pipeline_uid)

  def test_load_from_orchestrator_context(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1')
      with pstate.PipelineState.new(m, pipeline):
        pass

      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      self.assertLen(mlmd_contexts, 1)
      pipeline_state = pstate.PipelineState.load_from_orchestrator_context(
          m, mlmd_contexts[0])

      mlmd_contexts = pstate.get_orchestrator_contexts(m)
      self.assertLen(mlmd_contexts, 1)
      self.assertProtoPartiallyEquals(mlmd_contexts[0], pipeline_state.context)

      mlmd_executions = m.store.get_executions_by_context(mlmd_contexts[0].id)
      self.assertLen(mlmd_executions, 1)
      self.assertProtoPartiallyEquals(mlmd_executions[0],
                                      pipeline_state.execution)

      self.assertEqual(pipeline, pipeline_state.pipeline)
      self.assertEqual(
          task_lib.PipelineUid.from_pipeline(pipeline),
          pipeline_state.pipeline_uid)

  def test_new_pipeline_state_when_pipeline_already_exists(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1')
      with pstate.PipelineState.new(m, pipeline):
        pass

      with self.assertRaises(status_lib.StatusNotOkError) as exception_context:
        with pstate.PipelineState.new(m, pipeline):
          pass
      self.assertEqual(status_lib.Code.ALREADY_EXISTS,
                       exception_context.exception.code)

  def test_load_pipeline_state_when_no_active_pipeline(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1')
      pipeline_uid = task_lib.PipelineUid.from_pipeline(pipeline)

      # No such pipeline so NOT_FOUND error should be raised.
      with self.assertRaises(status_lib.StatusNotOkError) as exception_context:
        with pstate.PipelineState.load(m, pipeline_uid):
          pass
      self.assertEqual(status_lib.Code.NOT_FOUND,
                       exception_context.exception.code)

      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        pass

      # No error as there's an active pipeline.
      with pstate.PipelineState.load(m, pipeline_uid):
        pass

      # Inactivate the pipeline.
      execution = pipeline_state.execution
      execution.last_known_state = metadata_store_pb2.Execution.COMPLETE
      m.store.put_executions([execution])

      # No active pipeline so NOT_FOUND error should be raised.
      with self.assertRaises(status_lib.StatusNotOkError) as exception_context:
        with pstate.PipelineState.load(m, pipeline_uid):
          pass
      self.assertEqual(status_lib.Code.NOT_FOUND,
                       exception_context.exception.code)

  def test_stop_initiation(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1')
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        self.assertFalse(pipeline_state.is_stop_initiated())
        pipeline_state.initiate_stop()
        self.assertTrue(pipeline_state.is_stop_initiated())

      # Reload from MLMD and verify.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)) as pipeline_state:
        self.assertTrue(pipeline_state.is_stop_initiated())

  def test_initiate_node_start_stop(self):
    with self._mlmd_connection as m:
      pipeline = _test_pipeline('pipeline1')
      node_uid = task_lib.NodeUid(
          node_id='Trainer',
          pipeline_uid=task_lib.PipelineUid.from_pipeline(pipeline))
      with pstate.PipelineState.new(m, pipeline) as pipeline_state:
        pipeline_state.initiate_node_start(node_uid)
        self.assertFalse(pipeline_state.is_node_stop_initiated(node_uid))

      # Reload from MLMD and verify node is started.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)) as pipeline_state:
        self.assertFalse(pipeline_state.is_node_stop_initiated(node_uid))

        # Stop the node.
        pipeline_state.initiate_node_stop(node_uid)
        self.assertTrue(pipeline_state.is_node_stop_initiated(node_uid))

      # Reload from MLMD and verify node is stopped.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)) as pipeline_state:
        self.assertTrue(pipeline_state.is_node_stop_initiated(node_uid))

        # Restart node.
        pipeline_state.initiate_node_start(node_uid)
        self.assertFalse(pipeline_state.is_node_stop_initiated(node_uid))

      # Reload from MLMD and verify node is started.
      with pstate.PipelineState.load(
          m, task_lib.PipelineUid.from_pipeline(pipeline)) as pipeline_state:
        self.assertFalse(pipeline_state.is_node_stop_initiated(node_uid))


if __name__ == '__main__':
  tf.test.main()
