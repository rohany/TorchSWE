#! /usr/bin/env python3
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2021 Pi-Yueh Chuang <pychuang@gwu.edu>
#
# Distributed under terms of the BSD 3-Clause license.

"""Objects holding simulation configuraions.
"""
import pathlib as _pathlib
from typing import Literal as _Literal
from typing import Tuple as _Tuple
from typing import Union as _Union
from typing import Optional as _Optional
from yaml import load as _load
from yaml import Loader as _Loader
from yaml import add_constructor as _add_constructor
from yaml import add_representer as _add_representer
from pydantic import BaseModel as _BaseModel
from pydantic import Field as _Field
from pydantic import validator as _validator
from pydantic import root_validator as _root_validator
from pydantic import conint as _conint
from pydantic import confloat as _confloat
from pydantic import validate_model as _validate_model


# alias to type hints
BCTypeHint = _Literal["periodic", "extrap", "const", "inflow", "outflow"]

OutputTypeHint = _Union[
    _Tuple[_Literal["at"], _Tuple[_confloat(ge=0), ...]],
    _Tuple[
        _Literal["t_start every_seconds multiple"],
        _confloat(ge=0), _confloat(gt=0), _conint(ge=1)
    ],
    _Tuple[_Literal["t_start every_steps multiple"], _confloat(ge=0), _conint(ge=1), _conint(ge=1)],
    _Tuple[_Literal["t_start t_end n_saves"], _confloat(ge=0), _confloat(gt=0), _conint(ge=1)],
    _Tuple[_Literal["t_start t_end no save"], _confloat(ge=0), _confloat(gt=0)],
    _Tuple[_Literal["t_start n_steps no save"], _confloat(ge=0), _conint(ge=1)],
]

TemporalTypeHint = _Literal["Euler", "SSP-RK2", "SSP-RK3"]


class BaseConfig(_BaseModel):
    """Extending pydantic.BaseModel with __getitem__ method."""

    class Config:  # pylint: disable=too-few-public-methods
        """pydantic configuration of this model."""
        validate_all = True
        allow_population_by_field_name = True
        arbitrary_types_allowed = True
        extra = "forbid"

    def __getitem__(self, key):
        return super().__getattribute__(key)

    def __setitem__(self, key, value):
        self.__setattr__(key, value)

    def __str__(self):
        s = "\n"
        for k, v in self.__dict__.items():
            s += "{:<15}: {}\n".format(k, v)
        return s

    def check(self):
        """Manually trigger the validation of the data in this instance."""
        _, _, validation_error = _validate_model(self.__class__, self.__dict__)

        if validation_error:
            raise validation_error

        for field in self.__dict__.values():
            if isinstance(field, BaseConfig):
                field.check()


