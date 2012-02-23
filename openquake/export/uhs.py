# Copyright (c) 2010-2012, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3
# only, as published by the Free Software Foundation.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License version 3 for more details
# (a copy is included in the LICENSE file that accompanied this code).
#
# You should have received a copy of the GNU Lesser General Public License
# version 3 along with OpenQuake.  If not, see
# <http://www.gnu.org/licenses/lgpl-3.0.txt> for a copy of the LGPLv3 License.


import h5py
import numpy
import os

from openquake.db import models
from openquake.utils import round_float

#: Format string for HDF5 dataset names
_DS_NAME_FMT = 'lon:%s-lat:%s'
_HDF5_FILE_NAME_FMT = 'uhs_poe:%s.hdf5'


def _point_to_ds_name(point):
    """Generate a dataset name from a
    :class:`django.contrib.gis.geos.point.Point`. This dataset name is meant to
    be used in UHS HDF5 result files.

    :param point:
        :class:`django.contrib.gis.geos.point.Point` object.
    :returns:
        A dataset name generated from the point's lat/lon values. Example::

            "lon:-179.45-lat:-20.75"

    A simple example:

    >>> from django.contrib.gis.geos.point import Point
    >>> pt = Point(-179.45, -20.75)
    >>> _point_to_ds_name(pt)
    'lon:-179.45-lat:-20.75'

    This function uses :function:`openquake.utils.round_float` to round
    coordinate values. Thus, any coordinate value with more than 7 digits after
    the decimal will be rounded to 7 digits:

    >>> pt = Point(-179.12345675, 79.12345674)
    >>> _point_to_ds_name(pt)
    'lon:-179.1234568-lat:79.1234567'
    """
    return _DS_NAME_FMT % (round_float(point.x), round_float(point.y))


def export_uhs(output, target_dir):
    """ """

    # TODO: mkdir -p (if the target dir doesn't exist?)

    file_names = []

    uh_spectra = models.UhSpectra.objects.get(output=output.id)

    uh_spectrums = models.UhSpectrum.objects.filter(uh_spectra=uh_spectra.id)

    for spectrum in uh_spectrums:
        # create a file for each spectrum/poe
        uhs_data = models.UhSpectrumData.objects.filter(
            uh_spectrum=spectrum.id)

        # If there are multiple LT samples/realizations, we'll have multiple
        # records for each site. However, there should only be a 1 dataset per
        # site so we need to 'uniquify'.
        ds_names = list(set([_point_to_ds_name(datum.location)
                             for datum in uhs_data]))

        file_name = touch_result_hdf5_file(
            target_dir, spectrum.poe, ds_names, len(uh_spectra.realizations),
            len(uh_spectra.periods))
        # TODO: now write the actual data
        file_names.append(file_name)

    return file_names


def touch_result_hdf5_file(target_dir, poe, ds_names, n_realizations,
                           n_periods):
    """Create an empty HDF5 file with appropriately sized datasets. The quanity
    of datasets created is equal to the length of ``ds_names``. Each dataset
    will be a 2D matrix with a number of rows == ``n_realizations`` and number
    of columns == ``n_periods``. Each dataset will be created 'empty'
    (all values == 0.0).

    The datatype for each value is `numpy.float64`.

    :param str target_dir:
        Location to place the new file.
    :param float poe:
        Probability of Exceedance associated with this file. The PoE will be
        used to generate the resulting file name.
    :param list ds_names:
        List strings representing dataset names. 1 dataset will be created for
        each name.

        Note: Each dataset name should be unique.
    :param int n_realizations:
        Number of rows in each dataset.
    :param int n_periods:
        Number of columns in each dataset.

    :returns:
        The full path of the created file.
    """
    file_name = _HDF5_FILE_NAME_FMT % poe
    full_path = os.path.join(target_dir, file_name)

    ds_shape = (n_realizations, n_periods)

    with h5py.File(full_path, 'w') as h5_file:
        for name in ds_names:
            h5_file.create_dataset(name, dtype=numpy.float64, shape=ds_shape)

    return full_path
