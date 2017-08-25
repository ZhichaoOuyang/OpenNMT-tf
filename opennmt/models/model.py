"""Base class for models."""

import abc
import six
import time

import tensorflow as tf

from opennmt.utils import decay


def learning_rate_decay_fn(decay_type,
                           decay_rate,
                           decay_steps,
                           staircase=True,
                           start_decay_steps=0,
                           minimum_learning_rate=0):
  """Returns the learning rate decay functions.

  Args:
    decay_type: The type of decay. A function from `tf.train` as a `String`.
    decay_rate: The decay rate to apply.
    decay_steps: The decay steps as described in the decay type function.
    staircase: If `True`, learning rate is decayed in a staircase fashion.
    start_decay_steps: Start decay after this many steps.
    minimum_learning_rate: Do not decay past this learning rate value.

  Returns:
    A function with signature `lambda learning_rate, global_steps: decayed_learning_rate`.
  """
  def decay_fn(learning_rate, global_step):
    decay_op_name = None

    if decay_op_name is None:
      decay_op_name = getattr(tf.train, decay_type, None)
    if decay_op_name is None:
      decay_op_name = getattr(decay, decay_type, None)
    if decay_op_name is None:
      raise ValueError("Unknown decay function: " + decay_type)

    decayed_learning_rate = decay_op_name(
      learning_rate,
      tf.maximum(global_step - start_decay_steps, 0),
      decay_steps,
      decay_rate,
      staircase=staircase)
    decayed_learning_rate = tf.maximum(decayed_learning_rate, minimum_learning_rate)

    return decayed_learning_rate

  return decay_fn