class SpatialConfig(BaseConfig):
    """An object holding spatial configuration.

    Attributes
    ----------
    domain : a list/tuple of 4 floats
        The elements correspond the the bounds in west, east, south, and north.
    discretization : a list/tuple of 2 int
        The elements correspond the number of cells in west-east and south-north directions.
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    domain: _Tuple[float, float, float, float]
    discretization: _Tuple[_conint(strict=True, gt=0), _conint(strict=True, gt=0)]

    @_validator("domain")
    def domain_direction(cls, v):
        """Validate the East >= West and North >= South.
        """
        assert v[1] > v[0], "domain[1] must greater than domain[0]"
        assert v[3] > v[2], "domain[3] must greater than domain[2]"
        return v


class TemporalConfig(BaseConfig):
    """An object holding temporal configuration.

    Attributes
    ----------
    start : float
        The start time of the simulation, i.e., the time that the initial conditions are applied.
    end : float
        The end time of the simulation, i.e., the simulation stops when reaching this time.
    output : list/tuple or None
        Three available formats:
            1. ["at", [t1, t2, t3, t4, ...]]:
                saves solutions at t1, t2, t3, ..., etc.
            2. ["t_start every_seconds multiple", t0, dt, n]:
                starting from t0, saves solutions every dt seconds for n times.
            3. ["t_start every_steps multiple", t0, n0, n1]:
                starting from t0, saves solutions every n0 time steps for n1 times
            4. ["t_start t_end n_saves", t0, t1, n]:
                starting from t0, evenly saves n solutions up to time t1.
            5. ["t_start t_end no save", t0, t1]:
                runs the simulation from t0 to t1 without saving any solutions.
            6. ["t_start n_steps no save", t0, n]:
                runs the simulation from t0 and runs for n steps without saving any solutions.
        Default: None
    scheme : str
        Currently, either "Euler", "RK2", or "RK4". Default: "RK2"
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    dt: _confloat(gt=0.) = 1e-3
    adaptive: bool = True
    output: OutputTypeHint
    max_iters: _conint(gt=0) = _Field(1000000, alias="max iterations")
    scheme: TemporalTypeHint = "SSP-RK2"

    @_validator("output")
    def _val_output_method(cls, v, values):
        """Validate that end time > start time."""

        if v[0] == "at":
            msg = "Times are not monotonically increasing"
            assert all(v[1][i] > v[1][i-1] for i in range(1, len(v[1]))), msg
        elif v[0] in ["t_start every_steps multiple", "t_start n_steps no save"]:
            assert not values["adaptive"], "Needs \"adaptive=False\"."
        elif v[0] in ["t_start t_end n_saves", "t_start t_end no save"]:
            assert v[2] > v[1], "End time is not greater than start time."

        return v

    @_validator("max_iters")
    def _val_max_iters(cls, v, values):
        """Validate and modify max_iters."""
        try:
            if values["output"][0] in ["t_start every_steps multiple", "t_start n_steps no save"]:
                v = values["output"][2]  # use per_step as max_iters
        except KeyError as err:
            raise AssertionError("Fix `output` first") from err
        return v


class SingleBCConfig(BaseConfig):
    """An object holding configuration of the boundary conditions on a single boundary.

    Attributes
    ----------
    types : a length-3 tuple/list of str
        Boundary conditions correspond to the three conservative quantities. If the type is
        "inflow", they correspond to non-conservative quantities, i.e., u and v. Applying "inflow"
        to depth h or elevation w seems not be make any sense.
    values : a length-3 tuple of floats or None
        Some BC types require user-provided values (e.g., "const"). Use this to give values.
        Usually, they are the conservative quantities, i.e., w, hu, and hv. For "inflow", however,
        they are non-conservative quantities, i.e., u and v. Defautl: [None, None, None]
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    types: _Tuple[BCTypeHint, BCTypeHint, BCTypeHint]
    values: _Tuple[_Optional[float], _Optional[float], _Optional[float]] = [None, None, None]

    @_validator("types")
    def check_periodicity(cls, v):
        """If one component is periodic, all components should be periodic."""
        if any(t == "periodic" for t in v):
            assert all(t == "periodic" for t in v), "All components should be periodic."
        return v

    @_validator("values")
    def check_values(cls, v, values):
        """Check if values are set accordingly for some BC types.
        """
        if "types" not in values:
            return v

        for bctype, bcval in zip(values["types"], v):
            if bctype in ("const", "inflow"):
                assert isinstance(bcval, float), \
                    f"Using BC type \"{bctype.value}\" requires setting a value."
        return v


class BCConfig(BaseConfig):
    """An object holding configuration of the boundary conditions of all boundaries.

    Attributes
    ----------
    west, east, north, south : SingleBCConfig
        Boundary conditions on west, east, north, and south boundaries.
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    west: SingleBCConfig
    east: SingleBCConfig
    north: SingleBCConfig
    south: SingleBCConfig

    @_root_validator(pre=False)
    def check_periodicity(cls, values):
        """Check whether periodic BCs match at corresponding boundary pairs."""
        if any((t not in values) for t in ["west", "east", "south", "north"]):
            return values

        result = True
        for types in zip(values["west"]["types"], values["east"]["types"]):
            if any(t == "periodic" for t in types):
                result = all(t == "periodic" for t in types)
        for types in zip(values["north"]["types"], values["south"]["types"]):
            if any(t == "periodic" for t in types):
                result = all(t == "periodic" for t in types)
        if not result:
            raise ValueError("Periodic BCs do not match at boundaries and components.")
        return values


