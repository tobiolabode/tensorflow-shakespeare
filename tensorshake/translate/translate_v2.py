from __future__ import absolute_import
from __future__ import division
from __future__ import print_function


from tensorshake.prepare_corpus import MODERN_VOCAB_MAX, ORIGINAL_VOCAB_MAX
from tensorshake.prepare_corpus import MODERN_VOCAB_PATH, ORIGINAL_VOCAB_PATH
from tensorshake.prepare_corpus import MODERN_TRAIN_IDS_PATH, MODERN_DEV_IDS_PATH, ORIGINAL_TRAIN_IDS_PATH, ORIGINAL_DEV_IDS_PATH


import math
import os
import random
import sys
import time
import pdb

import tensorflow.python.platform

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

from tensorshake.translate import data_utils_v2
from tensorshake.translate import seq2seq_model_v2
from tensorshake.prepare_corpus import tokenizer

from tensorflow.python.platform import gfile
"""Binary for training translation models and decoding from them.

Running this program without --decode will download the WMT corpus into
the directory specified as --data_dir and tokenize it in a very basic way,
and then start training a model saving checkpoints to --train_dir.

Running with --decode starts an interactive loop so you can see how
the current checkpoint translates English sentences into French.

See the following papers for more information on neural translation models.
 * http://arxiv.org/abs/1409.3215
 * http://arxiv.org/abs/1409.0473
 * http://arxiv.org/pdf/1412.2007v2.pdf

 Adapted by Motoki Wu.
"""


tf.compat.v1.app.flags.DEFINE_float("learning_rate", 0.5, "Learning rate.")
tf.compat.v1.app.flags.DEFINE_float("learning_rate_decay_factor", 0.99,
                                    "Learning rate decays by this much.")
tf.compat.v1.app.flags.DEFINE_float("max_gradient_norm", 5.0,
                                    "Clip gradients to this norm.")
tf.compat.v1.app.flags.DEFINE_integer("batch_size", 64,
                                      "Batch size to use during training.")
tf.compat.v1.app.flags.DEFINE_integer("size", 1024, "Size of each model layer.")
tf.compat.v1.app.flags.DEFINE_integer("num_layers", 3, "Number of layers in the model.")
tf.compat.v1.app.flags.DEFINE_string("data_dir", "/tmp", "Data directory")
tf.compat.v1.app.flags.DEFINE_string("train_dir", "/tmp", "Training directory.")
tf.compat.v1.app.flags.DEFINE_integer("max_train_data_size", 0,
                                      "Limit on the size of training data (0: no limit).")
tf.compat.v1.app.flags.DEFINE_integer("steps_per_checkpoint", 200,
                                      "How many training steps to do per checkpoint.")
tf.compat.v1.app.flags.DEFINE_boolean("decode", False,
                                      "Set to True for interactive decoding.")
tf.compat.v1.app.flags.DEFINE_boolean("self_test", False,
                                      "Run a self-test if this is set to True.")

"""
MODERN ~ Modern English
ORIGINAL ~ Shakespeare

TensorFlow examples goes from EN -> FR.
This script goes from MODERN -> ORIGINAL.
"""


tf.compat.v1.app.flags.DEFINE_string("en_train", MODERN_TRAIN_IDS_PATH, "modern train ids path")
tf.compat.v1.app.flags.DEFINE_string("fr_train", ORIGINAL_TRAIN_IDS_PATH, "original train ids path")
tf.compat.v1.app.flags.DEFINE_string("en_dev", MODERN_DEV_IDS_PATH, "modern dev ids path")
tf.compat.v1.app.flags.DEFINE_string("fr_dev", ORIGINAL_DEV_IDS_PATH, "original dev ids path")
tf.compat.v1.app.flags.DEFINE_string("en_vocab", MODERN_VOCAB_PATH, "modern vocab path")
tf.compat.v1.app.flags.DEFINE_string("fr_vocab", ORIGINAL_VOCAB_PATH, "original vocab path")

tf.compat.v1.app.flags.DEFINE_integer("en_vocab_size", MODERN_VOCAB_MAX, "modern vocabulary size")
tf.compat.v1.app.flags.DEFINE_integer(
    "fr_vocab_size", ORIGINAL_VOCAB_MAX, "original vocabulary size")

FLAGS = tf.compat.v1.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model_v2.Seq2SeqModel for details of how they work.
# , (70, 80), (180, 198)] # TODO: maybe filter out long sentences?
_buckets = [(5, 10), (10, 15), (20, 25), (50, 50)]


