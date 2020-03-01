# Copyright (c) 2020 Scott Moisik and Pertti Palo.
#
# This file is part of Pixel Difference toolkit
# (see https://github.com/giuthas/pd/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# The example data packaged with this program is licensed under the
# Creative Commons Attribulton-NonCommercial-ShareAlike 4.0
# International (CC BY-NC-SA 4.0) License. You should have received a
# copy of the Creative Commons Attribution-NonCommercial-ShareAlike 4.0
# International (CC BY-NC-SA 4.0) License along with the data. If not,
# see <https://creativecommons.org/licenses/by-nc-sa/4.0/> for details.
#

from contextlib import closing
from datetime import datetime

#Built in packages
import csv
import math
import glob
import logging
import os
import os.path
import pickle
import re
import struct
import sys
import time

#Diffeomorphic demons algorithm implemented in python in the DIPY package
from dipy.data import get_fnames
from dipy.align.imwarp import SymmetricDiffeomorphicRegistration
from dipy.align.metrics import SSDMetric, CCMetric, EMMetric
import dipy.align.imwarp as imwarp
from dipy.viz import regtools

# Numpy and scipy
import numpy as np
import scipy.io as sio
import scipy.io.wavfile as sio_wavfile
from scipy.signal import butter, filtfilt, kaiser, sosfilt

from scipy import interpolate

# Scientific plotting
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

# create module logger
ofreg_logger = logging.getLogger('of.ofreg')


def read_prompt(filebase):
    with closing(open(filebase, 'r')) as promptfile:
        lines = promptfile.read().splitlines()
        prompt = lines[0]
        date = datetime.strptime(lines[1], '%d/%m/%Y %I:%M:%S %p')
        participant = lines[2].split(',')[0]

        return prompt, date, participant


def read_wav(filebase):
    samplerate, frames = sio_wavfile.read(filebase)
    # duration = frames.shape[0] / samplerate

    return frames, samplerate


def _parse_ult_meta(filebase):
    '''Return all metadata from AAA txt as dictionary.'''
    with closing(open(filebase, 'r')) as metafile:
        meta = {}
        for line in metafile:
            (key, value_str) = line.split("=")
            try:
                value = int(value_str)
            except ValueError:
                value = float(value_str)
            meta[key] = value

        return meta


def read_ult_meta(filebase):
    '''Convenience fcn for output of targeted metadata.'''
    meta = _parse_ult_meta(filebase)

    return (meta["NumVectors"],
            meta["PixPerVector"],
            meta["PixelsPerMm"],
            meta["FramesPerSec"],
            meta["TimeInSecsOfFirstFrame"])


def get_data_from_dir(directory):
    # this is equivalent with the following: sorted(glob.glob(directory + '/.' +  '/*US.txt'))
    ult_meta_files = sorted(glob.glob(directory + '/*US.txt'))

    # this takes care of *.txt and *US.txt overlapping.
    ult_prompt_files = [prompt_file
                        for prompt_file in glob.glob(directory + '/*.txt')
                        if not prompt_file in ult_meta_files
                        ]

    ult_prompt_files = sorted(ult_prompt_files)
    filebases = [os.path.splitext(pf)[0] for pf in ult_prompt_files]
    meta = [{'filebase': filebase} for filebase in filebases]

    # iterate over file base names and check for required files
    for i,fb in enumerate(filebases): 
        # Prompt file should always exist and correspond to the filebase because
        # the filebase list is generated from the directory listing of prompt files.
        meta[i]['ult_prompt_file'] = ult_prompt_files[i]
        (prompt, date, participant) = read_prompt(ult_prompt_files[i])
        meta[i]['prompt'] = prompt
        meta[i]['date'] = date
        meta[i]['participant'] = participant

        # generate candidates for file names
        ult_meta_file = os.path.join(fb + "US.txt")
        ult_wav_file = os.path.join(fb + ".wav")
        ult_file = os.path.join(fb + ".ult")

        # check if assumed files exist, and arrange to skip them if any do not
        if os.path.isfile(ult_meta_file):
            meta[i]['ult_meta_file'] = ult_meta_file
            meta[i]['ult_meta_exists'] = True
        else:
            notice = 'Note: ' + ult_meta_file + " does not exist."
            ofreg_logger.warning(notice)
            meta[i]['ult_meta_exists'] = False
            meta[i]['excluded'] = True

        if os.path.isfile(ult_wav_file):
            meta[i]['ult_wav_file'] = ult_wav_file
            meta[i]['ult_wav_exists'] = True
        else:
            notice = 'Note: ' + ult_wav_file + " does not exist."
            ofreg_logger.warning(notice)
            meta[i]['ult_wav_exists'] = False
            meta[i]['excluded'] = True

        if os.path.isfile(ult_file):
            meta[i]['ult_file'] = ult_file
            meta[i]['ult_exists'] = True
        else:
            notice = 'Note: ' + ult_file + " does not exist."
            ofreg_logger.warning(notice)
            meta[i]['ult_exists'] = False
            meta[i]['excluded'] = True

    meta = sorted(meta, key=lambda item: item['date'])

    return meta


