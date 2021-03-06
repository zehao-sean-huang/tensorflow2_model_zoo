"""Example program training/inference on digit recognition problem with tensorflow 2.0."""
import argparse
import cv2
import os
import tensorflow as tf
from datetime import datetime
from tensorflow import keras
from optimizers import SWA, Lookahead, get_optimizer
from optimizers.schedulers import LRFinder
# This is a modification to the mnist_mlp_eager.py
# simply wrap the training step and validation in function with tf.function decorator
# Added option to try some other optimizers.
# Added procedure to plot learning rate finder result.


BATCH_SIZE = 32
NUM_CLASS = 10
NUM_EPOCHS = 30
LEARNING_RATE = 1e-3
if not os.path.exists('models/mnist_mlp_function/'):
    os.mkdir('models/mnist_mlp_function/')
MODEL_FILE = 'models/mnist_mlp_function/model'


class MLP(keras.Model):
    """MLP model class using tf.Keras API."""
    def __init__(self, num_class=NUM_CLASS):
        super(MLP, self).__init__()
        self.encoder = keras.Sequential([
            keras.layers.Dense(units=128),
            keras.layers.BatchNormalization(),
            keras.layers.Activation(activation='relu'),
            keras.layers.Dense(units=32),
            keras.layers.BatchNormalization(),
            keras.layers.Activation(activation='relu')
        ])
        self.decoder = keras.Sequential([
            keras.layers.Dense(units=num_class),
            keras.layers.Dropout(rate=.1),
            keras.layers.Activation(activation='softmax')
        ])

    def call(self, x, training=True):
        x = self.encoder(x, training=training)
        x = self.decoder(x, training=training)
        return x


def find_lr(optimizer='Adam', verbose=0):
    fname = 'plots/mnist_lr_finder_for_{}.png'.format(optimizer)
    model = MLP()
    criterion = keras.losses.SparseCategoricalCrossentropy()
    if optimizer == 'Adam':
        optimizer = keras.optimizers.Adam(learning_rate=LEARNING_RATE)
    else:
        optimizer = get_optimizer(optimizer, learning_rate=LEARNING_RATE)
    mnist = keras.datasets.mnist
    (x_train, y_train), (x_valid, y_valid) = mnist.load_data()
    x_train = x_train.reshape(60000, 784).astype('float32') / 255.0
    train_dataset = tf.data.Dataset.from_tensor_slices((x_train, y_train)).batch(BATCH_SIZE)

    @tf.function
    def train_step(x_batch, y_batch):
        with tf.GradientTape() as tape:
            out = model(x_batch, training=True)
            loss = criterion(y_batch, out)
        grad = tape.gradient(loss, model.trainable_variables)
        optimizer.apply_gradients(zip(grad, model.trainable_variables))
        return loss

    lr_finder = LRFinder(start_lr=1e-7, max_lr=1e-1)
    for idx, (x_batch, y_batch) in enumerate(train_dataset):
        loss = train_step(x_batch, y_batch)
        new_lr = lr_finder.step(loss.numpy())
        optimizer.lr.assign(new_lr)
        if lr_finder.done:
            break
    lr_finder.plot_lr(fname)
    if verbose:
        print(lr_finder.history)


