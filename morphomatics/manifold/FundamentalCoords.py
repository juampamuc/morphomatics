################################################################################
#                                                                              #
#   This file is part of the Morphomatics library                              #
#       see https://github.com/morphomatics/morphomatics                       #
#                                                                              #
#   Copyright (C) 2021 Zuse Institute Berlin                                   #
#                                                                              #
#   Morphomatics is distributed under the terms of the ZIB Academic License.   #
#       see /LICENSE                                              #
#                                                                              #
################################################################################

import os
import numpy as np

from scipy import sparse

try:
    from sksparse.cholmod import cholesky as direct_solve
except:
    from scipy.sparse.linalg import factorized as direct_solve

from ..geom import Surface
from . import SO3
from . import SPD
from . import ShapeSpace


class FundamentalCoords(ShapeSpace):
    """ Shape space based on fundamental coordinates. """
    def __init__(self, reference: Surface, metric_weights=(1.0, 1.0)):
        """
        :arg reference: Reference surface (shapes will be encoded as deformations thereof)
        :arg metric_weights: weights (rotation, stretch) for commensuration between rotational and stretch parts
        """
        assert reference is not None
        self.ref = reference
        self.init_face = int(os.getenv('FCM_INIT_FACE', 0))                    # initial face for spanning tree path
        self.init_vert = int(os.getenv('FCM_INIT_VERT', 0))                    # id of fixed vertex

        self.integration_tol = float(os.getenv('FCM_INTEGRATION_TOL', 1e-05))  # integration tolerance local/global solver
        self.integration_iter = int(os.getenv('FCM_INTEGRATION_ITER', 2))      # max iteration local/global solver

        omega_C = float(os.getenv('FCM_WEIGHT_ROTATION', metric_weights[0]))
        omega_U = float(os.getenv('FCM_WEIGHT_STRETCH', metric_weights[1]))
        self.metric_weights = (omega_C, omega_U)

        self.update_ref_geom(self.ref.v)
        self.spanning_tree_path = self.setup_spanning_tree_path()

        # rotation and stretch manifolds
        self.SO = SO3(int(0.5 * self.ref.inner_edges.getnnz()))        # relative rotations (transition rotations)
        self.SPD = SPD(self.ref.f.shape[0], 2)                         # stretch w.r.t. tangent space

    def __str__(self):
        return 'Fundamental Coordinates Shape Space'

    @property
    def dim(self):
        return 3 * int(0.5 * self.ref.inner_edges.getnnz()) + 3 * len(self.ref.f)

    @property
    def typicaldist(self):
        return np.sqrt(self.dim)

    def update_ref_geom(self, v):
        self.ref.v=v

        # center of gravity
        self.CoG = self.ref.v.mean(axis=0)

        # setup Poisson system
        S = self.ref.div @ self.ref.grad
        # add soft-constraint fixing translational DoF
        S += sparse.coo_matrix(([1.0], ([0], [0])), S.shape)  # make pos-def
        self.poisson = direct_solve(S.tocsc())

        self.ref_frame_field = self.setup_frame_field()

        edgeAreaFactor = np.divide(self.ref.edge_areas, np.sum(self.ref.edge_areas))
        faceAreaFactor = np.divide(self.ref.face_areas, np.sum(self.ref.face_areas))

        # setup metric
        diag = np.concatenate((self.metric_weights[0] * np.repeat(edgeAreaFactor, 9), self.metric_weights[1] * np.repeat(faceAreaFactor, 4)), axis=None)
        self.metric = sparse.diags(diag, 0)

    def disentangle(self, c):
        """
        :arg c: vetorized fundamental coords. (tangent vectors)
        :returns: de-vectorized tuple of rotations and stretches (skew-sym. and sym. matrices)
        """
        # 2xkx3x3 array, rotations are stored in [0, :, :, :] and stretches in [1, :, :, :]
        e = int(0.5 * self.ref.inner_edges.getnnz())
        return c[:9*e].reshape(-1, 3, 3), c[9*e:].reshape(-1, 2, 2)

    def to_coords(self, v):
        """
        :arg v: #v-by-3 array of vertex coordinates
        :return: fundamental coords.
        """
        # compute gradients
        D = self.ref.grad @ v

        # decompose...
        U, S, Vt = np.linalg.svd(D.reshape(-1, 3, 3))

        # D holds transpose of def. grads.
        # -> compute left polar decomposition for right stretch tensor

        # ...rotation
        R = np.einsum('...ij,...jk', U, Vt)
        W = np.ones_like(S)
        W[:, -1] = np.linalg.det(R)
        R = np.einsum('...ij,...j,...jk', U, W, Vt)

        # ...stretch
        S[:, -1] = 1  # no stretch (=1) in normal direction
        U = np.einsum('...ij,...j,...kj', U, S, U)

        # frame field on actual shape pushed over from reference shape
        frame = np.einsum('...ji,...jk', R, self.ref_frame_field)

        # setup ...transition rotations for every inner edge
        e = sparse.triu(self.ref.inner_edges).tocoo()
        C = np.zeros((e.getnnz(), 3, 3))
        C[e.data[:]] = np.einsum('...ji,...jk', frame[e.row[:]], frame[e.col[:]])

        # transform ...stretch from gobal (standard) coordinates to tangential Ulocal
        # frame.T * U * frame
        Ulocal = np.einsum('...ji,...jk,...kl', self.ref_frame_field, U, self.ref_frame_field)
        Ulocal = Ulocal[:,0:-1, 0:-1]

        return np.concatenate([np.ravel(C), np.ravel(Ulocal)]).reshape(-1)

    def from_coords(self, c):
        """
        :arg c: fundamental coords.
        :returns: #v-by-3 array of vertex coordinates
        """
        ################################################################################################################
        # initialization with spanning tree path #######################################################################
        C, Ulocal = self.disentangle(c)

        eIds = self.spanning_tree_path[:,0]
        fsourceId = self.spanning_tree_path[:, 1]
        ftargetId = self.spanning_tree_path[:, 2]

        # organize transition rotations along the path
        CoI = C[eIds[:]]
        CC = np.zeros_like(CoI)
        BB = (fsourceId < ftargetId)
        CC[BB] = CoI[BB]
        CC[~BB] = np.einsum("...ij->...ji", CoI[~BB])

        R= np.repeat(np.eye(3)[np.newaxis, :, :], len(self.ref.f), axis=0)

        # walk along path and initialize rotations
        for l in range(eIds.shape[0]):
            # R[ftargetId[l]]= np.einsum('...ij,...jk,...kl,...ml', R[fsourceId[l]], self.ref.frame_field[fsourceId[l]] , CC[l], self.ref.frame_field[ftargetId[l]])
            R[ftargetId[l]] = R[fsourceId[l]] @ self.ref_frame_field[fsourceId[l]] @ CC[l] @ self.ref_frame_field[ftargetId[l]].T

        # transform (tangential) Ulocal to gobal (standard) coordinates
        U = np.zeros_like(R)
        U[:, 0:-1, 0:-1] = Ulocal
        # frame * U * frame.T
        U = np.einsum('...ij,...jk,...lk', self.ref_frame_field, U, self.ref_frame_field)

        idx_1, idx_2, idx_3, n_1, n_2, n_3 = self.ref.neighbors

        e = sparse.triu(self.ref.inner_edges).tocoo(); f = sparse.tril(self.ref.inner_edges).tocoo()

        e.data += 1; f.data += 1

        CC = np.zeros((C.shape[0] + 1, 3, 3)); CCt = np.zeros((C.shape[0] + 1, 3, 3))
        CC[e.data] = C[e.data - 1]; CCt[f.data] = np.einsum("...ij->...ji", C[f.data - 1])

        e = e.tocsr(); f = f.tocsr()

        Dijk = R.copy()
        n_iter = 0
        while n_iter < self.integration_iter:

        ################################################################################################################
        # global step ##################################################################################################

            # setup gradient matrix and solve Poisson system
            D = np.einsum('...ij,...kj', U, R)  # <-- from left polar decomp.
            rhs = self.ref.div @ D.reshape(-1, 3)
            v = self.poisson(rhs)

        ################################################################################################################
        # local step ###################################################################################################
            if n_iter + 1 == self.integration_iter:
                break

            # compute gradients again
            D = (self.ref.grad @ v).reshape(-1, 3, 3)

            Dijk[idx_1] = np.einsum('...ji,...jk,...kl,...lm,...nm', D[n_1[:, 0]], U[n_1[:, 0]], self.ref_frame_field[n_1[:, 0]], CCt[e[idx_1, n_1[:, 0]]] + CC[f[idx_1, n_1[:, 0]]], self.ref_frame_field[idx_1])
            if n_2.shape[0] > 0 :
                Dijk[idx_2] = Dijk[idx_2] + np.einsum('...ji,...jk,...kl,...lm,...nm', D[n_2[:, 1]], U[n_2[:, 1]], self.ref_frame_field[n_2[:, 1]], CCt[e[idx_2, n_2[:, 1]]] + CC[f[idx_2, n_2[:, 1]]], self.ref_frame_field[idx_2])
            if n_3.shape[0] > 0 :
                Dijk[idx_3] = Dijk[idx_3] + np.einsum('...ji,...jk,...kl,...lm,...nm', D[n_3[:, 2]], U[n_3[:, 2]], self.ref_frame_field[n_3[:, 2]], CC[f[idx_3, n_3[:, 2]]] + CCt[e[idx_3, n_3[:, 2]]], self.ref_frame_field[idx_3])

            Uijk, Sijk, Vtijk = np.linalg.svd(Dijk)
            R = np.einsum('...ij,...jk', Uijk, Vtijk)
            Wijk = np.ones_like(Sijk)
            Wijk[:, -1] = np.linalg.det(R)
            R = np.einsum('...ij,...j,...jk', Uijk, Wijk, Vtijk)

            v += self.CoG - v.mean(axis=0)

            n_iter += 1

        # orient w.r.t. fixed frame and move to fixed node
        v[:] = (self.ref_frame_field[self.init_face] @ FundamentalCoords.frame_of_face(v, self.ref.f, [self.init_face]).T @ v[:].T).T
        v += self.ref.v[self.init_vert] - v[self.init_vert]
        # print("v:
