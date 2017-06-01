import numpy as np
import random
from collections import deque

import gym
from gym import spaces

from contextlib import closing
import ngraph as ng
from ngraph.frontends import neon


class Namespace(object):
    pass


def make_axes(lengths, name=None):
    """
    returns an axes of axis objects with length specified by the array `lengths`

    note: this function may be removable if the ngraph version of make_axes is changed
    """
    if isinstance(lengths, ng.Axes):
        return lengths

    if isinstance(lengths, (int, long)):
        lengths = [lengths]

    def make_name(i):
        if name:
            return '{name}_{i}'.format(name=name, i=i)

    return ng.make_axes([
        ng.make_axis(length=length, name=make_name(i))
        for i, length in enumerate(lengths)
    ])


class ModelWrapper(object):
    """the ModelWrapper is responsible for interacting with neon and ngraph"""

    def __init__(
            self, state_axes, action_size, batch_size, model,
            learning_rate=0.0001
    ):
        """
        for now, model must be a function which takes action_axes, and
        returns a neon container
        """
        super(ModelWrapper, self).__init__()

        self.axes = Namespace()
        # todo: standardize axis pattern
        # todo: how to specify which of the axes are which?
        self.axes.state = make_axes(state_axes, name='state')
        self.axes.action = ng.make_axis(name='action', length=action_size)
        self.axes.n = ng.make_axis(name='N', length=batch_size)
        self.axes.n1 = ng.make_axis(name='N', length=1)

        # placeholders
        self.state = ng.placeholder(self.axes.state + [self.axes.n])
        self.state_single = ng.placeholder(self.axes.state + [self.axes.n1])
        self.target = ng.placeholder([self.axes.action, self.axes.n])

        # todo: accept model as input parameter to constructor
        self.model = model(self.axes.action)

        # construct inference computation
        with neon.Layer.inference_mode_on():
            inference = self.model(self.state)

        inference_computation = ng.computation(inference, self.state)

        # construct inference computation for evaluating a single observation
        with neon.Layer.inference_mode_on():
            inference_single = self.model(self.state_single)

        inference_computation_single = ng.computation(
            inference_single, self.state_single
        )

        # construct training computation
        loss = ng.squared_L2(self.model(self.state) - self.target)

        optimizer = neon.RMSProp(
            learning_rate=learning_rate,
            gradient_clip_value=1,
        )

        train_output = ng.sequential([
            optimizer(loss),
            loss,
        ])

        train_computation = ng.computation(
            train_output, self.state, self.target
        )

        # now bind computations we are interested in
        self.transformer = ng.transformers.make_transformer()
        self.inference_function = self.transformer.add_computation(
            inference_computation
        )
        self.inference_function_single = self.transformer.add_computation(
            inference_computation_single
        )
        self.train_function = self.transformer.add_computation(
            train_computation
        )

    def predict_single(self, state):
        """run inference on the model for a single input state"""
        # return self.inference_function_single(state[..., np.newaxis])[..., 0]
        state = np.stack([state] * self.axes.n.length, axis=-1)
        return self.inference_function(state)[..., 0]

    def predict(self, state):
        if state.shape != self.state.axes.lengths:
            raise ValueError((
                'predict received state with wrong shape. found {}, expected {} '
            ).format(state.shape, self.state.axes.lengths))
        return self.inference_function(state)

    def train(self, states, targets):
        # todo: check shape
        self.train_function(states, targets)


def space_shape(space):
    """return the shape of tensor expected for a given space"""
    if isinstance(space, spaces.Discrete):
        return [space.n]
    else:
        return space.shape


def linear_generator(start, end, steps):
    """
    linearly interpolate between start and end values.
    after `steps` have been taken, always returns end.
    """
    delta = end - start
    steps_taken = 0

    while True:
        if steps_taken < steps:
            yield start + delta * (steps_taken / float(steps - 1))
        else:
            yield end

        steps_taken += 1


def decay_generator(start, decay, minimum):
    """
    start by yielding `start` or `minimum` whichever is larger.  the second value
    will be `start * decay` or `minimum` whichever is larger, etc.
    """
    value = start
    if value < minimum:
        value = minimum

    while True:
        yield value

        value *= decay
        if value < minimum:
            value = minimum