class ICConfig(BaseConfig):
    """An object holding configuration of the initial conditions.

    Attributes
    ----------
    file : None or str or path-like object
        The path to a NetCDF file containing IC data.
    keys : None or a tuple/list of str
        The variable names in the `file` that correspond to w, hu, and hv. If `file` is None, this
        can be None.
    values : None or a tuple/list of floats
        If `file` is None, use this attribute to specify constant IC values.
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    file: _Optional[_pathlib.Path]
    keys: _Optional[_Tuple[str, str, str]]
    xykeys: _Optional[_Tuple[str, str]]
    values: _Optional[_Tuple[float, float, float]]

    @_root_validator(pre=True)
    def check_mutually_exclusive_attrs(cls, values):
        """\"file\" and \"values" should be mutually exclusive.
        """
        if "file" in values and values["file"] is not None:
            if "values" in values and values["values"] is not None:
                raise AssertionError("Only one of \"file\" or \"values\" can be set for I.C.")

            if "keys" not in values or values["keys"] is None:
                raise AssertionError("\"keys\" has to be set when \"file\" is not None for I.C.")

            if "xykeys" not in values or values["keys"] is None:
                raise AssertionError("\"xykeys\" has to be set when \"file\" is not None for I.C.")
        else:  # "file" is not specified or is None
            if "values" not in values or values["values"] is None:
                raise AssertionError("Either \"file\" or \"values\" has to be set for I.C.")

        return values


class TopoConfig(BaseConfig):
    """An object holding configuration of the topography file.

    Attributes
    ----------
    file : str or path-like object
        The path to a NetCDF file containing topography data.
    key : str
        The variable name in the `file` that corresponds to elevation data.
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    file: _pathlib.Path
    key: str
    xykeys: _Tuple[str, str]


class PointSourceConfig(BaseConfig):
    """An object holding configuration of point sources.

    Attributes
    ----------
    loc : a tuple of two floats
        The coordinates of the point source.
    times : a tuple of floats
        Times to change flow rates.
    rates : a tiple of floats
        Flow rates to use during specified time intervals.
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    loc: _Tuple[_confloat(strict=True), _confloat(strict=True)] = _Field(..., alias="location")
    times: _Tuple[_confloat(strict=True), ...]
    rates: _Tuple[_confloat(strict=True, ge=0.), ...]
    init_dt: _confloat(strict=True, gt=0.) = _Field(1e-3, alias="initial dt")

    @_validator("times")
    def val_times(cls, val):
        """Validate the tuple of times."""
        for i in range(1, len(val)):
            assert val[i] - val[i-1] > 0., f"{val[i]} is not greater than {val[i-1]}"
        return val

    @_validator("rates")
    def val_rates(cls, val, values):
        """Validate the tuple of rates."""
        try:
            target = values["times"]
        except KeyError as err:
            raise AssertionError("must correct `times` first") from err

        assert len(val) == len(target) + 1, \
            f"the length of rates ({len(val)}) does not match that of times ({len(target)})"

        return val


class ParamConfig(BaseConfig):
    """An object holding configuration of miscellaneous parameters.

    Attributes
    ----------
    gravity : float
        Gravity in m^2/sec. Default: 9.81
    theta : float
        Parameter controlling numerical dissipation. 1.0 < theta < 2.0. Default: 1.3
    drytol : float
        Dry tolerance in meters. Default: 1.0e-4.
    ngh : int
        Number of ghost cell layers per boundary. At least 2 required.
    dtype : str
        The floating number type. Either "float32" or "float64". Default: "float64"
    allow_async: bool
        If set to True, this will replace some operations that block runtime
        with an alternative that allows the runtime to proceed further.
        For e.g., min() computation in CFL computation will be replaced by a fixed dt
    vectorize_bc: bool
        If set to True, this will vectorize ghost region updates if possible
    dump_sol_at_end: bool
        If set to True, this will dump the soln to a pickle file at the end
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    gravity: _confloat(ge=0.) = 9.81
    theta: _confloat(ge=1., le=2.) = 1.3
    drytol: _confloat(ge=0.) = _Field(1.0e-4, alias="dry tolerance")
    ngh: _conint(ge=2) = 2
    allow_async: bool = _Field(False, alias="allow async") 
    vectorize_bc: bool = _Field(True, alias="vectorize bc")
    dump_solution: bool = _Field(False, alias="dump solution")
    log_steps: _conint(ge=1) = _Field(100000000000, alias="print steps")
    dtype: _Literal["float32", "float64"] = "float64"
    warmup: int = _Field(0, alias="warmup iterations")

    @_validator("ngh")
    def _val_ngh(cls, val):
        """Currently only support ngh=2"""
        assert val == 2, "Currently, the solver only supports ngh = 2"
        return val


