import datetime
import glob
import re
import time

import dask.array as da
import numpy as np
import pandas as pd
import xarray as xr
from dask.diagnostics import ProgressBar
from joblib import Parallel, delayed

from stglib.aqd import aqdutils
from stglib.core import qaqc, utils


def load_mat_files(f, outdir, metadata):
    """Read data from burst .mat files into burst netcdf files.
    Parameters
    ----------
    filnam : string
        The filename from list of files
    """

    mat = utils.loadmat(f)

    ds = xr.Dataset()

    # create variables

    # create burst start time

    burst_time = pd.to_datetime(mat["BurstTime"])

    ds["time"] = xr.DataArray([burst_time], dims="time")

    burst_num = int(mat["BurstNumber"])

    ds["burst_number"] = xr.DataArray([burst_num], dims="time")

    # find transducer and bin numbers

    tnum = list(range(mat["AbsBinRange"].shape[1]))

    ds["transducer_number"] = xr.DataArray(tnum, dims="transducer_number")

    bnum = list(range(mat["AbsBinRange"].shape[0]))

    ds["bin_number"] = xr.DataArray(bnum, dims="bin_number")

    # auxnum = list(range(mat['NumAuxChans']))

    # ds["aux_number"] = xr.DataArray(auxnum, dims = 'aux_number')

    sampnum = list(range(mat["NumAuxSamples"]))

    ds["sample_number"] = xr.DataArray(sampnum, dims="sample_number")

    auxsampnum = list(range(mat["NumAuxSamples"] + 1))

    ds["aux_sample_number"] = xr.DataArray(auxsampnum, dims="aux_sample_number")

    ds["bindist"] = xr.DataArray(
        mat["AbsBinRange"], dims=["bin_number", "transducer_number"]
    )

    AuxChannelName = mat["AuxChannelName"]

    for k in range(len(AuxChannelName)):

        ds[AuxChannelName[k].replace(" ", "")] = xr.DataArray(
            mat["AuxData"][:, k], dims="aux_sample_number"
        )

        ds[AuxChannelName[k].replace(" ", "")].attrs["units"] = mat["AuxChannelUnit"][k]

        ds["abs_data"] = xr.DataArray(
            mat["AbsData"], dims=["bin_number", "sample_number", "transducer_number"]
        )

    ds["mean_abs_data"] = xr.DataArray(
        mat["AbsMean"], dims=["bin_number", "transducer_number"]
    )

    # create global attributes
    names = [
        "WakeSource",
        "AuxChannelUnit",
        "SessionTitle",
        "PingRate",
        "NumPings",
        "NumAbsTimeSlots",
        "NumAuxChans",
        "AuxSampleRate",
        "AbsComplex",
        "AbsAverage",
        "AbsDecimation",
        "AbsBinLengthMM",
        "AbsBinLength",
        "AbsTransducerName",
        "AbsTransducerRadius",
        "AbsTransducerBeamWidth",
        "AbsTransducerKt",
        "AbsTxFrequency",
        "AbsTxPulseLength",
        "AbsStartingGain",
        "AbsTVG",
        "AbsPowerLevel",
        "AbsStartBin",
        "AbsNumBins",
        "AbsRxChan",
        "AbsTxChan",
        "AbsNumProfiles",
        "AbsProfileRate",
    ]

    for k in names:
        ds.attrs[k] = mat[k]

    fname = re.split(r"[\\|!|.]", f)

    cdf_filename = outdir + fname[1] + "_" + str(metadata["MOORING"]) + "-raw.cdf"

    ds.to_netcdf(cdf_filename)


def abs_rename(ds):
    """rename var names"""

    varnames = {
        "ExtTemperature": "Tx_1211",
        "sample_number": "sample",
        "abs_data": "abs",
    }

    for v in varnames:
        if v in ds:
            ds = ds.rename({v: varnames[v]})

    return ds