class Agent(object):
    """the Agent is responsible for interacting with the environment."""

    def __init__(
            self,
            state_axes,
            action_space,
            model,
            epsilon,
            gamma=0.99,
            batch_size=32,
            memory=None,
            learning_rate=0.0001
    ):
        super(Agent, self).__init__()

        self.update_after_episode = False
        self.epsilon = epsilon
        self.gamma = gamma
        self.batch_size = batch_size
        self.action_space = action_space

        if memory == None:
            self.memory = Memory(maxlen=1000000)
        else:
            self.memory = memory

        self.model_wrapper = ModelWrapper(
            state_axes=state_axes,
            action_size=action_space.n,
            batch_size=self.batch_size,
            model=model,
            learning_rate=learning_rate,
        )

    def act(self, state, training=True):
        """
        given a state, return the index of the action that should be taken

        if training is true, occasionally return a randomly sampled action
        from the action space instead
        """
        if training:
            if np.random.rand() <= self.epsilon.next():
                return self.action_space.sample()

        return np.argmax(self.model_wrapper.predict_single(state))

    def observe_results(self, state, action, reward, next_state, done):
        """
        this method should be called after an action has been taken to inform
        the agent about the results of the action it took
        """
        self.memory.append({
            'state': state,
            'action': action,
            'reward': reward,
            'next_state': next_state,
            'done': done
        })

        if not self.update_after_episode:
            self._update()

    def end_of_episode(self):
        if self.update_after_episode:
            self._update

    def _update(self):
        # only attempt to sample if our memory has at least one more record
        # then our batch size.  we need one extra because we need to sample
        # state as well as next_state which will overlap for all but one record
        if len(self.memory) <= self.batch_size + 1:
            return

        states = []
        targets = []
        samples = self.memory.sample(self.batch_size)

        # print([sample['state'].shape for sample in samples])

        # batch axis is the last axis
        states = np.stack([sample['state'] for sample in samples], axis=-1)
        next_states = np.stack([sample['next_state'] for sample in samples],
                               axis=-1)

        targets = self.model_wrapper.predict(states)
        next_values = self.model_wrapper.predict(next_states)

        for i, sample in enumerate(samples):
            target = sample['reward']

            if not sample['done']:
                # next_values[action=:, sample=i]
                target += self.gamma * np.amax(next_values[:, i])

            targets[sample['action'], i] = target

        # print('states', states)
        # print('targets', targets)
        self.model_wrapper.train(states, targets)


class Memory(deque):
    """
    Memory is used to keep track of what is happened in the past so that
    we can sample from it and learn.

    Arguments:
        maxlen (integer): the maximum number of memories to record.
    """

    def __init__(self, **kwargs):
        super(Memory, self).__init__(**kwargs)

    def sample(self, batch_size):
        return random.sample(self, batch_size)


class RepeatMemory(object):
    """
    RepeatMemory is used to efficiently remember the history in an environment
    where a RepeatWrapper is wrapping the environment and storing all of
    the observations would be wasteful since a large portion of the observation
    has already been stored in memory.

    Arguments:
        frames_per_observation (integer): the number of frames per observation
            that are repeated on every observation.
        maxlen (integer): the maximum number of memories to record.

    Warning: this memory can only be written to from a single episode at a time

    Note: repeated frames are expected to be in axis 0
    """

    def __init__(
            self,
            frames_per_observation,
            maxlen,
            observation_shape,
            dtype=np.float32
    ):
        self.frames_per_observation = frames_per_observation
        self.maxlen = maxlen

        self.records = [None] * maxlen
        self.observations = np.zeros((maxlen, ) + observation_shape,
                                     dtype=dtype)
        self.count = 0
        self.write_position = 0

    def __len__(self):
        return self.count

    def _check_record(self, record):
        # assume for now that the batch axis is at the end
        if not np.allclose(
                record['state'][1:, ...], record['next_state'][:-1, ...]
        ):
            raise ValueError((
                'expected state and next_state to differ by first frame and'
                'last frame respectively.  found: state: {}\nnext_state: {}'
            ).format(
                record['state'][1:, ...],
                record['next_state'][:-1, ...],
            ))

        assert record['state'].shape[0] == self.frames_per_observation
        assert record['next_state'].shape[0] == self.frames_per_observation

    def append(self, record):
        self._check_record(record)

        observation = record['next_state'][-1, ...]
        del record['state']
        del record['next_state']

        self.records[self.write_position] = record
        self.observations[self.write_position, :] = observation

        # increment counter and write_position
        if self.count < self.maxlen:
            self.count += 1

        self.write_position += 1
        if self.write_position == self.maxlen:
            self.write_position = 0

    def _sample_single(self):
        while True:
            i = random.randint(0, len(self) - self.frames_per_observation - 1)
            i_end = i + self.frames_per_observation + 1

            # don't sample from positions which have been partially overwritten
            if i < self.write_position and i_end > self.write_position:
                continue

            # check to see if this is a valid sample. it is invalid if there is
            # a terminal frame in the middle of the observation
            records = [self.records[j] for j in range(i, i_end)]
            if not any(record['done'] for record in records[:-1]):
                # we can stop looking if this is a valid set of records
                break

        # build observation
        state = self.observations[i:i_end - 1, ...]
        next_state = self.observations[i + 1:i_end, ...]

        record = records[-1].copy()
        record['state'] = state
        record['next_state'] = next_state

        return record

    def sample(self, batch_size):
        if len(self) < self.frames_per_observation:
            raise ValueError((
                'the number of record in memory ({}) must at least be same as the '
                'number of frames per observation ({}).'
            ).format(
                len(self),
                self.frames_per_observation,
            ))

        return [self._sample_single() for _ in range(batch_size)]
