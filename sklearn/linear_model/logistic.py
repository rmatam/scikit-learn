"""
Logistic Regression
"""

# Author: Gael Varoquaux <gael.varoquaux@normalesup.org>
#         Fabian Pedregosa <f@bianp.net>
#         Alexandre Gramfort <alexandre.gramfort@telecom-paristech.fr>
#         Manoj Kumar <manojkumarsivaraj334@gmail.com>

import numbers
import warnings

import numpy as np
from scipy import optimize, sparse

from .base import LinearClassifierMixin, SparseCoefMixin, BaseEstimator
from ..feature_selection.from_model import _LearntSelectorMixin
from ..preprocessing import LabelEncoder
from ..svm.base import BaseLibLinear
from ..utils import atleast2d_or_csc, check_arrays, compute_class_weight
from ..utils.extmath import log_logistic, safe_sparse_dot
from ..utils.validation import as_float_array
from ..utils.fixes import expit
from ..externals.joblib import Parallel, delayed
from ..cross_validation import _check_cv
from ..utils.optimize import newton_cg
from ..externals import six
from ..metrics import SCORERS


# .. some helper functions for logistic_regression_path ..
def _intercept_dot(w, X, y):
    """Computes y * np.dot(X, w).

    It takes into consideration if the intercept should be fit or not.

    Parameters
    ----------
    w : ndarray, shape (n_features,) or (n_features + 1,)
        Coefficient vector.

    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data.

    y : ndarray, shape (n_samples,)
        Array of labels.
    """
    c = 0.
    if w.size == X.shape[1] + 1:
        c = w[-1]
        w = w[:-1]

    z = safe_sparse_dot(X, w) + c
    return w, c, y*z


def _logistic_loss_and_grad(w, X, y, alpha, sample_weight=None):
    """Computes the logistic loss and gradient.

    Parameters
    ----------
    w : ndarray, shape (n_features,) or (n_features + 1,)
        Coefficient vector.

    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data.

    y : ndarray, shape (n_samples,)
        Array of labels.

    alpha : float
        Regularization parameter. alpha is equal to 1 / C.

    sample_weight: ndarray, shape (n_samples,) optional
        Array of weights that are assigned to individual samples.
        If not provided, then each sample is given unit weight.

    Returns
    -------
    out: float
        Logistic loss.

    grad: ndarray, shape (n_features,) or (n_features + 1,)
        Logistic gradient.
    """
    _, n_features = X.shape
    grad = np.empty_like(w)

    w, c, yz = _intercept_dot(w, X, y)

    if sample_weight is None:
        sample_weight = np.ones(y.shape[0])

    # Logistic loss is the negative of the log of the logistic function.
    out = -np.sum(sample_weight * log_logistic(yz)) + .5 * alpha * np.dot(w, w)

    z = expit(yz)
    z0 = sample_weight * (z - 1) * y

    grad[:n_features] = safe_sparse_dot(X.T, z0) + alpha * w

    # Case where we fit the intercept.
    if grad.shape[0] > n_features:
        grad[-1] = z0.sum()
    return out, grad


def _logistic_loss(w, X, y, alpha, sample_weight=None):
    """Computes the logistic loss.

    Parameters
    ----------
    w : ndarray, shape (n_features,) or (n_features + 1,)
        Coefficient vector.

    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data.

    y : ndarray, shape (n_samples,)
        Array of labels.

    alpha : float
        Regularization parameter. alpha is equal to 1 / C.

    sample_weight: ndarray, shape (n_samples,) optional
        Array of weights that are assigned to individual samples.
        If not provided, then each sample is given unit weight.

    Returns
    -------
    out: float
        Logistic loss.
    """
    w, c, yz = _intercept_dot(w, X, y)

    if sample_weight is None:
        sample_weight = np.ones(y.shape[0])

    # Logistic loss is the negative of the log of the logistic function.
    out = -np.sum(sample_weight * log_logistic(yz)) + .5 * alpha * np.dot(w, w)
    return out


