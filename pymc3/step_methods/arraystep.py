#   Copyright 2020 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from abc import ABC, abstractmethod
from enum import IntEnum, unique
from typing import Dict, List, Tuple, TypeVar, Union

import numpy as np

from aesara.graph.basic import Variable
from numpy.random import uniform

from pymc3.blocking import DictToArrayBijection, PointType, RaveledVars
from pymc3.model import modelcontext
from pymc3.step_methods.compound import CompoundStep
from pymc3.util import get_var_name

__all__ = ["ArrayStep", "ArrayStepShared", "metrop_select", "Competence"]

StatsType = TypeVar("StatsType")


@unique
class Competence(IntEnum):
    """Enum for characterizing competence classes of step methods.
    Values include:
    0: INCOMPATIBLE
    1: COMPATIBLE
    2: PREFERRED
    3: IDEAL
    """

    INCOMPATIBLE = 0
    COMPATIBLE = 1
    PREFERRED = 2
    IDEAL = 3


class BlockedStep(ABC):

    generates_stats = False
    stats_dtypes: List[Dict[str, np.dtype]] = []
    vars: List[Variable] = []

    def __new__(cls, *args, **kwargs):
        blocked = kwargs.get("blocked")
        if blocked is None:
            # Try to look up default value from class
            blocked = getattr(cls, "default_blocked", True)
            kwargs["blocked"] = blocked

        model = modelcontext(kwargs.get("model"))
        kwargs.update({"model": model})

        # vars can either be first arg or a kwarg
        if "vars" not in kwargs and len(args) >= 1:
            vars = args[0]
            args = args[1:]
        elif "vars" in kwargs:
            vars = kwargs.pop("vars")
        else:  # Assume all model variables
            vars = model.value_vars

        if not isinstance(vars, (tuple, list)):
            vars = [vars]

        if len(vars) == 0:
            raise ValueError("No free random variables to sample.")

        if not blocked and len(vars) > 1:
            # In this case we create a separate sampler for each var
            # and append them to a CompoundStep
            steps = []
            for var in vars:
                step = super().__new__(cls)
                # If we don't return the instance we have to manually
                # call __init__
                step.__init__([var], *args, **kwargs)
                # Hack for creating the class correctly when unpickling.
                step.__newargs = ([var],) + args, kwargs
                steps.append(step)

            return CompoundStep(steps)
        else:
            step = super().__new__(cls)
            # Hack for creating the class correctly when unpickling.
            step.__newargs = (vars,) + args, kwargs
            return step

    # Hack for creating the class correctly when unpickling.
    def __getnewargs_ex__(self):
        return self.__newargs

    @abstractmethod
    def step(point: PointType, *args, **kwargs) -> Union[PointType, Tuple[PointType, StatsType]]:
        """Perform a single step of the sampler."""

    @staticmethod
    def competence(var, has_grad):
        return Competence.INCOMPATIBLE

    @classmethod
    def _competence(cls, vars, have_grad):
        vars = np.atleast_1d(vars)
        have_grad = np.atleast_1d(have_grad)
        competences = []
        for var, has_grad in zip(vars, have_grad):
            try:
                competences.append(cls.competence(var, has_grad))
            except TypeError:
                competences.append(cls.competence(var))
        return competences

    def stop_tuning(self):
        if hasattr(self, "tune"):
            self.tune = False


class ArrayStep(BlockedStep):
    """
    Blocked step method that is generalized to accept vectors of variables.

    Parameters
    ----------
    vars: list
        List of variables for sampler.
    fs: list of logp Aesara functions
    allvars: Boolean (default False)
    blocked: Boolean (default True)
    """

    def __init__(self, vars, fs, allvars=False, blocked=True):
        self.vars = vars
        self.fs = fs
        self.allvars = allvars
        self.blocked = blocked

    def step(self, point: PointType):

        partial_funcs_and_point = [DictToArrayBijection.mapf(x, start_point=point) for x in self.fs]
        if self.allvars:
            partial_funcs_and_point.append(point)

        apoint = DictToArrayBijection.map({v.name: point[v.name] for v in self.vars})
        step_res = self.astep(apoint, *partial_funcs_and_point)

        if self.generates_stats:
            apoint_new, stats = step_res
        else:
            apoint_new = step_res

        if not isinstance(apoint_new, RaveledVars):
            # We assume that the mapping has stayed the same
            apoint_new = RaveledVars(apoint_new, apoint.point_map_info)

        point_new = DictToArrayBijection.rmap(apoint_new, start_point=point)

        if self.generates_stats:
            return point_new, stats

        return point_new

    @abstractmethod
    def astep(
        self, apoint: RaveledVars, point: PointType, *args
    ) -> Union[RaveledVars, Tuple[RaveledVars, StatsType]]:
        """Perform a single sample step in a raveled and concatenated parameter space."""


