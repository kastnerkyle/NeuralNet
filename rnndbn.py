
# Author: Kratarth Goel
# BITS Pilani Goa Campus (2014)
# RNN-DBN for polyphonic music generation
# for any further clarifications visit
# for the ICANN 2014 paper or email me @ kratarthgoel@gmail.com
# This code is based on the one writen by Nicolas Boulanger-Lewandowski
# University of Montreal (2012)
# RNN-RBM deep learning tutorial
# More information at http://deeplearning.net/tutorial/rnnrbm.html

import os
import sys
import numpy as np
import zipfile
try:
    import urllib.request as urllib  # for backwards compatibility
except ImportError:
    import urllib2 as urllib

try:
    from midi.utils import midiread, midiwrite
except ImportError:
    raise ImportError("Need GPL licensed midi utils",
                      "Can be downloaded by doing the following in the script dir",
                      "wget http://www.iro.umontreal.ca/~lisa/deep/midi.zip; unzip midi.zip")
import theano
from theano import tensor
from theano.tensor.shared_randomstreams import RandomStreams
import cPickle as pickle

#Don't use python long as this doesn't work on 32 bit computers.
np.random.seed(0xbeef)
rng = RandomStreams(seed=np.random.randint(1 << 30))
theano.config.warn.subtensor_merge_bug = False


def download(url, server_fname, local_fname=None, progress_update_percentage=5,
             bypass_certificate_check=False):
    """
    An internet download utility modified from
    http://stackoverflow.com/questions/22676/
    how-do-i-download-a-file-over-http-using-python/22776#22776
    """
    if bypass_certificate_check:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        u = urllib.urlopen(url, context=ctx)
    else:
        u = urllib.urlopen(url)
    if local_fname is None:
        local_fname = server_fname
    full_path = local_fname
    meta = u.info()
    with open(full_path, 'wb') as f:
        try:
            file_size = int(meta.get("Content-Length"))
        except TypeError:
            print("WARNING: Cannot get file size, displaying bytes instead!")
            file_size = 100
        print("Downloading: %s Bytes: %s" % (server_fname, file_size))
        file_size_dl = 0
        block_sz = int(1E7)
        p = 0
        while True:
            buffer = u.read(block_sz)
            if not buffer:
                break
            file_size_dl += len(buffer)
            f.write(buffer)
            if (file_size_dl * 100. / file_size) > p:
                status = r"%10d  [%3.2f%%]" % (file_size_dl, file_size_dl *
                                               100. / file_size)
                print(status)
                p += progress_update_percentage


def fetch_nottingham():
    url = "http://www.iro.umontreal.ca/~lisa/deep/data/Nottingham.zip"
    data_path = "Nottingham.zip"
    if not os.path.exists(data_path):
        download(url, data_path)
    key_range = (21, 109)
    dt = 0.3

    all_data = []
    with zipfile.ZipFile(data_path, "r") as f:
        for name in f.namelist():
            if ".mid" not in name:
                # Skip README
                continue
            data = midiread(f, key_range, dt).piano_roll.astype(
                theano.config.floatX)
            all_data.extend(data)
    return key_range, dt, all_data


def build_rbm(v, W, bv, bh, k):
    '''Construct a k-step Gibbs chain starting at v for an RBM.

v : Theano vector or matrix
  If a matrix, multiple chains will be run in parallel (batch).
W : Theano matrix
  Weight matrix of the RBM.
bv : Theano vector
  Visible bias vector of the RBM.
bh : Theano vector
  Hidden bias vector of the RBM.
k : scalar or Theano scalar
  Length of the Gibbs chain.

Return a (v_sample, cost, monitor, updates) tuple:

v_sample : Theano vector or matrix with the same shape as `v`
  Corresponds to the generated sample(s).
cost : Theano scalar
  Expression whose gradient with respect to W, bv, bh is the CD-k approximation
  to the log-likelihood of `v` (training example) under the RBM.
  The cost is averaged in the batch case.
monitor: Theano scalar
  Pseudo log-likelihood (also averaged in the batch case).
updates: dictionary of Theano variable -> Theano variable
  The `updates` object returned by scan.'''

    def gibbs_step(v):
        mean_h = tensor.nnet.sigmoid(tensor.dot(v, W) + bh)
        h = rng.binomial(size=mean_h.shape, n=1, p=mean_h,
                         dtype=theano.config.floatX)
        mean_v = tensor.nnet.sigmoid(tensor.dot(h, W.T) + bv)
        v = rng.binomial(size=mean_v.shape, n=1, p=mean_v,
                         dtype=theano.config.floatX)
        return mean_v, v

    chain, updates = theano.scan(lambda v: gibbs_step(v)[1], outputs_info=[v],
                                 n_steps=k)
    v_sample = chain[-1]
    mean_v = gibbs_step(v_sample)[0]
    monitor = tensor.xlogx.xlogy0(v, mean_v) + tensor.xlogx.xlogy0(1 - v, 1 - mean_v)
    monitor = monitor.sum() / v.shape[0]

    def free_energy(v):
        return -(v * bv).sum() - tensor.log(1 + tensor.exp(tensor.dot(v, W) + bh)).sum()
    cost = (free_energy(v) - free_energy(v_sample)) / v.shape[0]

    return v_sample, cost, monitor, updates


