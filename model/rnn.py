from theano import tensor

from blocks.bricks import application, MLP, Initializable, Tanh
from blocks.bricks.base import lazy
from blocks.bricks.recurrent import LSTM, recurrent
from blocks.utils import shared_floatx_zeros

from fuel.transformers import Batch, Padding
from fuel.streams import DataStream
from fuel.schemes import ConstantScheme, ShuffledExampleScheme

from model import ContextEmbedder
import data
from data import transformers
from data.hdf5 import TaxiDataset, TaxiStream
import error


class Model(Initializable):
    @lazy()
    def __init__(self, config, **kwargs):
        super(Model, self).__init__(**kwargs)
        self.config = config

        self.pre_context_embedder = ContextEmbedder(config.pre_embedder, name='pre_context_embedder')
        self.post_context_embedder = ContextEmbedder(config.post_embedder, name='post_context_embedder')

        in1 = 2 + sum(x[2] for x in config.pre_embedder.dim_embeddings)
        self.input_to_rec = MLP(activations=[Tanh()], dims=[in1, config.hidden_state_dim], name='input_to_rec')

        self.rec = LSTM(
                dim = config.hidden_state_dim,
                name = 'recurrent'
            )

        in2 = config.hidden_state_dim + sum(x[2] for x in config.post_embedder.dim_embeddings)
        self.rec_to_output = MLP(activations=[Tanh()], dims=[in2, 2], name='rec_to_output')

        self.sequences = ['latitude', 'latitude_mask', 'longitude']
        self.context = self.pre_context_embedder.inputs + self.post_context_embedder.inputs
        self.inputs = self.sequences + self.context
        self.children = [ self.pre_context_embedder, self.post_context_embedder, self.input_to_rec, self.rec, self.rec_to_output ]

        self.initial_state_ = shared_floatx_zeros((config.hidden_state_dim,),
                name="initial_state")
        self.initial_cells = shared_floatx_zeros((config.hidden_state_dim,),
                name="initial_cells")

    def _push_initialization_config(self):
        for mlp in [self.input_to_rec, self.rec_to_output]:
            mlp.weights_init = self.config.weights_init
            mlp.biases_init = self.config.biases_init
        self.rec.weights_init = self.config.weights_init

    def get_dim(self, name):
        return self.rec.get_dim(name)

    @application
    def initial_state(self, *args, **kwargs):
        return self.rec.initial_state(*args, **kwargs)

    @recurrent(states=['states', 'cells'], outputs=['destination', 'states', 'cells'], sequences=['latitude', 'longitude', 'latitude_mask'])
    def predict_all(self, latitude, longitude, latitude_mask, **kwargs):
        latitude = (latitude - data.train_gps_mean[0]) / data.train_gps_std[0]
        longitude = (longitude - data.train_gps_mean[1]) / data.train_gps_std[1]

        pre_emb = tuple(self.pre_context_embedder.apply(**kwargs))
        latitude = tensor.shape_padright(latitude)
        longitude = tensor.shape_padright(longitude)
        itr = self.input_to_rec.apply(tensor.concatenate(pre_emb + (latitude, longitude), axis=1))
        itr = itr.repeat(4, axis=1)
        (next_states, next_cells) = self.rec.apply(itr, kwargs['states'], kwargs['cells'], mask=latitude_mask, iterate=False)

        post_emb = tuple(self.post_context_embedder.apply(**kwargs))
        rto = self.rec_to_output.apply(tensor.concatenate(post_emb + (next_states,), axis=1))

        rto = (rto * data.train_gps_std) + data.train_gps_mean
        return (rto, next_states, next_cells)

    @predict_all.property('contexts')
    def predict_all_inputs(self):
        return self.context

    @application(outputs=['destination'])
    def predict(self, latitude, longitude, latitude_mask, **kwargs):
        latitude = latitude.T
        longitude = longitude.T
        latitude_mask = latitude_mask.T
        res = self.predict_all(latitude, longitude, latitude_mask, **kwargs)[0]
        return res[-1]

    @predict.property('inputs')
    def predict_inputs(self):
        return self.inputs

    @application(outputs=['cost'])
    def cost(self, latitude, longitude, latitude_mask, **kwargs):
        latitude = latitude.T
        longitude = longitude.T
        latitude_mask = latitude_mask.T

        res = self.predict_all(latitude, longitude, latitude_mask, **kwargs)[0]
        target = tensor.concatenate(
                    (kwargs['destination_latitude'].dimshuffle('x', 0, 'x'),
                     kwargs['destination_longitude'].dimshuffle('x', 0, 'x')),
                axis=2)
        target = target.repeat(latitude.shape[0], axis=0)
        ce = error.erdist(target.reshape((-1, 2)), res.reshape((-1, 2)))
        ce *= latitude_mask.flatten()
        return ce.sum() / latitude_mask.sum()

    @cost.property('inputs')
    def cost_inputs(self):
        return self.inputs + ['destination_latitude', 'destination_longitude']