def train(optimizer='Adam', use_swa=False, use_lookahead=False, mc_dropout=False, verbose=0):
    """Train the model."""
    # load dataset
    mnist = keras.datasets.mnist
    (x_train, y_train), (x_valid, y_valid) = mnist.load_data()
    x_train = x_train.reshape(60000, 784).astype('float32') / 255.0
    x_valid = x_valid.reshape(10000, 784).astype('float32') / 255.0
    train_dataset = tf.data.Dataset.from_tensor_slices((x_train, y_train)).batch(BATCH_SIZE)
    valid_dataset = tf.data.Dataset.from_tensor_slices((x_valid, y_valid)).batch(BATCH_SIZE)

    # config model
    model = MLP()
    criterion = keras.losses.SparseCategoricalCrossentropy()
    if optimizer == 'Adam':
        optimizer = keras.optimizers.Adam(learning_rate=LEARNING_RATE)
    else:
        optimizer = get_optimizer(optimizer, learning_rate=LEARNING_RATE)
    if use_swa:
        optimizer = SWA(optimizer, swa_start=25, swa_freq=1)
    if use_lookahead:
        optimizer = Lookahead(optimizer)
    train_loss = keras.metrics.Mean()
    train_accuracy = keras.metrics.SparseCategoricalAccuracy()
    test_loss = keras.metrics.Mean()
    test_accuracy = keras.metrics.SparseCategoricalAccuracy()

    @tf.function
    def train_step(x_batch, y_batch):
        with tf.GradientTape() as tape:
            out = model(x_batch, training=True)
            loss = criterion(y_batch, out)
        grad = tape.gradient(loss, model.trainable_variables)
        optimizer.apply_gradients(zip(grad, model.trainable_variables))
        train_loss(loss)
        train_accuracy(y_batch, out)

    @tf.function
    def valid_step(x_batch, y_batch):
        out = model(x_batch, training=False)
        loss = criterion(y_batch, out)
        test_loss(loss)
        test_accuracy(y_batch, out)

    @tf.function
    def valid_step_with_dropout(x_batch, y_batch, num_samples=100):
        outs = []
        for i in range(num_samples):
            outs.append(model(x_batch, training=True))
        out = tf.reduce_mean(tf.stack(outs), 0)
        loss = criterion(y_batch, out)
        test_loss(loss)
        test_accuracy(y_batch, out)

    # training loop
    for epoch in range(NUM_EPOCHS):
        t0 = datetime.now()
        # train
        train_loss.reset_states()
        train_accuracy.reset_states()
        for idx, (x_batch, y_batch) in enumerate(train_dataset):
            train_step(x_batch, y_batch)

        # validate
        test_loss.reset_states()
        test_accuracy.reset_states()
        for idx, (x_batch, y_batch) in enumerate(valid_dataset):
            valid_step(x_batch, y_batch)

        message_template = 'epoch {:>3} time {} sec / epoch train cce {:.4f} acc {:4.2f}% test cce {:.4f} acc {:4.2f}%'
        t1 = datetime.now()
        if verbose:
            print(message_template.format(
                epoch + 1, (t1 - t0).seconds,
                train_loss.result(), train_accuracy.result() * 100,
                test_loss.result(), test_accuracy.result() * 100
            ))
    if use_swa:
        # for swa, use swa weights and reset batch_norm moving averages
        optimizer.assign_swa_weights(model.variables)
        # fix batch_norm moving averages
        for _, (x_batch, __) in enumerate(train_dataset):
            model(x_batch, training=True)

        test_loss.reset_states()
        test_accuracy.reset_states()
        for idx, (x_batch, y_batch) in enumerate(valid_dataset):
            valid_step(x_batch, y_batch)
        print('SWA model cce {:.4f} acc {:4.2f}% cce'.format(test_loss.result(), test_accuracy.result() * 100))

    if mc_dropout:
        # see how Monte Carlo Dropout performs
        test_loss.reset_states()
        test_accuracy.reset_states()
        for idx, (x_batch, y_batch) in enumerate(valid_dataset):
            valid_step(x_batch, y_batch)
        message_template = 'test without mc dropout cce {:.4f} acc {:4.2f}%'
        print(message_template.format(test_loss.result(), test_accuracy.result() * 100))

        test_loss.reset_states()
        test_accuracy.reset_states()
        for idx, (x_batch, y_batch) in enumerate(valid_dataset):
            valid_step_with_dropout(x_batch, y_batch)
        message_template = 'test with mc dropout cce {:.4f} acc {:4.2f}%'
        print(message_template.format(test_loss.result(), test_accuracy.result() * 100))


    # it appears that for keras.Model subclass model, we can only save weights in 2.0 alpha
    model.save_weights(MODEL_FILE, save_format='tf')


def inference(filepath):
    """Reconstruct the model, load weights and run inference on a given picture."""
    model = MLP()
    model.load_weights(MODEL_FILE)
    image = cv2.imread(filepath, 0).reshape(1, 784).astype('float32') / 255
    probs = model.predict(image)
    print('it is a: {} with probability {:4.2f}%'.format(probs.argmax(), 100 * probs.max()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='parameters for program')
    parser.add_argument('procedure', choices=['train', 'inference', 'find_lr'],
                        help='Whether to train a new model or use trained model to inference.')
    parser.add_argument('--image_path', default=None, help='Path to jpeg image file to predict on.')
    parser.add_argument('--gpu', default='', help='gpu device id expose to program, default is cpu only.')
    parser.add_argument('--optimizer', default='Adam', help='optimizer of choice.')
    parser.add_argument('--use_swa', default=False, action='store_true', help='wrap optimizer with SWA')
    parser.add_argument('--use_lookahead', default=False, action='store_true', help='wrap optimizer with Lookahead')
    parser.add_argument('--mc_dropout', default=False, action='store_true', help='whehter to evalutate MC dropout')
    parser.add_argument('--verbose', type=int, default=0)
    args = parser.parse_args()

    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.procedure == 'find_lr':
        find_lr(args.optimizer, args.verbose)
    elif args.procedure == 'train':
        train(args.optimizer, args.use_swa, args.use_lookahead, args.mc_dropout, args.verbose)
    else:
        assert os.path.exists(MODEL_FILE + '.index'), 'model not found, train a model before calling inference.'
        assert os.path.exists(args.image_path), 'can not find image file.'
        inference(args.image_path)
