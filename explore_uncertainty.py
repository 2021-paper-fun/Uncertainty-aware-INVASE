'''
Written by Jinsung Yoon
Date: Jan 1th 2019
INVASE: Instance-wise Variable Selection using Neural Networks Implementation on Synthetic Datasets
Reference: J. Yoon, J. Jordon, M. van der Schaar, "INVASE: Instance-wise Variable Selection using Neural Networks," International Conference on Learning Representations (ICLR), 2019.
Paper Link: https://openreview.net/forum?id=BJg_roAcK7
Contact: jsyoon0823@g.ucla.edu
---------------------------------------------------
Instance-wise Variable Selection (INVASE) - with baseline networks
'''
import os
os.environ["CUDA_VISIBLE_DEVICES"] = ""

from keras.layers import Input, Dense, Multiply
from keras.layers import BatchNormalization, Lambda, Concatenate
from keras.losses import mean_squared_error
from keras.models import Sequential, Model
from keras.optimizers import Adam
from keras import regularizers
from keras import backend as K
import tensorflow as tf
import numpy as np
from sklearn.metrics import roc_auc_score
from sklearn.metrics import average_precision_score
from sklearn.metrics import accuracy_score
import argparse
from data_generation import generate_data
import json
import pandas as pd
import time
import initpath_alg
initpath_alg.init_sys_path()
import utilmlab
import data_loader_mlab


def array2str(a):
    s = ''
    for idx, el in enumerate(a):
        s += (' ' if idx > 0 else '') + '{:0.3f}'.format(el)
    return s


def one_hot_encoder(a):
    n_values = np.max(a) + 1
    return np.eye(n_values)[a]


def load_create_data(
        data_type,
        data_out,
        is_logging_enabled=True,
        fn_csv=None,
        label_nm=None):

    df_train, df_test, dset = None, None, None
    features = None
    if data_type in data_loader_mlab.get_available_datasets() + ['show'] \
       or fn_csv is not None:
        if fn_csv is not None:
            rval, dset = data_loader_mlab.load_dataset_from_csv(
                logger, fn_csv, label_nm)
        else:
            rval, dset = data_loader_mlab.get_dataset(data_type)
        assert rval == 0
        data_loader_mlab.dataset_log_properties(logger, dset)
        if is_logging_enabled:
            logger.info('warning no seed')
        df = dset['df']
        features = dset['features']
        labels = dset['targets']
        nsample = len(df)
        train_ratio = 0.8
        idx = np.random.permutation(nsample)
        ntrain = int(nsample * train_ratio)
        df_train = df.iloc[idx[:ntrain]]
        df_test = df.iloc[idx[ntrain:]]

        col_drop = utilmlab.col_with_nan(df)
        if is_logging_enabled and len(col_drop):
            print('warning: dropping features {}'
                  ', contains nan'.format(col_drop))
            time.sleep(2)

        features = [el for el in features if el not in col_drop]

        x_train = df_train[features].values
        y_train = df_train[labels].values
        x_test = df_test[features].values
        y_test = df_test[labels].values

        g_train, g_test = None, None

        y_train = one_hot_encoder(np.ravel(y_train))
        y_test = one_hot_encoder(np.ravel(y_test))
        if False:
            logger.info('y: train:{} test:{}'.format(
                set(np.ravel(y_train)), set(np.ravel(y_test))))
    else:
        x_train, y_train, g_train = generate_data(
            n=train_N, data_type=data_type, seed=train_seed, out=data_out)
        x_test,  y_test,  g_test = generate_data(
            n=test_N,  data_type=data_type, seed=test_seed,  out=data_out)
    if False:
        logger.info('{} {} {} {}'.format(
            x_train.shape,
            y_train.shape,
            x_test.shape,
            y_test.shape))

    x_test = x_test[:,feat_num].reshape(-1, 1)
    x_train = x_train[:,feat_num].reshape(-1, 1)
    features = features[feat_num]

    return x_train, y_train, g_train, x_test, y_test, \
        g_test, df_train, df_test, dset, features




