from __future__ import absolute_import

from .._compat import basestring
from .._compat import chain_exception
from .._compat import pickle
from collections import OrderedDict
import itertools
from warnings import warn
from time import time
import os

from lasagne.layers import get_all_layers
from lasagne.layers import get_output
from lasagne.layers import InputLayer
from lasagne.layers import Layer
from lasagne import regularization
from lasagne.objectives import aggregate
from lasagne.objectives import categorical_crossentropy, binary_crossentropy
from lasagne.objectives import squared_error
from lasagne.updates import nesterov_momentum
from lasagne.utils import unique
from lasagne.init import Uniform
import numpy as np
from sklearn.base import BaseEstimator
from sklearn.cross_validation import KFold
from sklearn.cross_validation import StratifiedKFold
from sklearn.metrics import accuracy_score
from sklearn.metrics import mean_squared_error
from sklearn.preprocessing import LabelEncoder
import theano
from theano import tensor as T

from . import PrintLog
from . import PrintLayerInfo

class _list(list):
    pass

class _dict(dict):
    def __contains__(self, key):
        return True


def _sldict(arr, sl):
    if isinstance(arr, dict):
        return {k: v[sl] for k, v in arr.items()}
    else:
        return arr[sl]


class Layers(OrderedDict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values()).__getitem__(key)
        elif isinstance(key, slice):
            items = list(self.items()).__getitem__(key)
            return Layers(items)
        else:
            return super(Layers, self).__getitem__(key)

    def keys(self):
        return list(super(Layers, self).keys())

    def values(self):
        return list(super(Layers, self).values())


