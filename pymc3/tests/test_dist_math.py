#   Copyright 2021 The PyMC Developers
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
import aesara
import aesara.tensor as at
import numpy as np
import numpy.testing as npt
import pytest
import scipy.special

from aesara import config, function
from aesara.tensor.random.basic import multinomial
from scipy import interpolate, stats

import pymc3 as pm

from pymc3.aesaraf import floatX
from pymc3.distributions import Discrete
from pymc3.distributions.dist_math import (
    MvNormalLogp,
    SplineWrapper,
    alltrue_scalar,
    bound,
    clipped_beta_rvs,
    factln,
    i0e,
    incomplete_beta,
    multigammaln,
)
from pymc3.tests.checks import close_to
from pymc3.tests.helpers import verify_grad


def test_bound():
    logp = at.ones((10, 10))
    cond = at.ones((10, 10))
    assert np.all(bound(logp, cond).eval() == logp.eval())

    logp = at.ones((10, 10))
    cond = at.zeros((10, 10))
    assert np.all(bound(logp, cond).eval() == (-np.inf * logp).eval())

    logp = at.ones((10, 10))
    cond = True
    assert np.all(bound(logp, cond).eval() == logp.eval())

    logp = at.ones(3)
    cond = np.array([1, 0, 1])
    assert not np.all(bound(logp, cond).eval() == 1)
    assert np.prod(bound(logp, cond).eval()) == -np.inf

    logp = at.ones((2, 3))
    cond = np.array([[1, 1, 1], [1, 0, 1]])
    assert not np.all(bound(logp, cond).eval() == 1)
    assert np.prod(bound(logp, cond).eval()) == -np.inf


def test_check_bounds_false():
    with pm.Model(check_bounds=False):
        logp = at.ones(3)
        cond = np.array([1, 0, 1])
        assert np.all(bound(logp, cond).eval() == logp.eval())


def test_alltrue_scalar():
    assert alltrue_scalar([]).eval()
    assert alltrue_scalar([True]).eval()
    assert alltrue_scalar([at.ones(10)]).eval()
    assert alltrue_scalar([at.ones(10), 5 * at.ones(101)]).eval()
    assert alltrue_scalar([np.ones(10), 5 * at.ones(101)]).eval()
    assert alltrue_scalar([np.ones(10), True, 5 * at.ones(101)]).eval()
    assert alltrue_scalar([np.array([1, 2, 3]), True, 5 * at.ones(101)]).eval()

    assert not alltrue_scalar([False]).eval()
    assert not alltrue_scalar([at.zeros(10)]).eval()
    assert not alltrue_scalar([True, False]).eval()
    assert not alltrue_scalar([np.array([0, -1]), at.ones(60)]).eval()
    assert not alltrue_scalar([np.ones(10), False, 5 * at.ones(101)]).eval()


def test_alltrue_shape():
    vals = [True, at.ones(10), at.zeros(5)]

    assert alltrue_scalar(vals).eval().shape == ()


class MultinomialA(Discrete):
    rv_op = multinomial

    @classmethod
    def dist(cls, n, p, *args, **kwargs):
        return super().dist([n, p], **kwargs)

    def logp(value, n, p):
        return bound(
            factln(n) - factln(value).sum() + (value * at.log(p)).sum(),
            value >= 0,
            0 <= p,
            p <= 1,
            at.isclose(p.sum(), 1),
            broadcast_conditions=False,
        )


class MultinomialB(Discrete):
    rv_op = multinomial

    @classmethod
    def dist(cls, n, p, *args, **kwargs):
        return super().dist([n, p], **kwargs)

    def logp(value, n, p):
        return bound(
            factln(n) - factln(value).sum() + (value * at.log(p)).sum(),
            at.all(value >= 0),
            at.all(0 <= p),
            at.all(p <= 1),
            at.isclose(p.sum(), 1),
            broadcast_conditions=False,
        )


def test_multinomial_bound():

    x = np.array([1, 5])
    n = x.sum()

    with pm.Model() as modelA:
        p_a = pm.Dirichlet("p", floatX(np.ones(2)))
        MultinomialA("x", n, p_a, observed=x)

    with pm.Model() as modelB:
        p_b = pm.Dirichlet("p", floatX(np.ones(2)))
        MultinomialB("x", n, p_b, observed=x)

    assert np.isclose(
        modelA.logp({"p_stickbreaking__": [0]}), modelB.logp({"p_stickbreaking__": [0]})
    )