class PVS():

    # 1. Initialization
    '''
    x_train: training samples
    data_type: Syn1 to Syn 6
    '''
    def __init__(self, x_train, data_type, nepoch, is_logging_enabled=True):
        self.is_logging_enabled = is_logging_enabled
        self.latent_dim1 = 100      # Dimension of actor (generator) network
        self.latent_dim2 = 200      # Dimension of critic (discriminator) network

        self.batch_size = min(1000, x_train.shape[0])      # Batch size
        self.epochs = nepoch        # Epoch size (large epoch is needed due to the policy gradient framework)
        self.lamda = 0.1            # Hyper-parameter for the number of selected features
        self.omega = 0.0

        self.input_shape = x_train.shape[1]     # Input dimension
        #logger.info('input shape: {}'.format(self.input_shape))

        # Actionvation. (For Syn1 and 2, relu, others, selu)
        self.activation = 'relu' if data_type in ['Syn1','Syn2'] else 'selu'

        # Use Adam optimizer with learning rate = 0.0001
        optimizer = Adam(0.0001)

        # Build and compile the discriminator (critic)
        self.discriminator = self.build_discriminator()
        # Use categorical cross entropy as the loss
        self.discriminator.summary()
        self.discriminator.compile(loss=["mean_squared_error",#'categorical_crossentropy',
                                         self.loss_z_var],
                                   loss_weights=[0, 1],
                                   optimizer=optimizer,
                                   metrics={'dense3': 'acc'})

        # Build the generator (actor)
        self.generator = self.build_generator()
        # Use custom loss (my loss)
        self.generator.compile(loss=self.my_loss, optimizer=optimizer)

        # Build and compile the value function
        self.valfunction = self.build_valfunction()
        # Use categorical cross entropy as the loss
        self.valfunction.compile(loss="mean_squared_error",#'categorical_crossentropy',
                                 optimizer=optimizer, metrics=['acc'])

    def loss_z_var(self, y_true, y_pred):
        d = 1  # y_pred.shape[-1] // 2
        z_log_sigma = y_pred[:, :d]
        mu = y_pred[:, d:]
        loss = 0.5 * tf.reduce_sum(K.square(mu - y_true) * tf.exp(-z_log_sigma) + z_log_sigma, axis=1)
        return tf.reduce_mean(loss)

        # %% Custom loss definition

    def my_loss(self, y_true, y_pred):

        # dimension of the features
        d = y_pred.shape[1]
        sq_sigma = y_true[:, :1]
        # Put all three in y_true
        # 1. selected probability
        sel_prob = y_true[:, 1: 1 + d]
        # 2. discriminator output
        dis_prob = y_true[:, 1 + d: 1 + (d + 2)]
        # 3. valfunction output
        val_prob = y_true[:, 1 + (d + 2): 1 + (d + 4)]
        # 4. ground truth
        y_final = y_true[:, 1 + (d + 4):]

        # A1. Compute the rewards of the actor network
        Reward1 = tf.reduce_sum(y_final * tf.log(dis_prob + 1e-8), axis=1)
        # A2. Compute the rewards of the actor network
        Reward2 = tf.reduce_sum(y_final * tf.log(val_prob + 1e-8), axis=1)

        # Difference is the rewards
        Reward = Reward1 - Reward2

        # B. Policy gradient loss computation.
        loss1 = Reward * tf.reduce_sum(sel_prob * K.log(y_pred + 1e-8) + (1 - sel_prob) * K.log(1 - y_pred + 1e-8),
                                       axis=1) - self.lamda * tf.reduce_mean(y_pred, axis=1) + self.omega * sq_sigma

        # C. Maximize the loss1
        loss = tf.reduce_mean(-loss1)

        return loss

        # %% Generator (Actor)

    def build_generator(self):

        model = Sequential()

        model.add(Dense(100, activation=self.activation, name='s/dense1', kernel_regularizer=regularizers.l2(1e-3),
                        input_dim=self.input_shape))
        model.add(Dense(100, activation=self.activation, name='s/dense2', kernel_regularizer=regularizers.l2(1e-3)))
        model.add(Dense(self.input_shape, activation='sigmoid', name='s/dense3', kernel_regularizer=regularizers.l2(1e-3)))

        # Since there's 1-dim, selection should always hit
        model.add(Lambda(lambda x: tf.add(x,  0.99 - x)))

        if self.is_logging_enabled:
            model.summary()

        feature = Input(shape=(self.input_shape,), dtype='float32')
        select_prob = model(feature)

        return Model(feature, select_prob)

        # %% Discriminator (Critic)

    def build_discriminator(self):
        # There are two inputs to be used in the discriminator
        # 1. Features
        feature = Input(shape=(self.input_shape,), dtype='float32')
        # 2. Selected Features
        select = Input(shape=(self.input_shape,), dtype='float32')
        # Element-wise multiplication
        model_input = Multiply()([feature, select])

        x = Dense(200, activation=self.activation, name='dense1',
                  kernel_regularizer=regularizers.l2(1e-3))(model_input)
        x = BatchNormalization()(x)

        z = Dense(200, name='dense2', kernel_regularizer=regularizers.l2(1e-3))(x)
        z = BatchNormalization()(z)
        pred = Dense(2, activation='softmax', name='dense3',
                     kernel_regularizer=regularizers.l2(1e-3))(z)

        z_log_var = Dense(200, activation=self.activation, name='pre_z_log_var',
                          kernel_regularizer=regularizers.l2(1e-3))(x)
        z_log_var = BatchNormalization()(z_log_var)
        # z_log_var = Dense(2, name = 'z_log_var', kernel_regularizer=regularizers.l2(1e-3))(z_log_var)
        z_log_var = Dense(1, name='z_log_var', kernel_regularizer=regularizers.l2(1e-3))(z_log_var)
        # For sake numerical stabiity
        z_log_var = Lambda(lambda tmp: tf.log(1e-6 + tf.exp(tmp)))(z_log_var)

        return Model(inputs=[feature, select], outputs=[pred, Concatenate()([z_log_var, pred])])

        # %% Value Function

    def build_valfunction(self):

        model = Sequential()

        model.add(Dense(200, activation=self.activation, name='v/dense1', kernel_regularizer=regularizers.l2(1e-3),
                        input_dim=self.input_shape))
        model.add(BatchNormalization())  # Use Batch norm for preventing overfitting
        model.add(Dense(200, activation=self.activation, name='v/dense2', kernel_regularizer=regularizers.l2(1e-3)))
        model.add(BatchNormalization())
        model.add(Dense(2, activation='softmax', name='v/dense3', kernel_regularizer=regularizers.l2(1e-3)))

        if self.is_logging_enabled:
            model.summary()

        # There are one inputs to be used in the value function
        # 1. Features
        feature = Input(shape=(self.input_shape,), dtype='float32')

        # Element-wise multiplication
        prob = model(feature)

        return Model(feature, prob)

        # %% Sampling the features based on the output of the generator

    def Sample_M(self, gen_prob):
        # Shape of the selection probability
        n = gen_prob.shape[0]
        d = gen_prob.shape[1]
        # print(np.sum(gen_prob))
        # Sampling
        samples = np.random.binomial(1, gen_prob, (n, d))
        return samples

        # %% Training procedure

    def train(self, x_train, y_train):

        # For each epoch (actually iterations)
        for epoch in range(self.epochs):

            # %% Train Discriminator
            # Select a random batch of samples
            idx = np.random.randint(0, x_train.shape[0], self.batch_size)
            x_batch = x_train[idx, :]
            y_batch = y_train[idx, :]

            # Generate a batch of probabilities of feature selection
            gen_prob = self.generator.predict(x_batch)

            # Sampling the features based on the generated probability
            sel_prob = self.Sample_M(gen_prob)

            # Compute the prediction of the critic based on the sampled features (used for generator training)
            dis_prob, z_log_var = self.discriminator.predict([x_batch, sel_prob])
            z_log_var = z_log_var[:, :1]
            # print("z_log_var", np.sum(z_log_var))

            # Train the discriminator
            d_loss_new = self.discriminator.train_on_batch([x_batch, sel_prob], [y_batch, y_batch])
            # print(self.discriminator.metrics_names, d_loss_new)
            d_loss = [d_loss_new[1], d_loss_new[3]]
            z_var_loss_value = d_loss_new[2]
            d_loss_sum = d_loss_new[0]

            # %% Train Valud function

            # Compute the prediction of the critic based on the sampled features (used for generator training)
            val_prob = self.valfunction.predict(x_batch)

            # Train the discriminator
            v_loss = self.valfunction.train_on_batch(x_batch, y_batch)

            # %% Train Generator
            # Use three things as the y_true: sel_prob, dis_prob, and ground truth (y_batch)
            y_batch_final = np.concatenate((z_log_var, sel_prob, np.asarray(dis_prob), np.asarray(val_prob), y_batch),
                                           axis=1)

            # Train the generator
            # print(np.sum(x_batch), np.sum(sel_prob), np.sum(dis_prob), np.sum(val_prob), np.sum(y_batch))

            g_loss = self.generator.train_on_batch(x_batch, y_batch_final)

            # %% Plot the progress
            dialog = 'Epoch: ' + '{:6d}'.format(epoch) + ', d_loss (Acc)): '
            dialog += '{:0.3f}'.format(d_loss[1]) + ', v_loss (Acc): '
            dialog += '{:0.3f}'.format(v_loss[1]) + ', g_loss: ' + '{:+6.4f}'.format(g_loss)

            if epoch % 100 == 0:
                print('{}'.format(dialog))

        # %% Selected Features

    def output(self, x_train):

        gen_prob = self.generator.predict(x_train)

        return np.asarray(gen_prob)

        # %% Prediction Results

    def get_prediction(self, x_train, m_train):

        val_prediction = self.valfunction.predict(x_train)

        dis_prediction, z_log_var = self.discriminator.predict([x_train, m_train])
        z_log_var = z_log_var[:, :1]

        return np.asarray(val_prediction), np.asarray(dis_prediction), np.exp(np.asarray(z_log_var))