def shared_normal(num_rows, num_cols, scale=1):
    '''Initialize a matrix shared variable with normally distributed
elements.'''
    return theano.shared(np.random.normal(
        scale=scale, size=(num_rows, num_cols)).astype(theano.config.floatX))


def shared_zeros(*shape):
    '''Initialize a vector shared variable with zero elements.'''
    return theano.shared(np.zeros(shape, dtype=theano.config.floatX))


def build_rnnrbm(n_visible, n_hidden, n_hidden_recurrent):
    '''Construct a symbolic RNN-RBM and initialize parameters.

n_visible : integer
  Number of visible units.
n_hidden : integer
  Number of hidden units of the conditional RBMs.
n_hidden_recurrent : integer
  Number of hidden units of the RNN.

Return a (v, v_sample, cost1, monitor1, params1, updates_train1,cost2, monitor2, params2, updates_train2, v_t,
          updates_generate) tuple:

v : Theano matrix
  Symbolic variable holding an input sequence (used during training)
v_sample : Theano matrix
  Symbolic variable holding the negative particles for CD log-likelihood
  gradient estimation (used during training)
cost1(2) : Theano scalar
  Expression whose gradient (considering v_sample constant) corresponds to the
  LL gradient of the RNN-RBM1(2) i.e. the visible layer and the first hidden layer of the DBN
  (used during training)
monitor1(2) : Theano scalar
  Frame-level pseudo-likelihood (useful for monitoring during training) for RNN_RBM1(2)
params1(2) : tuple of Theano shared variables
  The parameters of the RNN-RBM1(2) model to be optimized during training.
updates_train1(2) : dictionary of Theano variable -> Theano variable
  Update object that should be passed to theano.function when compiling the
  training function for the RNN-RBM1(2).
v_t : Theano matrix
  Symbolic variable holding a generated sequence (used during sampling)
updates_generate : dictionary of Theano variable -> Theano variable
  Update object that should be passed to theano.function when compiling the
  generation function.'''

    W1 = shared_normal(n_visible, n_hidden, 0.01)
    bv = shared_zeros(n_visible)
    bh1 = shared_zeros(n_hidden)
    Wuh1 = shared_normal(n_hidden_recurrent, n_hidden, 0.0001)
    Wuv = shared_normal(n_hidden_recurrent, n_visible, 0.0001)
    Wvu = shared_normal(n_visible, n_hidden_recurrent, 0.0001)
    Wuu = shared_normal(n_hidden_recurrent, n_hidden_recurrent, 0.0001)
    bu = shared_zeros(n_hidden_recurrent)

    params1 = W1, bv, bh1, Wuh1, Wuv, Wvu, Wuu, bu  # learned parameters as shared
                                                    # variables for RNN_RBM1
    W2 = shared_normal(n_hidden, n_hidden, 0.01)
    bh2 = shared_zeros(n_hidden)
    Wuh2 = shared_normal(n_hidden_recurrent, n_hidden, 0.0001)

    params2 = W2, bh2, bh1, Wuh2, Wuh1 # learned parameters as shared
                                                # variables for RNN-RBM2

    v = tensor.matrix()  # a training sequence
    lin_output = tensor.dot(v, W1) + bh1
    activation = theano.tensor.nnet.sigmoid
    h = activation(lin_output)
    u0 = tensor.zeros((n_hidden_recurrent,))  # initial value for the RNN hidden
                                         # units

    # deterministic recurrence to compute the variable
    # biases bv_t , bh1_t at each time step.
    def recurrence1(v_t, u_tm1):
        bv_t = bv + tensor.dot(u_tm1, Wuv)
        bh1_t = bh1 + tensor.dot(u_tm1, Wuh1)
        u_t = tensor.tanh(bu + tensor.dot(v_t, Wvu) + tensor.dot(u_tm1, Wuu))
        return [u_t, bv_t, bh1_t]

    # If `h_t` is given, deterministic recurrence to compute the variable
    # biases bh1_t, bh2_t at each time step. If `h_t` is None, same recurrence
    # but with a separate Gibbs chain at each time step to sample (generate)
    # of the top layer RBM from the RNN-DBN. The resulting sample v_t is returned
    # in order to be passed down to the sequence history.
    def recurrence2(v_t,h_t, u_tm1):
        bh1_t = bh1 + tensor.dot(u_tm1, Wuh1)
        bh2_t = bh2 + tensor.dot(u_tm1, Wuh2)
        generate = h_t is None
        if generate:
            h_t, _, _, updates = build_rbm(tensor.zeros((n_hidden,)), W2, bh1_t,
                                           bh2_t, k=25)
        u_t = tensor.tanh(bu + tensor.dot(v_t, Wvu) + tensor.dot(u_tm1, Wuu))
        return ([u_t, h_t], updates) if generate else [u_t, bh1_t, bh2_t]

    # function used for generation of a sample from the RNN_DBN.
    # Starting with the sampling if the first hidden layer by
    # Gibbs Sampling in the top layer RBM of the RNN_DBN, which involves
    # generation of the RBM parameters that depend upon the RNN.
    # This is followed by generation of the visible layer sample.
    def generate(u_tm1):
        bh1_t = bh1 + tensor.dot(u_tm1, Wuh1)
        bh2_t = bh2 + tensor.dot(u_tm1, Wuh2)
        h_t, _, _, updates = build_rbm(tensor.zeros((n_hidden,)), W2, bh1_t,
                                       bh2_t, k=25)
        lin_v_t = tensor.dot(h_t, W1.T) + bv
        mean_v = activation(lin_v_t)
        v_t = rng.binomial(size=mean_v.shape, n=1, p=mean_v,
                           dtype=theano.config.floatX)
        u_t = tensor.tanh(bu + tensor.dot(v_t, Wvu) + tensor.dot(u_tm1, Wuu))
        return ([u_t,v_t], updates)
    # For training, the deterministic recurrence is used to compute all the
    # {bv_t, bh_t, 1 <= t <= T} given v. Conditional RBMs can then be trained
    # in batches using those parameters.
    (u_t, bv_t, bh1_t), updates_train1 = theano.scan(
        lambda v_t, u_tm1, *_: recurrence1(v_t, u_tm1),
        sequences=v, outputs_info=[u0, None, None], non_sequences=params1)
    v_sample, cost1, monitor1, updates_rbm1 = build_rbm(v, W1, bv_t[:], bh1_t[:],
                                                     k=15)
    updates_train1.update(updates_rbm1)


    (u_t, bh1_t, bh2_t), updates_train2 = theano.scan(
        lambda v_t, h_t, u_tm1, *_: recurrence2(v_t , h_t, u_tm1),
        sequences=[v,h], outputs_info=[u0, None, None], non_sequences=params2)

    h1_sample, cost2, monitor2, updates_rbm2 = build_rbm(h, W2, bh1_t[:], bh2_t[:],
                                                     k=15)

    updates_train2.update(updates_rbm2)

    # symbolic loop for sequence generation
    (u_t,v_t), updates_generate = theano.scan(
        lambda u_tm1,*_ : generate(u_tm1), outputs_info = [u0,None],
        non_sequences = params2, n_steps=200)


    return (v, v_sample, cost1, monitor1, params1, updates_train1, h, h1_sample,cost2, monitor2, params2, updates_train2, v_t,
            updates_generate)
    '''
    return (v, v_sample, cost1, monitor1, params1, updates_train1, v_t,
            updates_generate)
    '''