def scale_vars(ds):
    """scale vars and apply any needed offset (due to binary raw data file size limit)
    -------------------------
    convert pressure to decibar
    """
    # pressure
    if "P_1_offset" in ds.attrs:
        p1offset = ds.attrs["P_1_offset"]
    else:
        p1offset = 0
    if "P_1_scale" in ds.attrs:
        p1scale = ds.attrs["P_1_scale"]
    else:
        p1scale = 1
    if ds["Pressure"].attrs["units"] == "Bar":
        convert = 10
        converttxt = "Pressure data converted to decibar."
    else:
        convert = 1

    # applying offset, scale, and converting to dbar

    if p1offset != 0 or p1scale != 1 or convert != 1:
        ds["Pressure"] = ((ds["Pressure"] - (p1offset)) * convert) * p1scale

        txt = "Pressure data corrected using an offset of {} Bars and scale factor of {}. {}".format(
            p1offset, p1scale, converttxt
        )

        ds = utils.insert_note(ds, "Pressure", txt)
        ds = utils.insert_history(ds, txt)

    # temperature

    if "Tx_offset" in ds.attrs:
        toffset = ds.attrs["Tx_offset"]
    else:
        toffset = 0
    if "Tx_scale" in ds.attrs:
        tscale = ds.attrs["Tx_scale"]
    else:
        tscale = 1

    if toffset != 0 or tscale != 1:
        ds["Tx_1211"] = (ds["Tx_1211"] - toffset) * tscale

        txt = "Temperature data corrected using an offset of {} Celsius and scale factor of {}.".format(
            toffset, tscale
        )

        ds = utils.insert_note(ds, "Tx_1211", txt)
        ds = utils.insert_history(ds, txt)

    # battery

    if "Bat_offset" in ds.attrs:
        boffset = ds.attrs["Bat_offset"]
    else:
        boffset = 0
    if "Bat_scale" in ds.attrs:
        bscale = ds.attrs["Bat_scale"]
    else:
        bscale = 1

    if boffset != 0 or bscale != 1:
        ds["Battery"] = (ds["Battery"] - boffset) * bscale
        txt = "Battery data corrected using an offset of {} V and scale factor of {}.".format(
            boffset, bscale
        )

        ds = utils.insert_note(ds, "Battery", txt)
        ds = utils.insert_history(ds, txt)

    return ds


def abs_drop_vars(ds):
    """drop unnecessary variables"""

    print(f"Dropping excess variables")

    varnames = {
        "bin_number",
        "burst_number",
        "PressureBridge",
        "NotConnected",
        "mean_abs_data",
        "brange",
    }

    for v in varnames:
        if v in ds:
            ds = ds.drop_vars(v)

    if ds["Analogue1"].all() == 0:
        ds = ds.drop_vars("Analogue1")

    if ds["Analogue2"].all() == 0:
        ds = ds.drop_vars("Analogue2")

    # drop bindist as dim and make variable

    ds["bindist"] = ds["bindist"].swap_dims({"bindist": "z"})
    ds = ds.reset_coords("bindist")

    return ds


def remove_aux_snum(ds):

    stop = len(ds.aux_sample_number)
    ds = ds.isel(aux_sample_number=slice(1, stop))

    for var in ds:
        if "aux_sample_number" in ds[var].dims:
            ds[var] = ds[var].swap_dims({"aux_sample_number": "sample_number"})

    ds = ds.drop_dims("aux_sample_number")

    return ds


def ds_add_attrs(ds):
    """add necessary attributes to variables"""

    print(f"Adding necessary attributes")

    ds["time"].attrs.update(
        {"standard_name": "time", "axis": "T", "long_name": "time (UTC)"}
    )

    ds["bindist"].attrs.update(
        {
            "units": "m",
            "long_name": "distance from transducer head",
            "bin_size": ds.attrs["AbsBinLengthMM"][0] * 0.001,
            "bin_count": ds.attrs["AbsNumBins"][0],
        }
    )

    ds["bin_depth"].attrs.update(
        {
            "units": "m",
            "long_name": "bin depth",
            "bin_size": ds.attrs["AbsBinLengthMM"][0] * 0.001,
            "bin_count": ds.attrs["AbsNumBins"][0],
        }
    )

    ds["sample"].attrs.update(
        {
            "long_name": "sample number",
            "units": "1",
        }
    )

    ds["Tx_1211"].attrs.update(
        {
            "units": "C",
            "long_name": "Instrument Internal Temperature",
            "epic_code": 1211,
        }
    )

    ds["P_1"].attrs.update(
        {
            "units": "dbar",
            "long_name": "Uncorrected pressure",
            "epic_code": 1,
        }
    )

    ds["Bat_106"].attrs.update(
        {"units": "V", "long_name": "Battery voltage", "epic_code": 106}
    )

    ds["abs"].attrs.update(
        {
            "units": "normalized counts",
            "long_name": "Transducer backscatter amplitude",
            "transducer_offset_from_bottom": ds.attrs["initial_instrument_height"],
        }
    )

    ds["amp"].attrs.update(
        {
            "units": "decibels",
            "long_name": "Transducer backscatter strength",
            "standard_name": "sound_intensity_level_in_water",
            "transducer_offset_from_bottom": ds.attrs["initial_instrument_height"],
        }
    )

    return ds


