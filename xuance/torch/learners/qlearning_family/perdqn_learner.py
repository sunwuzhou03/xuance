"""
DQN with Prioritized Experience Replay (PER-DQN)
Paper link: https://arxiv.org/pdf/1511.05952.pdf
Implementation: Pytorch
"""
import os
import torch
import numpy as np
from torch import nn
from xuance.common import Optional
from xuance.torch.learners import Learner
from argparse import Namespace


class PerDQN_Learner(Learner):
    def __init__(self,
                 config: Namespace,
                 policy: nn.Module):
        super(PerDQN_Learner, self).__init__(config, policy)
        self.optimizer = torch.optim.Adam(self.policy.parameters(), self.config.learning_rate, eps=1e-5)
        self.scheduler = torch.optim.lr_scheduler.LinearLR(self.optimizer, start_factor=1.0, end_factor=0.0,
                                                           total_iters=self.config.running_steps)
        self.gamma = config.gamma
        self.sync_frequency = config.sync_frequency
        self.mse_loss = nn.MSELoss()
        self.one_hot = nn.functional.one_hot
        self.n_actions = self.policy.action_dim

    def build_training_data(self, samples: Optional[dict]):
        batch_size = samples['batch_size']
        samples_Tensor = {}
        if self.world_size > 1:  # i.e., Multi-GPU settings.
            rank = int(os.environ['RANK'])
            batch_size_local = batch_size // self.world_size
            if rank < self.world_size - 1:
                indices = range(rank * batch_size_local, (rank + 1) * batch_size_local)
            else:
                indices = range(rank * batch_size_local, batch_size)
            for k, v in samples.items():
                if k in ['batch_size', 'weights', 'step_choices']:
                    continue
                samples_Tensor[k] = torch.as_tensor(v[indices], device=self.device)
        else:
            for k, v in samples.items():
                if k in ['batch_size', 'weights', 'step_choices']:
                    continue
                samples_Tensor[k] = torch.as_tensor(v, device=self.device)

        return samples_Tensor

    def update(self, **samples):
        self.iterations += 1
        sample_Tensor = self.build_training_data(samples=samples)
        obs_batch = sample_Tensor['obs']
        act_batch = sample_Tensor['actions']
        next_batch = sample_Tensor['obs_next']
        rew_batch = sample_Tensor['rewards']
        ter_batch = sample_Tensor['terminals']

        _, _, evalQ = self.policy(obs_batch)
        _, _, targetQ = self.policy.target(next_batch)
        targetQ = targetQ.max(dim=-1).values
        targetQ = rew_batch + self.gamma * (1 - ter_batch) * targetQ
        predictQ = (evalQ * self.one_hot(act_batch.long(), evalQ.shape[1])).sum(dim=-1)

        td_error = targetQ - predictQ
        loss = self.mse_loss(predictQ, targetQ)
        self.optimizer.zero_grad()
        loss.backward()
        if self.use_grad_clip:
            torch.nn.utils.clip_grad_norm_(self.policy.parameters(), self.grad_clip_norm)
        self.optimizer.step()
        if self.scheduler is not None:
            self.scheduler.step()

        # hard update for target network
        if self.iterations % self.sync_frequency == 0:
            self.policy.copy_target()
        lr = self.optimizer.state_dict()['param_groups'][0]['lr']

        if self.distributed_training:
            info = {
                f"Qloss/rank_{self.rank}": loss.item(),
                f"learning_rate/rank_{self.rank}": lr,
                f"predictQ/rank_{self.rank}": predictQ.mean().item()
            }
        else:
            info = {
                "Qloss": loss.item(),
                "learning_rate": lr,
                "predictQ": predictQ.mean().item()
            }
        
        return np.abs(td_error.cpu().detach().numpy()), info