class RnnRbm:
    '''Simple class to train an RNN-RBM from MIDI files and to generate sample
sequences.'''

    def __init__(self, n_hidden=150, n_hidden_recurrent=100, lr=0.001,
                 r=(21, 109), dt=0.3):
        '''Constructs and compiles Theano functions for training and sequence
generation.

n_hidden : integer
  Number of hidden units of the conditional RBMs.
n_hidden_recurrent : integer
  Number of hidden units of the RNN.
lr : float
  Learning rate
r : (integer, integer) tuple
  Specifies the pitch range of the piano-roll in MIDI note numbers, including
  r[0] but not r[1], such that r[1]-r[0] is the number of visible units of the
  RBM at a given time step. The default (21, 109) corresponds to the full range
  of piano (88 notes).
dt : float
  Sampling period when converting the MIDI files into piano-rolls, or
  equivalently the time difference between consecutive time steps.'''

        self.r = r
        self.dt = dt

        (v, v_sample, cost1, monitor1, params1, updates_train1, h, h1_sample , cost2, monitor2, params2, updates_train2, v_t,
         updates_generate) = build_rnnrbm(r[1] - r[0], n_hidden,
                                           n_hidden_recurrent)
        '''
        (v, v_sample, cost1, monitor1, params1, updates_train1,v_t,
         updates_generate) = build_rnnrbm(r[1] - r[0], n_hidden,
                                           n_hidden_recurrent)
        '''
        gradient1 = tensor.grad(cost1, params1, consider_constant=[v_sample])
        updates_train1.update(((p, p - lr * g) for p, g in zip(params1,
                                                               gradient1)))

        gradient2 = tensor.grad(cost2, params2, consider_constant=[h1_sample])
        updates_train2.update(((p, p - lr * g) for p, g in zip(params2,
                                                               gradient2)))

        self.train_function1 = theano.function([v], monitor1,
                                               updates=updates_train1)

        self.train_function2 = theano.function([v], monitor2,
                                               updates=updates_train2)

        self.generate_function = theano.function([], v_t,
                                                 updates=updates_generate)

    def train_RNNRBM1(self, dataset, batch_size=100, num_epochs=200):
        '''Train the RNN-RBM via stochastic gradient descent (SGD) using MIDI
files converted to piano-rolls.

files : list of strings
  List of MIDI files that will be loaded as piano-rolls for training.
batch_size : integer
  Training sequences will be split into subsequences of at most this size
  before applying the SGD updates.
num_epochs : integer
  Number of epochs (pass over the training set) performed. The user can
  safely interrupt training with Ctrl+C at any time.'''

        try:
            for epoch in xrange(num_epochs):
                np.random.shuffle(dataset)
                costs1 = []
                for s, sequence in enumerate(dataset):
                    for i in xrange(0, len(sequence), batch_size):
                        cost1 = self.train_function1(sequence[i:i + batch_size])
                        costs1.append(cost1)
                print('Epoch %i/%i' % (epoch + 1, num_epochs))
                print(np.mean(costs1))
                sys.stdout.flush()

        except KeyboardInterrupt:
            print('Interrupted by user.')

    def train_RNNRBM2(self, dataset, batch_size=100, num_epochs=200):
        '''Train the RNN-RBM via stochastic gradient descent (SGD) using MIDI
files converted to piano-rolls.

files : list of strings
  List of MIDI files that will be loaded as piano-rolls for training.
batch_size : integer
  Training sequences will be split into subsequences of at most this size
  before applying the SGD updates.
num_epochs : integer
  Number of epochs (pass over the training set) performed. The user can
  safely interrupt training with Ctrl+C at any time.'''

        try:
            for epoch in xrange(num_epochs):
                np.random.shuffle(dataset)
                costs2 = []
                for s, sequence in enumerate(dataset):
                    for i in xrange(0, len(sequence), batch_size):
                        cost2 = self.train_function2(sequence[i:i + batch_size])
                        costs2.append(cost2)
                print('For 2nd layer Epoch %i/%i' % (epoch + 1, num_epochs))
                print(np.mean(costs2))
                sys.stdout.flush()

        except KeyboardInterrupt:
            print('Interrupted by user.')


    def generate(self):
        '''Generate a sample sequence, plot the resulting piano-roll and save
it as a MIDI file.

filename : string
  A MIDI file will be created at this location.
show : boolean
  If True, a piano-roll of the generated sequence will be shown.'''
        piano_roll = self.generate_function()
        return piano_roll