class TestMvNormalLogp:
    def test_logp(self):
        np.random.seed(42)

        chol_val = floatX(np.array([[1, 0.9], [0, 2]]))
        cov_val = floatX(np.dot(chol_val, chol_val.T))
        cov = at.matrix("cov")
        cov.tag.test_value = cov_val
        delta_val = floatX(np.random.randn(5, 2))
        delta = at.matrix("delta")
        delta.tag.test_value = delta_val
        expect = stats.multivariate_normal(mean=np.zeros(2), cov=cov_val)
        expect = expect.logpdf(delta_val).sum()
        logp = MvNormalLogp()(cov, delta)
        logp_f = aesara.function([cov, delta], logp)
        logp = logp_f(cov_val, delta_val)
        npt.assert_allclose(logp, expect)

    @aesara.config.change_flags(compute_test_value="ignore")
    def test_grad(self):
        np.random.seed(42)

        def func(chol_vec, delta):
            chol = at.stack(
                [
                    at.stack([at.exp(0.1 * chol_vec[0]), 0]),
                    at.stack([chol_vec[1], 2 * at.exp(chol_vec[2])]),
                ]
            )
            cov = at.dot(chol, chol.T)
            return MvNormalLogp()(cov, delta)

        chol_vec_val = floatX(np.array([0.5, 1.0, -0.1]))

        delta_val = floatX(np.random.randn(1, 2))
        verify_grad(func, [chol_vec_val, delta_val])

        delta_val = floatX(np.random.randn(5, 2))
        verify_grad(func, [chol_vec_val, delta_val])

    @aesara.config.change_flags(compute_test_value="ignore")
    def test_hessian(self):
        chol_vec = at.vector("chol_vec")
        chol_vec.tag.test_value = floatX(np.array([0.1, 2, 3]))
        chol = at.stack(
            [
                at.stack([at.exp(0.1 * chol_vec[0]), 0]),
                at.stack([chol_vec[1], 2 * at.exp(chol_vec[2])]),
            ]
        )
        cov = at.dot(chol, chol.T)
        delta = at.matrix("delta")
        delta.tag.test_value = floatX(np.ones((5, 2)))
        logp = MvNormalLogp()(cov, delta)
        g_cov, g_delta = at.grad(logp, [cov, delta])
        # TODO: What's the test?  Something needs to be asserted.
        at.grad(g_delta.sum() + g_cov.sum(), [delta, cov])


class TestSplineWrapper:
    @aesara.config.change_flags(compute_test_value="ignore")
    def test_grad(self):
        x = np.linspace(0, 1, 100)
        y = x * x
        spline = SplineWrapper(interpolate.InterpolatedUnivariateSpline(x, y, k=1))
        verify_grad(spline, [0.5])

    @aesara.config.change_flags(compute_test_value="ignore")
    def test_hessian(self):
        x = np.linspace(0, 1, 100)
        y = x * x
        spline = SplineWrapper(interpolate.InterpolatedUnivariateSpline(x, y, k=1))
        x_var = at.dscalar("x")
        (g_x,) = at.grad(spline(x_var), [x_var])
        with pytest.raises(NotImplementedError):
            at.grad(g_x, [x_var])


class TestI0e:
    @aesara.config.change_flags(compute_test_value="ignore")
    def test_grad(self):
        verify_grad(i0e, [0.5])
        verify_grad(i0e, [-2.0])
        verify_grad(i0e, [[0.5, -2.0]])
        verify_grad(i0e, [[[0.5, -2.0]]])


@pytest.mark.parametrize("dtype", ["float16", "float32", "float64"])
def test_clipped_beta_rvs(dtype):
    # Verify that the samples drawn from the beta distribution are never
    # equal to zero or one (issue #3898)
    values = clipped_beta_rvs(0.01, 0.01, size=1000000, dtype=dtype)
    assert not (np.any(values == 0) or np.any(values == 1))


def check_vals(fn1, fn2, *args):
    v = fn1(*args)
    close_to(v, fn2(*args), 1e-6 if v.dtype == np.float64 else 1e-4)


def test_multigamma():
    x = at.vector("x")
    p = at.scalar("p")

    xvals = [np.array([v], dtype=config.floatX) for v in [0.1, 2, 5, 10, 50, 100]]

    multigammaln_ = function([x, p], multigammaln(x, p), mode="FAST_COMPILE")

    def ref_multigammaln(a, b):
        return np.array(scipy.special.multigammaln(a[0], b), config.floatX)

    for p in [0, 1, 2, 3, 4, 100]:
        for x in xvals:
            if np.all(x > 0.5 * (p - 1)):
                check_vals(multigammaln_, ref_multigammaln, x, p)


def test_incomplete_beta_deprecation():
    with pytest.warns(DeprecationWarning, match="incomplete_beta has been deprecated"):
        res = incomplete_beta(3, 5, 0.5).eval()
    assert np.isclose(res, at.betainc(3, 5, 0.5).eval())
