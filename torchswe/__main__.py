#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2020-2021 Pi-Yueh Chuang <pychuang@gwu.edu>
#
# Distributed under terms of the BSD 3-Clause license.

"""Main function.
"""
# pylint: disable=wrong-import-position

import time as wtime
from torchswe.utils.timing import time
import logging
import pathlib
import argparse
import pickle as pkl

# due to openmpi's problematic implementation of one-sided communication

from torchswe import nplike
from torchswe import is_backend_cunumeric
from torchswe.utils.data import get_timeline
from torchswe.utils.data import get_topography, get_custom_topography
from torchswe.utils.data import get_pointsource
from torchswe.utils.data import get_frictionmodel
from torchswe.utils.data import get_initial_states
from torchswe.utils.misc import DummyDict
from torchswe.utils.misc import exchange_states
from torchswe.utils.io import write_snapshot
from torchswe.utils.io import read_snapshot
from torchswe.utils.io import dump_solution
from torchswe.utils.config import get_config
from torchswe.kernels import reconstruct_cell_centers
from torchswe.bcs import setup_bc 
from torchswe.temporal import euler, ssprk2, ssprk3
from torchswe.sources import topography_gradient, point_mass_source, friction, zero_stiff_terms

# enforce print precision
nplike.set_printoptions(precision=15, linewidth=200)

# available time marching options
MARCHING_OPTIONS = {"Euler": euler, "SSP-RK2": ssprk2, "SSP-RK3": ssprk3}  # available options
    
def get_cmd_arguments(argv=None):
    """Parse and get CMD arguments.

    Attributes
    ----------
    argv : list or None
        By default, None means using `sys.argv`. Only explicitly use this argument for debugging.

    Returns
    -------
    args : argparse.Namespace
        CMD arguments.
    """

    # parse command-line arguments
    parser = argparse.ArgumentParser(
        prog="TorchSWE",
        description="GPU shallow-water equation solver utilizing Legate",
        epilog="Website: https://github.com/piyueh/TorchSWE",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False
    )

    parser.add_argument(
        "case_folder", metavar="PATH", action="store", type=pathlib.Path,
        help="The path to a case folder."
    )

    parser.add_argument(
        "--continue", action="store", type=float, default=None, metavar="TIME", dest="cont",
        help="Indicate this run should continue from this time point."
    )

    parser.add_argument(
        "--sp", action="store_true", dest="sp",
        help="Use single precision instead of double precision floating numbers"
    )

    parser.add_argument(
        "--tm", action="store", type=str, choices=["SSP-RK2", "SSP-RK3", "Euler"], default=None,
        help="Overwrite the time-marching scheme. Default is to respect the setting in config.yaml."
    )

    parser.add_argument(
        "--log-steps", action="store", type=int, default=None, metavar="STEPS",
        help="How many steps to output a log message to stdout. Default is to respect config.yaml."
    )

    parser.add_argument(
        "--log-level", action="store", type=str, default="normal", metavar="LEVEL",
        choices=["debug", "normal", "quiet"],
        help="Enabling logging debug messages."
    )

    parser.add_argument(
        "--log-file", action="store", type=pathlib.Path, default=None, metavar="FILE",
        help="Saving log messages to a file instead of stdout."
    )

    parser.add_argument(
        "--nx", action="store", type=int, default=-1, metavar="NX",
        help="Number of points in x-direction (overrides nx from config file)"
    )

    parser.add_argument(
        "--ny", action="store", type=int, default=-1, metavar="NY",
        help="Number of points in y-direction (overrides ny from config file)"
    )

    parser.add_argument(
        "--dt", action="store", type=float, default=-1, metavar="DT",
        help="Time step size (overrides dt from config file)"
    )

    parser.add_argument(
        "--log-file-mode", action="store", type=str, default="w", metavar="a or w",
        help="Logging file handler mode (a: append, w:write)"
    )

    args, extra = parser.parse_known_args(argv)

    # make sure the case folder path is absolute
    if args.case_folder is not None:
        args.case_folder = args.case_folder.expanduser().resolve()

    # convert log level from string to corresponding Python type
    level_options = {"quiet": logging.ERROR, "normal": logging.INFO, "debug": logging.DEBUG}
    args.log_level = level_options[args.log_level]

    # make sure the file path is absolute
    if args.log_file is not None:
        args.log_file = args.log_file.expanduser().resolve()

    if args.log_file_mode not in ["w", "a"]:
        args.log_file_mode = "w"

    return args


