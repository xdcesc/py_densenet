#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2017 Alvin Zhu. All Rights Reserved.
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
# ==============================================================================

"""DenseNet"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf


class DenseNet:
    def __init__(self, num_classes, growth_rate, depth, bc_mode,
                 total_blocks, dropout_rate, reduction,
                 weight_decay, nesterov_momentum):
        self.num_classes = num_classes
        self.growth_rate = growth_rate
        self.depth = depth
        self.bc_mode = bc_mode
        self.first_output_features = growth_rate * 2 if bc_mode else 16
        self.total_blocks = total_blocks
        self.layers_per_block = (depth - (total_blocks + 1)) // total_blocks
        if self.bc_mode:
            self.layers_per_block = self.layers_per_block // 2
        self.reduction = reduction

        self.dropout_rate = dropout_rate
        self.nesterov_momentum = nesterov_momentum
        self.weight_decay = weight_decay

        print("Build DenseNet model with {} blocks, {} bottleneck layers and {} composite layers each.".format(
            self.total_blocks, self.layers_per_block,
            self.layers_per_block))
        print("Reduction at transition layers: {:.1f}".format(self.reduction))

    @staticmethod
    def conv2d(input_, out_features, kernel_size,
               strides=(1, 1, 1, 1), padding='SAME'):
        in_features = int(input_.get_shape()[-1])
        kernel = tf.get_variable(
            name='kernel',
            shape=[kernel_size, kernel_size, in_features, out_features],
            initializer=tf.variance_scaling_initializer())
        output = tf.nn.conv2d(input_, kernel, strides, padding)
        return output

    @staticmethod
    def avg_pool(input_, k):
        ksize = [1, k, k, 1]
        strides = [1, k, k, 1]
        padding = 'VALID'
        output = tf.nn.avg_pool(input_, ksize, strides, padding)
        return output

    @staticmethod
    def batch_norm(input_, training):
        output = tf.contrib.layers.batch_norm(
            input_, scale=True, is_training=training,
            updates_collections=None)
        return output

    def dropout(self, input_, training):
        output = tf.layers.dropout(input_, self.dropout_rate, training)
        return output

    def composite_function(self, input_, out_features, kernel_size, training):
        """Function from paper H_l that performs:
        - batch normalization
        - ReLU nonlinearity
        - convolution with required kernel
        - dropout, if required
        """
        with tf.variable_scope("composite_function"):
            # BN
            output = self.batch_norm(input_, training)
            # ReLU
            output = tf.nn.relu(output)
            # convolution
            output = self.conv2d(
                output, out_features=out_features, kernel_size=kernel_size)
            # dropout(in case of training and in case it is no 1.0)
            output = self.dropout(output, training)
        return output

    def bottleneck(self, input_, out_features, training):
        with tf.variable_scope("bottleneck"):
            output = self.batch_norm(input_, training)
            output = tf.nn.relu(output)
            output = self.conv2d(
                output, out_features=out_features, kernel_size=1,
                padding='VALID')
            output = self.dropout(output, training)
        return output

    def add_layer(self, input_, out_features, training):
        """Perform H_l composite function for the layer and after concatenate
        input with output from composite function.
        """
        # call composite function with 3x3 kernel
        if self.bc_mode:
            output = self.bottleneck(input_, out_features * 4, training)
        else:
            output = input_
        output = self.composite_function(
            output, out_features, kernel_size=3, training=training)
        # concatenate _input with out from composite function
        output = tf.concat(axis=3, values=(input_, output))

        return output

    def add_dense_block(self, input_, growth_rate, layers_per_block, training):
        """Add N H_l internal layers"""
        output = input_
        for layer in range(layers_per_block):
            with tf.variable_scope("layer_%d" % layer):
                output = self.add_layer(input_, growth_rate, training)
        return output

    def transition_layer(self, input_, training):
        """Call H_l composite function with 1x1 kernel and after average
        pooling
        """
        # call composite function with 1x1 kernel
        out_features = int(input_.get_shape()[-1])
        if self.bc_mode:
            out_features = int(out_features * self.reduction)
        output = self.composite_function(
            input_, out_features=out_features, kernel_size=1, training=training)
        # run average pooling
        output = self.avg_pool(output, k=2)
        return output

    @staticmethod
    def fully_connected(input_, out_dim):
        with tf.name_scope('fully_connected'):
            output = tf.layers.dense(input_,
                                     out_dim,
                                     kernel_initializer=tf.contrib.layers.xavier_initializer()
                                     )
        return output

    def classification_layer(self, input_, training):
        """This is last transition to get probabilities by classes. It perform:
        - batch normalization
        - ReLU nonlinearity
        - wide average pooling
        - FC layer multiplication
        """
        # BN
        output = self.batch_norm(input_, training)
        # ReLU
        output = tf.nn.relu(output)
        # average pooling
        last_pool_kernel = int(output.get_shape()[-2])
        output = self.avg_pool(output, k=last_pool_kernel)
        # FC
        features_total = int(output.get_shape()[-1])
        output = tf.reshape(output, [-1, features_total])

        logits = self.fully_connected(output, self.num_classes)
        return logits

    def cifar_model_fn(self, features, labels, mode):
        layers_per_block = (self.depth - 4) // 3
        if self.bc_mode:
            layers_per_block = layers_per_block // 2
        growth_rate = self.growth_rate
        training = tf.constant(mode == tf.estimator.ModeKeys.TRAIN)

        with tf.variable_scope("Image_Processing"):
            images = features["images"]

            if mode == tf.estimator.ModeKeys.PREDICT:
                mean, variance = tf.nn.moments(images, axes=[0, 1, 2])
                std = tf.sqrt(variance)
                images = tf.cast(images, tf.float32) / 255.0
                images = (images - mean) / std

            images = tf.image.resize_image_with_crop_or_pad(images, 32, 32)
        # first - initial 3 x 3 conv to first_output_features
        with tf.variable_scope("Initial_convolution"):
            output = self.conv2d(
                images,
                out_features=self.first_output_features,
                kernel_size=3)

        # add N required blocks
        with tf.variable_scope("DenseNet"):
            for block in range(self.total_blocks):
                with tf.variable_scope("Dense_Block_%d" % block):
                    output = self.add_dense_block(output, growth_rate, layers_per_block, training)
                # last block exist without transition layer
                if block != self.total_blocks - 1:
                    with tf.variable_scope("Transition_Layer_%d" % block):
                        output = self.transition_layer(output, training)

            with tf.variable_scope("Classification_Layer"):
                logits = self.classification_layer(output, training)
        probabilities = tf.nn.softmax(logits)
        classes = tf.argmax(input=probabilities, axis=1)

        predictions = {
            "classes": classes,
            "probabilities": probabilities
        }

        if mode == tf.estimator.ModeKeys.PREDICT:
            return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions)

        # Losses
        onehot_labels = tf.one_hot(indices=tf.cast(labels, tf.int32), depth=self.num_classes)
        loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(
            logits=logits, labels=onehot_labels))

        eval_metric_ops = {
            "accuracy": tf.metrics.accuracy(labels=labels, predictions=classes)
        }

        if mode == tf.estimator.ModeKeys.EVAL:
            return tf.estimator.EstimatorSpec(
                mode=mode, loss=loss, eval_metric_ops=eval_metric_ops)

        l2_loss = tf.add_n(
            [tf.nn.l2_loss(var) for var in tf.trainable_variables()])

        # optimizer and train step
        optimizer = tf.train.MomentumOptimizer(
            features['learning_rate'], self.nesterov_momentum, use_nesterov=True)
        train_op = optimizer.minimize(
            loss + l2_loss * self.weight_decay,
            global_step=tf.train.get_global_step())

        correct_prediction = tf.equal(
            classes,
            labels)
        accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32), name='train_accuracy')

        if mode == tf.estimator.ModeKeys.TRAIN:
            tf.summary.scalar("loss_per_batch", loss)
            tf.summary.scalar("accuracy_per_batch", accuracy)

            return tf.estimator.EstimatorSpec(
                mode=mode,
                loss=loss,
                train_op=train_op)
