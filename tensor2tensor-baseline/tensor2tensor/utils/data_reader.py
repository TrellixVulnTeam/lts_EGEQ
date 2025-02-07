# coding=utf-8
# Copyright 2017 The Tensor2Tensor Authors.
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

"""Data reader module."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools

# Dependency imports

import numpy as np

import six
from six.moves import xrange  # pylint: disable=redefined-builtin

import tensorflow as tf


def cast_int64_to_int32(features):
  f = {}
  for k, v in six.iteritems(features):
    if v.dtype == tf.int64:
      v = tf.to_int32(v)
    f[k] = v
  return f


def feature_placeholders(data_fields, data_items_to_decoders):
  """Construct Placeholders and run decoders."""
  example = {}
  for field, config in data_fields.items():
    if isinstance(config, tf.VarLenFeature):
      shape = [None, None]
    else:
      shape = config.shape

    example[field] = tf.placeholder(dtype=config.dtype, shape=shape, name=field)

  # Decode
  if data_items_to_decoders is None:
    data_items_to_decoders = {
        field: tf.contrib.slim.tfexample_decoder.Tensor(field)
        for field in data_fields
    }

  decoded_example = {}
  for field, decoder in data_items_to_decoders.items():
    keys_to_tensors = {key: example[key] for key in decoder.keys}
    decoded_example[field] = decoder.tensors_to_item(keys_to_tensors)

  return decoded_example


def input_pipeline(problem,
                   data_dir,
                   capacity,
                   mode,
                   hparams,
                   batching_scheme,
                   dataset_split=None,
                   shard=None):
  """Input pipeline, returns a dictionary of batched and padded tensors.

  Args:
    problem: Problem instance for which to build the input pipeline.
    data_dir: directory with input data.
    capacity: int, data pipeline buffer capacity.
    mode: tf.estimator.ModeKeys entry.
    hparams: an HParams object.
    batching_scheme: a dictionary containing
      "boundaries": a list of integers for the boundaries that will be
        used for bucketing; see bucket_by_sequence_length for more details.
      "batch_sizes": a list of batch sizes corresponding to the buckets
      "min_length": an integer.  We drop sequences which are shorter.
      "max_length": an integer.  We drop sequences which are longer.
    dataset_split: tf.estimator.ModeKeys + ["test"], which split of the dataset
      to use. Defaults to mode.
    shard: int, if provided, will only read data from the specified shard.

  Returns:
    dict <feature name, batched and padded Tensor>
  """
  is_training = mode == tf.estimator.ModeKeys.TRAIN
  num_threads = 4 if is_training else 1

  with tf.name_scope("input_pipeline"):
    dataset = problem.dataset(
        mode,
        data_dir=data_dir,
        num_threads=num_threads,
        output_buffer_size=capacity,
        hparams=hparams,
        dataset_split=dataset_split,
        shard=shard)
    print("#$%dataset",dataset)
    dataset = dataset.map(cast_int64_to_int32, num_threads=num_threads)
    #dataset = dataset.filter(
    #    functools.partial(
    #        example_valid_size,
    #        min_length=batching_scheme["min_length"],
    #        max_length=batching_scheme["max_length"],
    #    ))
    if is_training:
      dataset = dataset.shuffle(capacity)
      dataset = dataset.repeat(None)

    bucket_id_fn = _example_length
    if len(batching_scheme["boundaries"]) == 1:
      bucket_id_fn = lambda _: tf.constant(0)

    if "padded_shapes" not in batching_scheme:
      batching_scheme["padded_shapes"] = None

    dataset = bucket_by_sequence_length(
        dataset,
        bucket_id_fn,
        batching_scheme["boundaries"],
        batching_scheme["batch_sizes"],
        batching_scheme["window_size"],
        padded_shapes=batching_scheme["padded_shapes"])

    # We reshuffle the batches to prevent many long-sequence batches at once.
    # TODO(rsepassi): Rm hasattr call once new dynamic window size functionality
    # is in a stable TF release.
    if (batching_scheme["shuffle_queue_size"] is not None and
        not hasattr(dataset, "apply")):
      dataset = dataset.shuffle(batching_scheme["shuffle_queue_size"])
    batched_examples = dataset.make_one_shot_iterator().get_next()
    #batched_examples["teachers"]=tf.Print(batched_examples["teachers"],[batched_examples["teachers"]],"batched_examples",summarize=1000)
    return batched_examples


def _example_length(example):
  length = 0
  # Length of the example is the maximum length of the feature lengths
  for v in example.values():
    # For images the sequence length is the size of the spatial dimensions.
    feature_length = (tf.shape(v)[0] if len(v.get_shape()) < 3 else
                      tf.shape(v)[0] * tf.shape(v)[1])
    length = tf.maximum(length, feature_length)
  return length


def example_valid_size(example, min_length, max_length):
  length = _example_length(example)
  return tf.logical_and(
      length >= min_length,
      length <= max_length,
  )


def bucket_by_sequence_length(dataset,
                              example_length_fn,
                              bucket_boundaries,
                              bucket_batch_sizes,
                              window_size,
                              padded_shapes=None):
  """Bucket entries in dataset by length.

  Args:
    dataset: Dataset of dict<feature name, Tensor>.
    example_length_fn: function from example to int, determines the length of
      the example, which will determine the bucket it goes into.
    bucket_boundaries: list<int>, boundaries of the buckets.
    bucket_batch_sizes: list<int>, batch size per bucket.
    window_size: an integer divisible by all elements of bucket_batch_sizes
    padded_shapes: dict<feature name, list<int>>, optional, shapes of the
      features with None where feature should be padded to max in that dim.

  Returns:
    Dataset of padded and batched examples.
  """
  with tf.name_scope("bucket_by_seq_length"):

    def example_to_bucket_id(example):
      """Return int64 id of the length bucket for this example."""
      seq_length = example_length_fn(example)

      boundaries = list(bucket_boundaries)
      buckets_min = [np.iinfo(np.int32).min] + boundaries
      buckets_max = boundaries + [np.iinfo(np.int32).max]
      conditions_c = tf.logical_and(
          tf.less_equal(buckets_min, seq_length),
          tf.less(seq_length, buckets_max))
      bucket_id = tf.reduce_min(tf.where(conditions_c))

      return bucket_id

    def window_size_fn(bucket_id):
      # window size = batch size
      batch_sizes = tf.constant(bucket_batch_sizes, dtype=tf.int64)
      window_size = batch_sizes[bucket_id]
      return window_size

    def batching_fn(bucket_id, grouped_dataset):
      batch_sizes = tf.constant(bucket_batch_sizes, dtype=tf.int64)
      batch_size = batch_sizes[bucket_id]
      return padded_batch(grouped_dataset, batch_size, padded_shapes)

    # TODO(rsepassi): Rm branch once the new group_by_window functionality is in
    # a stable TF release.
    if hasattr(dataset, "apply"):
      # If the Dataset supports dynamic window size, use it.
      dataset = dataset.apply(
          tf.contrib.data.group_by_window(example_to_bucket_id, batching_fn,
                                          None, window_size_fn))
    else:
      dataset = dataset.group_by_window(example_to_bucket_id, batching_fn,
                                        window_size)
    return dataset


def padded_batch(dataset, batch_size, padded_shapes=None):
  padded_shapes = padded_shapes or dict(
      [(name, [None] * len(shape))
       for name, shape in dataset.output_shapes.items()])
  return dataset.padded_batch(batch_size, padded_shapes)


def _bucket_boundaries(max_length, min_length=8, length_bucket_step=1.1):
  """A default set of length-bucket boundaries."""
  assert length_bucket_step > 1.0
  x = min_length
  boundaries = []
  while x < max_length:
    boundaries.append(x)
    x = max(x + 1, int(x * length_bucket_step))
  return boundaries


def _batching_scheme(batch_size,
                     max_length,
                     min_length_bucket,
                     length_bucket_step,
                     drop_long_sequences=False,
                     shard_multiplier=1,
                     length_multiplier=1,
                     min_length=0):
  """A batching scheme based on model hyperparameters.

  Every batch containins a number of sequences divisible by `shard_multiplier`.

  Args:
    batch_size: int, total number of tokens in a batch.
    max_length: int, sequences longer than this will be skipped. Defaults to
      batch_size.
    min_length_bucket: int
    length_bucket_step: float greater than 1.0
    drop_long_sequences: bool, if True, then sequences longer than
      `max_length` are dropped.  This prevents generating batches with
      more than the usual number of tokens, which can cause out-of-memory
      errors.
    shard_multiplier: an integer increasing the batch_size to suit splitting
      across datashards.
    length_multiplier: an integer multiplier that is used to increase the
      batch sizes and sequence length tolerance.
    min_length: int, sequences shorter than this will be skipped.

  Returns:
     A dictionary with parameters that can be passed to input_pipeline:
       * boundaries: list of bucket boundaries
       * batch_sizes: list of batch sizes for each length bucket
       * max_length: int, maximum length of an example

  Raises:
    ValueError: If min_length > max_length
  """
  max_length = max_length or batch_size
  if max_length < min_length:
    raise ValueError("max_length must be greater or equal to min_length")

  boundaries = _bucket_boundaries(max_length, min_length_bucket,
                                  length_bucket_step)
  boundaries = [boundary * length_multiplier for boundary in boundaries]
  max_length *= length_multiplier

  batch_sizes = [
      max(1, batch_size // length) for length in boundaries + [max_length]
  ]
  max_batch_size = max(batch_sizes)
  # Since the Datasets API only allows a single constant for window_size,
  # and it needs divide all bucket_batch_sizes, we pick a highly-compoisite
  # window size and then round down all batch sizes to divisors of that window
  # size, so that a window can always be divided evenly into batches.
  # TODO(noam): remove this when Dataset API improves.
  highly_composite_numbers = [
      1, 2, 4, 6, 12, 24, 36, 48, 60, 120, 180, 240, 360, 720, 840, 1260, 1680,
      2520, 5040, 7560, 10080, 15120, 20160, 25200, 27720, 45360, 50400, 55440,
      83160, 110880, 166320, 221760, 277200, 332640, 498960, 554400, 665280,
      720720, 1081080, 1441440, 2162160, 2882880, 3603600, 4324320, 6486480,
      7207200, 8648640, 10810800, 14414400, 17297280, 21621600, 32432400,
      36756720, 43243200, 61261200, 73513440, 110270160
  ]
  window_size = max(
      [i for i in highly_composite_numbers if i <= 3 * max_batch_size])
  divisors = [i for i in xrange(1, window_size + 1) if window_size % i == 0]
  batch_sizes = [max([d for d in divisors if d <= bs]) for bs in batch_sizes]
  window_size *= shard_multiplier
  batch_sizes = [bs * shard_multiplier for bs in batch_sizes]
  # The Datasets API splits one window into multiple batches, which
  # produces runs of many consecutive batches of the same size.  This
  # is bad for training.  To solve this, we will shuffle the batches
  # using a queue which must be several times as large as the maximum
  # number of batches per window.
  max_batches_per_window = window_size // min(batch_sizes)
  shuffle_queue_size = max_batches_per_window * 3

  ret = {
      "boundaries": boundaries,
      "batch_sizes": batch_sizes,
      "min_length": min_length,
      "max_length": (max_length if drop_long_sequences else 10**9),
      "shuffle_queue_size": shuffle_queue_size,
      "window_size": window_size,
  }
  tf.logging.info("batching_scheme = %s" % ret)
  return ret


def hparams_to_batching_scheme(hparams,
                               drop_long_sequences=False,
                               shard_multiplier=1,
                               length_multiplier=1):
  """Wrapper around _batching_scheme with hparams."""
  return _batching_scheme(
      batch_size=hparams.batch_size,
      min_length=hparams.min_length,
      max_length=hparams.max_length,
      min_length_bucket=hparams.min_length_bucket,
      length_bucket_step=hparams.length_bucket_step,
      drop_long_sequences=drop_long_sequences,
      shard_multiplier=shard_multiplier,
      length_multiplier=length_multiplier)


def constant_batching_scheme(constant_batch_size_in_sequences):
  """A batching scheme with constant batch size.

  Args:
    constant_batch_size_in_sequences: an integer

  Returns:
     a dictionary
  """
  boundaries = _bucket_boundaries(1024)
  batch_sizes = [constant_batch_size_in_sequences] * (1 + len(boundaries))
  return {
      "boundaries": boundaries,
      "batch_sizes": batch_sizes,
      "min_length": 0,
      "max_length": 10**9,
      "shuffle_queue_size": None,
      "window_size": constant_batch_size_in_sequences,
  }


def serving_input_fn(problem, hparams):
  """Input fn for serving, starting from Placeholders."""
  data_fields, data_items_to_decoders = problem.example_reading_spec()

  # Feature placeholders that mimic what's on disk
  example = feature_placeholders(data_fields, data_items_to_decoders)

  # Preprocess
  example = problem.preprocess_example(example, tf.estimator.ModeKeys.PREDICT,
                                       hparams)
  example = cast_int64_to_int32(example)

  # 4-D inputs and space ids
  constants = {}
  constants["target_space_id"] = tf.constant(
      problem.get_hparams().target_space_id)
  constants["problem_choice"] = tf.constant(0)
  if problem.has_inputs:
    while len(example["inputs"].get_shape()) != 4:
      example["inputs"] = tf.expand_dims(example["inputs"], axis=-1)
    constants["input_space_id"] = tf.constant(
        problem.get_hparams().input_space_id)
    example.pop("targets")
  else:
    while len(example["targets"].get_shape()) != 4:
      example["targets"] = tf.expand_dims(example["targets"], axis=-1)

  features = constants
  features.update(example)

  return tf.estimator.export.ServingInputReceiver(
      features=features, receiver_tensors=example)