def _logistic_loss_grad_hess(w, X, y, alpha, sample_weight=None):
    """Computes the logistic loss, gradient and the Hessian.

    Parameters
    ----------
    w : ndarray, shape (n_features,) or (n_features + 1,)
        Coefficient vector.

    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data.

    y : ndarray, shape (n_samples,)
        Array of labels.

    alpha : float
        Regularization parameter. alpha is equal to 1 / C.

    sample_weight: ndarray, shape (n_samples,) optional
        Array of weights that are assigned to individual samples.
        If not provided, then each sample is given unit weight.

    Returns
    -------
    out: float
        Logistic loss.

    grad: ndarray, shape (n_features,) or (n_features + 1,)
        Logistic gradient.

    Hs: callable
        Function that takes the gradient as a parameter and returns the
        matrix product of the Hessian and gradient.
    """
    n_samples, n_features = X.shape
    grad = np.empty_like(w)

    w, c, yz = _intercept_dot(w, X, y)

    if sample_weight is None:
        sample_weight = np.ones(y.shape[0])

    # Logistic loss is the negative of the log of the logistic function.
    out = -np.sum(sample_weight * log_logistic(yz)) + .5 * alpha * np.dot(w, w)

    z = expit(yz)
    z0 = sample_weight * (z - 1) * y

    grad[:n_features] = safe_sparse_dot(X.T, z0) + alpha * w

    # Case where we fit the intercept.
    if grad.shape[0] > n_features:
        grad[-1] = z0.sum()

    # The mat-vec product of the Hessian
    d = sample_weight * z * (1 - z)
    if sparse.issparse(X):
        dX = safe_sparse_dot(sparse.dia_matrix((d, 0),
                             shape=(n_samples, n_samples)), X)
    else:
        # Precompute as much as possible
        dX = d[:, np.newaxis] * X

    if c is not None:
        # Calculate the double derivative with respect to intercept
        # In the case of sparse matrices this returns a matrix object.
        dd_intercept = np.squeeze(np.array(dX.sum(axis=0)))

    def Hs(s):
        ret = np.empty_like(s)
        ret[:n_features] = X.T.dot(dX.dot(s[:n_features]))
        ret[:n_features] += alpha * s[:n_features]

        # For the fit intercept case.
        if s.shape[0] > n_features:
            ret[:n_features] += s[-1] * dd_intercept
            ret[-1] = dd_intercept.dot(s[:n_features])
            ret[-1] += d.sum() * s[-1]
        return ret

    return out, grad, Hs


