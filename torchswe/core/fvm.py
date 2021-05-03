#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2020-2021 Pi-Yueh Chuang <pychuang@gwu.edu>
#
# Distributed under terms of the BSD 3-Clause license.

"""Linear reconstruction.
"""
import numpy
from .sources import topography_gradient
from .limiters import minmod_slope
from .reconstruction import get_discontinuous_cnsrv_q, correct_negative_depth
from .misc import decompose_variables, get_local_speed
from .flux import get_discontinuous_flux
from .numerical_flux import central_scheme
from ..utils.config import Config
from ..utils.data import States, Gridlines, Topography
from ..utils.dummydict import DummyDict


def fvm(states: States, grid: Gridlines, topo: Topography, config: Config, runtime: DummyDict):
    """Get the right-hand-side of a time-marching step with finite volume method.

    Arguments
    ---------
    states : torchswe.utils.data.States
    grid : torchswe.utils.data.Gridlines
    topo : torchswe.utils.data.Topography
    config : torchswe.utils.config.Config
    runtime : torchswe.utils.dummydict.DummyDict

    Returns:
    --------
    states : torchswe.utils.data.States
        The same object as the input. Updated in-place. Returning it just for coding style.
    max_dt : float
        A scalar indicating the maximum safe time-step size.
    """
    # pylint: disable=invalid-name

    # calculate source term contributed from topography gradients
    states = topography_gradient(states, topo, config.params.gravity)

    # calculate slopes of piecewise linear approximation
    states = minmod_slope(states, grid, config.params.theta, runtime.tol)

    # interpolate to get discontinuous conservative quantities at cell faces
    states = get_discontinuous_cnsrv_q(states, grid)

    # fix non-physical negative depth
    states = correct_negative_depth(states, topo)

    # get non-conservative variables at cell faces
    states = decompose_variables(states, topo, runtime.epsilon)

    # get local speed at cell faces
    states = get_local_speed(states, config.params.gravity)

    # get discontinuous PDE flux at cell faces
    states = get_discontinuous_flux(states, topo, config.params.gravity)

    # get common/continuous numerical flux at cell faces
    states = central_scheme(states, runtime.tol)

    # get final right hand side
    states.rhs.w = \
        (states.face.x.num_flux.w[:, :-1] - states.face.x.num_flux.w[:, 1:]) / grid.x.delta + \
        (states.face.y.num_flux.w[:-1, :] - states.face.y.num_flux.w[1:, :]) / grid.y.delta + \
        states.src.w

    states.rhs.hu = \
        (states.face.x.num_flux.hu[:, :-1] - states.face.x.num_flux.hu[:, 1:]) / grid.x.delta + \
        (states.face.y.num_flux.hu[:-1, :] - states.face.y.num_flux.hu[1:, :]) / grid.y.delta + \
        states.src.hu

    states.rhs.hv = \
        (states.face.x.num_flux.hv[:, :-1] - states.face.x.num_flux.hv[:, 1:]) / grid.x.delta + \
        (states.face.y.num_flux.hv[:-1, :] - states.face.y.num_flux.hv[1:, :]) / grid.y.delta + \
        states.src.hv

    # remove rounding errors
    ji = numpy.nonzero(numpy.logical_and(states.rhs.w > -runtime.tol, states.rhs.w < runtime.tol))
    states.rhs.w[ji] = 0.

    ji = numpy.nonzero(numpy.logical_and(states.rhs.hu > -runtime.tol, states.rhs.hu < runtime.tol))
    states.rhs.hu[ji] = 0.

    ji = numpy.nonzero(numpy.logical_and(states.rhs.hv > -runtime.tol, states.rhs.hv < runtime.tol))
    states.rhs.hv[ji] = 0.

    # obtain the maximum safe dt
    amax = numpy.max(numpy.maximum(states.face.x.plus.a, -states.face.x.minus.a))
    bmax = numpy.max(numpy.maximum(states.face.y.plus.a, -states.face.y.minus.a))
    max_dt = min(0.25*grid.x.delta/amax, 0.25*grid.y.delta/bmax)

    return states, max_dt