", v)
        return v

    @property
    def identity(self):
        return np.concatenate([np.tile(np.eye(3), (int(0.5 * self.ref.inner_edges.getnnz()) , 1)).ravel(), np.tile(np.eye(2), (len(self.ref.f), 1)).ravel() ]).reshape(-1)

    def inner(self, X, G, H):
        """
        :arg G: (list of) tangent vector(s) at X
        :arg H: (list of) tangent vector(s) at X
        :returns: inner product at X between G and H, i.e. <G,H>_X
        """
        return G @ self.metric @ np.asanyarray(H).T

    def proj(self, X, A):
        """orthogonal (with respect to the euclidean inner product) projection of ambient
        vector (vectorized (2,k,3,3) array) onto the tangentspace at X"""
        # disentangle coords. into rotations and stretches
        R, U = self.disentangle(X)
        r, u = self.disentangle(A)

        # project in each component
        r = self.SO.proj(R, r)
        u = self.SPD.proj(U, u)

        return np.concatenate([r, u]).reshape(-1)

    def egrad2rgrad(self, X, D):
        """converts euclidean gradient(vectorized (2,k,3,3) array))
        into riemannian gradient, vectorized inputs!"""
        # disentangle coords. into rotations and stretches
        R, U = self.disentangle(X)
        r, u = self.disentangle(D)

        # componentwise
        r = self.SO.egrad2rgrad(R, r)
        u = self.SPD.egrad2rgrad(U, u)
        grad = np.concatenate([r, u]).reshape(-1)

        # multiply with inverse of metric
        grad /= self.metric.diagonal()

        return grad

    def exp(self, X, G):
        # disentangle coords. into rotations and stretches
        C, U = self.disentangle(X)
        c, u = self.disentangle(G)

        # alloc coords.
        Y = np.zeros_like(X)
        Cy, Uy = self.disentangle(Y)

        # exp C
        Cy[:] = self.SO.exp(C, c)
        # exp U (avoid additional exp/log)
        Uy[:] = self.SPD.exp(U, u)

        return Y

    def geopoint(self, X, Y, t):
        return self.exp(X, t * self.log(X, Y))

    retr = exp

    def log(self, X, Y):
        # disentangle coords. into rotations and stretches
        Cx, Ux = self.disentangle(X)
        Cy, Uy = self.disentangle(Y)

        # alloc tangent vector
        y = np.zeros(9 * int(0.5 * self.ref.inner_edges.getnnz()) + 4 * len(self.ref.f))
        c, u = self.disentangle(y)

        # log R1
        c[:] = self.SO.log(Cx, Cy)
        # log U (avoid additional log/exp)
        u[:] = self.SPD.log(Ux, Uy)

        return y

    def transp(self, X, Y, G):
        """
        :param X: element of the space of fundamental coordinates
        :param Y: element of the space of fundamental coordinates
        :param G: tangent vector at X
        :return: parallel transport of G along the geodesic from X to Y
        """
        # disentangle coords. into rotations and stretches
        Cx, Ux = self.disentangle(X)
        Cy, Uy = self.disentangle(Y)
        cx, ux = self.disentangle(G)

        # alloc coords.
        Y = np.zeros_like(X)
        cy, uy = self.disentangle(Y)

        cy[:] = self.SO.transp(Cx, Cy, cx)
        uy[:] = self.SPD.transp(Ux, Uy, ux)

        return Y

    def projToGeodesic(self, X, Y, P, max_iter = 10):
        '''
        :arg X, Y: fundamental coords defining geodesic X->Y.
        :arg P: fundamental coords to be projected to X->Y.
        :returns: fundamental coords of projection of P to X->Y
        '''

        assert X.shape == Y.shape
        assert Y.shape == P.shape

        # all tagent vectors in common space i.e. algebra
        v = self.log(X, Y)
        v /= self.norm(X, v)

        # initial guess
        Pi = X

        # solver loop
        for _ in range(max_iter):
            w = self.log(Pi, P)
            d = self.inner(Pi, v, w)

            # print(f'|<v, w>|={d}')
            if abs(d) < 1e-6: break

            Pi = self.exp(Pi, d * v)

        return Pi


    def jacop(self, X, Y, r):
        """ Evaluate the Jacobi operator along the geodesic from X to Y at r.

        For the definition of the Jacobi operator see:
            Rentmeesters, Algorithms for data fitting on some common homogeneous spaces, p. 74.

        :param X: element of the space of fundamental coordinates
        :param Y: element of the space of fundamental coordinates
        :param r: tangent vector at the rotational part of X
        :returns: skew-symmetric part of J_G(H)
        """
        v, w = self.disentangle(self.log(X, Y))
        w[:] = 0 * w
        v = 1 / 4 * (-np.einsum('...ij,...jk,...kl', v, v, r) + 2 * np.einsum('...ij,...jk,...kl', v, r, v)
                     - np.einsum('...ij,...jk,...kl', r, v, v))

        return v

    def jacONB(self, X, Y):
        """
        Let J be the Jacobi operator along the geodesic from X to Y. This code diagonalizes J. Note that J restricted
        to the Sym+ part is the zero operator.
        :param X: element of the space of fundamental coordinates
        :param Y: element of the space of fundamental coordinates
        :returns lam, G: eigenvalues and orthonormal eigenbasis of  the rotational part of J at X
        """
        Cx, Ux = self.disentangle(X)
        Cy, Uy = self.disentangle(Y)
        return self.SO.jacONB(Cx, Cy)

    def adjJacobi(self, X, Y, t, G):
        """
        Evaluates an adjoint Jacobi field along the geodesic gam from X to Z at X.
        :param X: element of the space of fundamental coordinates
        :param Y: element of the space of fundamental coordinates
        :param t: scalar in [0,1]
        :param G: tangent vector at gam(t)
        :return: tangent vector at X
        """

        # assert X.shape == Y.shape and X.shape == G.shape

        # disentangle coords. into rotations and stretches
        Cx, Ux = self.disentangle(X)
        Cy, Uy = self.disentangle(Y)

        c, u = self.disentangle(G)

        j = np.zeros_like(G)
        jr, js = self.disentangle(j)

        # SO(3) part
        jr[:] = self.SO.adjJacobi(Cx, Cy, t, c)
        # Sym+(3) part
        js[:] = self.SPD.adjJacobi(Ux, Uy, t, u)

        return j

    def adjDxgeo(self, X, Y, t, G):
        """Evaluates the adjoint of the differential of the geodesic gamma from X to Y w.r.t the starting point X at G,
        i.e, the adjoint  of d_X gamma(t; ., Y) applied to G, which is en element of the tangent space at gamma(t).
        """
        return self.adjJacobi(X, Y, t, G)

    def adjDygeo(self, X, Y, t, G):
        """Evaluates the adjoint of the differential of the geodesic gamma from X to Y w.r.t the endpoint Y at G,
        i.e, the adjoint  of d_Y gamma(t; X, .) applied to G, which is en element of the tangent space at gamma(t).
        """
        return self.adjJacobi(Y, X, 1 - t, G)

    def rand(self):
        v = self.zerovec()
        vr, vs = self.disentangle(v)
        vr[:] = self.SO.rand()
        vs[:] = self.SPD.rand()
        return v


    def setup_spanning_tree_path(self):
        """
        Setup a path across spanning tree of the refrence surface beginning at self.init_face.
        :return: n x 3 - array holding column wise an edge id and the respective neighbouring faces.
        """
        depth =[-1]*(len(self.ref.f))

        depth[self.init_face] = 0
        idcs = []
        idcs.append(self.init_face)

        spanningTreePath = []
        while idcs:
            idx = idcs.pop(0)
            d = depth[idx] + 1
            neighs = self.ref.inner_edges.getrow(idx).tocoo()

            for neigh, edge in zip(neighs.col, neighs.data):
                if depth[neigh] >= 0:
                    continue
                depth[neigh] = d
                idcs.append(neigh)

                spanningTreePath.append([edge, idx, neigh])
        return np.asarray(spanningTreePath)

    def setup_frame_field(self):
        """
        Compute frames for every face of the surface with some added pi(e).
        :return: n x 3 x 3 - array holding one frame for every face, column wise organized with c1, c2 tangential and c3 normal..
        """
        v1 = self.ref.v[self.ref.f[:, 2]] - self.ref.v[self.ref.f[:, 1]]
        v2 = self.ref.v[self.ref.f[:, 0]] - self.ref.v[self.ref.f[:, 2]]

        # orthonormal basis for face plane
        proj = np.divide(np.einsum('ij,ij->i', v2, v1), np.einsum('ij,ij->i', v1, v1))
        proj = sparse.diags(proj)

        v2 = v2 - proj @ v1

        # normalize and calculation of normal
        v1 = v1 / np.linalg.norm(v1, axis=1, keepdims=True)
        v2 = v2 / np.linalg.norm(v2, axis=1, keepdims=True)
        v3 = np.cross(v1, v2, axisa=1, axisb=1, axisc=1)

        # shape as n x 3 x 3 with basis vectors as cols
        frame = np.reshape(np.concatenate((v1, v2, v3), axis=1), [-1, 3, 3])
        frame = np.einsum('ijk->ikj', frame)

        return frame

    @staticmethod
    def frame_of_face(v, f, fId : int):
        """
        :arg fId: id of face to caluclate frame for
        :return: frame (colunm wise) with c1, c2 tangential and c3 normal.
        """
        v1 = v[f[fId, 2]] - v[f[fId, 1]]
        v2 = v[f[fId, 0]] - v[f[fId, 2]]

        # orthonormal basis for face plane
        v2 = v2 - (np.dot(v2, v1.T) / np.dot(v1, v1.T)) * v1

        # normalize and calculation of normal
        v1 = v1 / np.linalg.norm(v1)
        v2 = v2 / np.linalg.norm(v2)
        v3 = np.cross(v1, v2)

        return np.column_stack((v1.T, v2.T, v3.T))