def remove_attributes(ds):
    """remove unnecessary global attributes from raw instrument file"""

    names = [
        "AbsComplex",
        "AuxChannelUnit",
        "AbsStartBin",
        "AbsNumBins",
        "AbsPowerLevel",
        "AbsTVG",
        "AbsStartingGain",
        "AbsDecimation",
        "WakeSource",
        "AbsTransducerName",
        "AbsRxChan",
        "AbsTxChan",
        "AbsNumProfiles",
        "AbsBinLengthMM",
    ]

    for att in names:
        del ds.attrs[att]

    return ds


def reorder_dims(ds):
    """reorder dimensions for CF compliance"""

    for var in ds:
        if "mean" not in var and "abs" in var:
            ds[var] = ds[var].transpose("time", "sample", "z", "frequency")

    return ds


def add_brange(ds):
    """use highest abs backscatter strength to find distance to boundary, omit bins in blanking distance"""

    print(f"Adding distance to boundary variables (brange)")

    var_mean = ds["abs"].mean(dim="sample")

    index = (
        var_mean.swap_dims({"z": "bindist"})
        .where(ds.bindist > 0.2)
        .argmax(dim="bindist")
    )

    brange = ds["bindist"][index]

    ds["brange"] = xr.DataArray(brange, dims=["time", "frequency"])

    for i in range(len(ds.frequency)):
        brange_name = "brange_" + str(i + 1)
        freq = str(ds.frequency[i].values)
        brange = ds["brange"].sel(frequency=ds.frequency[i]).values
        ds[brange_name] = xr.DataArray(brange, dims=["time"])

        ds[brange_name].attrs.update(
            {
                "units": "m",
                "long_name": "Transducer distance to boundary",
                "frequency": freq,
                "note": "Calculated from average of abs values in burst",
            }
        )

    return ds


def add_amp(ds):
    """convert abs data in counts to amplitude in dB"""

    print(f"Adding backscatter strength variables (amp) in decibels (dB)")

    amp = ds["abs"].values * 65536
    amp = 20 * (np.log10(amp, where=(amp != 0)))
    ds["amp"] = xr.DataArray(amp, dims=["time", "sample", "z", "frequency"])

    return ds


def ds_qaqc(ds):
    """apply stglib qaqc tools to dataset"""

    for var in ds.data_vars:

        ds = qaqc.trim_min(ds, var)
        ds = qaqc.trim_max(ds, var)
        ds = qaqc.trim_min_diff(ds, var)
        ds = qaqc.trim_min_diff_pct(ds, var)
        ds = qaqc.trim_max_diff(ds, var)
        ds = qaqc.trim_maxabs_diff_2d(ds, var)
        ds = qaqc.trim_max_diff_pct(ds, var)
        ds = qaqc.trim_med_diff(ds, var)
        ds = qaqc.trim_med_diff_pct(ds, var)
        ds = qaqc.trim_max_blip(ds, var)
        ds = qaqc.trim_max_blip_pct(ds, var)
        ds = qaqc.trim_bad_ens(ds, var)
        ds = qaqc.trim_bad_ens_indiv(ds, var)
        ds = qaqc.trim_fliers(ds, var)
        ds = qaqc.trim_warmup(ds, var)

    return ds


def var_encoding(ds):

    for v in ["sample"]:
        ds[v].encoding["dtype"] = "int32"
        ds[v].encoding["_FillValue"] = -2147483648

    return ds


def time_encoding(ds):
    """ensure we don't set dtypes uint for CF compliance"""

    if "units" in ds["time"].encoding:
        ds["time"].encoding.pop("units")

    if utils.check_time_fits_in_int32(ds, "time"):
        ds["time"].encoding["dtype"] = "i4"

    else:
        print("time variable will not fit in int32; casting to double")
        ds["time"].encoding["dtype"] = "double"

    ds["time"].attrs.update(
        {"standard_name": "time", "axis": "T", "long_name": "time (UTC)"}
    )

    return ds