class BatchIterator(object):
    def __init__(self, batch_size):
        self.batch_size = batch_size

    def __call__(self, X, y=None):
        self.X, self.y = X, y
        return None, self

    def __iter__(self):
        bs = self.batch_size
        for i in range((self.n_samples + bs - 1) // bs):
            sl = slice(i * bs, (i + 1) * bs)
            Xb = _sldict(self.X, sl)
            if self.y is not None:
                yb = self.y[sl]
            else:
                yb = None
            yield self.transform(Xb, yb)

    @property
    def n_samples(self):
        X = self.X
        if isinstance(X, dict):
            return len(list(X.values())[0])
        else:
            return len(X)

    def transform(self, Xb, yb):
        return Xb, yb

    def __getstate__(self):
        state = dict(self.__dict__)
        for attr in ('X', 'y',):
            if attr in state:
                del state[attr]
        return state


class TrainSplit(object):
    def __init__(self, eval_size, stratify=True):
        self.eval_size = eval_size
        self.stratify = stratify

    def __call__(self, X, y, net):
        if self.eval_size:
            if net.regression or not self.stratify:
                kf = KFold(y.shape[0], round(1. / self.eval_size))
            else:
                kf = StratifiedKFold(y, round(1. / self.eval_size))

            train_indices, valid_indices = next(iter(kf))
            X_train, y_train = _sldict(X, train_indices), y[train_indices]
            X_valid, y_valid = _sldict(X, valid_indices), y[valid_indices]
        else:
            X_train, y_train = X, y
            X_valid, y_valid = _sldict(X, slice(len(y), None)), y[len(y):]

        return X_train, X_valid, y_train, y_valid


class LegacyTrainTestSplit(object):  # BBB
    def __init__(self, eval_size=0.2):
        self.eval_size = eval_size

    def __call__(self, X, y, net):
        return net.train_test_split(X, y, self.eval_size)


def objective(layers,
              loss_function,
              target,
              aggregate=aggregate,
              mode='mean',
              weights=None,
              deterministic=False,
              l1=0,
              l2=0,
              l3=0,
              l3_layers=[],
              get_output_kw=None):
    if get_output_kw is None:
        get_output_kw = {}
    output_layer = layers[-1]
    network_output = get_output(
        output_layer, deterministic=deterministic, **get_output_kw)
    loss = aggregate(loss_function(network_output, target), weights=weights, mode=mode)

    if l1:
        loss += regularization.regularize_layer_params(
            layers.values(), regularization.l1) * l1
    if l2:
        loss += regularization.regularize_layer_params(
            layers.values(), regularization.l2) * l2
    if l3:
        for layer in l3_layers:
            loss += regularization.regularize_layer_params(
                layer, regularization.l2) * l3
    return loss


class NeuralNet(BaseEstimator):
    """A scikit-learn estimator based on Lasagne.
    """
    def __init__(
        self,
        layers,
        update=nesterov_momentum,
        loss=None,  # BBB
        objective=objective,
        objective_loss_function=None,
        batch_iterator_train=BatchIterator(batch_size=128),
        batch_iterator_test=BatchIterator(batch_size=128),
        regression=False,
        max_epochs=100,
        train_split=TrainSplit(eval_size=0.2),
        custom_score=None,
        X_tensor_type=None,
        y_tensor_type=None,
        use_label_encoder=False,
        on_batch_finished=None,
        on_epoch_finished=None,
        on_training_started=None,
        on_training_finished=None,
        more_params=None,
        layer_weights= None,
        account_weights=False,
        account_weight_layers = [],
        l3_layers = [],
        verbose=0,
        identifier='test',
        HOME='',
        netname = '',
        **kwargs
        ):
        assert isinstance( int( identifier ), int )
        fp_accW = str(identifier)+'_accW'
        if loss is not None:
            raise ValueError(
                "The 'loss' parameter was removed, please use "
                "'objective_loss_function' instead.")  # BBB
        if hasattr(objective, 'get_loss'):
            raise ValueError(
                "The 'Objective' class is no longer supported, please "
                "use 'nolearn.lasagne.objective' or similar.")  # BBB
        if objective_loss_function is None:
            objective_loss_function = (
                squared_error if regression else categorical_crossentropy)

        if hasattr(self, 'train_test_split'):  # BBB
            warn("The 'train_test_split' method has been deprecated, please "
                 "use the 'train_split' parameter instead.")
            train_split = LegacyTrainTestSplit(
                eval_size=kwargs.pop('eval_size', 0.2))

        if 'eval_size' in kwargs:  # BBB
            warn("The 'eval_size' argument has been deprecated, please use "
                 "the 'train_split' parameter instead, e.g.\n"
                 "train_split=TrainSplit(eval_size=0.4)")
            train_split.eval_size = kwargs.pop('eval_size')

        if y_tensor_type is None:
            if regression:
                y_tensor_type = T.TensorType(
                    theano.config.floatX, (False, False))
            else:
                y_tensor_type = T.ivector

        if X_tensor_type is not None:
            raise ValueError(
                "The 'X_tensor_type' parameter has been removed. "
                "It's unnecessary.")  # BBB

        if isinstance(layers, Layer):
            layers = _list([layers])

        self.layers = layers
        self.update = update
        self.objective = objective
        self.objective_loss_function = objective_loss_function
        self.batch_iterator_train = batch_iterator_train
        self.batch_iterator_test = batch_iterator_test
        self.regression = regression
        self.max_epochs = max_epochs
        self.train_split = train_split
        self.custom_score = custom_score
        self.y_tensor_type = y_tensor_type
        self.use_label_encoder = use_label_encoder
        self.on_batch_finished = on_batch_finished or []
        self.on_epoch_finished = on_epoch_finished or []
        self.on_training_started = on_training_started or []
        self.on_training_finished = on_training_finished or []
        self.more_params = more_params or {}
        self.layer_weights = layer_weights
        self.account_weights = account_weights
        self.account_weight_layers = account_weight_layers
        self.fp_accW = fp_accW
        self.l3_layers = l3_layers
        self.verbose = verbose
        self.identifier = identifier
        self.netname = netname
        self.HOME = HOME
        if self.verbose:
            # XXX: PrintLog should come before any other handlers,
            # because early stopping will otherwise cause the last
            # line not to be printed
            self.on_epoch_finished.append(PrintLog())
            self.on_training_started.append(PrintLayerInfo())

        for key in kwargs.keys():
            assert not hasattr(self, key)
        vars(self).update(kwargs)
        self._kwarg_keys = list(kwargs.keys())

        self.train_history_ = []


        if 'batch_iterator' in kwargs:  # BBB
            raise ValueError(
                "The 'batch_iterator' argument has been replaced. "
                "Use 'batch_iterator_train' and 'batch_iterator_test' instead."
                )
    def print_list_of_layers( self ):
        nameL = []
        for name, layer in self.layers_.items():
            NORML = 'norm' in name
            POOLL = 'pool' in name
            INL   = 'input' in name
            DROPL = 'drop' in name
            if not NORML and not POOLL and not INL and not DROPL:
                nameL.append( name )
        print nameL
        print 'You need {} layer_weights\n'.format( 2 * len(nameL) )

    def _check_for_unused_kwargs(self):
        names = self.layers_.keys() + ['update', 'objective']
        for k in self._kwarg_keys:
            for n in names:
                prefix = '{}_'.format(n)
                if k.startswith(prefix):
                    break
            else:
                raise ValueError("Unused kwarg: {}".format(k))

    def _check_good_input(self, X, y=None):
        if isinstance(X, dict):
            lengths = [len(X1) for X1 in X.values()]
            if len(set(lengths)) > 1:
                raise ValueError("Not all values of X are of equal length.")
            x_len = lengths[0]
        else:
            x_len = len(X)

        if y is not None:
            if len(y) != x_len:
                raise ValueError("X and y are not of equal length.")

        if self.regression and y is not None and y.ndim == 1:
            y = y.reshape(-1, 1)

        return X, y

    def initialize(self):
        if getattr(self, '_initialized', False):
            return

        out = getattr(self, '_output_layer', None)
        if out is None:
            out = self._output_layer = self.initialize_layers()
        self._check_for_unused_kwargs()

        iter_funcs = self._create_iter_funcs(
            self.layers_, self.objective, self.update,
            self.y_tensor_type,
            )
        self.train_iter_, self.eval_iter_, self.predict_iter_ = iter_funcs
        self._initialized = True

    def _get_params_for(self, name):
        collected = {}
        prefix = '{}_'.format(name)

        params = vars(self)
        more_params = self.more_params

        for key, value in itertools.chain(params.items(), more_params.items()):
            if key.startswith(prefix):
                collected[key[len(prefix):]] = value

        return collected

    def _layer_name(self, layer_class, index):
        return "{}{}".format(
            layer_class.__name__.lower().replace("layer", ""), index)

    def initialize_layers(self, layers=None):
        if layers is not None:
            self.layers = layers
        self.layers_ = Layers()

        if isinstance(self.layers[0], Layer):
            # 'self.layers[0]' is already the output layer with type
            # 'lasagne.layers.Layer', so we only have to fill
            # 'self.layers_' and we're done:
            for i, layer in enumerate(get_all_layers(self.layers[0])):
                name = layer.name or self._layer_name(layer.__class__, i)
                self.layers_[name] = layer
                if self._get_params_for(name) != {}:
                    raise ValueError(
                        "You can't use keyword params when passing a Lasagne "
                        "instance object as the 'layers' parameter of "
                        "'NeuralNet'."
                        )
            return self.layers[0]

        # 'self.layers' are a list of '(Layer class, kwargs)', so
        # we'll have to actually instantiate the layers given the
        # arguments:
        layer = None
        for i, layer_def in enumerate(self.layers):

            if isinstance(layer_def[0], basestring):
                # The legacy format: ('name', Layer)
                layer_name, layer_factory = layer_def
                layer_kw = {'name': layer_name}
            else:
                # New format: (Layer, {'layer': 'kwargs'})
                layer_factory, layer_kw = layer_def
                layer_kw = layer_kw.copy()

            if 'name' not in layer_kw:
                layer_kw['name'] = self._layer_name(layer_factory, i)

            more_params = self._get_params_for(layer_kw['name'])
            layer_kw.update(more_params)

            if layer_kw['name'] in self.layers_:
                raise ValueError(
                    "Two layers with name {}.".format(layer_kw['name']))

            # Any layers that aren't subclasses of InputLayer are
            # assumed to require an 'incoming' paramter.  By default,
            # we'll use the previous layer as input:
            if not issubclass(layer_factory, InputLayer):
                if 'incoming' in layer_kw:
                    layer_kw['incoming'] = self.layers_[
                        layer_kw['incoming']]
                elif 'incomings' in layer_kw:
                    layer_kw['incomings'] = [
                        self.layers_[name] for name in layer_kw['incomings']]
                else:
                    layer_kw['incoming'] = layer

            for attr in ('W', 'b'):
                if isinstance(layer_kw.get(attr), str):
                    name = layer_kw[attr]
                    layer_kw[attr] = getattr(self.layers_[name], attr, None)

            try:
                layer_wrapper = layer_kw.pop('layer_wrapper', None)
                layer = layer_factory(**layer_kw)
            except TypeError as e:
                msg = ("Failed to instantiate {} with args {}.\n"
                       "Maybe parameter names have changed?".format(
                           layer_factory, layer_kw))
                chain_exception(TypeError(msg), e)
            self.layers_[layer_kw['name']] = layer
            if layer_wrapper is not None:
                layer = layer_wrapper(layer)
                self.layers_["LW_%s" % layer_kw['name']] = layer

        if self.layer_weights is not None:
            print '\nAssure that the weights for each layer are in the correct order!'
            print 'the order should be:'
            self.print_list_of_layers()
            print 'Note that norm and pooling layers do not have any weights related to them.\n'\
                  'Conv and dense layers have, however, a bias unit plus the weight matrix.\n'\
                  'In lasagne these count as two independent parametersets.\n'
        return layer

    def _create_iter_funcs(self, layers, objective, update, output_type):
        y_batch = output_type('y_batch')

        output_layer = layers[-1]
        objective_kw = self._get_params_for('objective')

        l3Layers = []
        for l3_name in self.l3_layers:
            l3Layers.append( layers[ l3_name ] )

        loss_train = objective(
            layers, target=y_batch, l3_layers=l3Layers, **objective_kw)
        loss_eval = objective(
            layers, target=y_batch, deterministic=True, **objective_kw)
        predict_proba = get_output(output_layer, None, deterministic=True)

        if not self.regression:
            predict = predict_proba.argmax(axis=1)
            accuracy = T.mean(T.eq(predict, y_batch))
        elif self.objective_loss_function is binary_crossentropy:
            predict = T.where( predict_proba >= 0.5, 1, 0 )
            accuracy = T.mean( T.eq(predict, y_batch) )
        else:
            predict = T.where( predict_proba > 0., 1, 0 )
            label   = T.where( y_batch > 0., 1, 0 )
            accuracy = T.mean( T.eq( predict, label ) )

        all_params = self.get_all_params(trainable=True)
        update_params = self._get_params_for('update')
        updates = update(loss_train, all_params, layer_weights=self.layer_weights, **update_params )

        input_layers = [layer for layer in layers.values()
                        if isinstance(layer, InputLayer)]

        X_inputs = [theano.Param(input_layer.input_var, name=input_layer.name)
                    for input_layer in input_layers]
        inputs = X_inputs + [theano.Param(y_batch, name="y")]

        train_iter = theano.function(
            inputs=inputs,
            outputs=[loss_train],
            updates=updates,
            allow_input_downcast=True,
            )
        eval_iter = theano.function(
            inputs=inputs,
            outputs=[loss_eval, accuracy],
            allow_input_downcast=True,
            )
        predict_iter = theano.function(
            inputs=X_inputs,
            outputs=predict_proba,
            allow_input_downcast=True,
            )

        return train_iter, eval_iter, predict_iter

    def fit(self, X_train, y_train, X_valid, y_valid ):
        X_train, y_train = self._check_good_input(X_train, y_train)
        X_valid, y_valid = self._check_good_input(X_valid, y_valid)

        if self.use_label_encoder:
            self.enc_ = LabelEncoder()
            y = self.enc_.fit_transform(y).astype(np.int32)
            self.classes_ = self.enc_.classes_
        self.initialize()

        try:
            self.train_loop(X_train, y_train, X_valid, y_valid )
        except KeyboardInterrupt:
            pass
        return self

    def partial_fit(self, X, y, classes=None):
        return self.fit(X, y, epochs=1)

    def train_loop(self, X_train, y_train, X_valid, y_valid ):
#        X_train, X_valid, y_train, y_valid = self.train_split(X, y, self)

        on_batch_finished = self.on_batch_finished
        if not isinstance(on_batch_finished, (list, tuple)):
            on_batch_finished = [on_batch_finished]

        on_epoch_finished = self.on_epoch_finished
        if not isinstance(on_epoch_finished, (list, tuple)):
            on_epoch_finished = [on_epoch_finished]

        on_training_started = self.on_training_started
        if not isinstance(on_training_started, (list, tuple)):
            on_training_started = [on_training_started]

        on_training_finished = self.on_training_finished
        if not isinstance(on_training_finished, (list, tuple)):
            on_training_finished = [on_training_finished]

        epoch = 0
        best_valid_loss = (
            min([row['valid_loss'] for row in self.train_history_]) if
            self.train_history_ else np.inf
            )
        best_train_loss = (
            min([row['train_loss'] for row in self.train_history_]) if
            self.train_history_ else np.inf
            )
        best_train_accuracy = (
            max([row['train_accuracy'] for row in self.train_history_]) if
            self.train_history_ else  - np.inf
            )
        best_valid_accuracy = (
            max([row['valid_accuracy'] for row in self.train_history_]) if
            self.train_history_ else - np.inf
            )
        for func in on_training_started:
            func(self, self.train_history_)

        num_epochs_past = len(self.train_history_)
    
        self.SANE = True
        while epoch < self.max_epochs:
            if not self.SANE:
                continue
            epoch += 1

            train_losses = []
            train_accuracies = []
            valid_losses = []
            valid_accuracies = []
            custom_score = []

            t0 = time()
            time_i = time()
            for k, fpaths, Xb, yb in self.batch_iterator_train( X_train, y_train ):
                if not self.SANE:
                    continue
                #print time() - time_i
                if self.account_weights:
                    time0 = time()
                    self.load_account_weights( k )

                time0 = time()
                batch_train_loss = self.apply_batch_func( self.train_iter_, Xb, yb )
                #print 'training batch', time() - time0
                accuracy = 0.

#                dummy, accuracy = self.apply_batch_func(
#                    self.eval_iter_, Xb, yb)

                train_accuracies.append(accuracy)
                train_losses.append(batch_train_loss)
   
                for func in on_batch_finished:
                    func(self, self.train_history_)

                if self.account_weights:
                    time0 = time()
                    self.save_account_weights( k )
                time_i = time()

            for k, fpaths, Xb, yb in self.batch_iterator_train( X_valid, y_valid ):
                if self.account_weights:
                    self.load_account_weights( k )
           
                time0 = time()
                batch_valid_loss, accuracy = self.apply_batch_func(
                    self.eval_iter_, Xb, yb)
                #print 'predicting batch', time()-time0

                valid_losses.append(batch_valid_loss)
                valid_accuracies.append( accuracy )

                if self.custom_score:
                    y_prob = self.apply_batch_func(self.predict_iter_, Xb)
                    custom_score.append(self.custom_score[1](yb, y_prob))

            avg_train_loss = np.mean(train_losses)
            avg_valid_loss = np.mean(valid_losses)

            avg_train_accuracy = np.mean( train_accuracies )
            avg_valid_accuracy = np.mean( valid_accuracies )

            if custom_score:
                avg_custom_score = np.mean(custom_score)

            if avg_train_loss < best_train_loss:
                best_train_loss = avg_train_loss
            if avg_valid_loss < best_valid_loss:
                best_valid_loss = avg_valid_loss

            if avg_train_accuracy > best_train_accuracy:
                best_train_accuracy = avg_train_accuracy
            if avg_valid_accuracy > best_valid_accuracy:
                best_valid_accuracy = avg_valid_accuracy

            info = {
                'epoch': num_epochs_past + epoch,
                'train_loss': avg_train_loss,
                'train_loss_best': best_train_loss == avg_train_loss,
                'valid_loss': avg_valid_loss,
                'valid_loss_best': best_valid_loss == avg_valid_loss,
                'train_accuracy': avg_train_accuracy,
                'train_acc_best': avg_train_accuracy == best_train_accuracy,
                'valid_accuracy': avg_valid_accuracy,
                'valid_acc_best': avg_valid_accuracy == best_valid_accuracy,
                'dur': time() - t0,
                }
            if self.custom_score:
                info[self.custom_score[0]] = avg_custom_score
            self.train_history_.append(info)

            try:
                for func in on_epoch_finished:
                    func(self, self.train_history_)
            except StopIteration:
                break

        for func in on_training_finished:
            func(self, self.train_history_)

    @staticmethod
    def apply_batch_func(func, Xb, yb=None):
        if isinstance(Xb, dict):
            kwargs = dict(Xb)
            if yb is not None:
                kwargs['y'] = yb
            return func(**kwargs)
        else:
            return func(Xb) if yb is None else func(Xb, yb)

    def predict_proba(self, X, y=None ):
        probas = []
        real_probas = []
        X_reordered = []
        y_reordered = []

        for k, fpaths, Xb, yb in self.batch_iterator_test( X, y ):
            X_reordered.append( fpaths )
            if self.account_weights:
                self.load_account_weights( k, BEST_LOSS = True )

            probas.append(self.apply_batch_func(self.predict_iter_, Xb))

            try:
                yb = np.reshape( yb, ( len(yb),1 ) )
            except TypeError:
                pass
            y_reordered.append( yb )

        X_reordered = [ val for arr in X_reordered for val in arr ]
        return np.vstack( probas ),  np.vstack(y_reordered)[:,0], X_reordered


    def predict(self, X, y=None):
        y_pred, y_reordered, X_reordered = self.predict_proba(X,y)
        if self.regression:
            return y_pred, y_reordered, X_reordered
        else:
            y_pred = np.argmax( y_pred, axis = 1 )
            if self.use_label_encoder:
                y_pred = self.enc_.inverse_transform( y_pred )
            return y_pred, y_reordered, X_reordered

    def get_all_layers(self):
        return self.layers_.values()

    def get_all_params(self, **kwargs):
        layers = self.get_all_layers()
        params = sum([l.get_params(**kwargs) for l in layers], [])
        return unique(params)

    def get_all_params_values(self):
        return_value = OrderedDict()
        for name, layer in self.layers_.items():
            return_value[name] = [p.get_value() for p in layer.get_params()]
        return return_value

    def load_params_from(self, source):
        self.initialize()

        if isinstance(source, basestring):
            with open(source, 'rb') as f:
                source = pickle.load(f)

        if isinstance(source, NeuralNet):
            source = source.get_all_params_values()

        success = "Loaded parameters to layer '{}' (shape {})."
        failure = ("Could not load parameters to layer '{}' because "
                   "shapes did not match: {} vs {}.")

        for key, values in source.items():
            layer = self.layers_.get(key)
            if layer is not None:
                for p1, p2v in zip(layer.get_params(), values):

                    shape1 = p1.get_value().shape
                    shape2 = p2v.shape
                    shape1s = 'x'.join(map(str, shape1))
                    shape2s = 'x'.join(map(str, shape2))
                    if shape1 == shape2:
                        p1.set_value(p2v)
#                        if layer.name == 'conv1':
#                            print '\nvalues', p1.get_value()
                        if self.verbose:
                            print(success.format(
                                key, shape1s, shape2s))
                    else:
                        if self.verbose:
                            print(failure.format(
                                key, shape1s, shape2s))

    def save_params_to(self, fname):
        params = self.get_all_params_values()
        with open(fname, 'wb') as f:
            pickle.dump(params, f, -1)

    def load_weights_from(self, source):
        warn("The 'load_weights_from' method will be removed in nolearn 0.6. "
             "Please use 'load_params_from' instead.")

        if isinstance(source, list):
            raise ValueError(
                "Loading weights from a list of parameter values is no "
                "longer supported.  Please send me something like the "
                "return value of 'net.get_all_params_values()' instead.")

        return self.load_params_from(source)

    def save_weights_to(self, fname):
        warn("The 'save_weights_to' method will be removed in nolearn 0.6. "
             "Please use 'save_params_to' instead.")
        return self.save_params_to(fname)

    def load_account_weights( self, k, BEST_LOSS=False ):
        '''
        Function to load account specific weights
        '''
        if BEST_LOSS:
            fpath = self.HOME+'trainedParams/'+self.fp_accW+'_'+str(k)+'_loss_prms.pkl'
            if not os.path.isfile( fpath ):
                fpath = self.HOME+'trainedParams/'+self.fp_accW+'_'+str(k)+'.pkl'
        else:
            fpath = self.HOME+'trainedParams/'+self.fp_accW+'_'+str(k)+'.pkl'
        
        for name in self.account_weight_layers:
            try:
                with open( fpath, 'r' ) as f:
                    paramDic = pickle.load( f )
                    Wval = paramDic[ str(k)+name ].astype( np.float32 )
                    if self.layers_[ name ].b is not None:
                        bval = paramDic[ str(k)+name+'_b' ].astype( np.float32 )
                        if len( np.shape( bval ) ) == 0:
                            bval = np.reshape( bval, (1,) )
            except:
                print 'init weights: ', k
                uniformInit = Uniform()
                Wval = uniformInit.sample( np.shape( self.layers_[ name ].W.get_value() ) )
                if self.layers_[ name ].b is not None:
                    bval = uniformInit.sample( np.shape( self.layers_[ name ].b.get_value() ) )
        
            self.layers_[ name ].W.set_value( Wval )
            if self.layers_[ name ].b is not None:
                self.layers_[ name ].b.set_value( bval )

    def save_account_weights( self, k, BEST_LOSS=False ):
        '''
        Function that stores account specific weights
        '''
        fpath = self.HOME+'trainedParams/'+self.fp_accW+'_'+str(k)+'.pkl'
        if BEST_LOSS:
            fpath = self.HOME+'trainedParams/'+self.fp_accW+'_'+str(k)+'_loss_prms.pkl'

        if os.path.isfile( fpath ):
            with open( fpath, 'r' ) as f:
                paramDic = pickle.load( f )
                
        else:
            paramDic = {}
           
        for name in self.account_weight_layers:
            W = self.layers_[ name ].W.get_value()
            paramDic[ str(k)+name ] = W

            if self.layers_[ name ].b is not None:
                b = self.layers_[ name ].b.get_value()
                paramDic[ str(k)+name+'_b' ] = b

        with open( fpath, 'w' ) as f:
            pickle.dump( paramDic, f )

    def __getstate__(self):
        state = dict(self.__dict__)
        for attr in (
            'train_iter_',
            'eval_iter_',
            'predict_iter_',
            '_initialized',
            ):
            if attr in state:
                del state[attr]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.initialize()

    def get_params(self, deep=True):
        params = super(NeuralNet, self).get_params(deep=deep)

        # Incidentally, Lasagne layers have a 'get_params' too, which
        # for sklearn's 'clone' means it would treat it in a special
        # way when cloning.  Wrapping the list of layers in a custom
        # list type does the trick here, but of course it's crazy:
        params['layers'] = _list(params['layers'])
        return _dict(params)

    def _get_param_names(self):
        # This allows us to have **kwargs in __init__ (woot!):
        param_names = super(NeuralNet, self)._get_param_names()
        return param_names + self._kwarg_keys
