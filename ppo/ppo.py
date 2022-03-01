from ast import arg
import wandb

import numpy as np
import torch
from tqdm.auto import tqdm
from torch.optim import Adam
import gym
import gym_os2r
from gym_os2r import randomizers
import time
import core
import functools
import os
# from spinup.utils.logx import EpochLogger
# from spinup.utils.mpi_pytorch import setup_pytorch_for_mpi, sync_params, mpi_avg_grads
# from spinup.utils.mpi_tools import mpi_fork, mpi_avg, proc_id, mpi_statistics_scalar, num_procs


def make_env(env_id):

    def make_env_from_id(env_id: str, **kwargs) -> gym.Env:
        return gym.make(env_id, **kwargs)

    # Create a partial function passing the environment id
    create_env = functools.partial(make_env_from_id, env_id=env_id)
    env = randomizers.monopod_no_rand.MonopodEnvNoRandomizer(env=create_env)

    # Enable the rendering
    # env.render('human')

    # Initialize the seed
    print(env)
    return env

class PPOBuffer:

    def __init__(self, obs_dim, act_dim, size, gamma=0.99, lam=0.95):
        self.obs_buf = np.zeros(core.combined_shape(size, obs_dim), dtype=np.float32)
        self.act_buf = np.zeros(core.combined_shape(size, act_dim), dtype=np.float32)
        self.adv_buf = np.zeros(size, dtype=np.float32)
        self.rew_buf = np.zeros(size, dtype=np.float32)
        self.ret_buf = np.zeros(size, dtype=np.float32)
        self.val_buf = np.zeros(size, dtype=np.float32)
        self.logp_buf = np.zeros(size, dtype=np.float32)
        self.gamma, self.lam = gamma, lam
        self.ptr, self.path_start_idx, self.max_size = 0, 0, size

    def store(self, obs, act, rew, val, logp):

        assert self.ptr < self.max_size     # buffer has to have room so you can store
        self.obs_buf[self.ptr] = obs
        self.act_buf[self.ptr] = act
        self.rew_buf[self.ptr] = rew
        self.val_buf[self.ptr] = val
        self.logp_buf[self.ptr] = logp
        self.ptr += 1

    def finish_path(self, last_val=0):
        path_slice = slice(self.path_start_idx, self.ptr)
        rews = np.append(self.rew_buf[path_slice], last_val)
        vals = np.append(self.val_buf[path_slice], last_val)
        
        # the next two lines implement GAE-Lambda advantage calculation
        deltas = rews[:-1] + self.gamma * vals[1:] - vals[:-1]
        self.adv_buf[path_slice] = core.discount_cumsum(deltas, self.gamma * self.lam)
        
        # the next line computes rewards-to-go, to be targets for the value function
        self.ret_buf[path_slice] = core.discount_cumsum(rews, self.gamma)[:-1]
        
        self.path_start_idx = self.ptr

    def get(self):
        """
        Call this at the end of an epoch to get all of the data from
        the buffer, with advantages appropriately normalized (shifted to have
        mean zero and std one). Also, resets some pointers in the buffer.
        """
        assert self.ptr == self.max_size    # buffer has to be full before you can get
        self.ptr, self.path_start_idx = 0, 0
        # the next two lines implement the advantage normalization trick
        # adv_mean, adv_std = mpi_statistics_scalar(self.adv_buf)
        # self.adv_buf = (self.adv_buf - adv_mean) / adv_std
        data = dict(obs=self.obs_buf, act=self.act_buf, ret=self.ret_buf,
                    adv=self.adv_buf, logp=self.logp_buf)
        return {k: torch.as_tensor(v, dtype=torch.float32) for k,v in data.items()}

def evaluate(o,ac,env,args,curr_step):
    # Main loop: collect experience in env and update/log each epoch
    progress_bar = tqdm(range(args.eval_episodes),desc='Evaluation Epochs')
    ep_ret = 0
    ep_len = 0
    total_ep_return = 0
    for episode in range(args.eval_episodes):
        for t in range(args.max_ep_len):

            with torch.no_grad():
                a, v, logp = ac.step(torch.as_tensor(o, dtype=torch.float32),eval=True)

            next_o, r, d, _ = env.step(a)
            ep_ret += r
            ep_len += 1
                
                # Update obs (critical!)
            o = next_o

            timeout = ep_len == args.max_ep_len
            terminal = d or timeout

            if terminal:
                if not(timeout):
                    print('Warning: trajectory cut off by epoch at %d steps.'%ep_len, flush=True)
                # if trajectory didn't reach terminal state, bootstrap value target
                if timeout:
                    _, v, _ = ac.step(torch.as_tensor(o, dtype=torch.float32))
                else:
                    v = 0
                total_ep_return += ep_ret
                o, ep_ret, ep_len = env.reset(), 0, 0
                break
        progress_bar.update(1)
    total_ep_return = total_ep_return/args.eval_episodes
    wandb.log({"evaluation episode reward":total_ep_return})
    return o,ep_ret,ep_len