def logistic_regression_path(X, y, pos_class=None, Cs=10, fit_intercept=True,
                             max_iter=100, tol=1e-4, verbose=0,
                             solver='liblinear', coef=None, copy=True,
                             class_weight=None, dual=False, penalty='l2'):
    """Compute a Logistic Regression model for a list of regularization
    parameters.

    This is an implementation that uses the result of the previous model
    to speed up computations along the set of solutions, making it faster
    than sequentially calling LogisticRegression for the different parameters.

    Parameters
    ----------
    X : array-like or sparse matrix, shape (n_samples, n_features)
        Input data.

    y : array-like, shape (n_samples,)
        Input data, target values.

    Cs : array-like or integer of shape (n_cs,)
        List of values for the regularization parameter or integer specifying
        the number of regularization parameters that should be used. In this
        case, the parameters will be chosen in a logarithmic scale between
        1e-4 and 1e4.

    pos_class: int, None
        The class with respect to which we perform a one-vs-all fit.
        If None, then it is assumed that the given problem is binary.

    fit_intercept : bool
        Whether to fit an intercept for the model. In this case the shape of
        the returned array is (n_cs, n_features + 1).

    max_iter : int
        Maximum number of iterations for the solver.

    tol : float
        Stopping criterion. The iteration will stop when
        ``max{|g_i | i = 1, ..., n} <= tol``
        where ``g_i`` is the i-th component of the gradient.

    verbose: int
        Print convergence message if True.

    solver : {'lbfgs', 'newton-cg', 'liblinear'}
        Numerical solver to use.

    coef: array-like, shape (n_features,), default None
        Initialization value for coefficients of logistic regression.

    copy: bool, default True
        Whether or not to produce a copy of the data. Setting this to
        True will be useful in cases, when logistic_regression_path
        is called repeatedly with the same data, as y is modified
        along the path.

    class_weight : ndarray, None
        Provide a array of weights corresponding to each class, as obtained
        from the output of compute_class_weight. It None, then all classes
        are assumed to have weight one.

    dual : bool
        Dual or primal formulation. Dual formulation is only implemented for
        l2 penalty with liblinear solver. Prefer dual=False when
        n_samples > n_features.

    penalty : str, 'l1' or 'l2'
        Used to specify the norm used in the penalization. The newton-cg and
        lbfgs solvers support only l2 penalties.

    Returns
    -------
    coefs : ndarray, shape (n_cs, n_features) or (n_cs, n_features + 1)
        List of coefficients for the Logistic Regression model. If
        fit_intercept is set to True then the second dimension will be
        n_features + 1, where the last item represents the intercept.

    Cs : ndarray
        Grid of Cs used for cross-validation.

    Notes
    -----
    You might get slighly different results with the solver trust-ncg than
    with the others since this uses LIBLINEAR which penalizes the intercept.
    """
    if isinstance(Cs, numbers.Integral):
        Cs = np.logspace(-4, 4, Cs)

    X = atleast2d_or_csc(X, dtype=np.float64)
    X, y = check_arrays(X, y, copy=copy)
    n_classes = np.unique(y)

    if pos_class is None:
        if not (n_classes.size == 2):
            raise ValueError('To fit OvA, use the pos_class argument')
        # np.unique(y) gives labels in sorted order.
        pos_class = n_classes[1]

    mask = (y == pos_class)
    y[mask] = 1
    y[~mask] = -1

    # To take care of object dtypes
    y = as_float_array(y, copy=False)

    if class_weight is None:
        class_weight = np.ones(len(n_classes))

    le = LabelEncoder()
    sample_weight = class_weight[le.fit_transform(y)]

    if fit_intercept:
        w0 = np.zeros(X.shape[1] + 1)
    else:
        w0 = np.zeros(X.shape[1])

    if coef is not None:
        # it must work both giving the bias term and not
        if not coef.size in (X.shape[1], w0.size):
            raise ValueError('Initialization coef is not of correct shape')
        w0[:coef.size] = coef
    coefs = list()

    for C in Cs:
        if solver == 'lbfgs':
            func = _logistic_loss_and_grad
            try:
                out = optimize.fmin_l_bfgs_b(
                    func, w0, fprime=None,
                    args=(X, y, 1. / C, sample_weight),
                    iprint=verbose > 0, pgtol=tol, maxiter=max_iter)
            except TypeError:
                # old scipy doesn't have maxiter
                out = optimize.fmin_l_bfgs_b(
                    func, w0, fprime=None,
                    args=(X, y, 1. / C, sample_weight),
                    iprint=verbose > 0, pgtol=tol)
            w0 = out[0]
            if out[2]["warnflag"] == 1:
                warnings.warn("lbfgs failed to converge. Increase the number "
                              "of iterations.")

        elif solver == 'newton-cg':
            grad = lambda x, *args: _logistic_loss_and_grad(x, *args)[1]
            w0 = newton_cg(_logistic_loss_grad_hess, _logistic_loss, grad,
                           w0, args=(X, y, 1. /C, sample_weight),
                           maxiter=max_iter, xtol=tol)
        elif solver == 'liblinear':
            lr = LogisticRegression(C=C, fit_intercept=fit_intercept, tol=tol,
                                    class_weight=class_weight, dual=dual,
                                    penalty=penalty)
            lr.fit(X, y)
            if fit_intercept:
                w0 = np.concatenate([lr.coef_.ravel(), lr.intercept_])
            else:
                w0 = lr.coef_.ravel()
        else:
            raise ValueError("solver must be one of {'liblinear', 'lbfgs', "
                             "'newton-cg'}, got '%s' instead" % solver)
        coefs.append(w0)
    return coefs, np.array(Cs)


