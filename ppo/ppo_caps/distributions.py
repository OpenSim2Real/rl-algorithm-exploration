"""Probability distributions."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union

import gym
import torch as th
from gym import spaces
from torch import nn
from torch.distributions import Beta, Normal

def mlp(sizes, activation, output_activation=nn.Identity):
    layers = []
    for j in range(len(sizes)-1):
        act = activation if j < len(sizes)-2 else output_activation
        layers += [nn.Linear(sizes[j], sizes[j+1]), act()]
    return nn.Sequential(*layers)

class Distribution(ABC):
    """Abstract base class for distributions."""

    def __init__(self):
        super(Distribution, self).__init__()

    @abstractmethod
    def proba_distribution_net(self, *args, **kwargs) -> Union[nn.Module, Tuple[nn.Module, nn.Parameter]]:
        """Create the layers and parameters that represent the distribution.

        Subclasses must define this, but the arguments and return type vary between
        concrete classes."""

    @abstractmethod
    def proba_distribution(self, *args, **kwargs) -> "Distribution":
        """Set parameters of the distribution.

        :return: self
        """

    @abstractmethod
    def log_prob(self, x: th.Tensor) -> th.Tensor:
        """
        Returns the log likelihood

        :param x: the taken action
        :return: The log likelihood of the distribution
        """

    @abstractmethod
    def entropy(self) -> Optional[th.Tensor]:
        """
        Returns Shannon's entropy of the probability

        :return: the entropy, or None if no analytical form is known
        """

    @abstractmethod
    def sample(self) -> th.Tensor:
        """
        Returns a sample from the probability distribution

        :return: the stochastic action
        """

    @abstractmethod
    def mode(self) -> th.Tensor:
        """
        Returns the most likely action (deterministic output)
        from the probability distribution

        :return: the stochastic action
        """

    def get_actions(self, deterministic: bool = False) -> th.Tensor:
        """
        Return actions according to the probability distribution.

        :param deterministic:
        :return:
        """
        if deterministic:
            return self.mode()
        return self.sample()

    @abstractmethod
    def actions_from_params(self, *args, **kwargs) -> th.Tensor:
        """
        Returns samples from the probability distribution
        given its parameters.

        :return: actions
        """

    @abstractmethod
    def log_prob_from_params(self, *args, **kwargs) -> Tuple[th.Tensor, th.Tensor]:
        """
        Returns samples and the associated log probabilities
        from the probability distribution given its parameters.

        :return: actions and log prob
        """


def sum_independent_dims(tensor: th.Tensor) -> th.Tensor:
    """
    Continuous actions are usually considered to be independent,
    so we can sum components of the ``log_prob`` or the entropy.

    :param tensor: shape: (n_batch, n_actions) or (n_batch,)
    :return: shape: (n_batch,)
    """
    if len(tensor.shape) > 1:
        tensor = tensor.sum(dim=1)
    else:
        tensor = tensor.sum()
    return tensor

class BetaDistribution(Distribution):
    """
    Beta Distribution.

    :param action_dim:  Dimension of the action space.
    """

    def __init__(self, action_dim: int):
        super(BetaDistribution, self).__init__()
        self.distribution = None
        self.action_dim = action_dim


    def proba_distribution_net(self, obs_dim, act_dim, hidden_sizes, activation, output_activation=nn.Identity) -> Tuple[nn.Module, nn.Module]:
        """
        Create the layers and parameter that represent the distribution:
        """

        # alpha = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=nn.Softplus)
        # beta = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=nn.Softplus)
        # return alpha, beta

        alpheta = mlp([obs_dim] + list(hidden_sizes) + [2 * act_dim], activation=nn.Softplus, output_activation=nn.Softplus)
        return alpheta

    def proba_distribution(self, alpha: th.Tensor, beta: th.Tensor) -> "BetaDistribution":
        """
        Create the distribution given its parameters (alpha, beta),
        Note params must be alpha, beta > 0 .Which will be shifted to be
        alpha, beta > 1.

        :param alpha:
        :param beta:
        :return:
        """
        self.distribution = Beta(alpha + 1, beta + 1)
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        """
        Get the log probabilities of actions according to the distribution.
        Note that you must first call the ``proba_distribution()`` method.

        :param actions:
        :return:
        """
        #  [-1, 1] --> [0, 1]
        #  [-1, 1] +1 --> [0, 2] / 2 --> [0, 1]
        act = (actions + 1) / 2

        log_prob = self.distribution.log_prob(act)
        return sum_independent_dims(log_prob)

    def entropy(self) -> th.Tensor:
        return sum_independent_dims(self.distribution.entropy())

    def sample(self) -> th.Tensor:
        # Reparametrization trick to pass gradients
        # [0,1] --> [-1,1]
        return 2*self.distribution.rsample() - 1

    def mode(self) -> th.Tensor:
        # [0,1] --> [-1,1]
        return 2*self.distribution.mean - 1

    def actions_from_params(self, alpha: th.Tensor, beta: th.Tensor, deterministic: bool = False) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(alpha, beta)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(self, alpha: th.Tensor, beta: th.Tensor) -> Tuple[th.Tensor, th.Tensor]:
        """
        Compute the log probability of taking an action
        given the distribution parameters. Which will be shifted to be
        alpha, beta > 1.

        :param alpha:
        :param beta:
        :return:
        """
        actions = self.actions_from_params(alpha, beta)
        log_prob = self.log_prob(actions)
        return actions, log_prob

class BetaDistributionReparam(Distribution):
    """
    Beta Distribution.

    :param action_dim:  Dimension of the action space.
    """

    def __init__(self, action_dim: int):
        super(BetaDistribution, self).__init__()
        self.distribution = None
        self.action_dim = action_dim


    def proba_distribution_net(self, obs_dim, act_dim, hidden_sizes, activation, output_activation=nn.Identity) -> Tuple[nn.Module, nn.Module]:
        """
        Create the layers and parameter that represent the distribution:
        """

        u = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=nn.Sigmoid)
        # k = mlp([obs_dim + act_dim] + list(hidden_sizes) + [act_dim], activation=nn.Softplus, output_activation=nn.Softplus)
        k = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation=nn.Softplus, output_activation=nn.Softplus)
        return u, k

    def proba_distribution(self, u: th.Tensor, k: th.Tensor) -> "BetaDistribution":
        """
        Create the distribution given its parameters (alpha, beta),
        Note params must be alpha, beta > 0 .Which will be shifted to be
        alpha, beta > 1.

        :param u = alpha / (alpha + beta):
        :param k = alpha + beta + 1:
        :return:
        """
        alpha = u * (k - 1)
        beta = (1 - u) * (k - 1)

        self.distribution = Beta(alpha + 1, beta + 1)
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        """
        Get the log probabilities of actions according to the distribution.
        Note that you must first call the ``proba_distribution()`` method.

        :param actions:
        :return:
        """
        #  [-1, 1] --> [0, 1]
        #  [-1, 1] +1 --> [0, 2] / 2 --> [0, 1]
        act = (actions + 1) / 2

        log_prob = self.distribution.log_prob(act)
        return sum_independent_dims(log_prob)

    def entropy(self) -> th.Tensor:
        return sum_independent_dims(self.distribution.entropy())

    def sample(self) -> th.Tensor:
        # Reparametrization trick to pass gradients
        # [0,1] --> [-1,1]
        return 2*self.distribution.rsample() - 1

    def mode(self) -> th.Tensor:
        # [0,1] --> [-1,1]
        return 2*self.distribution.mean - 1

    def actions_from_params(self, u: th.Tensor, k: th.Tensor, deterministic: bool = False) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(u, k)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(self, u: th.Tensor, k: th.Tensor) -> Tuple[th.Tensor, th.Tensor]:
        """
        Compute the log probability of taking an action
        given the distribution parameters.

        :param mean_actions:
        :param log_std:
        :return:
        """
        actions = self.actions_from_params(u, k)
        log_prob = self.log_prob(actions)
        return actions, log_prob

class BetaDistributionReparam2(Distribution):
    """
    Beta Distribution.

    :param action_dim:  Dimension of the action space.
    """

    def __init__(self, action_dim: int):
        super(BetaDistribution, self).__init__()
        self.distribution = None
        self.action_dim = action_dim


    def proba_distribution_net(self, obs_dim, act_dim, hidden_sizes, activation, output_activation=nn.Identity) -> Tuple[nn.Module, nn.Module]:
        """
        Create the layers and parameter that represent the distribution:
        """

        u = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=nn.Sigmoid)
        # k = mlp([obs_dim + act_dim] + list(hidden_sizes) + [act_dim], activation=nn.Softplus, output_activation=nn.Softplus)
        s = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=nn.Sigmoid)
        return u, s

    def proba_distribution(self, u: th.Tensor, s: th.Tensor) -> "BetaDistribution2":
        """
        Create the distribution given its parameters (alpha, beta),
        Note params must be alpha, beta > 0 .Which will be shifted to be
        alpha, beta > 1.

        :param u - mean:
        :param k - std:
        :return:
        """
        var = (s*0.15)**2

        alpha = m**2*(1-m) / var
        beta = m*(1-m)**2 / var

        self.distribution = Beta(alpha + 1, beta + 1)
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        """
        Get the log probabilities of actions according to the distribution.
        Note that you must first call the ``proba_distribution()`` method.

        :param actions:
        :return:
        """
        #  [-1, 1] --> [0, 1]
        #  [-1, 1] +1 --> [0, 2] / 2 --> [0, 1]
        act = (actions + 1) / 2

        log_prob = self.distribution.log_prob(act)
        return sum_independent_dims(log_prob)

    def entropy(self) -> th.Tensor:
        return sum_independent_dims(self.distribution.entropy())

    def sample(self) -> th.Tensor:
        # Reparametrization trick to pass gradients
        # [0,1] --> [-1,1]
        return 2*self.distribution.rsample() - 1

    def mode(self) -> th.Tensor:
        # [0,1] --> [-1,1]
        return 2*self.distribution.mean - 1

    def actions_from_params(self, u: th.Tensor, s: th.Tensor, deterministic: bool = False) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(u, s)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(self, u: th.Tensor, k: th.Tensor) -> Tuple[th.Tensor, th.Tensor]:
        """
        Compute the log probability of taking an action
        given the distribution parameters.

        :param mean_actions:
        :param log_std:
        :return:
        """
        actions = self.actions_from_params(u, k)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class DiagGaussianDistribution(Distribution):
    """
    Gaussian distribution with diagonal covariance matrix, for continuous actions.

    :param action_dim:  Dimension of the action space.
    """

    def __init__(self, action_dim: int):
        super(DiagGaussianDistribution, self).__init__()
        self.distribution = None
        self.action_dim = action_dim

    def proba_distribution_net(self, obs_dim, act_dim, hidden_sizes, activation, output_activation=nn.Identity, log_std_init: float = -0.5) -> Tuple[nn.Module, nn.Parameter]:
        """
        Create the layers and parameter that represent the distribution:
        one output will be the mean of the Gaussian, the other parameter will be the
        standard deviation (log std in fact to allow negative values)

        :param latent_dim: Dimension of the last layer of the policy (before the action layer)
        :param log_std_init: Initial value for the log standard deviation
        :return:
        """

        mean_actions = mlp([obs_dim] + list(hidden_sizes) + [act_dim], activation, output_activation=output_activation)
        # TODO: allow action dependent std
        # log_std = nn.Parameter(th.ones(self.action_dim) * log_std_init, requires_grad=True)

        # log_std = -0.5 * np.ones(act_dim, dtype=np.float32)
        # log_std = th.nn.Parameter(th.as_tensor(log_std))

        log_std = th.nn.Parameter(th.ones(self.action_dim) * log_std_init)
        return mean_actions, log_std

    def proba_distribution(self, mean_actions: th.Tensor, log_std: th.Tensor) -> "DiagGaussianDistribution":
        """
        Create the distribution given its parameters (mean, std)

        :param mean_actions:
        :param log_std:
        :return:
        """
        # action_std = th.ones_like(mean_actions) * log_std.exp()
        # self.distribution = Normal(mean_actions, action_std)
        self.distribution = Normal(mean_actions, th.exp(log_std))
        return self

    def log_prob(self, actions: th.Tensor) -> th.Tensor:
        """
        Get the log probabilities of actions according to the distribution.
        Note that you must first call the ``proba_distribution()`` method.

        :param actions:
        :return:
        """
        log_prob = self.distribution.log_prob(actions)
        return sum_independent_dims(log_prob)

    def entropy(self) -> th.Tensor:
        return sum_independent_dims(self.distribution.entropy())

    def sample(self) -> th.Tensor:
        # Reparametrization trick to pass gradients
        return self.distribution.rsample()

    def mode(self) -> th.Tensor:
        return self.distribution.mean

    def actions_from_params(self, mean_actions: th.Tensor, log_std: th.Tensor, deterministic: bool = False) -> th.Tensor:
        # Update the proba distribution
        self.proba_distribution(mean_actions, log_std)
        return self.get_actions(deterministic=deterministic)

    def log_prob_from_params(self, mean_actions: th.Tensor, log_std: th.Tensor) -> Tuple[th.Tensor, th.Tensor]:
        """
        Compute the log probability of taking an action
        given the distribution parameters.

        :param mean_actions:
        :param log_std:
        :return:
        """
        actions = self.actions_from_params(mean_actions, log_std)
        log_prob = self.log_prob(actions)
        return actions, log_prob


class SquashedDiagGaussianDistribution(DiagGaussianDistribution):
    """
    Gaussian distribution with diagonal covariance matrix, followed by a squashing function (tanh) to ensure bounds.

    :param action_dim: Dimension of the action space.
    :param epsilon: small value to avoid NaN due to numerical imprecision.
    """

    def __init__(self, action_dim: int, epsilon: float = 1e-6):
        super(SquashedDiagGaussianDistribution, self).__init__(action_dim)
        # Avoid NaN (prevents division by zero or log of zero)
        self.epsilon = epsilon
        self.gaussian_actions = None

    def proba_distribution(self, mean_actions: th.Tensor, log_std: th.Tensor) -> "SquashedDiagGaussianDistribution":
        super(SquashedDiagGaussianDistribution, self).proba_distribution(mean_actions, log_std)
        return self

    def log_prob(self, actions: th.Tensor, gaussian_actions: Optional[th.Tensor] = None) -> th.Tensor:
        # Inverse tanh
        # Naive implementation (not stable): 0.5 * torch.log((1 + x) / (1 - x))
        # We use numpy to avoid numerical instability
        if gaussian_actions is None:
            # It will be clipped to avoid NaN when inversing tanh
            gaussian_actions = TanhBijector.inverse(actions)

        # Log likelihood for a Gaussian distribution
        log_prob = super(SquashedDiagGaussianDistribution, self).log_prob(gaussian_actions)
        # Squash correction (from original SAC implementation)
        # this comes from the fact that tanh is bijective and differentiable
        log_prob -= th.sum(th.log(1 - actions ** 2 + self.epsilon), dim=-1)
        return log_prob

    def entropy(self) -> Optional[th.Tensor]:
        # No analytical form,
        # entropy needs to be estimated using -log_prob.mean()
        return None

    def sample(self) -> th.Tensor:
        # Reparametrization trick to pass gradients
        self.gaussian_actions = super().sample()
        return th.tanh(self.gaussian_actions)

    def mode(self) -> th.Tensor:
        self.gaussian_actions = super().mode()
        # Squash the output
        return th.tanh(self.gaussian_actions)

    def log_prob_from_params(self, mean_actions: th.Tensor, log_std: th.Tensor) -> Tuple[th.Tensor, th.Tensor]:
        action = self.actions_from_params(mean_actions, log_std)
        log_prob = self.log_prob(action, self.gaussian_actions)
        return action, log_prob


class TanhBijector(object):
    """
    Bijective transformation of a probability distribution
    using a squashing function (tanh)
    TODO: use Pyro instead (https://pyro.ai/)

    :param epsilon: small value to avoid NaN due to numerical imprecision.
    """

    def __init__(self, epsilon: float = 1e-6):
        super(TanhBijector, self).__init__()
        self.epsilon = epsilon

    @staticmethod
    def forward(x: th.Tensor) -> th.Tensor:
        return th.tanh(x)

    @staticmethod
    def atanh(x: th.Tensor) -> th.Tensor:
        """
        Inverse of Tanh

        Taken from pyro: https://github.com/pyro-ppl/pyro
        0.5 * torch.log((1 + x ) / (1 - x))
        """
        return 0.5 * (x.log1p() - (-x).log1p())

    @staticmethod
    def inverse(y: th.Tensor) -> th.Tensor:
        """
        Inverse tanh.

        :param y:
        :return:
        """
        eps = th.finfo(y.dtype).eps
        # Clip the action to avoid NaN
        return TanhBijector.atanh(y.clamp(min=-1.0 + eps, max=1.0 - eps))

    def log_prob_correction(self, x: th.Tensor) -> th.Tensor:
        # Squash correction (from original SAC implementation)
        return th.log(1.0 - th.tanh(x) ** 2 + self.epsilon)