@six.add_metaclass(abc.ABCMeta)
class Model(object):

  def __init__(self, name):
    self.name = name

  def __call__(self, features, labels, params, mode):
    """Creates the model. See `tf.estimator.Estimator`'s `model_fn` argument
    for more details about arguments and the returned value.
    """
    self._count_words(features, labels)
    with tf.variable_scope(self.name):
      return self._build(features, labels, params, mode)

  def _count_words(self, features, labels):
    """Stores a word counter operator for sequences of features and labels."""
    def _add_counter(word_count, name):
      word_count = tf.cast(word_count, tf.int64)
      total_word_count = tf.Variable(
        initial_value=0,
        name=name + "_init",
        trainable=False,
        dtype=tf.int64)
      total_word_count = tf.assign_add(
        total_word_count,
        word_count,
        name=name)

    features_length = features.get("length")
    labels_length = labels.get("length") if labels is not None and isinstance(labels, dict) else None

    with tf.variable_scope("words_per_sec"):
      if features_length is not None:
        _add_counter(tf.reduce_sum(features_length), "features")
      if labels_length is not None:
        _add_counter(tf.reduce_sum(labels_length), "labels")

  @abc.abstractmethod
  def _build(self, features, labels, params, mode):
    """Creates the model. Subclasses should override this function."""
    raise NotImplementedError()

  def _build_train_op(self, loss, params):
    """Builds the training op given parameters."""
    global_step = tf.train.get_or_create_global_step()

    if params["decay_type"] is not None:
      decay_fn = learning_rate_decay_fn(
        params["decay_type"],
        params["decay_rate"],
        params["decay_steps"],
        staircase=params["staircase"],
        start_decay_steps=params["start_decay_steps"],
        minimum_learning_rate=params["minimum_learning_rate"])
    else:
      decay_fn = None

    train_op = tf.contrib.layers.optimize_loss(
      loss,
      global_step,
      params["learning_rate"],
      params["optimizer"],
      clip_gradients=params["clip_gradients"],
      learning_rate_decay_fn=decay_fn,
      summaries=[
        "learning_rate",
        "loss",
        "global_gradient_norm",
      ])

    return train_op

  def _filter_example(self,
                      features,
                      labels,
                      maximum_features_length=None,
                      maximum_labels_length=None):
    """Defines an example filtering condition."""
    features_length = features.get("length")
    labels_length = labels.get("length") if isinstance(labels, dict) else None

    cond = []

    if features_length is not None:
      cond.append(tf.greater(features_length, 0))
      if maximum_features_length is not None:
        cond.append(tf.less_equal(features_length, maximum_features_length))

    if labels_length is not None:
      cond.append(tf.greater(labels_length, 0))
      if maximum_labels_length is not None:
        cond.append(tf.less_equal(labels_length, maximum_labels_length))

    return tf.reduce_all(cond)

  @abc.abstractmethod
  def _build_features(self, features_file, resources):
    """Builds a dataset from features file.

    Args:
      features_file: The file of features.
      resources: A dictionary containing additional resources set
        by the user.

    Returns:
      (`tf.contrib.data.Dataset`, `padded_shapes`)
    """
    raise NotImplementedError()

  @abc.abstractmethod
  def _build_labels(self, labels_file, resources):
    """Builds a dataset from labels file.

    Args:
      labels_file: The file of labels.
      resources: A dictionary containing additional resources set
        by the user.

    Returns:
      (`tf.contrib.data.Dataset`, `padded_shapes`)
    """
    raise NotImplementedError()

  def _input_fn_impl(self,
                     mode,
                     batch_size,
                     buffer_size,
                     num_buckets,
                     resources,
                     features_file,
                     labels_file=None,
                     maximum_features_length=None,
                     maximum_labels_length=None):
    """See `input_fn`."""
    features_dataset, features_padded_shapes = self._build_features(
      features_file,
      resources)

    if labels_file is None:
      dataset = features_dataset
      padded_shapes = features_padded_shapes
    else:
      labels_dataset, labels_padded_shapes = self._build_labels(
        labels_file,
        resources)
      dataset = tf.contrib.data.Dataset.zip((features_dataset, labels_dataset))
      padded_shapes = (features_padded_shapes, labels_padded_shapes)

    if mode == tf.estimator.ModeKeys.TRAIN:
      dataset = dataset.filter(lambda features, labels: self._filter_example(
        features,
        labels,
        maximum_features_length=maximum_features_length,
        maximum_labels_length=maximum_labels_length))
      dataset = dataset.shuffle(buffer_size, seed=int(time.time()))
      dataset = dataset.repeat()

    if mode == tf.estimator.ModeKeys.PREDICT or num_buckets <= 1:
      dataset = dataset.padded_batch(
        batch_size,
        padded_shapes=padded_shapes)
    else:
      # For training and evaluation, use bucketing.

      def key_func(features, labels):
        if maximum_features_length:
          bucket_width = (maximum_features_length + num_buckets - 1) // num_buckets
        else:
          bucket_width = 10

        bucket_id = features["length"] // bucket_width
        bucket_id = tf.minimum(bucket_id, num_buckets)
        return tf.to_int64(bucket_id)

      def reduce_func(key, dataset):
        return dataset.padded_batch(
          batch_size,
          padded_shapes=padded_shapes)

      dataset = dataset.group_by_window(
        key_func=key_func,
        reduce_func=reduce_func,
        window_size=batch_size)

    iterator = dataset.make_initializable_iterator()

    # Add the initializer to a standard collection for it to be initialized.
    tf.add_to_collection(tf.GraphKeys.TABLE_INITIALIZERS, iterator.initializer)

    return iterator.get_next()

  def input_fn(self,
               mode,
               batch_size,
               buffer_size,
               num_buckets,
               resources,
               features_file,
               labels_file=None,
               maximum_features_length=None,
               maximum_labels_length=None):
    """Returns an input function.

    See also `tf.estimator.Estimator`.

    Args:
      mode: A `tf.estimator.ModeKeys` mode.
      batch_size: The batch size to use.
      buffer_size: The prefetch buffer size (used e.g. for shuffling).
      num_buckets: The number of buckets to store examples of similar sizes.
      resources: A dictionary containing additional resources set
        by the user.
      features_file: The file containing input features.
      labels_file: The file containing output labels.
      maximum_features_length: The maximum length of feature sequences
        during training (if it applies).
      maximum_labels_length: The maximum length of label sequences
        during training (if it applies).

    Returns:
      A callable that returns the next element.
    """
    if mode != tf.estimator.ModeKeys.PREDICT and labels_file is None:
      raise ValueError("Labels file is required for training and evaluation")

    return lambda: self._input_fn_impl(
      mode,
      batch_size,
      buffer_size,
      num_buckets,
      resources,
      features_file,
      labels_file=labels_file,
      maximum_features_length=maximum_features_length,
      maximum_labels_length=maximum_labels_length)

  def format_prediction(self, prediction, params=None):
    """Formats the model prediction.

    Args:
      prediction: The evaluated prediction returned by `__call__`.
      params: (optional) Dictionary of formatting parameters.

    Returns:
      The final prediction.
    """
    return prediction