# helper function for LogisticCV
def _log_reg_scoring_path(X, y, train, test, pos_class=None, Cs=10,
                          scoring=None, fit_intercept=False,
                          max_iter=100, tol=1e-4, class_weight=None,
                          verbose=0, method='liblinear', penalty='l2',
                          dual=False):
    """Computes scores across logistic_regression_path

    Parameters
    ----------
    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        Training data.

    y : array-like, shape (n_samples,) or (n_samples, n_targets)
        Target labels.

    train : list of indices
        The indices of the train set.

    test : list of indices
        The indices of the test set.

    pos_class: int, None
        The class with respect to which we perform a one-vs-all fit.
        If None, then it is assumed that the given problem is binary.

    Cs: list of floats | int
        Each of the values in Cs describes the inverse of
        regularization strength. If Cs is as an int, then a grid of Cs
        values are chosen in a logarithmic scale between 1e-4 and 1e4.
        If not provided, then a fixed set of values for Cs are used.

    scoring : callable
        For a list of scoring functions that can be used, look at
        :mod:`sklearn.metrics`. The default scoring option used is
        accuracy_score.

    fit_intercept : bool
        If False, then the bias term is set to zero. Else the last
        term of each coef_ gives us the intercept.

    max_iter : int
        Maximum no. of iterations for the solver.

    tol : float
        Tolerance for stopping criteria.

    class_weight : ndarray, None
        Provide a array of weights corresponding to each class, as obtained
        from the output of compute_class_weight. It None, then all classes
        are assumed to have weight one.

    verbose : int
        Amount of verbosity.

    method : {'lbfgs', 'newton-cg', 'liblinear'}
        Decides which solver to use.

    penalty : str, 'l1' or 'l2'
        Used to specify the norm used in the penalization. The newton-cg and
        lbfgs solvers support only l2 penalties.

    dual : bool
        Dual or primal formulation. Dual formulation is only implemented for
        l2 penalty with liblinear solver. Prefer dual=False when
        n_samples > n_features.
    """

    log_reg = LogisticRegression(fit_intercept=fit_intercept)
    log_reg._enc = LabelEncoder()
    log_reg._enc.fit_transform([-1, 1])

    X_train = X[train]
    X_test = X[test]
    y_train = y[train]
    y_test = y[test]

    if pos_class is not None:
        # In order to avoid a copy in y, mask test and train separately
        mask = (y_train == pos_class)
        y_train[mask] = 1
        y_train[~mask] = -1
        mask = (y_test == pos_class)
        y_test[mask] = 1
        y_test[~mask] = -1

    # To deal with object dtypes, we need to convert into an array of floats.
    X_train = as_float_array(X_train, copy=False)
    y_train = as_float_array(y_train, copy=False)
    X_test = as_float_array(X_test, copy=False)
    y_test = as_float_array(y_test, copy=False)

    coefs, Cs = logistic_regression_path(X_train, y_train, Cs=Cs,
                                         fit_intercept=fit_intercept,
                                         solver=method,
                                         max_iter=max_iter,
                                         class_weight=class_weight,
                                         copy=False,
                                         tol=tol, verbose=verbose,
                                         dual=dual, penalty=penalty)

    scores = list()

    if isinstance(scoring, six.string_types):
        scoring = SCORERS[scoring]
    for w in coefs:
        if fit_intercept:
            log_reg.coef_ = w[np.newaxis, :-1]
            log_reg.intercept_ = w[-1]
        else:
            log_reg.coef_ = w[np.newaxis, :]
            log_reg.intercept_ = 0.
        if scoring is None:
            scores.append(log_reg.score(X_test, y_test))
        else:
            scores.append(scoring(log_reg, X_test, y_test))
    return coefs, Cs, np.array(scores)


