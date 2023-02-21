from __future__ import annotations

from warnings import warn

import numpy as np
from optht import optht
from scipy.signal import lsim
from scipy.signal import lti
from sklearn.utils.validation import check_is_fitted

from ..common import drop_nan_rows
from ..differentiation._derivative import Derivative
from ._base import BaseRegressor


class HAVOK(BaseRegressor):
    """
    Hankel Alternative View of Koopman (HAVOK) regressor.

    Aims to determine the system matrices A,B
    that satisfy d/dt v = Av + Bu, where v is the vector of the leading delay
    coordinates and u is a low-energy delay coordinate acting as forcing.
    A and B are the unknown system and control matrices, respectively.
    The delay coordinates are obtained by computing the SVD from a Hankel matrix.

    The objective function,
    :math:`\\|dV-AV-BU\\|_F`,
    is minimized using least-squares regression.

    See the following reference for more details:

        `Brunton, S.L., Brunton, B.W., Proctor, J.L., Kaiser, E. & Kutz, J.N.
        "Chaos as an intermittently forced linear system."
        Nature Communications, Vol. 8(19), 2017.
        <https://www.nature.com/articles/s41467-017-00030-8>`_

    Parameters
    ----------

    Attributed
    ----------
    coef_ : array, shape (n_input_features_, n_input_features_) or
        (n_input_features_, n_input_features_ + n_control_features_)
        Weight vectors of the regression problem. Corresponds to either [A] or [A,B]

    state_matrix_ : array, shape (n_input_features_, n_input_features_)
        Identified state transition matrix A of the underlying system.

    control_matrix_ : array, shape (n_input_features_, n_control_features_)
        Identified control matrix B of the underlying system.

    projection_matrix_ : array, shape (n_input_features_+n_control_features_, svd_rank)
        Projection matrix into low-dimensional subspace.

    projection_matrix_output_ : array, shape (n_input_features_+n_control_features_,
                                              svd_output_rank)
        Projection matrix into low-dimensional subspace.
    """

    def __init__(
        self,
        svd_rank=None,
        differentiator=Derivative(kind="finite_difference", k=1),
    ):
        self.svd_rank = svd_rank
        self.differentiator = differentiator

    def fit(self, x, y=None, dt=None):
        """
        Parameters
        ----------
        x: numpy ndarray, shape (n_samples, n_features)
            Measurement data to be fit.

        y: not used

        dt: scalar
            Discrete time-step

        Returns
        -------
        self: returns a fitted ``HAVOK`` instance
        """

        if y is not None:
            warn("havok regressor does not require the y argument when fitting.")

        if dt is None:
            raise ValueError("havok regressor requires a timestep dt when fitting.")

        self.dt_ = dt
        self.n_samples_, self.n_input_features_ = x.shape
        self.n_control_features_ = 1

        # Create time vector
        t = np.arange(0, self.dt_ * self.n_samples_, self.dt_)

        # SVD to calculate intrinsic observables
        U, s, Vh = np.linalg.svd(x.T, full_matrices=False)

        # calculate rank using optimal hard threshold by Gavish & Donoho
        if self.svd_rank is None:
            self.svd_rank = optht(x, sv=s, sigma=None)
        Vrh = Vh[: self.svd_rank, :]
        Vr = Vrh.T
        Ur = U[:, : self.svd_rank]
        sr = s[: self.svd_rank]

        # Vrh[:-1, :].T

        # calculate time derivative dxdt of only the first rank-1 & normalize
        dVr = self.differentiator(Vr[:, :-1], t)
        # this line actually makes vh and dvh transposed
        dVr, t, V = drop_nan_rows(dVr, t, Vh.T)

        # regression on intrinsic variables v
        # xi = np.zeros((self.svd_rank - 1, self.svd_rank))
        # for i in range(self.svd_rank - 1):
        #     # here, we use rank terms in V to fit the rank-1 terms dV/dt
        #     # we perform column wise
        #     xi[i, :] = np.linalg.lstsq(Vr, dVr[:, i], rcond=None)[0]

        xi = np.linalg.lstsq(Vr, dVr, rcond=None)[0].T
        assert xi.shape == (self.svd_rank - 1, self.svd_rank)

        self.forcing_signal = Vr[:, -1]
        self._state_matrix_ = xi[:, :-1]
        self._control_matrix_ = xi[:, -1].reshape(-1, 1)

        self.svals = s
        self._ur = Ur[:, :-1] @ np.diag(sr[:-1])
        self._coef_ = np.hstack([self.state_matrix_, self.control_matrix_])

        eigenvalues_, self._eigenvectors_ = np.linalg.eig(self.state_matrix_)
        # because we fit the model in continuous time,
        # so we need to convert to discrete time
        self._eigenvalues_ = np.exp(eigenvalues_ * dt)

        self._unnormalized_modes = self._ur @ self.eigenvectors_
        self._tmp_compute_psi = np.linalg.inv(self.eigenvectors_) @ self._ur.T

        # self.C = np.linalg.multi_dot(
        #     [
        #         np.linalg.inv(self.eigenvectors_),
        #         np.diag(np.reciprocal(s[: self.svd_rank - 1])),
        #         U[:, : self.svd_rank - 1].T,
        #     ]
        # )

    def predict(self, x, u, t):
        """
        Parameters
        ----------
        x: numpy ndarray, shape (n_samples, n_features)
            Measurement data upon which to base prediction.

        u: numpy.ndarray, shape (n_samples, n_control_features), \
                optional (default None)
            Time series of external actuation/control, which is sampled at time
            instances in t.

        t: numpy.ndarray, shape (n_samples)
            Time vector. Instances at which solution vector shall be provided.
            Must start at 0.


        Returns
        -------
        y: numpy ndarray, shape (n_samples, n_features)
            Prediction of x at time instances provided in t.

        """
        if t[0] != 0:
            raise ValueError("the time vector must start at 0.")

        check_is_fitted(self, "coef_")
        y0 = (
            # np.linalg.inv(np.diag(self.svals[: self.svd_rank - 1]))
            # @
            np.linalg.pinv(self._ur)
            @ x.T
        )
        sys = lti(
            self.state_matrix_,
            self.control_matrix_,
            self._ur,
            np.zeros((self.n_input_features_, self.n_control_features_)),
        )
        tout, ypred, xpred = lsim(sys, U=u, T=t, X0=y0.T)
        return ypred

    def _compute_phi(self, x):
        """Returns `phi(x)` given `x`"""
        if x.ndim == 1:
            x = x.reshape(1, -1)
        phi = self._ur.T @ x.T
        return phi

    def _compute_psi(self, x):
        """Returns `psi(x)` given `x`

        Parameters
        ----------
        x : numpy.ndarray, shape (n_samples, n_features)
            Measurement data upon which to compute psi values.

        Returns
        -------
        phi : numpy.ndarray, shape (n_samples, n_input_features_)
            value of Koopman psi at x
        """
        # compute psi - one column if x is a row
        if x.ndim == 1:
            x = x.reshape(1, -1)
        psi = self._tmp_compute_psi @ x.T
        return psi

    @property
    def coef_(self):
        check_is_fitted(self, "_coef_")
        return self._coef_

    @property
    def state_matrix_(self):
        check_is_fitted(self, "_state_matrix_")
        return self._state_matrix_

    @property
    def control_matrix_(self):
        check_is_fitted(self, "_control_matrix_")
        return self._control_matrix_

    @property
    def eigenvectors_(self):
        check_is_fitted(self, "_eigenvectors_")
        return self._eigenvectors_

    @property
    def eigenvalues_(self):
        check_is_fitted(self, "_eigenvalues_")
        return self._eigenvalues_

    @property
    def unnormalized_modes(self):
        check_is_fitted(self, "_unnormalized_modes")
        return self._unnormalized_modes

    @property
    def ur(self):
        check_is_fitted(self, "_ur")
        return self._ur
