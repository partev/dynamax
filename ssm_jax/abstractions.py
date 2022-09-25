from abc import ABC
from abc import abstractmethod
from functools import partial
from tqdm.auto import trange

import jax.numpy as jnp
import jax.random as jr
import optax
from jax import jit, lax, vmap
from jax.tree_util import tree_map

import blackjax

from ssm_jax.optimize import run_sgd
from ssm_jax.parameters import to_unconstrained, from_unconstrained

class SSM(ABC):
    """A base class for state space models. Such models consist of parameters, which
    we may learn, as well as hyperparameters, which specify static properties of the
    model. This base class allows parameters to be indicated a standardized way
    so that they can easily be converted to/from unconstrained form. It also uses
    these parameters to implement the tree_flatten and tree_unflatten methods necessary
    to register a model as a JAX PyTree.
    """

    @abstractmethod
    def initial_distribution(self, params, **covariates):
        """Return an initial distribution over latent states.
        Returns:
            dist (tfd.Distribution): distribution over initial latent state.
        """
        raise NotImplementedError

    @abstractmethod
    def transition_distribution(self, params, state, **covariates):
        """Return a distribution over next latent state given current state.
        Args:
            state (PyTree): current latent state
        Returns:
            dist (tfd.Distribution): conditional distribution of next latent state.
        """
        raise NotImplementedError

    @abstractmethod
    def emission_distribution(self, params, state, **covariates):
        """Return a distribution over emissions given current state.
        Args:
            state (PyTree): current latent state.
        Returns:
            dist (tfd.Distribution): conditional distribution of current emission.
        """
        raise NotImplementedError

    def sample(self, params, key, num_timesteps, **covariates):
        """Sample a sequence of latent states and emissions.
        Args:
            key: rng key
            num_timesteps: length of sequence to generate
        """

        def _step(prev_state, args):
            key, covariate = args
            key1, key2 = jr.split(key, 2)
            state = self.transition_distribution(params, prev_state, **covariate).sample(seed=key2)
            emission = self.emission_distribution(params, state, **covariate).sample(seed=key1)
            return state, (state, emission)

        # Sample the initial state
        key1, key2, key = jr.split(key, 3)
        initial_covariate = tree_map(lambda x: x[0], covariates)
        initial_state = self.initial_distribution(params, **initial_covariate).sample(seed=key1)
        initial_emission = self.emission_distribution(params, initial_state, **initial_covariate).sample(seed=key2)

        # Sample the remaining emissions and states
        next_keys = jr.split(key, num_timesteps - 1)
        next_covariates = tree_map(lambda x: x[1:], covariates)
        _, (next_states, next_emissions) = lax.scan(_step, initial_state, (next_keys, next_covariates))

        # Concatenate the initial state and emission with the following ones
        expand_and_cat = lambda x0, x1T: jnp.concatenate((jnp.expand_dims(x0, 0), x1T))
        states = tree_map(expand_and_cat, initial_state, next_states)
        emissions = tree_map(expand_and_cat, initial_emission, next_emissions)
        return states, emissions

    def log_prob(self, params, states, emissions, **covariates):
        """Compute the log joint probability of the states and observations"""

        def _step(carry, args):
            lp, prev_state = carry
            state, emission, covariate = args
            lp += self.transition_distribution(params, prev_state, **covariate).log_prob(state)
            lp += self.emission_distribution(params, state, **covariate).log_prob(emission)
            return (lp, state), None

        # Compute log prob of initial time step
        initial_state = tree_map(lambda x: x[0], states)
        initial_emission = tree_map(lambda x: x[0], emissions)
        initial_covariate = tree_map(lambda x: x[0], covariates)
        lp = self.initial_distribution(params, **initial_covariate).log_prob(initial_state)
        lp += self.emission_distribution(params, initial_state, **initial_covariate).log_prob(initial_emission)

        # Scan over remaining time steps
        next_states = tree_map(lambda x: x[1:], states)
        next_emissions = tree_map(lambda x: x[1:], emissions)
        next_covariates = tree_map(lambda x: x[1:], covariates)
        (lp, _), _ = lax.scan(_step, (lp, initial_state), (next_states, next_emissions, next_covariates))
        return lp

    def log_prior(self, params):
        """Return the log prior probability of any model parameters.
        Returns:
            lp (Scalar): log prior probability.
        """
        return 0.0

    def fit_em(self, initial_params, param_props, batch_emissions, num_iters=50, verbose=True, **batch_covariates):
        """Fit this HMM with Expectation-Maximization (EM).
        """
        @jit
        def em_step(params):
            batch_posteriors, lls = vmap(partial(self.e_step, params))(batch_emissions, **batch_covariates)
            lp = self.log_prior(params) + lls.sum()
            params = self.m_step(params, param_props, batch_emissions, batch_posteriors, **batch_covariates)
            return params, lp

        log_probs = []
        params = initial_params
        pbar = trange(num_iters) if verbose else range(num_iters)
        for _ in pbar:
            params, marginal_loglik = em_step(params)
            log_probs.append(marginal_loglik)
        return params, jnp.array(log_probs)

    def fit_sgd(self,
                curr_params,
                param_props,
                batch_emissions,
                optimizer=optax.adam(1e-3),
                batch_size=1,
                num_epochs=50,
                shuffle=False,
                key=jr.PRNGKey(0),
                **batch_covariates):
        """
        Fit this HMM by running SGD on the marginal log likelihood.
        Note that batch_emissions is initially of shape (N,T)
        where N is the number of independent sequences and
        T is the length of a sequence. Then, a random susbet with shape (B, T)
        of entire sequence, not time steps, is sampled at each step where B is
        batch size.
        Args:
            batch_emissions (chex.Array): Independent sequences.
            optmizer (optax.Optimizer): Optimizer.
            batch_size (int): Number of sequences used at each update step.
            num_epochs (int): Iterations made through entire dataset.
            shuffle (bool): Indicates whether to shuffle minibatches.
            key (chex.PRNGKey): RNG key to shuffle minibatches.
        Returns:
            losses: Output of loss_fn stored at each step.
        """
        curr_unc_params, fixed_params = to_unconstrained(curr_params, param_props)

        def _loss_fn(unc_params, minibatch):
            """Default objective function."""
            params = from_unconstrained(unc_params, fixed_params, param_props)
            minibatch_emissions, minibatch_covariates = minibatch
            scale = len(batch_emissions) / len(minibatch_emissions)
            minibatch_lls = vmap(partial(self.marginal_log_prob, params))(minibatch_emissions, **minibatch_covariates)
            lp = self.log_prior(params) + minibatch_lls.sum() * scale
            return -lp / batch_emissions.size

        dataset = (batch_emissions, batch_covariates)
        unc_params, losses = run_sgd(_loss_fn,
                                     curr_unc_params,
                                     dataset,
                                     optimizer=optimizer,
                                     batch_size=batch_size,
                                     num_epochs=num_epochs,
                                     shuffle=shuffle,
                                     key=key)

        params = from_unconstrained(unc_params, fixed_params, param_props)
        return params, losses

    def fit_hmc(self,
                initial_params,
                param_props,
                key,
                num_samples,
                batch_emissions,
                batch_inputs=None,
                warmup_steps=500,
                num_integration_steps=30):
        """Sample parameters of the model using HMC."""

        initial_unc_params, fixed_params = to_unconstrained(initial_params, param_props)

        # The log likelihood that the HMC samples from
        def _logprob(unc_params):
            params = from_unconstrained(unc_params, fixed_params, param_props)
            batch_lls = vmap(partial(self.marginal_log_prob, params))(batch_emissions, batch_inputs)
            lp = self.log_prior() + batch_lls.sum()
            # TODO Correct for the log determinant of the jacobian
            return lp

        # Initialize the HMC sampler using window_adaptation
        warmup = blackjax.window_adaptation(blackjax.hmc,
                                            _logprob,
                                            num_steps=warmup_steps,
                                            num_integration_steps=num_integration_steps)
        hmc_initial_state, hmc_kernel, _ = warmup.run(key, initial_unc_params)

        def hmc_step(hmc_state, rng_key):
            next_hmc_state, _ = hmc_kernel(rng_key, hmc_state)
            return next_hmc_state, hmc_state.position

        # Start sampling
        _, unc_param_samples = lax.scan(hmc_step, hmc_initial_state, jr.split(key, num_samples))

        # Convert back into full parameters
        return vmap(from_unconstrained, in_axes=(0, None, None))(
            unc_param_samples, fixed_params, param_props)