class LogisticRegression(BaseLibLinear, LinearClassifierMixin,
                         _LearntSelectorMixin, SparseCoefMixin):
    """Logistic Regression (aka logit, MaxEnt) classifier.

    In the multiclass case, the training algorithm uses a one-vs.-all (OvA)
    scheme, rather than the "true" multinomial LR.

    This class implements regularized logistic regression using the
    `liblinear` library, newton-cg and lbfgs solvers. It can handle both
    dense and sparse input. Use C-ordered arrays or CSR matrices containing
    64-bit floats for optimal performance; any other input format will be
    converted (and copied).

    The newton-cg and lbfgs solvers support only L2 regularization with primal
    formulation. The liblinear solver supports both L1 and L2 regularization,
    with a dual formulation only for the L2 penalty.

    Parameters
    ----------
    penalty : str, 'l1' or 'l2'
        Used to specify the norm used in the penalization. The newton-cg and
        lbfgs solvers support only l2 penalties.

    dual : bool
        Dual or primal formulation. Dual formulation is only implemented for
        l2 penalty with liblinear solver. Prefer dual=False when
        n_samples > n_features.

    C : float, optional (default=1.0)
        Inverse of regularization strength; must be a positive float.
        Like in support vector machines, smaller values specify stronger
        regularization.

    fit_intercept : bool, default: True
        Specifies if a constant (a.k.a. bias or intercept) should be
        added the decision function.

    intercept_scaling : float, default: 1
        when self.fit_intercept is True, instance vector x becomes
        [x, self.intercept_scaling],
        i.e. a "synthetic" feature with constant value equals to
        intercept_scaling is appended to the instance vector.
        The intercept becomes intercept_scaling * synthetic feature weight
        Note! the synthetic feature weight is subject to l1/l2 regularization
        as all other features.
        To lessen the effect of regularization on synthetic feature weight
        (and therefore on the intercept) intercept_scaling has to be increased.

    class_weight : {dict, 'auto'}, optional
        Over-/undersamples the samples of each class according to the given
        weights. If not given, all classes are supposed to have weight one.
        The 'auto' mode selects weights inversely proportional to class
        frequencies in the training set.

    max_iter : int
        Useful only for the newton-cg and lbfgs solvers. Maximum number of
        iterations taken for the solvers to converge.

    random_state: int seed, RandomState instance, or None (default)
        The seed of the pseudo random number generator to use when
        shuffling the data.

    solver: {'newton-cg', 'lbfgs', 'liblinear'}
        Algorithm to use in the optimization problem.

    tol: float, optional
        Tolerance for stopping criteria.

    Attributes
    ----------
    `coef_` : array, shape (n_classes, n_features)
        Coefficient of the features in the decision function.

    `intercept_` : array, shape = (n_classes,)
        Intercept (a.k.a. bias) added to the decision function.
        If `fit_intercept` is set to False, the intercept is set to zero.

    See also
    --------
    SGDClassifier: incrementally trained logistic regression (when given
        the parameter ``loss="log"``).
    sklearn.svm.LinearSVC: learns SVM models using the same algorithm.

    Notes
    -----
    The underlying C implementation uses a random number generator to
    select features when fitting the model. It is thus not uncommon,
    to have slightly different results for the same input data. If
    that happens, try with a smaller tol parameter.

    References:

    LIBLINEAR -- A Library for Large Linear Classification
        http://www.csie.ntu.edu.tw/~cjlin/liblinear/

    Hsiang-Fu Yu, Fang-Lan Huang, Chih-Jen Lin (2011). Dual coordinate descent
        methods for logistic regression and maximum entropy models.
        Machine Learning 85(1-2):41-75.
        http://www.csie.ntu.edu.tw/~cjlin/papers/maxent_dual.pdf
    """

    def __init__(self, penalty='l2', dual=False, tol=1e-4, C=1.0,
                 fit_intercept=True, intercept_scaling=1, class_weight=None,
                 random_state=None, solver='liblinear', max_iter=100):

        super(LogisticRegression, self).__init__(
            penalty=penalty, dual=dual, loss='lr', tol=tol, C=C,
            fit_intercept=fit_intercept, intercept_scaling=intercept_scaling,
            class_weight=class_weight, random_state=random_state,
            solver=solver, max_iter=max_iter)

    def predict_proba(self, X):
        """Probability estimates.

        The returned estimates for all classes are ordered by the
        label of classes.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]

        Returns
        -------
        T : array-like, shape = [n_samples, n_classes]
            Returns the probability of the sample for each class in the model,
            where classes are ordered as they are in ``self.classes_``.
        """
        return self._predict_proba_lr(X)

    def predict_log_proba(self, X):
        """Log of probability estimates.

        The returned estimates for all classes are ordered by the
        label of classes.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]

        Returns
        -------
        T : array-like, shape = [n_samples, n_classes]
            Returns the log-probability of the sample for each class in the
            model, where classes are ordered as they are in ``self.classes_``.
        """
        return np.log(self.predict_proba(X))