def get_final_config(args: argparse.Namespace):
    """Get a Config object with values overwritten by CMD options.

    Arguments
    ---------
    args : argparse.Namespace
        The result of parsing command-line arguments.

    Returns
    -------
    config : torchswe.utils.config.Config
    """

    args.case_folder = args.case_folder.expanduser().resolve()

    # read yaml config file (using the get_config from torchswe.utils.config)
    config = get_config(args.case_folder)

    if args.nx > 0 and args.ny > 0:
        config.spatial.discretization = [int(args.nx), int(args.ny)]
    elif args.nx > 0 and args.ny < 0:
        ny = config.spatial.discretization[1]
        config.spatial.discretization = [int(args.nx), int(ny)]
    elif args.nx < 0 and args.ny > 0:
        nx = config.spatial.discretization[0]
        config.spatial.discretization = [int(nx), int(args.ny)]

    if args.dt > 0:
        config.temporal.dt = args.dt

    # add args to config
    config.case = args.case_folder

    config.params.dtype = "float32" if args.sp else config.params.dtype  # overwrite dtype if needed

    if args.log_steps is not None:  # overwrite log_steps if needed
        config.params.log_steps = args.log_steps

    if args.tm is not None:  # overwrite the setting in config.yaml
        config.temporal.scheme = args.tm

    # if topo filepath is relative, change to abs path
    config.topo.file = config.topo.file.expanduser()
    if not config.topo.file.is_absolute():
        config.topo.file = config.case.joinpath(config.topo.file).resolve()

    # if ic filepath is relative, change to abs path
    if config.ic.file is not None:
        config.ic.file = config.ic.file.expanduser()
        if not config.ic.file.is_absolute():
            config.ic.file = config.case.joinpath(config.ic.file).resolve()

    # if filepath of the prehook script is relative, change to abs path
    if config.prehook is not None:
        config.prehook = config.prehook.expanduser()
        if not config.prehook.is_absolute():
            config.prehook = config.case.joinpath(config.prehook).resolve()

    # validate data again
    config.check()

    return config


def get_logger(filename, file_mode: str, level, mpi_size, mpi_rank):
    """Get a logger based on the debug level and whether to use log files.

    Arguments
    ---------
    filename : str or os.PathLike
    file_mode: str (a or w)
    level : int
    mpi_size : int
    mpi_rank : int

    Returns
    -------
    logging.Logger
    """

    # setup the top-level (i.e., package-level/torchswe) logger
    logger = logging.getLogger("torchswe")

    if filename is not None:
        # different ranks write to different log files
        if mpi_size != 1:
            filename = filename.with_name(filename.name+f".proc.{mpi_rank:02d}")

        fmt = "%(asctime)s %(name)s %(funcName)s [%(levelname)s] %(message)s"  # format
        logger.setLevel(level)
        logger.addHandler(logging.FileHandler(filename, file_mode))
        logger.handlers[-1].setFormatter(logging.Formatter(fmt, "%m-%d %H:%M:%S"))
    else:
        if level == logging.INFO:
            if mpi_rank == 0:
                logger.setLevel(logging.INFO)
            else:
                logger.setLevel(logging.WARNING)
        else:
            logger.setLevel(level)

        fmt = f"[Rank {mpi_rank}] %(asctime)s %(message)s"
        logger.addHandler(logging.StreamHandler())
        logger.handlers[-1].setFormatter(logging.Formatter(fmt, "%H:%M:%S"))

    # make the final & returned logger refer to this specific file (main function)
    logger = logging.getLogger("torchswe.main")

    return logger