def read_data(source_path, target_path, max_size=None):
    """Read data from source and target files and put into buckets.

    Args:
      source_path: path to the files with token-ids for the source language.
      target_path: path to the file with token-ids for the target language;
        it must be aligned with the source file: n-th line contains the desired
        output for n-th line from the source_path.
      max_size: maximum number of lines to read, all other will be ignored;
        if 0 or None, data files will be read completely (no limit).

    Returns:
      data_set: a list of length len(_buckets); data_set[n] contains a list of
        (source, target) pairs read from the provided data files that fit
        into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
        len(target) < _buckets[n][1]; source and target are lists of token-ids.
    """
    data_set = [[] for _ in _buckets]
    with gfile.GFile(source_path, mode="r") as source_file:
        with gfile.GFile(target_path, mode="r") as target_file:
            source, target = source_file.readline(), target_file.readline()
            counter = 0
            while source and target and (not max_size or counter < max_size):
                counter += 1
                if counter % 100000 == 0:
                    print("  reading data line %d" % counter)
                    sys.stdout.flush()
                source_ids = [int(x) for x in source.split()][:50]  # TODO: hmm
                target_ids = [int(x) for x in target.split()][:50]
                target_ids.append(data_utils_v2.EOS_ID)
                for bucket_id, (source_size, target_size) in enumerate(_buckets):
                    if len(source_ids) < source_size and len(target_ids) < target_size:
                        data_set[bucket_id].append([source_ids, target_ids])
                        break
                source, target = source_file.readline(), target_file.readline()
    return data_set


def create_model(session, forward_only):
    """Create translation model and initialize or load parameters in session."""
    print("en_vocab_size", FLAGS.en_vocab_size)
    print("fr_vocab_size", FLAGS.fr_vocab_size)
    model = seq2seq_model_v2.Seq2SeqModel(
        FLAGS.en_vocab_size, FLAGS.fr_vocab_size, _buckets,
        FLAGS.size, FLAGS.num_layers, FLAGS.max_gradient_norm, FLAGS.batch_size,
        FLAGS.learning_rate, FLAGS.learning_rate_decay_factor,
        forward_only=forward_only)
    ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
    if ckpt and gfile.Exists(ckpt.model_checkpoint_path):
        print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
        model.saver.restore(session, ckpt.model_checkpoint_path)
    else:
        print("Created model with fresh parameters.")
        # session.run(tf.variables.initialize_all_variables())
        session.run(tf.compat.v1.initialize_all_variables())
    return model


def train():
    """Train a en->fr translation model using WMT data."""
    # Prepare WMT data.
    # print("Preparing WMT data in %s" % FLAGS.data_dir)
    # en_train, fr_train, en_dev, fr_dev, _, _ = data_utils_v2.prepare_wmt_data(
    # FLAGS.data_dir, FLAGS.en_vocab_size, FLAGS.fr_vocab_size)

    en_train = FLAGS.en_train
    fr_train = FLAGS.fr_train
    en_dev = FLAGS.en_dev
    fr_dev = FLAGS.fr_dev

    print("en_train", en_train)
    print("fr_train", fr_train)
    print("en_dev", en_dev)
    print("fr_dev", fr_dev)

    with tf.compat.v1.Session() as sess:
        # Create model.
        print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
        model = create_model(sess, False)

        # Read data into buckets and compute their sizes.
        print("Reading development and training data (limit: %d)."
              % FLAGS.max_train_data_size)
        dev_set = read_data(en_dev, fr_dev)
        train_set = read_data(en_train, fr_train, FLAGS.max_train_data_size)
        train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
        train_total_size = float(sum(train_bucket_sizes))

        # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
        # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
        # the size if i-th training bucket, as used later.
        train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                               for i in xrange(len(train_bucket_sizes))]

        # This is the training loop.
        step_time, loss = 0.0, 0.0
        current_step = 0
        previous_losses = []
        while True:
            # Choose a bucket according to data distribution. We pick a random number
            # in [0, 1] and use the corresponding interval in train_buckets_scale.
            random_number_01 = np.random.random_sample()
            bucket_id = min([i for i in xrange(len(train_buckets_scale))
                             if train_buckets_scale[i] > random_number_01])

            # Get a batch and make a step.
            start_time = time.time()
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                train_set, bucket_id)
            # print("encoder_inputs", "-"*80)
            # print(encoder_inputs)
            # print("decoder_inputs", "-"*80)
            # print(decoder_inputs)
            # print("bucket_id", bucket_id)
            _, step_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                         target_weights, bucket_id, False)
            step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
            loss += step_loss / FLAGS.steps_per_checkpoint
            current_step += 1
            # print("loss", loss)
            sys.stdout.flush()

            # Once in a while, we save checkpoint, print statistics, and run evals.
            if current_step % FLAGS.steps_per_checkpoint == 0:
                # Print statistics for the previous epoch.
                perplexity = math.exp(loss) if loss < 300 else float('inf')
                print("global step %d learning rate %.4f step-time %.2f perplexity "
                      "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                                step_time, perplexity))
                # Decrease learning rate if no improvement was seen over last 3 times.
                if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
                    sess.run(model.learning_rate_decay_op)
                previous_losses.append(loss)
                # Save checkpoint and zero timer and loss.
                checkpoint_path = os.path.join(FLAGS.train_dir, "translate.ckpt")
                model.saver.save(sess, checkpoint_path, global_step=model.global_step)
                step_time, loss = 0.0, 0.0
                # Run evals on development set and print their perplexity.
                for bucket_id in xrange(len(_buckets)):
                    encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                        dev_set, bucket_id)
                    _, eval_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                                 target_weights, bucket_id, True)
                    eval_ppx = math.exp(eval_loss) if eval_loss < 300 else float('inf')
                    print("  eval: bucket %d perplexity %.2f" % (bucket_id, eval_ppx))
                sys.stdout.flush()


