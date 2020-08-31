#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: common.py
# Author: Yuxin Wu <ppwwyyxxc@gmail.com>
# Modified: Amir Alansary <amiralansary@gmail.com>

import random
import time
import threading
import numpy as np
from tqdm import tqdm
import multiprocessing
from six.moves import queue

# from tensorpack import *
# from tensorpack.utils.stats import *
from tensorpack.utils import logger
# from tensorpack.callbacks import Triggerable
from tensorpack.callbacks.base import Callback
from tensorpack.utils.stats import StatCounter
from tensorpack.utils.utils import get_tqdm_kwargs
from tensorpack.utils.concurrency import (StoppableThread, ShareSessionThread)

import traceback


###############################################################################

def play_one_episode(env, func, render=False):
    def predict(s):
        """
        Run a full episode, mapping observation to action, WITHOUT 0.001 greedy.
    :returns sum of rewards
        """
        # pick action with best predicted Q-value
        q_values = func(s[None, :, :, :])[0][0]
        act = q_values.argmax()

        # eps greedy disabled
        # if random.random() < 0.001:
        #     spc = env.action_space
        #     act = spc.sample()
        return act, q_values

    ob = env.reset()
    sum_r = 0
    while True:
        act, q_values = predict(ob)
        ob, r, isOver, info = env.step(act, q_values)
        if render:
            env.render()
        sum_r += r
        if isOver:
            return sum_r, info['filename'], info['distError'], info['loc'],info['target_loc'],info['spacing'],info['start_pos']

###############################################################################

def play_n_episodes(player, predfunc, nr, render=False):
    """wraps play_one_episode, playing a single episode at a time and logs results
    used when playing demos."""
    logger.info("Start Playing ... ")
    info_episodes = []
    info_episodes.append(['idx_num',  'filename', 'score', 'distance_error',
                          'final_coordinates_x','final_coordinates_y','final_coordinates_z',
                          'target_x','target_y','target_z',
                          'spacing_x','spacing_y','spacing_z'])
    for k in range(nr):
        # if k != 0:
        #     player.restart_episode()
        score, filename, ditance_error, loc, target_loc, spacing,start_pos = play_one_episode(player,
                                                                    predfunc,
                                                                    render=render)
        logger.info(
            " Starting Position:{}\n{:04d}/{:04d} - {:>15} - score {:>5.2f} - distError {:>5.2f} - final_loc {} - target_loc{} - spacing {}".format(start_pos,k + 1, nr, filename, score, ditance_error,
                                                                        loc,target_loc,spacing))
        info_episodes.append([k + 1, filename, score, ditance_error,
                              loc[0],loc[1],loc[2],
                              target_loc[0],target_loc[1],target_loc[2],
                              spacing[0],spacing[1],spacing[2]])
    return info_episodes


###############################################################################

def eval_with_funcs(predictors, nr_eval, get_player_fn, files_list=None):
    """
    Args:
        predictors ([PredictorBase])

    Runs episodes in parallel, returning statistics about the model performance.
    """

    class Worker(StoppableThread, ShareSessionThread):
        def __init__(self, func, queue, distErrorQueue):
            super(Worker, self).__init__()
            self._func = func
            self.q = queue
            self.q_dist = distErrorQueue

        def func(self, *args, **kwargs):
            if self.stopped():
                raise RuntimeError("stopped!")
            return self._func(*args, **kwargs)

        def run(self):
            with self.default_sess():
                player = get_player_fn(task=False,
                                       files_list=files_list)
                while not self.stopped():
                    try:
                        score, filename, ditance_error, q_values = play_one_episode(player, self.func)
                        # print("Score, ", score)
                    except RuntimeError:
                        return
                    self.queue_put_stoppable(self.q, score)
                    self.queue_put_stoppable(self.q_dist, ditance_error)

    q = queue.Queue()
    q_dist = queue.Queue()

    threads = [Worker(f, q, q_dist) for f in predictors]

    # start all workers
    for k in threads:
        k.start()
        time.sleep(0.1)  # avoid simulator bugs
    stat = StatCounter()
    dist_stat = StatCounter()

    # show progress bar w/ tqdm
    for _ in tqdm(range(nr_eval), **get_tqdm_kwargs()):
        r = q.get()
        stat.feed(r)
        dist = q_dist.get()
        dist_stat.feed(dist)

    logger.info("Waiting for all the workers to finish the last run...")
    for k in threads:
        k.stop()
    for k in threads:
        k.join()
    while q.qsize():
        r = q.get()
        stat.feed(r)

    while q_dist.qsize():
        dist = q_dist.get()
        dist_stat.feed(dist)

    if stat.count > 0:
        return (stat.average, stat.max, dist_stat.average, dist_stat.max)
    return (0, 0, 0, 0)


###############################################################################

def eval_model_multithread(pred, nr_eval, get_player_fn, files_list):
    """
    Args:
        pred (OfflinePredictor): state -> Qvalue

    Evaluate pretrained models, or checkpoints of models during training
    """
    NR_PROC = min(multiprocessing.cpu_count() // 2, 8)
    with pred.sess.as_default():
        mean_score, max_score, mean_dist, max_dist = eval_with_funcs(
            [pred] * NR_PROC, nr_eval, get_player_fn, files_list)
    logger.info("Average Score: {}; Max Score: {}; Average Distance: {}; Max Distance: {}".format(mean_score, max_score, mean_dist, max_dist))

###############################################################################

class Evaluator(Callback):

    def __init__(self, nr_eval, input_names, output_names,
                 get_player_fn, files_list=None):
        self.files_list = files_list
        self.eval_episode = nr_eval
        self.input_names = input_names
        self.output_names = output_names
        self.get_player_fn = get_player_fn

    def _setup_graph(self):
        NR_PROC = min(multiprocessing.cpu_count() // 2, 20)
        self.pred_funcs = [self.trainer.get_predictor(
            self.input_names, self.output_names)] * NR_PROC

    def _trigger(self):
        """triggered by Trainer"""
        t = time.time()
        mean_score, max_score, mean_dist, max_dist = eval_with_funcs(
            self.pred_funcs, self.eval_episode, self.get_player_fn, self.files_list)
        t = time.time() - t
        if t > 10 * 60:  # eval takes too long
            self.eval_episode = int(self.eval_episode * 0.94)

        # log scores
        self.trainer.monitors.put_scalar('mean_score', mean_score)
        self.trainer.monitors.put_scalar('max_score', max_score)
        self.trainer.monitors.put_scalar('mean_distance', mean_dist)
        self.trainer.monitors.put_scalar('max_distance', max_dist)

###############################################################################