def get_runtime(config, logger):
    """Get a runtime object.
    """

    # get initial solution object
    states = get_initial_states(config)  # let the function create a new Domain in States
    logger.info("Obtained an initial solution object")

    # `runtime` holds things not available in config.yaml or may change during runtime
    runtime = DummyDict()  # it's just a dict and not a data model. so, no data validation

    # get temporal axis
    runtime.times = get_timeline(config)
    logger.info("Obtained a Timeline object")

    # get dem (digital elevation model); reuse the Domain object in the State object
    #runtime.topo = get_topography(config, states.domain)
    runtime.topo = get_custom_topography(config, states.domain)
    logger.info("Obtained a Topography object")

    # make sure initial depths are non-negative
    states.q[(0,)+states.domain.nonhalo_c] = nplike.maximum(
        runtime.topo.c[states.domain.nonhalo_c], states.q[(0,)+states.domain.nonhalo_c])
    states.check()

    runtime.cfl = 1.0
    logger.info("CFL limit: %e", runtime.cfl)

    runtime.dt = config.temporal.dt  # time step size; may be changed during runtime
    logger.info("Initial dt: %e", runtime.dt)

    # this dt array will be used if config.params.allow_async is True 
    # we assume CFL to be one for this right now
    if not config.params.allow_async:
        runtime.dt_array = None
    else:
        ny, nx = states.domain.shape
        runtime.dt_array = nplike.full((ny, nx), runtime.dt, dtype=config.params.dtype)

    runtime.dt_constraint = float("inf")
    logger.info("Initial dt constraint: %e", runtime.dt_constraint)

    runtime.tidx = 0
    logger.info("Initial output time index: %d", runtime.tidx)

    runtime.cur_t = runtime.times[0]  # the current simulation time
    logger.info("Initial t: %e", runtime.cur_t)

    runtime.next_t = runtime.times[1]
    logger.info("The next t: %s", runtime.next_t)

    runtime.counter = 0  # to count the current number of iterations
    logger.info("The current iteration counter: %d", runtime.counter)

    runtime.tol = 1e-12  # up to how big can be treated as zero
    logger.info("Tolerance: %e", runtime.tol)

    runtime.outfile = config.case.joinpath("solutions.h5")  # solution file
    logger.info("Output solution file: %s", str(runtime.outfile))

    runtime.marching = MARCHING_OPTIONS[config.temporal.scheme]  # time marching scheme
    logger.info("Time marching scheme: %s", config.temporal.scheme)

    runtime.gh_updater = setup_bc(states, runtime.topo, config)

    #runtime.gh_updater = get_ghost_cell_updaters(states, runtime.topo, config.bc)
    logger.info("Done setting ghost cell updaters")

    runtime.sources = [topography_gradient]
    logger.info("Explicit source term: topography gradients")

    # create an array for gravity (this is to avoid allocation of Futures)
    ny, nx = states.domain.shape
    runtime.gravity_array = nplike.full((ny, nx), config.params.gravity, dtype=config.params.dtype)

    if config.ptsource is not None:
        # add the function of calculating point source
        runtime.ptsource = get_pointsource(config, 0, states.domain)
        runtime.sources.append(point_mass_source)
        logger.info("Explicit source term: point source")

    runtime.stiff_sources = []
    if config.friction is not None:
        runtime.friction = get_frictionmodel(config, states.domain)
        runtime.stiff_sources.append(zero_stiff_terms)  # zerofy the ss array in states
        runtime.stiff_sources.append(friction)
        logger.info("Friction fucntion added to stiff source terms")
        logger.info("Friction coefficient model: %s", config.friction.model)

    return states, runtime


def init(args=None):
    """Initialize a simulation.

    Attributes
    ----------
    args : None or argparse.Namespace
        By default, None means getting arguments from command-line. Only use this for debugging.

    Returns
    -------
    args : argparse.Namespace
        The CMD option values.
    config : a torchswe.utils.config.Config
        A Config instance holding a case's simulation configurations.
    logger : logging.Logger
        Python's logging utility object.
    states : torchswe.utils.data.states.States
        Data model holding solutions/states.
    runtime : torchswe.utils.misc.DummyDict
        A dictionary-like object holding auxiliary data/information required during runtime.
    """

    # TODO: update size & rank from input args
    size = 1
    rank = 0

    # get cmd arguments
    args = get_cmd_arguments() if args is None else args

    # setup the top-level (i.e., package-level/torchswe) logger
    logger = get_logger(args.log_file, args.log_file_mode, args.log_level, size, rank)

    # get configuration
    config = get_final_config(args)

    # print 
    s = "\nConfiguration:\n" + "-"*60 + "\n"
    logger.info(s)
    logger.info(config)
    s = "\n" + "-"*60 + "\n"
    logger.info(s)

    # get states and runtime data holder
    states, runtime = get_runtime(config, logger)

    return args, config, logger, states, runtime