def decode():
    with tf.compat.v1.Session() as sess:
        # Create model and load parameters.
        model = create_model(sess, True)
        model.batch_size = 1  # We decode one sentence at a time.

        # Load vocabularies.
        # en_vocab_path = os.path.join(FLAGS.data_dir,
        # "vocab%d.en" % FLAGS.en_vocab_size)
        # fr_vocab_path = os.path.join(FLAGS.data_dir,
        # "vocab%d.fr" % FLAGS.fr_vocab_size)
        en_vocab_path = FLAGS.en_vocab
        fr_vocab_path = FLAGS.fr_vocab
        print("en_vocab_path", FLAGS.en_vocab)
        print("fr_vocab_path", FLAGS.fr_vocab)

        en_vocab, _ = data_utils_v2.initialize_vocabulary(en_vocab_path)
        _, rev_fr_vocab = data_utils_v2.initialize_vocabulary(fr_vocab_path)

        # Decode from standard input.
        sys.stdout.write("> ")
        sys.stdout.flush()
        sentence = sys.stdin.readline()
        while sentence:
            # Get token-ids for the input sentence.
            token_ids = data_utils_v2.sentence_to_token_ids(sentence, en_vocab, tokenizer=tokenizer)
            # Which bucket does it belong to?
            bucket_id = min([b for b in xrange(len(_buckets))
                             if _buckets[b][0] > len(token_ids)])
            # Get a 1-element batch to feed the sentence to the model.
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                {bucket_id: [(token_ids, [])]}, bucket_id)
            # Get output logits for the sentence.
            _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                             target_weights, bucket_id, True)
            # TODO: change greedy decoder (either sampled or beam search)
            # This is a greedy decoder - outputs are just argmaxes of output_logits.
            # pdb.set_trace()
            outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]
            # If there is an EOS symbol in outputs, cut them at that point.
            if data_utils_v2.EOS_ID in outputs:
                outputs = outputs[:outputs.index(data_utils_v2.EOS_ID)]
            # Print out French sentence corresponding to outputs.
            print(" ".join([rev_fr_vocab[output] for output in outputs]))
            print("> ", end="")
            sys.stdout.flush()
            sentence = sys.stdin.readline()


def self_test():
    """Test the translation model."""
    with tf.compat.v1.Session() as sess:
        print("Self-test for neural translation model.")
        # Create model with vocabularies of 10, 2 small buckets, 2 layers of 32.
        model = seq2seq_model_v2.Seq2SeqModel(10, 10, [(3, 3), (6, 6)], 32, 2,
                                              5.0, 32, 0.3, 0.99, num_samples=8)
        # sess.run(tf.variables.initialize_all_variables())
        sess.run(tf.compat.v1.initialize_all_variables())

        # Fake data set for both the (3, 3) and (6, 6) bucket.
        data_set = ([([1, 1], [2, 2]), ([3, 3], [4]), ([5], [6])],
                    [([1, 1, 1, 1, 1], [2, 2, 2, 2, 2]), ([3, 3, 3], [5, 6])])
        for _ in xrange(5):  # Train the fake model for 5 steps.
            bucket_id = random.choice([0, 1])
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                data_set, bucket_id)
            model.step(sess, encoder_inputs, decoder_inputs, target_weights,
                       bucket_id, False)


def main(_):
    if FLAGS.self_test:
        self_test()
    elif FLAGS.decode:
        decode()
    else:
        train()


if __name__ == "__main__":
    tf.compat.v1.app.run()