class FluidPropsConfig(BaseConfig):
    """An object holding configuration of fluid properties.

    Attributes
    ----------
    ref_mu : float
        A reference dynamic viscosity in unit mPa-s (= cP = 1e-3 kg/s/m)
    ref_temp : float
        The reference temperature at which the `ref_mu` is defined. Unit: Celsius.
    amb_temp : float
        The ambiant temperature at which the simulation operates. Unit: Celsius.
    rho : float
        The density of fluid at `amb_temp`. Unit: kg/m^3
    nu : float
        The kinematic viscosity at `amb_temp`. Unit: m^2/s
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    rho: _confloat(strict=True, gt=0.) = _Field(..., alias="density")
    ref_mu: _confloat(strict=True, gt=0.) = _Field(..., alias="reference mu")
    ref_temp: _confloat(strict=True, gt=-273.15) = _Field(..., alias="reference temperature")
    amb_temp: _confloat(strict=True, gt=-273.15) = _Field(..., alias="ambient temperature")
    nu : _Optional[_confloat(strict=True, gt=0.)] = _Field(None)

    @_validator("nu")
    def val_nu(cls, val, values):
        """Validate nu."""
        if val is None:
            try:
                # get dynamic viscosity at ambient temperature (unit: cP) (Lewis-Squires formula)
                val = values["ref_mu"]**(-0.2661) + (values["amb_temp"] - values["ref_temp"]) / 233.
                val = val**(-1./0.2661) * 1e-3 # convert to kg / s / m
                val /= values["rho"]  # kinematic viscosity (m^2 / s)
            except KeyError as err:
                raise AssertionError("Correct other errors first.") from err
        return val


class FrictionConfig(BaseConfig):
    """An object holding configuration of bottom friction.

    Attributes
    ----------
    file : path-like or None
        The CF-compliant NetCDF file containing surface roughness data.
    key : str or None
        The key of the roughness data in the file.
    value : float or None
        A constant roughness for the whole computational domain. See notes.
    model : str
        The friction coefficient model. Currently, only "bellos_et_al_2018" is available.

    Notes
    -----
    Only one of the `file`-`key` pair or `value` can be non-None at the same time.
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use
    file: _Optional[_pathlib.Path] = _Field(None, alias="roughness file")
    key: _Optional[str] = _Field(None, alias="roughness key")
    xykeys: _Optional[_Tuple[str, str]] = _Field(None, alias="roughness xykeys")
    value: _Optional[_confloat(strict=True, ge=0.)] = _Field(None, alias="roughness")
    model: _Literal["bellos_et_al_2018"] = _Field("bellos_et_al_2018", alias="coefficient model")

    @_validator("value")
    def val_value(cls, val, values):
        """Validate FrictionConfig.value"""
        try:
            if val is None:
                msg = "when not using constant roughness, {} must be set"
                assert values["file"] is not None, msg.format("roughness file")
                assert values["key"] is not None, msg.format("roughness key")
                assert values["xykeys"] is not None, msg.format("xykeys")
            else:
                msg = "when using constant roughness, {} must not be set"
                assert values["file"] is None, msg.format("roughness file")
                assert values["key"] is None, msg.format("roughness key")
                assert values["xykeys"] is None, msg.format("xykeys")
        except KeyError as err:
            raise AssertionError("Please fix other fields first.") from err
        return val