class Stream(object):
    def __init__(self, config):
        self.config = config

    def train(self, req_vars):
        valid = TaxiDataset(self.config.valid_set, 'valid.hdf5', sources=('trip_id',))
        valid_trips_ids = valid.get_data(None, slice(0, valid.num_examples))[0]

        stream = TaxiDataset('train')
        stream = DataStream(stream, iteration_scheme=ShuffledExampleScheme(stream.num_examples))
        stream = transformers.TaxiExcludeTrips(stream, valid_trips_ids)
        stream = transformers.TaxiExcludeEmptyTrips(stream)
        stream = transformers.taxi_add_datetime(stream)
        stream = transformers.add_destination(stream)
        stream = transformers.Select(stream, tuple(v for v in req_vars if not v.endswith('_mask')))

        stream = transformers.balanced_batch(stream, key='latitude', batch_size=self.config.batch_size, batch_sort_size=self.config.batch_sort_size)
        stream = Padding(stream, mask_sources=['latitude', 'longitude'])
        stream = transformers.Select(stream, req_vars)
        return stream

    def valid(self, req_vars):
        stream = TaxiStream(self.config.valid_set, 'valid.hdf5')
        stream = transformers.taxi_add_datetime(stream)
        stream = transformers.add_destination(stream)
        stream = transformers.Select(stream, tuple(v for v in req_vars if not v.endswith('_mask')))

        stream = Batch(stream, iteration_scheme=ConstantScheme(1000))
        stream = Padding(stream, mask_sources=['latitude', 'longitude'])
        stream = transformers.Select(stream, req_vars)
        return stream

    def test(self, req_vars):
        stream = TaxiStream('test')
        stream = transformers.taxi_add_datetime(stream)
        stream = transformers.taxi_remove_test_only_clients(stream)
        stream = transformers.Select(stream, tuple(v for v in req_vars if not v.endswith('_mask')))

        stream = Batch(stream, iteration_scheme=ConstantScheme(1))
        stream = Padding(stream, mask_sources=['latitude', 'longitude'])
        stream = transformers.Select(stream, req_vars)
        return stream

    def inputs(self):
        return {'call_type': tensor.bvector('call_type'),
                'origin_call': tensor.ivector('origin_call'),
                'origin_stand': tensor.bvector('origin_stand'),
                'taxi_id': tensor.wvector('taxi_id'),
                'timestamp': tensor.ivector('timestamp'),
                'day_type': tensor.bvector('day_type'),
                'missing_data': tensor.bvector('missing_data'),
                'latitude': tensor.matrix('latitude'),
                'longitude': tensor.matrix('longitude'),
                'latitude_mask': tensor.matrix('latitude_mask'),
                'longitude_mask': tensor.matrix('longitude_mask'),
                'week_of_year': tensor.bvector('week_of_year'),
                'day_of_week': tensor.bvector('day_of_week'),
                'qhour_of_day': tensor.bvector('qhour_of_day'),
                'destination_latitude': tensor.vector('destination_latitude'),
                'destination_longitude': tensor.vector('destination_longitude')}