def frequency_dim(ds):
    """create frequency dimension and replace with transducer_number, sort frequency by ascending"""

    print(f"Creating frequency dim")

    ds["frequency"] = ds.attrs["AbsTxFrequency"] / 1000000
    ds["frequency"].attrs.update({"units": "MHz", "long_name": "transducer frequency"})
    for var in ds:
        if "abs" in var:
            ds[var] = ds[var].swap_dims({"transducer_number": "frequency"})
    ds = ds.drop_dims("transducer_number")
    ds = ds.sortby(ds["frequency"])

    return ds


def mat2cdf(metadata):

    outdir = metadata["outdir"]

    mat_dir = metadata["matdir"] + f"*mat"

    matfiles = glob.glob(mat_dir)  # finding all files that end with .mat

    if len(matfiles) > 1:
        Parallel(n_jobs=-1, verbose=10)(
            delayed(load_mat_files)(f, outdir, metadata) for f in matfiles
        )

    print(f"Loading -raw.CDF files")

    cdffiles = outdir + f"*" + metadata["MOORING"] + f"-raw.cdf"

    cdffiles = glob.glob(cdffiles)

    ds = xr.open_mfdataset(cdffiles, parallel=True)

    ds = utils.write_metadata(ds, metadata)

    ds = utils.ensure_cf(ds)

    # configure file

    cdf_filename = ds.attrs["filename"] + "-raw.cdf"

    # ds.to_netcdf(cdf_filename, unlimited_dims=["time"])

    delayed_obj = ds.to_netcdf(cdf_filename, unlimited_dims=["time"], compute=False)

    with ProgressBar():

        delayed_obj.compute()

    # utils.check_compliance(cdf_filename, conventions=ds.attrs["Conventions"])

    print(f"Finished writing data to {cdf_filename}")


def cdf2nc(cdf_filename, atmpres=False):

    ds = xr.open_dataset(cdf_filename)

    ds["bindist"] = ds["bindist"].sel(transducer_number=0, time=ds["time"][0])

    ds = ds.swap_dims({"bin_number": "bindist"})
    ds = remove_aux_snum(ds)
    ds = abs_rename(ds)
    ds = scale_vars(ds)

    if atmpres is not False:
        ds = aqdutils.atmos_correct(ds, atmpres)

    # Clip data to in/out water times or via good_ens

    ds = utils.clip_ds(ds)
    ds = utils.create_z(ds)
    ds = aqdutils.make_bin_depth(ds)
    ds = utils.create_nominal_instrument_depth(ds)
    ds = utils.add_start_stop_time(ds)
    ds = utils.add_delta_t(ds)
    ds = utils.shift_time(ds, 0)
    ds = aqdutils.ds_swap_dims(ds)
    ds = aqdutils.ds_rename(ds)
    ds = frequency_dim(ds)
    ds = reorder_dims(ds)
    ds = add_brange(ds)
    ds = add_amp(ds)

    ds = ds_qaqc(ds)

    ds = abs_drop_vars(ds)
    ds = utils.add_min_max(ds)
    ds = ds_add_attrs(ds)
    ds = remove_attributes(ds)
    ds = var_encoding(ds)
    ds = time_encoding(ds)

    # configure burst file
    nc_burst_filename = ds.attrs["filename"] + "b-cal.nc"

    delayed_obj = ds.to_netcdf(
        nc_burst_filename, unlimited_dims=["time"], compute=False
    )

    with ProgressBar():

        delayed_obj.compute()

    utils.check_compliance(nc_burst_filename, conventions=ds.attrs["Conventions"])

    print(f"Finished writing data to {nc_burst_filename}")

    # take mean over sample dimension for all variables
    ds = ds.mean(dim="sample", keep_attrs=True)

    # configure sample file
    nc_averaged_filename = ds.attrs["filename"] + "s-cal.nc"

    delayed_obj = ds.to_netcdf(
        nc_averaged_filename, unlimited_dims=["time"], compute=False
    )

    with ProgressBar():

        delayed_obj.compute()

    utils.check_compliance(nc_averaged_filename, conventions=ds.attrs["Conventions"])

    print(f"Finished writing data to {nc_averaged_filename}")