class ArrayStepShared(BlockedStep):
    """Faster version of ArrayStep that requires the substep method that does not wrap
       the functions the step method uses.

    Works by setting shared variables before using the step. This eliminates the mapping
    and unmapping overhead as well as moving fewer variables around.
    """

    def __init__(self, vars, shared, blocked=True):
        """
        Parameters
        ----------
        vars: list of sampling variables
        shared: dict of Aesara variable -> shared variable
        blocked: Boolean (default True)
        """
        self.vars = vars
        self.shared = {get_var_name(var): shared for var, shared in shared.items()}
        self.blocked = blocked

    def step(self, point):

        for name, shared_var in self.shared.items():
            shared_var.set_value(point[name])

        q = DictToArrayBijection.map({v.name: point[v.name] for v in self.vars})

        step_res = self.astep(q)

        if self.generates_stats:
            apoint, stats = step_res
        else:
            apoint = step_res

        if not isinstance(apoint, RaveledVars):
            # We assume that the mapping has stayed the same
            apoint = RaveledVars(apoint, q.point_map_info)

        new_point = DictToArrayBijection.rmap(apoint, start_point=point)

        if self.generates_stats:
            return new_point, stats

        return new_point


class PopulationArrayStepShared(ArrayStepShared):
    """Version of ArrayStepShared that allows samplers to access the states
    of other chains in the population.

    Works by linking a list of Points that is updated as the chains are iterated.
    """

    def __init__(self, vars, shared, blocked=True):
        """
        Parameters
        ----------
        vars: list of sampling variables
        shared: dict of Aesara variable -> shared variable
        blocked: Boolean (default True)
        """
        self.population = None
        self.this_chain = None
        self.other_chains = None
        return super().__init__(vars, shared, blocked)

    def link_population(self, population, chain_index):
        """Links the sampler to the population.

        Parameters
        ----------
        population: list of Points. (The elements of this list must be
            replaced with current chain states in every iteration.)
        chain_index: int of the index of this sampler in the population
        """
        self.population = population
        self.this_chain = chain_index
        self.other_chains = [c for c in range(len(population)) if c != chain_index]
        if not len(self.other_chains) > 1:
            raise ValueError(
                "Population is just {} + {}. "
                "This is too small and the error should have been raised earlier.".format(
                    self.this_chain, self.other_chains
                )
            )
        return


class GradientSharedStep(ArrayStepShared):
    def __init__(
        self, vars, model=None, blocked=True, dtype=None, logp_dlogp_func=None, **aesara_kwargs
    ):
        model = modelcontext(model)

        if logp_dlogp_func is None:
            func = model.logp_dlogp_function(vars, dtype=dtype, **aesara_kwargs)
        else:
            func = logp_dlogp_func

        self._logp_dlogp_func = func

        super().__init__(vars, func._extra_vars_shared, blocked)

    def step(self, point):
        self._logp_dlogp_func._extra_are_set = True
        return super().step(point)


def metrop_select(mr, q, q0):
    """Perform rejection/acceptance step for Metropolis class samplers.

    Returns the new sample q if a uniform random number is less than the
    metropolis acceptance rate (`mr`), and the old sample otherwise, along
    with a boolean indicating whether the sample was accepted.

    Parameters
    ----------
    mr: float, Metropolis acceptance rate
    q: proposed sample
    q0: current sample

    Returns
    -------
    q or q0
    """
    # Compare acceptance ratio to uniform random number
    # TODO XXX: This `uniform` is not given a model-specific RNG state, which
    # means that sampler runs that use it will not be reproducible.
    if np.isfinite(mr) and np.log(uniform()) < mr:
        return q, True
    else:
        return q0, False
