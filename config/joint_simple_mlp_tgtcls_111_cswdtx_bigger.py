import cPickle

import model.joint_simple_mlp_tgtcls as model

from blocks.initialization import IsotropicGaussian, Constant

import data

n_begin_end_pts = 10     # how many points we consider at the beginning and end of the known trajectory
n_end_pts = 10

n_valid = 1000

with open("%s/arrival-clusters.pkl" % data.path) as f:
    dest_tgtcls = cPickle.load(f)

# generate target classes for time prediction as a Fibonacci sequence
time_tgtcls = [1, 2]
for i in range(21):
    time_tgtcls.append(time_tgtcls[-1] + time_tgtcls[-2])

dim_embeddings = [
    ('origin_call', data.origin_call_size+1, 15),
    ('origin_stand', data.stands_size+1, 10),
    ('week_of_year', 52, 10),
    ('day_of_week', 7, 10),
    ('qhour_of_day', 24 * 4, 10),
    ('day_type', 3, 10),
    ('taxi_id', 448, 10),
]

# Common network part
dim_input = n_begin_end_pts * 2 * 2 + sum(x for (_, _, x) in dim_embeddings)
dim_hidden = [1000]

# Destination prediction part
dim_hidden_dest = [400]
dim_output_dest = dest_tgtcls.shape[0]

# Time prediction part
dim_hidden_time = [400]
dim_output_time = len(time_tgtcls)

# Cost ratio between distance cost and time cost
time_cost_factor = 4

embed_weights_init = IsotropicGaussian(0.01)
mlp_weights_init = IsotropicGaussian(0.1)
mlp_biases_init = Constant(0.01)

learning_rate = 0.000001
momentum = 0.99
batch_size = 200

valid_set = 'cuts/test_times_0'