def compute(item):
    # inputs: elements in data dictionary generated by get_data_from_dir
    # i.e. all_data[i] is an item
    # TODO don't arrange this externally to these scripts; i.e. transfer the loop in driver.py into here
    ofreg_logger.info("PD: " + item['filebase'] + " " + item['prompt'] + '. item processed.')
    
    (ult_wav_frames, ult_wav_fs) = read_wav(item['ult_wav_file'])
    (ult_NumVectors, ult_PixPerVector, ult_PixelsPerMm, ult_fps, ult_TimeInSecOfFirstFrame) = read_ult_meta(item['ult_meta_file'])

    with closing(open(item['ult_file'], 'rb')) as ult_file:
        ult_data = ult_file.read()
        ultra = np.fromstring(ult_data, dtype=np.uint8)
        ultra = ultra.astype("float32")

        ult_no_frames = int(len(ultra) / (ult_NumVectors * ult_PixPerVector))

        # reshape into vectors containing a frame each
        ultra = ultra.reshape((ult_no_frames, ult_NumVectors, ult_PixPerVector))

        # Interpolate the data to form isometric pixels
        #lengthDepthRatio = D(fIdx).probeArrayDepthMm / D(fIdx).probeArrayLengthMm;
        #sz = size(D(fIdx).rawData);
        #xTargetSize = ceil(sz(2) * lengthDepthRatio) * 2;
        #yTargetSize = sz(2) * 2;
        #[X, Y] = meshgrid(1:sz(2), 1: sz(1));
        #[Xq, Yq] = meshgrid(linspace(1, sz(2), yTargetSize), linspace(1, sz(1), xTargetSize));

        #interpolate the data to correct the axis scaling for purposes of image registration
        probe_array_length_mm = 40  #TODO 40 mm long probe assumed!!!
        probe_array_depth_mm = ult_PixPerVector/ult_PixelsPerMm
        length_depth_ratio = probe_array_depth_mm/probe_array_length_mm

        x = np.linspace(1, ult_NumVectors, ult_NumVectors)
        y = np.linspace(1, ult_PixPerVector, ult_PixPerVector)

        xnew = np.linspace(1, ult_NumVectors, ult_NumVectors * 2)
        ynew = np.linspace(1, ult_PixPerVector, math.ceil(ult_NumVectors * length_depth_ratio) * 2)
        f = interpolate.interp2d(x, y, np.transpose(ultra[1, :, :]), kind='linear')

        ultra_interp = []

        # debug plotting
        if False:
            fig, ax = plt.subplots(1, 1)
            im = ax.imshow(f(xnew, ynew))

        for fIdx in range(0, ult_no_frames):
            f = interpolate.interp2d(x, y, np.transpose(ultra[fIdx, :, :]), kind='linear')
            ultra_interp.append(f(xnew, ynew))

            # debug plotting
            if False:
                im.set_data(ultra_interp[fIdx])
                ax.set_title(str(fIdx))
                fig.canvas.draw_idle()
                plt.pause(0.01)

        #perform registration using diffeomorphic demons algorithm (from DIPY package)
        #https: // dipy.org / documentation / 1.1.1. / examples_built / syn_registration_2d /  # example-syn-registration-2d

        # specify the number of levels in the multiresolution pyramid
        level_iters = [200, 100, 50, 25]

        # create a registration metric
        sigma_diff = 3.0
        radius = 2
        metric = CCMetric(2, sigma_diff, radius)

        # create the registration object
        sdr = SymmetricDiffeomorphicRegistration(metric, level_iters, inv_iter=100)

        # create the storage for the optical flow
        ofdisp = []

        # iterate through the frame pairs and perform the registration each
        ult_no_frames = 3
        debug_plot_ofreg = False

        for fIdx in range(ult_no_frames - 1):
            sys.stdout.write("Working on frame pair %d of %d\n" % (fIdx, ult_no_frames - 1))
            current_im = ultra_interp[fIdx]
            next_im = ultra_interp[fIdx + 1]

            # execute the optimization
            mapping = sdr.optimize(next_im, current_im)
            ofdisp.append(mapping)

            # debug plotting
            if debug_plot_ofreg:
                fig, ax = plt.subplots(1, 2)
                ax[0].imshow(current_im)
                ax[1].imshow(next_im)
                plt.show()
                plt.pause(0.05)

        print("Finished computing optical flow")

        #Visualize registration as quiver plot
        xx, yy = np.meshgrid(xnew, ynew)
        plt.quiver(xx, yy, ofdisp[1].forward[:, :, 0], ofdisp[1].forward[:, :, 1])
        plt.show()

        # compute the ultrasound time vector
        ultra_time = np.linspace(0, ult_no_frames, ult_no_frames, endpoint=False) / ult_fps
        ultra_time = ultra_time + ult_TimeInSecOfFirstFrame + .5 / ult_fps

        ult_wav_time = np.linspace(0, len(ult_wav_frames), len(ult_wav_frames), endpoint=False) / ult_wav_fs

        data = {}
        data['of'] = ofdisp
        data['frames'] = ultra
        data['ult_time'] = ultra_time
        data['wav_time'] = ult_wav_time

    print("toast")