def restart(states, runtime, config, cont, logger):
    """Update data if we're continue from a previous solution."""

    if cont is None:  # not restarting
        return states, runtime

    try:
        runtime.tidx = runtime.times.values.index(cont)
        logger.info("Restart from output time index and time: %d, %f", runtime.tidx, cont)
    except ValueError as err:
        if "not in tuple" not in str(err):  # other kinds of ValueError
            raise
        raise ValueError(
            f"Target restarting time {cont} was not found among {runtime.times.values}"
        ) from err

    # update current and the next time
    runtime.cur_t = cont
    runtime.next_t = runtime.times.values[runtime.tidx+1]
    logger.info("The next output time: %s", runtime.next_t)

    # make the counter non-zero to avoid some functions using counter == 0 as condition
    runtime.counter = 1
    logger.info("The iteration counter starts from: %d", runtime.counter)

    # update initial solution and point source info
    states, runtime = read_snapshot(states, runtime, config)
    logger.info("Initial solution reset to T=%e", runtime.cur_t)

    if "ptsource" in runtime and runtime.ptsource is not None:
        logger.info("Point source reset: %s", runtime.ptsource)

    return states, runtime


def main():
    """Main function."""

    assert is_backend_cunumeric(), "Only cuNumeric backend is supported in this branch." 

    # initialize
    args, config, logger, soln, runtime = init()
    logger.info("Done initialization.")

    # log the backend
    logger.info("The np-like backend is: %s", nplike.__name__)

    # update data if this is a continued run
    soln, runtime = restart(soln, runtime, config, args.cont, logger)

    logger.info('disabling runtime.times.save')
    runtime.times.save = False 

    # create an NetCDF file and append I.C.
    if runtime.times.save and runtime.tidx == 0:
        soln = exchange_states(soln)
        soln = reconstruct_cell_centers(soln, runtime, config)
        soln = write_snapshot(soln, runtime, config)
        logger.info("Done writing the current states to %s", runtime.outfile)
    else:
        logger.info("No need to save data for \"no save\" method or for a continued run.")

    # Don't time the warmup iteration
    if config.params.warmup > 0:
        max_iters = config.temporal.max_iters
        assert max_iters >= config.params.warmup
        config.temporal.max_iters = config.params.warmup
        t0 = time()
        soln = runtime.marching(soln, runtime, config)
        t1 = time()
        logger.info("Warmup time (wall time): %s seconds", (t1 - t0)/1e6)
        config.temporal.max_iters = max_iters - config.params.warmup

    perf_t0 = time()
    # start running time marching until each output time
    for runtime.next_t in runtime.times[runtime.tidx+1:]:
        logger.info("Marching from T=%s to T=%s", runtime.cur_t, runtime.next_t)
        soln = runtime.marching(soln, runtime, config)

        # sanity check for the current time
        # SJ; Wed 14 Dec 2022 04:00:13 PM PST
        # assert abs(runtime.next_t-runtime.cur_t) < 1e-10

        # update tidx
        runtime.tidx += 1

        # append to the NetCDF file
        if runtime.times.save:
            soln = write_snapshot(soln, runtime, config)
            logger.info("Done writing the states at T=%s to the solution file.", runtime.next_t)

    logger.info("Done time marching.")
    logger.info("Run time (wall time): %s seconds", (time()-perf_t0)/1e6)
    logger.info("Program ends now.")

    # dump mesh and solution variables to a pickle file if requested
    dump_solution(soln, runtime, config, filename='end.pkl')

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