class ProbesConfig(BaseConfig):
    """An object holding configuration of probes.

    Attributes
    ----------
    """
    locs: _Tuple[_Tuple[_conint(ge=0), _Tuple[float, float]], ...] = None


class Config(BaseConfig):
    """An object holding all configurations of a simulation case.

    Attributes
    ----------
    spatial : SpatialConfig
        Spatial information.
    temporal : TemporalConfig
        Temporal control.
    bc : BCConfig
        Boundary conditions.
    ic : ICConfig
        Initial conditions.
    topo : TopoConfig
        Topography information.
    ptsource : PointSourceConfig
        Point source configuration.
    props : FluidProps
        Fluid properties.
    params : ParamConfig
        Miscellaneous parameters.
    prehook : None or path-like
        The path to a Python script that will be executed before running a simulation.
    case : path-like
        The path to the case folder.
    """
    # pylint: disable=too-few-public-methods, no-self-argument, invalid-name, no-self-use

    spatial: SpatialConfig
    temporal: TemporalConfig
    bc: BCConfig = _Field(..., alias="boundary")
    ic: ICConfig = _Field(..., alias="initial")
    topo: TopoConfig = _Field(..., alias="topography")
    ptsource: _Optional[PointSourceConfig] = _Field(None, alias="point source")
    friction: _Optional[FrictionConfig] = _Field(None, alias="friction")
    probes: _Optional[ProbesConfig] = _Field(None, alias="probes")
    props: _Optional[FluidPropsConfig] = _Field(None, alias="fluid properties")
    params: ParamConfig = _Field(ParamConfig(), alias="parameters")
    prehook: _Optional[_pathlib.Path]
    case: _Optional[_pathlib.Path]

    @_validator("props")
    def val_props(cls, val, values):
        """Validate props."""
        try:
            if values["ptsource"] is not None and val is None:
                raise AssertionError("When `point source` presents, `fluid properties` must be set")
            if values["friction"] is not None and val is None:
                raise AssertionError("When `friction` presents, `fluid properties` must be set")
        except KeyError as err:
            raise AssertionError("Please fix other fields first.") from err
        return val


# register the Config class in yaml with tag !Config
_add_constructor(
    "!Config",
    lambda loader, node: Config(**loader.construct_mapping(node, deep=True))
)

_add_representer(
    Config,
    lambda dumper, data: dumper.represent_mapping(
        tag="!Config", mapping=_load(
            data.json(by_alias=True), Loader=_Loader),
        flow_style=True
    )
)


def get_config(case: str):
    """Get configuration from a case folder.

    Arguments
    ---------
    case : str or os.PathLike
        The path to the case folder. A file called `config.yaml` must exsit in that folder.

    Returns
    -------
    torchswe.utils.config.Config
    """

    case = _pathlib.Path(case).expanduser().resolve()

    with open(case.joinpath("config.yaml"), "r", encoding="utf-8") as fobj:
        config = _load(fobj, _Loader)

    assert isinstance(config, Config), \
        f"Failed to parse {case.joinpath('config.yaml')} as a Config object. " + \
        "Check if `--- !Config` appears in the header of the YAML"

    return config