class LogisticRegressionCV(BaseEstimator, LinearClassifierMixin,
                           _LearntSelectorMixin):
    """Logistic Regression CV (aka logit, MaxEnt) classifier.

    This class implements logistic regression using liblinear, newton-cg or
    LBFGS optimizer. The newton-cg and lbfgs solvers support only L2
    regularization with primal formulation. The liblinear solver supports both
    L1 and L2 regularization, with a dual formulation only for the L2 penalty.

    Parameters
    ----------
    Cs: list of floats | int
        Each of the values in Cs describes the inverse of regularization
        strength. If Cs is as an int, then a grid of Cs values are chosen
        in a logarithmic scale between 1e-4 and 1e4.
        Like in support vector machines, smaller values specify stronger
        regularization.

    fit_intercept: bool, default: True
        Specifies if a constant (a.k.a. bias or intercept) should be
        added the decision function.

    class_weight : {dict, 'auto'}, optional
        Over-/undersamples the samples of each class according to the given
        weights. If not given, all classes are supposed to have weight one.
        The 'auto' mode selects weights inversely proportional to class
        frequencies in the training set.

    cv : integer or cross-validation generator
        The default cross-validation generator used is Stratified K-Folds.
        If an integer is provided, then it is the number of folds used.
        See the module :mod:`sklearn.cross_validation` module for the
        list of possible cross-validation objects.

    penalty : str, 'l1' or 'l2'
        Used to specify the norm used in the penalization. The newton-cg and
        lbfgs solvers support only l2 penalties.

    dual : bool
        Dual or primal formulation. Dual formulation is only implemented for
        l2 penalty with liblinear solver. Prefer dual=False when
        n_samples > n_features.

    scoring: callabale
        Scoring function to use as cross-validation criteria. For a list of
        scoring functions that can be used, look at :mod:`sklearn.metrics`.
        The default scoring option used is accuracy_score.

    solver: {'newton-cg', 'lbfgs', 'liblinear'}
        Algorithm to use in the optimization problem.

    tol: float, optional
        Tolerance for stopping criteria.

    max_iter: int, optional
        Maximum number of iterations of the optimization algorithm.

    class_weight : {dict, 'auto'}, optional
        Over-/undersamples the samples of each class according to the given
        weights. If not given, all classes are supposed to have weight one.
        The 'auto' mode selects weights inversely proportional to class
        frequencies in the training set.

    n_jobs : int, optional
        Number of CPU cores used during the cross-validation loop. If given
        a value of -1, all cores are used.

    verbose : bool | int
        Amount of verbosity.

    refit : bool
        If set to True, the scores are averaged across all folds, and the
        coefs and the C that corresponds to the best score is taken, and a
        final refit is done using these parameters.
        Otherwise the coefs, intercepts and C that correspond to the
        best scores across folds are averaged.

    Attributes
    ----------
    `coef_` : array, shape (1, n_features) or (n_classes, n_features)
        Coefficient of the features in the decision function.

        `coef_` is of shape (1, n_features) when the given problem
        is binary.
        `coef_` is readonly property derived from `raw_coef_` that
        follows the internal memory layout of liblinear.

    `intercept_` : array, shape (1,) or (n_classes,)
        Intercept (a.k.a. bias) added to the decision function.
        It is available only when parameter intercept is set to True
        and is of shape(1,) when the problem is binary.

    `Cs_` : array
        Array of C i.e. inverse of regularization parameter values used
        for cross-validation.

    `coefs_paths_` : array, shape (n_folds, len(Cs_), n_features) or
                     (n_folds, len(Cs_), n_features + 1)
        dict with classes as the keys, and the path of coefficients obtained
        during cross-validating across each fold and then across each Cs
        after doing an OvA for the corresponding class.
        Each dict value has shape (n_folds, len(Cs_), n_features) or
        (n_folds, len(Cs_), n_features + 1) depending on whether the
        intercept is fit or not.

    `scores_` : dict
        dict with classes as the keys, and the values as the
        grid of scores obtained during cross-validating each fold, after doing
        an OvA for the corresponding class.
        Each dict value has shape (n_folds, len(Cs))

    `C_` : array, shape (n_classes,) or (n_classes - 1,)
        Array of C that maps to the best scores across every class.

    See also
    --------
    LogisticRegression

    """

    def __init__(self, Cs=10, fit_intercept=True, cv=None, dual=False,
                 penalty='l2', scoring=None, solver='newton-cg', tol=1e-4,
                 max_iter=100, class_weight=None, n_jobs=1, verbose=False,
                 refit=True):
        self.Cs = Cs
        self.fit_intercept = fit_intercept
        self.cv = cv
        self.dual = dual
        self.penalty = penalty
        self.scoring = scoring
        self.tol = tol
        self.max_iter = max_iter
        self.class_weight = class_weight
        self.n_jobs = n_jobs
        self.verbose = verbose
        self.solver = solver
        self.refit = refit

    def fit(self, X, y):
        """Fit the model according to the given training data.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape (n_samples, n_features)
            Training vector, where n_samples in the number of samples and
            n_features is the number of features.

        y : array-like, shape (n_samples,)
            Target vector relative to X.

        Returns
        -------
        self : object
            Returns self.
        """
        if self.solver != 'liblinear':
            if self.penalty != 'l2':
                raise ValueError("newton-cg and lbfgs solvers support only "
                                 "l2 penalties.")
            if self.dual:
                raise ValueError("newton-cg and lbfgs solvers support only "
                                 "the primal form.")

        X = atleast2d_or_csc(X, dtype=np.float64)
        X, y = check_arrays(X, y, copy=False)

        # init cross-validation generator
        cv = _check_cv(self.cv, X, y, classifier=True)
        folds = list(cv)

        self.classes_ = labels = np.unique(y)
        n_classes = len(labels)

        self.class_weight_ = compute_class_weight(
            self.class_weight, self.classes_, y)

        if n_classes == 2:
            # OvA in case of binary problems is as good as fitting
            # the higher label
            n_classes = 1
            labels = labels[1:]

        fold_coefs_ = Parallel(n_jobs=self.n_jobs, verbose=self.verbose)(
            delayed(_log_reg_scoring_path)(X, y, train, test,
                                           pos_class=label,
                                           Cs=self.Cs,
                                           fit_intercept=self.fit_intercept,
                                           penalty=self.penalty,
                                           dual=self.dual,
                                           method=self.solver,
                                           max_iter=self.max_iter,
                                           tol=self.tol,
                                           class_weight=self.class_weight_,
                                           verbose=max(0, self.verbose - 1),
                                           scoring=self.scoring)
            for label in labels
            for train, test in folds
            )
        coefs_paths, Cs, scores = zip(*fold_coefs_)

        self.Cs_ = Cs[0]
        coefs_paths = np.reshape(coefs_paths, (n_classes, len(folds),
                                 len(self.Cs_), -1))
        self.coefs_paths_ = dict(zip(labels, coefs_paths))
        scores = np.reshape(scores, (n_classes, len(folds), -1))
        self.scores_ = dict(zip(labels, scores))

        self.C_ = list()
        self.coef_ = list()
        self.intercept_ = list()

        for label in labels:
            scores = self.scores_[label]
            coefs_paths = self.coefs_paths_[label]

            if self.refit:
                best_index = scores.sum(axis=0).argmax()
                C_ = self.Cs_[best_index]
                self.C_.append(C_)
                coef_init = np.mean(coefs_paths[:, best_index, :], axis=0)

                w, _ = logistic_regression_path(
                    X, y, pos_class=label, Cs=[C_], solver=self.solver,
                    fit_intercept=self.fit_intercept, coef=coef_init,
                    max_iter=self.max_iter, tol=self.tol,
                    class_weight=self.class_weight_,
                    verbose=max(0, self.verbose - 1))
                w = w[0]

            else:
                # Take the best scores across every fold and the average of all
                # coefficients coressponding to the best scores.
                best_indices = np.argmax(scores, axis=1)
                w = np.mean([
                    coefs_paths[i][best_indices[i]]
                    for i in range(len(folds))
                    ], axis=0)
                self.C_.append(np.mean(self.Cs_[best_indices]))

            if self.fit_intercept:
                self.coef_.append(w[:-1])
                self.intercept_.append(w[-1])
            else:
                self.coef_.append(w)
                self.intercept_.append(0.)
        self.coef_ = np.asarray(self.coef_)
        self.intercept_ = np.asarray(self.intercept_)
        return self