def ppo(env_fn, args ,actor_critic=core.MLPActorCritic, ac_kwargs=dict()):

    if not os.path.isdir(args.save_dir):
            os.mkdir(args.save_dir)

    # Random seed
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    # Instantiate environment
    env = env_fn()
    obs_dim = env.observation_space.shape
    act_dim = env.action_space.shape

    # Create actor-critic module
    ac = actor_critic(env.observation_space, env.action_space, **ac_kwargs)

    # Set up experience buffer
    local_steps_per_epoch = int(args.steps_per_epoch)
    buf = PPOBuffer(obs_dim, act_dim, local_steps_per_epoch, args.gamma, args.lam)

    # Set up function for computing PPO policy loss
    def compute_loss_pi(data):
        obs, act, adv, logp_old = data['obs'], data['act'], data['adv'], data['logp']

        # Policy loss
        pi, logp = ac.pi(obs, act)
        ratio = torch.exp(logp - logp_old)
        clip_adv = torch.clamp(ratio, 1-args.clip_ratio, 1+args.clip_ratio) * adv
        loss_pi = -(torch.min(ratio * adv, clip_adv)).mean()

        # Useful extra info
        approx_kl = (logp_old - logp).mean().item()
        ent = pi.entropy().mean().item()
        clipped = ratio.gt(1+args.clip_ratio) | ratio.lt(1-args.clip_ratio)
        clipfrac = torch.as_tensor(clipped, dtype=torch.float32).mean().item()
        pi_info = dict(kl=approx_kl, ent=ent, cf=clipfrac)

        return loss_pi, pi_info

    # Set up function for computing value loss
    def compute_loss_v(data):
        obs, ret = data['obs'], data['ret']
        return ((ac.v(obs) - ret)**2).mean()

    # Set up optimizers for policy and value function
    pi_optimizer = Adam(ac.pi.parameters(), lr=args.pi_lr)
    vf_optimizer = Adam(ac.v.parameters(), lr=args.vf_lr)


    def update():
        data = buf.get()

        pi_l_old, pi_info_old = compute_loss_pi(data)
        pi_l_old = pi_l_old.item()
        v_l_old = compute_loss_v(data).item()

        # Train policy with multiple steps of gradient descent
        for i in range(args.train_pi_iters):
            pi_optimizer.zero_grad()
            loss_pi, pi_info = compute_loss_pi(data)
            loss_pi.backward()
            pi_optimizer.step()

        # logger.store(StopIter=i)

        # Value function learning
        for i in range(args.train_v_iters):
            vf_optimizer.zero_grad()
            loss_v = compute_loss_v(data)
            loss_v.backward()
            vf_optimizer.step()

    # Prepare for interaction with environment
    o, ep_ret, ep_len = env.reset(), 0, 0
    eval_steps = 1
    train_episodes = 1

    # Main loop: collect experience in env and update/log each epoch
    progress_bar = tqdm(range(args.epochs),desc='Training Epoch')
    for epoch in range(args.epochs):
        for t in range(local_steps_per_epoch):
            a, v, logp = ac.step(torch.as_tensor(o, dtype=torch.float32))

            next_o, r, d, _ = env.step(a)
            ep_ret += r
            ep_len += 1

            # save and log
            buf.store(o, a, r, v, logp)
            
            # Update obs (critical!)
            o = next_o

            timeout = ep_len == args.max_ep_len
            terminal = d or timeout
            epoch_ended = t==local_steps_per_epoch-1

            if terminal or epoch_ended:
                if epoch_ended and not(terminal):
                    print('Warning: trajectory cut off by epoch at %d steps.'%ep_len, flush=True)
                # if trajectory didn't reach terminal state, bootstrap value target
                if timeout or epoch_ended:
                    _, v, _ = ac.step(torch.as_tensor(o, dtype=torch.float32))
                else:
                    v = 0
                buf.finish_path(v)
                wandb.log({"training episode reward":ep_ret},step=train_episodes)
                train_episodes+=1
                o, ep_ret, ep_len = env.reset(), 0, 0
        progress_bar.update(1)
        # Save model
        if epoch %args.save_freq == 0:
            PATH = args.save_dir + "best_model_" + str(epoch) + ".pt"
            torch.save({
                'actor_state_dict': ac.state_dict(),
            }, PATH)

        # Perform PPO update!
        update()

        o,ep_ret,ep_len = evaluate(o,ac,env,args,eval_steps)
        eval_steps+=1

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=str, default='Monopod-balance-v3')
    parser.add_argument('--hid', type=int, default=64)
    parser.add_argument('--l', type=int, default=2)
    parser.add_argument('--gamma', type=float, default=0.99)
    parser.add_argument('--lam', type=float, default=0.97)
    parser.add_argument('--clip_ratio', type=float, default=0.2)
    parser.add_argument('--target_kl', type=float, default=0.01)
    parser.add_argument('--pi_lr', type=float, default=3e-4)
    parser.add_argument('--vf_lr',type=float, default=1e-3)
    parser.add_argument('--train_pi_iters', type=int, default=80)
    parser.add_argument('--train_v_iters', type=int, default=80)
    parser.add_argument('--seed', '-s', type=int, default=42)
    parser.add_argument('--steps_per_epoch', type=int, default=10000)
    parser.add_argument('--max_ep_len', type=int, default=8000)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--eval_episodes', type=int, default=1)
    parser.add_argument('--save_freq', type=int, default=10)
    parser.add_argument('--exp_name', type=str, default='ppo')
    parser.add_argument('--save_dir', type=str, default='exp/')
    
    args = parser.parse_args()
    wandb.init(project='capstone',entity='nickioan')

    ppo(lambda : make_env(args.env), args, actor_critic=core.MLPActorCritic,
        ac_kwargs=dict(hidden_sizes=[args.hid]*args.l),)