def init_arg():
    parser = argparse.ArgumentParser()
    parser.add_argument("--feat", default=0, type=int)
    parser.add_argument("--it", default=1500, type=int)
    parser.add_argument("-o", default='feature_score.csv.gz')
    parser.add_argument(
        '--dataset',
        help='load one of the available/buildin datasets'
        ' [spam, spambase, letter, ...] use show to see a list')
    parser.add_argument(
        '-i', help='load data as a csv file, requires the name of the label'
        ' (reponsevar) to be specified as well (if applicable), this column'
        'will not be processed')
    parser.add_argument(
        '--target', help='specifies the column with the response var '
        'if applicable when loading a csv file, this column will'
        ' not be processed')
    return parser.parse_args()


if __name__ == '__main__':

    args = init_arg()

    ocsv = args.o
    odir = os.path.dirname(ocsv)
    odir = '.' if not len(odir) else odir
    fn_csv = args.i
    label_nm = args.target
    nepoch = args.it
    logger = utilmlab.init_logger(odir)

    feat_num = args.feat

    dataset = args.dataset

    assert dataset is not None or fn_csv is not None
    assert fn_csv is None or label_nm is not None

    # Data output can be either binary (Y) or Probability (Prob)
    data_out_sets = ['Y', 'Prob']
    data_out = data_out_sets[0]

    logger.info('invase: {} {} {} {}'.format(dataset, nepoch, odir, data_out))

    # Number of Training and Testing samples
    train_N = 10000
    test_N = 10000

    # Seeds (different seeds for training and testing)
    train_seed = 0
    test_seed = 1

    x_train_feature, y_train_label = [], []
    x_test_feature, y_test_pred, sq_sigma_list = [], [], []

    x_train, y_train, _, x_test, y_test, g_test, df_train, df_test, \
        dset, features = load_create_data(
            dataset,
            data_out,
            is_logging_enabled=True,
            fn_csv=fn_csv,
            label_nm=label_nm)

    x_train_feature.extend(x_train.flatten().tolist())
    x_test_feature.extend(x_test.flatten().tolist())
    y_train_label.extend(y_train[:,0].flatten().tolist())

    PVS_Alg = PVS(x_train, dataset, nepoch)

    PVS_Alg.train(x_train, y_train)

    # 3. Get the selection probability on the testing set
    Sel_Prob_Test = PVS_Alg.output(x_test)

    # 4. Selected features
    score = 1.*(Sel_Prob_Test > 0.5)

    # 5. Prediction
    val_predict, dis_predict, sq_sigma = PVS_Alg.get_prediction(x_test, score)

    y_test_pred.extend(dis_predict[:,0].flatten().tolist())
    sq_sigma_list.extend(sq_sigma.flatten().tolist())


    #%% Performance Metrics
    def performance_metric(score, g_truth):

        n = len(score)
        Temp_TPR = np.zeros([n,])
        Temp_FDR = np.zeros([n,])

        for i in range(n):

            # TPR
            TPR_Nom = np.sum(score[i,:] * g_truth[i,:])
            TPR_Den = np.sum(g_truth[i,:])
            Temp_TPR[i] = 100 * float(TPR_Nom)/float(TPR_Den+1e-8)

            # FDR
            FDR_Nom = np.sum(score[i,:] * (1-g_truth[i,:]))
            FDR_Den = np.sum(score[i,:])
            Temp_FDR[i] = 100 * float(FDR_Nom)/float(FDR_Den+1e-8)

        return np.mean(Temp_TPR), np.mean(Temp_FDR),\
            np.std(Temp_TPR), np.std(Temp_FDR)

    #%% Output

    TPR_mean, TPR_std = -1, 0
    FDR_mean, FDR_std = -1, 0
    if g_test is not None:
        TPR_mean, FDR_mean, TPR_std, FDR_std = performance_metric(
            score, g_test)

        logger.info('TDR mean: {:0.1f}%  std: {:0.1f}%'.format(
            TPR_mean, TPR_std))
        logger.info('FDR mean: {:0.1f}%  std: {:0.1f}%'.format(
            FDR_mean, FDR_std))
    else:
        logger.info('no ground truth relevance')

    log_var, y_pred, y_true, x = [], [], [], []
    #%% Prediction Results
    Predict_Out = np.zeros([20, 3, 2])

    for i in range(20):

        # different teat seed
        test_seed = i + 2
        x_train, y_train, _, x_test, y_test, _, train, df_test, dset, features = load_create_data(
            dataset,
            data_out,
            is_logging_enabled=True,
            fn_csv=fn_csv,
            label_nm=label_nm)

        #logger.info('x_test:{}'.format(x_test.shape))

        # 1. Get the selection probability on the testing set
        Sel_Prob_Test = PVS_Alg.output(x_test)

        # 2. Selected features
        score = 1.*(Sel_Prob_Test > 0.5)

        #logger.info('selprob {}) {}'.format(i, np.mean(Sel_Prob_Test, axis=0)))
        #logger.info('score   {}) {}'.format(i, np.mean(score, axis=0)))

        # 3. Prediction
        val_predict, dis_predict, sq_sigma = PVS_Alg.get_prediction(x_test, score)

        #y.extend(np.abs(np.abs(y_test[:,1] - val_predict[:,1])-np.abs(y_test[:,1] - dis_predict[:,1])).flatten().tolist())
        #y.extend(np.abs(val_predict[:,1] - dis_predict[:,1]).tolist())
        #y.extend((- y_test[:, 1] * np.log(dis_predict[:, 1]) - (1 - y_test[:, 1]) * np.log(1 - dis_predict[:, 1])).flatten().tolist())
        #y.extend((2 * np.square(y_test[:, 1] - dis_predict[:, 1])).tolist())
        log_var.extend(np.log(sq_sigma).flatten().tolist())
        y_pred.extend(dis_predict[:, 1].flatten().tolist())
        y_true.extend(y_test[:, 1].flatten().tolist())
        x.extend(x_test.flatten().tolist())

        # 4. Prediction Results
        Predict_Out[i,0,0] = roc_auc_score(y_test[:,1], val_predict[:,1])
        Predict_Out[i,1,0] = average_precision_score(y_test[:,1], val_predict[:,1])
        Predict_Out[i,2,0] = accuracy_score(y_test[:,1], 1. * (val_predict[:,1]>0.5) )

        Predict_Out[i,0,1] = roc_auc_score(y_test[:,1], dis_predict[:,1])
        Predict_Out[i,1,1] = average_precision_score(y_test[:,1], dis_predict[:,1])
        Predict_Out[i,2,1] = accuracy_score(y_test[:,1], 1. * (dis_predict[:,1]>0.5) )


    #'''

    title = None
    x_title =  features
    y_title = "Prediction"

    import matplotlib.pyplot as plt

    test_idx = np.argsort(x_test_feature)
    x_test_feature = np.array(x_test_feature)[test_idx]
    y_test_pred = np.array(y_test_pred)[test_idx]
    sq_sigma_list = np.array(sq_sigma_list)[test_idx]

    train_idx = np.argsort(x_train_feature)
    x_train_feature = np.array(x_train_feature)[train_idx]
    y_train_label = np.array(y_train_label)[train_idx]

    plt.figure()
    plt.scatter(x_train_feature, y_train_label, label="Train", color="b")
    plt.scatter(x_test_feature, y_test_pred, label="Test", color="r")
    sigma = np.sqrt(np.array(sq_sigma_list))
    #sigma = (sigma - sigma.min()) / (sigma.max() - sigma.min())
    plt.fill_between(x_test_feature, y_test_pred - sigma, y_test_pred + sigma, alpha=0.2, label="Uncertainty", interpolate=True, color="r")

    plt.title(title, fontsize=16)
    plt.xlabel(x_title, fontsize=16)
    plt.ylabel(y_title, fontsize=16)
    plt.tick_params(labelsize=14)
    plt.legend(fontsize=16)
    #plt.show()
    plt.savefig("%d.png" % args.feat, format="png", bbox_inches="tight")