def train_rnnrbm(model, dataset, batch_size=100, num_epochs=200):
    model.train_RNNRBM1(dataset,
                        batch_size=batch_size, num_epochs=num_epochs)
    model.train_RNNRBM2(dataset,
                        batch_size=batch_size, num_epochs=num_epochs)
    return model


if __name__ == '__main__':
    key_range, dt, dataset = fetch_nottingham()
    # coarse model persistance
    if not os.path.exists('saved_rnndbn.pkl'):
        model = RnnRbm()
        model = train_rnnrbm(model, dataset)

        cur = sys.getrecursionlimit()
        sys.setrecursionlimit(40000)
        with open('saved_rnndbn.pkl', mode='w') as f:
            pickle.dump(model, f, -1)
        sys.setrecursionlimit(cur)

    with open('saved_rnndbn.pkl', mode='r') as f:
        model = pickle.load(f)

    import matplotlib
    matplotlib.use('Agg')
    sample_files = ["sample1.mid", "sample2.mid"]
    for s in sample_files:
        piano_roll = model.generate()
        midiwrite(s, piano_roll, key_range, dt)
        extent = (0, dt * len(piano_roll)) + key_range
        import matplotlib.pyplot as plt
        plt.imshow(piano_roll.T, origin='lower', aspect='auto',
                   interpolation='nearest', cmap="gray_r",
                   extent=extent)
        plt.xlabel('time (s)')
        plt.ylabel('MIDI note number')
        plt.title('generated piano-roll')
        plt.savefig(s[:-4] + '.png')
